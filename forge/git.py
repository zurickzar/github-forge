"""Git smart HTTP: info/refs, upload-pack (v2 + v0), receive-pack (v0).

Protocol v2 for upload-pack (ls-refs / fetch, with shallow + deepen),
protocol v0 for receive-pack advertisements. Packs are v2, no deltas.
"""
import re

from . import gitobj
from .gitobj import ZERO_OID

CAPS_UPLOAD_V0 = ("symref=HEAD:refs/heads/main multi_ack thin-pack side-band "
                  "side-band-64k ofs-delta shallow no-progress agent=github-forge/1.0")
CAPS_RECEIVE_V0 = ("report-status side-band-64k delete-refs ofs-delta "
                   "symref=HEAD:refs/heads/main agent=github-forge/1.0")
CAPS_UPLOAD_V2 = ["ls-refs=unborn", "fetch=shallow wait-for-done",
                  "object-format=sha1", "agent=github-forge/1.0"]

# Pack cache: a pack's bytes are a pure function of the object-oid set
# (objects are content-addressed), so identical fetches reuse the bytes.
# Bounded FIFO; keeps repeated clones of the same repo near-free.
_PACK_CACHE = {}
_PACK_CACHE_MAX = 32


def _cached_pack(repo, send_oids):
    key = tuple(send_oids)
    pack = _PACK_CACHE.get(key)
    if pack is None:
        pack = gitobj.write_pack([repo.objects[oid] for oid in send_oids])
        if len(_PACK_CACHE) >= _PACK_CACHE_MAX:
            _PACK_CACHE.pop(next(iter(_PACK_CACHE)))
        _PACK_CACHE[key] = pack
    return pack


def _repo(ctx, owner, name):
    if not ctx.forge.resolve_owner(owner):
        return None
    return ctx.forge.repos.get(name)


def _send(handler, status, raw, content_type):
    handler._send(status, raw, content_type)


def _not_found(handler):
    handler._send_json(404, {"message": "Not Found"})


def _ref_lines(repo):
    """Sorted (oid, refname) pairs, HEAD first, with peeled tag lines."""
    lines = []
    main = repo.ref("refs/heads/main")
    if main:
        lines.append((main, "HEAD"))
    for ref in sorted(repo.refs):
        lines.append((repo.refs[ref], ref))
        if ref.startswith("refs/tags/"):
            typ, data = repo.objects.get(repo.refs[ref], (None, None))
            if typ == "tag":
                target = gitobj.parse_tag(data)["object"]
                lines.append((target, ref + "^{}"))
    return lines


def _advertise_upload_v0(repo):
    out = bytearray()
    out += gitobj.pkt_line("# service=git-upload-pack\n")
    out += gitobj.pkt_flush()
    lines = _ref_lines(repo)
    if not lines:
        out += gitobj.pkt_line("%s capabilities^{}\0%s\n" % (ZERO_OID, CAPS_UPLOAD_V0))
    else:
        first = True
        for oid, name in lines:
            if first:
                out += gitobj.pkt_line("%s %s\0%s\n" % (oid, name, CAPS_UPLOAD_V0))
                first = False
            else:
                out += gitobj.pkt_line("%s %s\n" % (oid, name))
    out += gitobj.pkt_flush()
    return bytes(out)


def _advertise_receive_v0(repo):
    out = bytearray()
    out += gitobj.pkt_line("# service=git-receive-pack\n")
    out += gitobj.pkt_flush()
    lines = _ref_lines(repo)
    if not lines:
        out += gitobj.pkt_line("%s capabilities^{}\0%s\n" % (ZERO_OID, CAPS_RECEIVE_V0))
    else:
        first = True
        for oid, name in lines:
            if first:
                out += gitobj.pkt_line("%s %s\0%s\n" % (oid, name, CAPS_RECEIVE_V0))
                first = False
            else:
                out += gitobj.pkt_line("%s %s\n" % (oid, name))
    out += gitobj.pkt_flush()
    return bytes(out)


