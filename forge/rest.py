"""REST API v3: repos, branches, commits, contents, issues, labels, pulls.

Every reply: application/json, non-empty X-GitHub-Request-Id (set by the
server layer). 4xx bodies are {"message": ...}. Lists paginate via the
Link header with absolute same-origin URLs.
"""
import base64
import json

from . import gitobj
from .store import FIXED_DATE, VIEWER_LOGIN, Issue, PullRequest


def _json_body(handler, body):
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _page_params(query):
    try:
        per_page = int((query.get("per_page") or ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int((query.get("page") or ["1"])[0])
    except ValueError:
        page = 1
    return max(per_page, 1), max(page, 1)


def _paginate(handler, path, query, items):
    per_page, page = _page_params(query)
    total = len(items)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    host = handler.headers.get("Host") or "localhost"
    base = "http://%s%s" % (host, path)
    links = []
    if start + per_page < total:
        links.append('<%s?per_page=%d&page=%d>; rel="next"' % (base, per_page, page + 1))
    if page > 1:
        links.append('<%s?per_page=%d&page=%d>; rel="prev"' % (base, per_page, page - 1))
    last = max((total + per_page - 1) // per_page, 1)
    links.append('<%s?per_page=%d&page=1>; rel="first"' % (base, per_page))
    links.append('<%s?per_page=%d&page=%d>; rel="last"' % (base, per_page, last))
    return chunk, "; ".join(links)


class Rest:
    def __init__(self, ctx, handler):
        self.ctx = ctx
        self.handler = handler
        self.forge = ctx.forge

    def send(self, status, obj, link=None):
        h = self.handler
        raw = json.dumps(obj).encode("utf-8") if obj is not None else b""
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("X-GitHub-Request-Id", self.forge.next_request_id())
        if link:
            h.send_header("Link", link)
        h.send_header("Content-Length", str(len(raw)))
        h.end_headers()
        if h.command != "HEAD" and raw:
            try:
                h.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def error(self, status, message):
        self.send(status, {"message": message})

    # -- repo resolution -----------------------------------------------------
    def repo(self, owner, name):
        return self.forge.get_repo(owner, name)


def handle(ctx, handler, method, path, query, body):
    r = Rest(ctx, handler)
    parts = [p for p in path.split("/") if p != ""]
    # parts[0]='api', parts[1]='v3', rest follows
    rest = parts[2:]

    if rest == ["user"] and method == "GET":
        r.send(200, {"login": ctx.forge.known_owner, "id": 1000,
                     "type": "User", "name": "ada iversen"})
        return
    if rest == ["meta"] and method == "GET":
        r.send(200, {"installed_version": "1.0.0", "verifiable_password_authentication": True})
        return
    if rest == ["user", "repos"]:
        if method == "POST":
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            name = data.get("name")
            if not name:
                r.error(422, "name is required")
                return
            repo = ctx.forge.create_repo(name, data.get("private", False),
                                         data.get("description", ""))
            if repo is None:
                r.error(422, "name already exists on this account")
                return
            r.send(201, repo.shape())
            return
        if method == "GET":
            items = [repo.shape() for repo in ctx.forge.repos.values()]
            chunk, link = _paginate(handler, path, query, items)
            r.send(200, chunk, link=link)
            return
        r.error(404, "Not Found")
        return

    if len(rest) >= 3 and rest[0] == "repos":
        _repos(ctx, r, handler, method, rest[1:], query, body)
        return

    r.error(404, "Not Found")


def _repos(ctx, r, handler, method, tail, query, body):
    """tail: [owner, name, ...subpath]"""
    owner, name = tail[0], tail[1]
    sub = tail[2:]
    repo = ctx.forge.get_repo(owner, name)

    if not sub:
        if method == "GET":
            if repo is None:
                r.error(404, "Not Found")
            else:
                r.send(200, repo.shape())
            return
        if method == "PATCH":
            if repo is None:
                r.error(404, "Not Found")
                return
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            if "description" in data:
                repo.description = data["description"] or ""
            r.send(200, repo.shape())
            return
        if method == "DELETE":
            if repo is None:
                r.error(404, "Not Found")
                return
            del ctx.forge.repos[name]
            r.send(204, None)
            return
        r.error(404, "Not Found")
        return

    if repo is None:
        r.error(404, "Not Found")
        return

    head = sub[0]

    if head == "branches":
        if len(sub) == 1 and method == "GET":
            out = [{"name": b, "commit": {"sha": repo.ref("refs/heads/" + b)},
                    "protected": False} for b in repo.branches()]
            r.send(200, out)
            return
        if len(sub) == 2 and method == "GET":
            tip = repo.ref("refs/heads/" + sub[1])
            if tip is None:
                r.error(404, "Not Found")
            else:
                r.send(200, {"name": sub[1], "commit": {"sha": tip}, "protected": False})
            return
        r.error(404, "Not Found")
        return

    if head == "commits" and method == "GET":
        sha = (query.get("sha") or [None])[0]
        if sha:
            tip = repo.ref("refs/heads/" + sha) or (sha if sha in repo.objects else None)
        else:
            tip = repo.ref("refs/heads/" + repo.default_branch)
        if not tip:
            r.send(200, [])
            return
        out = []
        for oid in repo.commits_from(tip):
            c = gitobj.parse_commit(repo.objects[oid][1])
            out.append({
                "sha": oid,
                "commit": {
                    "message": c["message"],
                    "author": {"name": VIEWER_LOGIN, "email": "adaiversen@example.invalid",
                               "date": FIXED_DATE},
                    "committer": {"name": VIEWER_LOGIN, "email": "adaiversen@example.invalid",
                                  "date": FIXED_DATE},
                },
                "parents": [{"sha": p} for p in c["parents"]],
            })
        chunk, link = _paginate(handler, handler.path.split("?")[0], query, out)
        r.send(200, chunk, link=link)
        return

    if head == "contents":
        _contents(ctx, r, handler, method, repo, sub[1:], query, body)
        return

    if head == "git" and len(sub) >= 2 and sub[1] == "refs":
        if method == "GET" and len(sub) == 4:
            kind, refname = sub[2], sub[3]
            full = "refs/%s/%s" % (kind, refname)
            oid = repo.ref(full)
            if oid is None:
                r.error(404, "Not Found")
                return
            typ, _ = repo.objects.get(oid, ("commit", b""))
            r.send(200, {"ref": full, "object": {"sha": oid, "type": typ}})
            return
        if method == "POST" and len(sub) == 2:
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            ref = data.get("ref")
            sha = data.get("sha")
            if not ref or not sha:
                r.error(422, "ref and sha are required")
                return
            if repo.ref(ref) is not None:
                r.error(422, "Reference already exists")
                return
            if sha not in repo.objects:
                r.error(422, "Object does not exist")
                return
            repo.set_ref(ref, sha)
            typ, _ = repo.objects[sha]
            r.send(201, {"ref": ref, "object": {"sha": sha, "type": typ}})
            return
        r.error(404, "Not Found")
        return

    if head == "labels":
        if method == "GET":
            r.send(200, [{"name": n, "color": c, "description": ""}
                         for n, c in sorted(repo.labels.items())])
            return
        if method == "POST":
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            name = data.get("name")
            if not name:
                r.error(422, "name is required")
                return
            repo.labels[name] = data.get("color", "ededed")
            r.send(201, {"name": name, "color": repo.labels[name], "description": ""})
            return
        r.error(404, "Not Found")
        return

    if head == "issues":
        _issues(ctx, r, handler, method, repo, sub[1:], query, body)
        return

    if head == "pulls":
        _pulls(ctx, r, handler, method, repo, sub[1:], query, body)
        return

    r.error(404, "Not Found")


def _resolve_ref_tip(repo, ref):
    """ref: branch name or commit sha -> tip commit oid or None."""
    if ref is None:
        return repo.ref("refs/heads/" + repo.default_branch)
    tip = repo.ref("refs/heads/" + ref)
    if tip:
        return tip
    if ref in repo.objects and repo.objects[ref][0] == "commit":
        return ref
    return None


def _contents(ctx, r, handler, method, repo, tail, query, body):
    path = "/".join(tail)
    ref = (query.get("ref") or [None])[0]
    if method == "GET":
        tip = _resolve_ref_tip(repo, ref)
        if tip is None:
            r.error(404, "Not Found")
            return
        commit = gitobj.parse_commit(repo.objects[tip][1])
        files = gitobj.flatten_tree(repo.objects, commit["tree"])
        if path == "":
            out = []
            seen = set()
            for fpath, (mode, oid) in sorted(files.items()):
                first = fpath.split("/")[0]
                if first in seen:
                    continue
                seen.add(first)
                if "/" in fpath:
                    sub = gitobj.flatten_tree(repo.objects, commit["tree"])
                    out.append({"type": "dir", "name": first, "path": first,
                                "sha": _dir_tree_oid(repo, commit["tree"], first)})
                else:
                    out.append({"type": "file", "name": first, "path": first,
                                "sha": oid, "size": len(repo.objects[oid][1])})
            r.send(200, out)
            return
        if path in files:
            mode, oid = files[path]
            data = repo.objects[oid][1]
            r.send(200, {"type": "file", "encoding": "base64",
                         "content": base64.b64encode(data).decode("ascii"),
                         "sha": oid, "size": len(data), "name": path.split("/")[-1],
                         "path": path})
            return
        # directory?
        prefix = path + "/"
        children = {}
        for fpath, (mode, oid) in sorted(files.items()):
            if not fpath.startswith(prefix):
                continue
            rest_path = fpath[len(prefix):]
            first = rest_path.split("/")[0]
            if first in children:
                continue
            if "/" in rest_path:
                children[first] = {"type": "dir", "name": first,
                                   "path": path + "/" + first,
                                   "sha": _dir_tree_oid(repo, commit["tree"], path + "/" + first)}
            else:
                children[first] = {"type": "file", "name": first,
                                   "path": fpath, "sha": oid,
                                   "size": len(repo.objects[oid][1])}
        if children:
            r.send(200, [children[k] for k in sorted(children)])
            return
        r.error(404, "Not Found")
        return

    if method == "PUT":
        data = _json_body(handler, body)
        if data is None:
            r.error(400, "Invalid JSON")
            return
        message = data.get("message", "")
        content_b64 = data.get("content")
        if content_b64 is None:
            r.error(422, "content is required")
            return
        try:
            content = base64.b64decode(content_b64, validate=False)
        except Exception:
            r.error(400, "Invalid base64 content")
            return
        branch = data.get("branch") or repo.default_branch
        tip = repo.ref("refs/heads/" + branch)
        if tip is None:
            default_tip = repo.ref("refs/heads/" + repo.default_branch)
            if default_tip:
                repo.set_ref("refs/heads/" + branch, default_tip)
                tip = default_tip
        existing = None
        if tip:
            commit = gitobj.parse_commit(repo.objects[tip][1])
            files = gitobj.flatten_tree(repo.objects, commit["tree"])
            existing = files.get(path)
        if existing is not None and data.get("sha") and data["sha"] != existing[1]:
            r.error(409, "sha does not match the current file")
            return
        commit_oid, _ = repo.commit_on_branch(branch, message, {path: content})
        blob_oid = repo.store_object("blob", content)
        status = 201 if existing is None else 200
        r.send(status, {"content": {"name": path.split("/")[-1], "path": path,
                                    "sha": blob_oid, "size": len(content),
                                    "type": "file", "encoding": "base64",
                                    "content": content_b64},
                        "commit": {"sha": commit_oid, "message": message}})
        return

    if method == "DELETE":
        data = _json_body(handler, body)
        if data is None:
            r.error(400, "Invalid JSON")
            return
        branch = data.get("branch") or repo.default_branch
        tip = repo.ref("refs/heads/" + branch)
        if tip is None:
            r.error(404, "Not Found")
            return
        commit = gitobj.parse_commit(repo.objects[tip][1])
        files = gitobj.flatten_tree(repo.objects, commit["tree"])
        if path not in files:
            r.error(404, "Not Found")
            return
        commit_oid, _ = repo.commit_on_branch(branch, data.get("message", ""),
                                              {path: None})
        r.send(200, {"commit": {"sha": commit_oid, "message": data.get("message", "")}})
        return

    r.error(404, "Not Found")


def _dir_tree_oid(repo, root_tree_oid, dir_path):
    parts = dir_path.split("/")
    oid = root_tree_oid
    for part in parts:
        found = None
        for mode, name, child in gitobj.parse_tree(repo.objects[oid][1]):
            if name == part:
                found = child
                break
        if found is None:
            return None
        oid = found
    return oid


def _issues(ctx, r, handler, method, repo, tail, query, body):
    if not tail:
        if method == "POST":
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            title = data.get("title")
            if not title:
                r.error(422, "title is required")
                return
            labels = data.get("labels") or []
            for name in labels:
                if name not in repo.labels:
                    r.error(422, "Label does not exist")
                    return
            number = repo.next_number()
            issue = Issue(repo, number, title, data.get("body", ""), labels)
            repo.issues[number] = issue
            r.send(201, issue.shape())
            return
        if method == "GET":
            state = (query.get("state") or ["open"])[0]
            direction = (query.get("direction") or ["asc"])[0]
            items = [i for i in repo.issues.values()
                     if state == "all" or i.state == state]
            items.sort(key=lambda i: i.created_seq, reverse=(direction == "desc"))
            chunk, link = _paginate(handler, handler.path.split("?")[0], query,
                                   [i.shape() for i in items])
            r.send(200, chunk, link=link)
            return
        r.error(404, "Not Found")
        return

    try:
        number = int(tail[0])
    except ValueError:
        r.error(404, "Not Found")
        return
    issue = repo.issues.get(number)
    if len(tail) == 1:
        if method == "GET":
            if issue is None:
                r.error(404, "Not Found")
            else:
                r.send(200, issue.shape())
            return
        if method == "PATCH":
            if issue is None:
                r.error(404, "Not Found")
                return
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            if "state" in data and data["state"] in ("open", "closed"):
                issue.state = data["state"]
            if "title" in data:
                issue.title = data["title"]
            if "body" in data:
                issue.body = data["body"] or ""
            r.send(200, issue.shape())
            return
        r.error(404, "Not Found")
        return

    if len(tail) == 2 and tail[1] == "comments":
        if issue is None:
            r.error(404, "Not Found")
            return
        if method == "POST":
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            if not data.get("body"):
                r.error(422, "body is required")
                return
            comment = {"id": ctx.forge.next_id(), "body": data["body"],
                       "user": {"login": VIEWER_LOGIN}, "created_at": FIXED_DATE}
            issue.comments.append(comment)
            r.send(201, comment)
            return
        if method == "GET":
            r.send(200, list(issue.comments))
            return
        r.error(404, "Not Found")
        return

    r.error(404, "Not Found")


def _pulls(ctx, r, handler, method, repo, tail, query, body):
    if not tail:
        if method == "POST":
            data = _json_body(handler, body)
            if data is None:
                r.error(400, "Invalid JSON")
                return
            head = data.get("head")
            base = data.get("base")
            title = data.get("title")
            if not head or not base or not title:
                r.error(422, "head, base and title are required")
                return
            if repo.ref("refs/heads/" + head) is None or repo.ref("refs/heads/" + base) is None:
                r.error(422, "head or base branch does not exist")
                return
            for pr in repo.pulls.values():
                if pr.head == head and pr.state == "open":
                    r.error(422, "A pull request already exists for %s" % head)
                    return
            number = repo.next_number()
            pr = PullRequest(repo, number, title, data.get("body", ""), head, base)
            repo.pulls[number] = pr
            r.send(201, pr.shape())
            return
        if method == "GET":
            state = (query.get("state") or ["open"])[0]
            items = [p for p in repo.pulls.values()
                     if state == "all" or p.state == state]
            items.sort(key=lambda p: p.created_seq)
            chunk, link = _paginate(handler, handler.path.split("?")[0], query,
                                   [p.shape() for p in items])
            r.send(200, chunk, link=link)
            return
        r.error(404, "Not Found")
        return

    try:
        number = int(tail[0])
    except ValueError:
        r.error(404, "Not Found")
        return
    pr = repo.pulls.get(number)

    if len(tail) == 1 and method == "GET":
        if pr is None:
            r.error(404, "Not Found")
        else:
            r.send(200, pr.shape())
        return

    if len(tail) == 2 and tail[1] == "files" and method == "GET":
        if pr is None:
            r.error(404, "Not Found")
            return
        head_files = repo.branch_tip_files(pr.head)
        base_files = repo.branch_tip_files(pr.base)
        r.send(200, repo.diff_files(base_files, head_files))
        return

    if len(tail) == 2 and tail[1] == "merge" and method == "PUT":
        if pr is None:
            r.error(404, "Not Found")
            return
        merge_sha, err = repo.squash_merge(pr)
        if err:
            r.error(405, err)
            return
        r.send(200, {"merged": True, "sha": merge_sha,
                     "message": "Merge pull request #%d from %s" % (pr.number, pr.head)})
        return

    r.error(404, "Not Found")
