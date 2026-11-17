"""Git object model, pack format and pkt-line helpers.

Pure stdlib. No git binary. Objects are SHA-1 over "<type> <len>\\0" + data.
Pack v2 without delta on the wire; the reader also resolves OFS/REF deltas
so packs produced by a real git client are accepted.
"""
import hashlib
import struct
import zlib

ZERO_OID = "0" * 40
FIXED_TS = 1700000000  # deterministic timestamp for every server-built commit

TYPE_COMMIT = 1
TYPE_TREE = 2
TYPE_BLOB = 3
TYPE_TAG = 4
_TYPE_CODE = {"commit": TYPE_COMMIT, "tree": TYPE_TREE, "blob": TYPE_BLOB, "tag": TYPE_TAG}
_TYPE_NAME = {v: k for k, v in _TYPE_CODE.items()}


def hash_object(type_name, data):
    header = ("%s %d\0" % (type_name, len(data))).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def build_commit(tree_oid, parents, author, message, ts=FIXED_TS):
    lines = ["tree %s" % tree_oid]
    for p in parents:
        lines.append("parent %s" % p)
    lines.append("author %s %d +0000" % (author, ts))
    lines.append("committer %s %d +0000" % (author, ts))
    body = "\n".join(lines) + "\n\n" + message
    if not body.endswith("\n"):
        body += "\n"
    return body.encode("utf-8")


def build_tag(target_oid, target_type, tag_name, tagger, message, ts=FIXED_TS):
    body = ("object %s\ntype %s\ntag %s\ntagger %s %d +0000\n\n%s"
            % (target_oid, target_type, tag_name, tagger, ts, message))
    if not body.endswith("\n"):
        body += "\n"
    return body.encode("utf-8")