def _advertise_upload_v2(repo):
    out = bytearray()
    out += gitobj.pkt_line("# service=git-upload-pack\n")
    out += gitobj.pkt_flush()
    out += gitobj.pkt_line("version 2\n")
    for cap in CAPS_UPLOAD_V2:
        out += gitobj.pkt_line(cap + "\n")
    out += gitobj.pkt_flush()
    return bytes(out)


def _parse_request_lines(body):
    """Parse a v2 command request. Returns (command, args) or raises."""
    items, _ = gitobj.read_pkt_lines(body)
    command = None
    args = []
    in_args = False
    for kind, payload in items:
        if kind == "flush":
            break
        if kind == "delim":
            in_args = True
            continue
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if command is None:
            if text.startswith("command="):
                command = text[len("command="):]
                continue
            raise ValueError("expected command line, got %r" % text)
        if not in_args:
            continue  # agent= / object-format= lines
        args.append(text)
    if command is None:
        raise ValueError("no command in request")
    return command, args


def _ls_refs(repo, args):
    peel = False
    symrefs = False
    unborn = False
    prefixes = []
    for arg in args:
        if arg == "peel":
            peel = True
        elif arg == "symrefs":
            symrefs = True
        elif arg == "unborn":
            unborn = True
        elif arg.startswith("ref-prefix "):
            prefixes.append(arg[len("ref-prefix "):])

    def keep(name):
        return not prefixes or any(name.startswith(p) for p in prefixes)

    out = bytearray()
    main = repo.ref("refs/heads/main")
    if main:
        if symrefs and keep("HEAD"):
            out += gitobj.pkt_line("%s HEAD symref-target:refs/heads/main\n" % main)
        for ref in sorted(repo.refs):
            if not keep(ref):
                continue
            suffix = ""
            if peel and ref.startswith("refs/tags/"):
                typ, data = repo.objects.get(repo.refs[ref], (None, None))
                if typ == "tag":
                    target = gitobj.parse_tag(data)["object"]
                    suffix = " peeled:%s" % target
            out += gitobj.pkt_line("%s %s%s\n" % (repo.refs[ref], ref, suffix))
    elif unborn:
        out += gitobj.pkt_line("unborn HEAD symref-target:refs/heads/main\n")
    out += gitobj.pkt_flush()
    return bytes(out)


def _tree_objects(store, tree_oid):
    """All tree+blob oids under a tree (not commits)."""
    out = set()
    stack = [tree_oid]
    while stack:
        oid = stack.pop()
        if oid in out or oid not in store:
            continue
        typ, data = store[oid]
        if typ != "tree":
            continue
        out.add(oid)
        for _, _, child in gitobj.parse_tree(data):
            if store.get(child, (None,))[0] == "tree":
                stack.append(child)
            else:
                out.add(child)
    return out


def _fetch_objects(repo, wants, haves, depth, shallow_existing=None):
    """Return (objects_to_send, shallow_list, unshallow_list).

    objects_to_send: list of (type, data) in deterministic order.
    The client's shallow boundaries are NOT followed when computing what
    it already holds: a shallow clone has the boundary commit and its
    tree, but none of its ancestors.
    """
    store = repo.objects
    shallow_existing = set(shallow_existing or [])
    have_reach = gitobj.reachable_shallow(store, haves, shallow_existing)
    if depth:
        dist = gitobj.commit_distance(store, wants)
        included = {oid for oid, d in dist.items() if d < depth}
        # boundary: included commits whose parent is not included
        shallow = []
        for oid in sorted(included):
            parents = gitobj.parse_commit(store[oid][1])["parents"]
            if parents and any(p not in included for p in parents):
                shallow.append(oid)
        send = set()
        for oid in included:
            if oid in have_reach:
                continue
            send.add(oid)
            send |= _tree_objects(store, gitobj.parse_commit(store[oid][1])["tree"])
            send -= have_reach
        return sorted(send), sorted(shallow), []
    # full fetch: reachable(wants) minus reachable(haves)
    want_reach = gitobj.reachable(store, wants)
    send = want_reach - have_reach
    return sorted(send), [], []


def _fetch_v2(repo, body):
    command, args = _parse_request_lines(body)
    if command == "ls-refs":
        return _ls_refs(repo, args)
    if command != "fetch":
        raise ValueError("unknown command %r" % command)
    wants = []
    haves = []
    shallow_existing = []
    depth = None
    for arg in args:
        if arg.startswith("want "):
            wants.append(arg[5:].strip())
        elif arg.startswith("have "):
            haves.append(arg[5:].strip())
        elif arg.startswith("shallow "):
            shallow_existing.append(arg[8:].strip())
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                pass
        elif arg == "done":
            pass
    send_oids, shallow, _ = _fetch_objects(repo, wants, haves, depth, shallow_existing)
    # unshallow: old shallow boundaries whose parents are now included
    unshallow = []
    if depth:
        dist = gitobj.commit_distance(repo.objects, wants)
        included = {oid for oid, d in dist.items() if d < depth}
        for old in shallow_existing:
            if old not in repo.objects:
                continue
            parents = gitobj.parse_commit(repo.objects[old][1])["parents"]
            if parents and all(p in included for p in parents):
                unshallow.append(old)
            elif not parents:
                unshallow.append(old)
    out = bytearray()
    # The acknowledgments section is only sent when the client sent 'have'
    # lines: a fresh clone (no haves) expects the response to start with
    # shallow-info/packfile, and real git dies with
    # "expected 'packfile', received 'acknowledgments'" otherwise.
    if haves:
        out += gitobj.pkt_line("acknowledgments\n")
        out += gitobj.pkt_line("NAK\n")
        out += gitobj.pkt_delim()
    if shallow or unshallow:
        out += gitobj.pkt_line("shallow-info\n")
        for oid in shallow:
            out += gitobj.pkt_line("shallow %s\n" % oid)
        for oid in unshallow:
            out += gitobj.pkt_line("unshallow %s\n" % oid)
        out += gitobj.pkt_delim()
    if send_oids:
        pack = _cached_pack(repo, send_oids)
        out += gitobj.pkt_line("packfile\n")
        out += gitobj.sideband_pack(pack)
    else:
        out += gitobj.pkt_flush()
    return bytes(out)


def _fetch_v0(repo, body):
    """Minimal v0 fetch: NAK + sideband pack. (Suite always uses v2.)"""
    try:
        items, _ = gitobj.read_pkt_lines(body)
    except ValueError:
        items = []
    wants = []
    haves = []
    for kind, payload in items:
        if kind != "data":
            continue
        text = payload.decode("utf-8", "replace").rstrip("\n")
        if text.startswith("want "):
            wants.append(text[5:].strip().split(" ")[0])
        elif text.startswith("have "):
            haves.append(text[5:].strip())
        elif text == "done":
            break
    send_oids, _, _ = _fetch_objects(repo, wants, haves, None)
    out = bytearray()
    out += gitobj.pkt_line("NAK\n")
    if send_oids:
        pack = _cached_pack(repo, send_oids)
        out += gitobj.sideband_pack(pack)
    else:
        out += gitobj.pkt_flush()
    return bytes(out)


def _is_descendant(repo, ancestor, tip):
    if ancestor == ZERO_OID:
        return True
    return ancestor in gitobj.commit_ancestors(repo.objects, tip)