def parse_commit(data):
    """Return dict with tree, parents, author, committer, message."""
    header, _, message = data.partition(b"\n\n")
    out = {"tree": None, "parents": [], "author": "", "committer": "",
           "message": message.decode("utf-8", "replace").rstrip("\n")}
    for line in header.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            out["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            out["parents"].append(line[7:].strip())
        elif line.startswith("author "):
            out["author"] = line[7:]
        elif line.startswith("committer "):
            out["committer"] = line[10:]
    return out


def parse_tag(data):
    header, _, message = data.partition(b"\n\n")
    out = {"object": None, "type": None, "tag": "", "message": message.decode("utf-8", "replace")}
    for line in header.decode("utf-8", "replace").split("\n"):
        if line.startswith("object "):
            out["object"] = line[7:].strip()
        elif line.startswith("type "):
            out["type"] = line[5:].strip()
        elif line.startswith("tag "):
            out["tag"] = line[4:]
    return out


def tree_sort_key(name, is_dir):
    """Git tree sort: directories compare as name + '/'."""
    return name + "/" if is_dir else name


def build_tree(entries):
    """entries: list of (mode_str, name, oid_hex). Returns raw tree bytes."""
    decorated = []
    for mode, name, oid in entries:
        is_dir = mode in ("40000", "040000")
        decorated.append((tree_sort_key(name, is_dir), mode, name, oid))
    decorated.sort(key=lambda t: t[0])
    out = b""
    for _, mode, name, oid in decorated:
        out += ("%s %s\0" % (mode, name)).encode("utf-8") + bytes.fromhex(oid)
    return out


def parse_tree(data):
    """Return list of (mode_str, name, oid_hex)."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8", "replace")
        oid = data[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        i = nul + 21
    return out


def build_tree_from_files(file_map):
    """file_map: {path: (mode, oid)} -> (root_tree_oid, {oid: (type, data)}).

    Builds every intermediate tree bottom-up. Returns the root tree oid and
    a dict of every newly built object (trees only; blobs come from caller).
    """
    # nested dict: name -> ('file', mode, oid) | ('dir', {children})
    root = {}
    for path, (mode, oid) in sorted(file_map.items()):
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("path conflict: %s" % path)
        node[parts[-1]] = ("file", mode, oid)
    new_objects = {}

    def build(node):
        entries = []
        for name, child in node.items():
            if isinstance(child, dict):
                sub_oid = build(child)
                entries.append(("40000", name, sub_oid))
            else:
                _, mode, oid = child
                entries.append((mode, name, oid))
        data = build_tree(entries)
        oid = hash_object("tree", data)
        new_objects[oid] = ("tree", data)
        return oid

    return build(root), new_objects


def flatten_tree(store, tree_oid, prefix=""):
    """store: mapping oid -> (type, data). Returns {path: (mode, oid)}."""
    out = {}
    if tree_oid is None:
        return out
    typ, data = store[tree_oid]
    if typ != "tree":
        return out
    for mode, name, oid in parse_tree(data):
        path = prefix + name
        if mode in ("40000", "040000"):
            out.update(flatten_tree(store, oid, path + "/"))
        else:
            out[path] = (mode, oid)
    return out


# ---------------------------------------------------------------------------
# pkt-line
# ---------------------------------------------------------------------------

def pkt_line(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return ("%04x" % (len(data) + 4)).encode("ascii") + data


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def read_pkt_lines(buf, pos=0):
    """Parse pkt-lines from buf[pos:]. Returns (items, end_pos).

    items: list of ('data', bytes) | ('flush', None) | ('delim', None).
    """
    items = []
    n = len(buf)
    while pos < n:
        if pos + 4 > n:
            raise ValueError("truncated pkt-line length")
        raw_len = buf[pos:pos + 4]
        try:
            length = int(raw_len, 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % raw_len)
        if length == 0:
            items.append(("flush", None))
            pos += 4
        elif length == 1:
            items.append(("delim", None))
            pos += 4
        else:
            if pos + length > n:
                raise ValueError("truncated pkt-line payload")
            items.append(("data", buf[pos + 4:pos + length]))
            pos += length
    return items, pos


def split_sideband(data):
    """Demux sideband packets. Returns (band1, band2, band3)."""
    band1 = b""
    band2 = b""
    band3 = b""
    items, _ = read_pkt_lines(data)
    for kind, payload in items:
        if kind != "data" or not payload:
            continue
        band = payload[0]
        chunk = payload[1:]
        if band == 1:
            band1 += chunk
        elif band == 2:
            band2 += chunk
        elif band == 3:
            band3 += chunk
    return band1, band2, band3


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------

def _read_varint(data, pos):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, pos


def _read_type_size(data, pos):
    b = data[pos]
    pos += 1
    typ = (b >> 4) & 0x7
    size = b & 0x0F
    shift = 4
    while b & 0x80:
        b = data[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
    return typ, size, pos


def _apply_delta(base, delta):
    pos = 0
    base_size, pos = _read_varint(delta, pos)
    if base_size != len(base):
        raise ValueError("delta base size mismatch")
    result_size, pos = _read_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != result_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def read_pack(data):
    """Parse a PACK file. Returns list of (type_name, raw_bytes).

    Resolves OFS_DELTA / REF_DELTA against earlier entries in the same pack
    and against nothing else (bases already in the store must be in the pack).
    """
    if len(data) < 12 or data[:4] != b"PACK":
        raise ValueError("not a pack file")
    version = struct.unpack(">I", data[4:8])[0]
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = struct.unpack(">I", data[8:12])[0]
    pos = 12
    entries = []          # (type_name, data)
    offsets = []          # pack offset of each entry (for OFS_DELTA)
    by_oid = {}           # oid -> (type_name, data) for REF_DELTA
    for _ in range(count):
        entry_start = pos
        typ, size, pos = _read_type_size(data, pos)
        if typ in (6, 7):  # OFS_DELTA / REF_DELTA
            if typ == 6:
                b = data[pos]
                pos += 1
                ofs = b & 0x7F
                while b & 0x80:
                    b = data[pos]
                    pos += 1
                    ofs = ((ofs + 1) << 7) | (b & 0x7F)
                base_pos = entry_start - ofs
                base_idx = offsets.index(base_pos)
                base_type, base_data = entries[base_idx]
            else:
                base_oid = data[pos:pos + 20].hex()
                pos += 20
                base_type, base_data = by_oid[base_oid]
            decomp = zlib.decompressobj()
            delta = decomp.decompress(data[pos:])
            consumed = len(data[pos:]) - len(decomp.unused_data)
            pos += consumed
            raw = _apply_delta(base_data, delta)
            typ_name = base_type
        else:
            typ_name = _TYPE_NAME[typ]
            decomp = zlib.decompressobj()
            raw = decomp.decompress(data[pos:])
            consumed = len(data[pos:]) - len(decomp.unused_data)
            pos += consumed
            if len(raw) != size:
                raise ValueError("object size mismatch")
        entries.append((typ_name, raw))
        offsets.append(entry_start)
        by_oid[hash_object(typ_name, raw)] = (typ_name, raw)
    trailer = data[pos:pos + 20]
    expect = hashlib.sha1(data[:pos]).digest()
    if trailer != expect:
        raise ValueError("pack trailer checksum mismatch")
    return entries


def write_pack(objects):
    """objects: list of (type_name, raw_bytes). Returns PACK bytes (no deltas)."""
    out = bytearray()
    out += b"PACK"
    out += struct.pack(">I", 2)
    out += struct.pack(">I", len(objects))
    for typ_name, raw in objects:
        code = _TYPE_CODE[typ_name]
        size = len(raw)
        header = bytearray()
        b0 = (code << 4) | (size & 0x0F)
        size >>= 4
        if size:
            b0 |= 0x80
        header.append(b0)
        while size:
            b = size & 0x7F
            size >>= 7
            if size:
                b |= 0x80
            header.append(b)
        out += bytes(header)
        out += zlib.compress(raw, 1)  # level 1: much faster; still valid zlib
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def sideband_pack(pack_bytes, chunk=32768):
    """Wrap pack bytes as band-1 sideband pkt-lines + flush."""
    out = bytearray()
    for i in range(0, len(pack_bytes), chunk):
        out += pkt_line(b"\x01" + pack_bytes[i:i + chunk])
    out += pkt_flush()
    return bytes(out)


def reachable(store, tips):
    """All object oids reachable from tip commits (commits, trees, blobs, tags)."""
    return reachable_shallow(store, tips, set())


def reachable_shallow(store, tips, shallow):
    """Like reachable(), but never follows the parents of a commit in
    `shallow`: a shallow clone holds the boundary commit and its tree,
    not its ancestors."""
    seen = set()
    stack = list(tips)
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        if oid not in store:
            continue
        seen.add(oid)
        typ, data = store[oid]
        if typ == "commit":
            c = parse_commit(data)
            if c["tree"]:
                stack.append(c["tree"])
            if oid not in shallow:
                stack.extend(c["parents"])
        elif typ == "tree":
            for _, _, child in parse_tree(data):
                stack.append(child)
        elif typ == "tag":
            t = parse_tag(data)
            if t["object"]:
                stack.append(t["object"])
    return seen


def commit_ancestors(store, tip):
    """Set of commit oids reachable from tip (following parents only)."""
    seen = set()
    stack = [tip]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID or oid not in store:
            continue
        typ, data = store[oid]
        if typ != "commit":
            continue
        seen.add(oid)
        stack.extend(parse_commit(data)["parents"])
    return seen


def commit_distance(store, tips):
    """Map commit oid -> shortest distance (generations) from any tip."""
    dist = {}
    queue = []
    for t in tips:
        if t in store and store[t][0] == "commit":
            dist[t] = 0
            queue.append(t)
    i = 0
    while i < len(queue):
        oid = queue[i]
        i += 1
        for parent in parse_commit(store[oid][1])["parents"]:
            if parent not in dist and parent in store and store[parent][0] == "commit":
                dist[parent] = dist[oid] + 1
                queue.append(parent)
    return dist