def _receive_pack(repo, body):
    """Parse commands + pack, apply fast-forward rules, report status."""
    # split pkt-lines (commands) from the raw pack that follows the flush
    pos = 0
    commands = []
    pack_start = len(body)
    n = len(body)
    while pos < n:
        if pos + 4 > n:
            break
        try:
            length = int(body[pos:pos + 4], 16)
        except ValueError:
            break
        if length == 0:
            pack_start = pos + 4
            break
        if length == 1:
            pos += 4
            continue
        payload = body[pos + 4:pos + length]
        commands.append(payload)
        pos += length
    pack_data = body[pack_start:] if pack_start < n else b""

    parsed = []
    for raw in commands:
        text = raw.decode("utf-8", "replace")
        if "\0" in text:
            text = text.split("\0", 1)[0]
        parts = text.strip().split(" ")
        if len(parts) != 3:
            continue
        parsed.append((parts[0], parts[1], parts[2]))

    # unpack objects (even if some refs are refused, objects are stored)
    unpack_ok = True
    unpack_error = ""
    if pack_data:
        try:
            for typ_name, data in gitobj.read_pack(pack_data):
                repo.store_object(typ_name, data)
        except Exception as exc:  # noqa: BLE001 - malformed pack -> unpack error
            unpack_ok = False
            unpack_error = str(exc)

    report = []
    if unpack_ok:
        report.append("unpack ok")
    else:
        report.append("unpack error %s" % (unpack_error or "bad pack"))
    for old, new, ref in parsed:
        if not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")):
            report.append("ng %s invalid ref" % ref)
            continue
        current = repo.ref(ref)
        if new == ZERO_OID:
            # delete
            if current is None:
                report.append("ng %s reference does not exist" % ref)
            elif old != ZERO_OID and old != current:
                report.append("ng %s non-fast-forward" % ref)
            else:
                repo.set_ref(ref, ZERO_OID)
                report.append("ok %s" % ref)
            continue
        if new not in repo.objects:
            report.append("ng %s object not found" % ref)
            continue
        if old == ZERO_OID:
            if current is not None:
                report.append("ng %s already exists" % ref)
            else:
                repo.set_ref(ref, new)
                report.append("ok %s" % ref)
            continue
        if current is None:
            report.append("ng %s reference does not exist" % ref)
        elif old != current:
            report.append("ng %s non-fast-forward" % ref)
        elif not _is_descendant(repo, old, new):
            report.append("ng %s non-fast-forward" % ref)
        else:
            repo.set_ref(ref, new)
            report.append("ok %s" % ref)

    # Band 1 carries the report as a pkt-line stream TERMINATED BY ITS OWN
    # flush packet (git's report parser reads pkt-lines out of band 1 until
    # that flush; without it the reader hits EOF -> "remote end hung up").
    # The outer flush then terminates the sideband stream itself.
    inner = bytearray()
    for line in report:
        inner += gitobj.pkt_line(line + "\n")
    inner += gitobj.pkt_flush()
    out = bytearray()
    out += gitobj.pkt_line(b"\x01" + bytes(inner))
    out += gitobj.pkt_flush()
    return bytes(out)


def handle(ctx, handler, method, path, query, body):
    m = re.match(r"^/([^/]+)/([^/]+)\.git/(.+)$", path)
    if not m:
        _not_found(handler)
        return
    owner, repo_name, action = m.group(1), m.group(2), m.group(3)
    repo = _repo(ctx, owner, repo_name)
    if repo is None:
        _not_found(handler)
        return
    try:
        if action == "info/refs":
            service = (query.get("service") or [""])[0]
            if service == "git-upload-pack":
                proto = handler.headers.get("Git-Protocol") or ""
                if "version=2" in proto:
                    raw = _advertise_upload_v2(repo)
                else:
                    raw = _advertise_upload_v0(repo)
                _send(handler, 200, raw, "application/x-git-upload-pack-advertisement")
            elif service == "git-receive-pack":
                raw = _advertise_receive_v0(repo)
                _send(handler, 200, raw, "application/x-git-receive-pack-advertisement")
            else:
                _not_found(handler)
        elif action == "git-upload-pack" and method == "POST":
            proto = handler.headers.get("Git-Protocol") or ""
            if "version=2" in proto:
                raw = _fetch_v2(repo, body)
            else:
                raw = _fetch_v0(repo, body)
            _send(handler, 200, raw, "application/x-git-upload-pack-result")
        elif action == "git-receive-pack" and method == "POST":
            raw = _receive_pack(repo, body)
            _send(handler, 200, raw, "application/x-git-receive-pack-result")
        else:
            _not_found(handler)
    except (ValueError, KeyError, IndexError):
        # malformed protocol input: stay healthy, answer 400
        handler._send_json(400, {"message": "Bad Request"})
