"""MCP over streamable HTTP (plain JSON). JSON-RPC 2.0.

Methods: server/discover (2026-07-28 era), initialize (legacy fallback),
notifications/initialized, tools/list, tools/call, ping.
Auth is enforced by the server layer (401 for any method without the token).
Non-JSON body -> HTTP 400 with JSON-RPC -32700. Unknown method -> -32601.
Unknown tool / missing required args -> -32602. Tool results carry both
content (text JSON) and structuredContent. The tool list is fixed and
deterministic (two tools/list answers are byte-identical).
"""
import base64
import json

from .store import Issue, PullRequest

PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
                     "2024-11-05"]
SERVER_NAME = "github-forge"
SERVER_VERSION = "1.0.0"


def _tool(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties,
                        "required": required},
    }


def _string(desc):
    return {"type": "string", "description": desc}


def _boolean(desc):
    return {"type": "boolean", "description": desc}


def _integer(desc):
    return {"type": "integer", "description": desc}


TOOLS = [
    _tool("create_repo", "Create a new repository for the token owner.",
          {"name": _string("Repository name (required)."),
           "description": _string("Short description of the repository."),
           "private": _boolean("Whether the repository is private.")},
          ["name"]),
    _tool("get_repo", "Get a repository by owner and name.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required).")},
          ["owner", "repo"]),
    _tool("create_issue", "Create an issue in a repository.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "title": _string("Issue title (required)."),
           "body": _string("Issue body.")},
          ["owner", "repo", "title"]),
    _tool("list_issues", "List issues of a repository.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "state": _string("Issue state: open, closed or all.")},
          ["owner", "repo"]),
    _tool("create_pull_request", "Create a pull request.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "title": _string("Pull request title (required)."),
           "head": _string("Head branch name (required)."),
           "base": _string("Base branch name (required)."),
           "body": _string("Pull request body.")},
          ["owner", "repo", "title", "head", "base"]),
    _tool("merge_pull_request", "Merge an open pull request (squash).",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "pull_number": _integer("Pull request number (required).")},
          ["owner", "repo", "pull_number"]),
    _tool("get_issue", "Get one issue by number.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "issue_number": _integer("Issue number (required).")},
          ["owner", "repo", "issue_number"]),
    _tool("list_repos", "List repositories of the token owner.",
          {}, []),
    _tool("get_file_content", "Read a file from a repository branch.",
          {"owner": _string("Repository owner login (required)."),
           "repo": _string("Repository name (required)."),
           "path": _string("File path in the repository (required)."),
           "ref": _string("Branch name (defaults to the default branch).")},
          ["owner", "repo", "path"]),
]


class McpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class ToolError(Exception):
    pass


def _result_text(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}],
            "structuredContent": obj}


def _require(args, *names):
    for name in names:
        if args.get(name) is None:
            raise McpError(-32602, "Missing required argument: %s" % name)


class Tools:
    def __init__(self, forge):
        self.forge = forge

    def _repo(self, args):
        repo = self.forge.get_repo(args.get("owner"), args.get("repo"))
        if repo is None:
            raise ToolError("Repository not found")
        return repo

    def create_repo(self, args):
        _require(args, "name")
        repo = self.forge.create_repo(args["name"], bool(args.get("private")),
                                      args.get("description", "") or "")
        if repo is None:
            raise ToolError("Repository already exists")
        return repo.shape()

    def get_repo(self, args):
        _require(args, "owner", "repo")
        return self._repo(args).shape()

    def create_issue(self, args):
        _require(args, "owner", "repo", "title")
        repo = self._repo(args)
        number = repo.next_number()
        issue = Issue(repo, number, args["title"], args.get("body", "") or "", [])
        repo.issues[number] = issue
        return issue.shape()

    def list_issues(self, args):
        _require(args, "owner", "repo")
        repo = self._repo(args)
        state = args.get("state") or "open"
        items = [i.shape() for i in sorted(repo.issues.values(),
                                           key=lambda x: x.created_seq)
                 if state == "all" or i.state == state]
        return {"issues": items}

    def create_pull_request(self, args):
        _require(args, "owner", "repo", "title", "head", "base")
        repo = self._repo(args)
        head, base = args["head"], args["base"]
        if repo.ref("refs/heads/" + head) is None or repo.ref("refs/heads/" + base) is None:
            raise ToolError("head or base branch does not exist")
        for existing in repo.pulls.values():
            if existing.head == head and existing.state == "open":
                return existing.shape()
        number = repo.next_number()
        pr = PullRequest(repo, number, args["title"], args.get("body", "") or "",
                         head, base)
        repo.pulls[number] = pr
        return pr.shape()

    def merge_pull_request(self, args):
        _require(args, "owner", "repo", "pull_number")
        repo = self._repo(args)
        try:
            number = int(args["pull_number"])
        except (TypeError, ValueError):
            raise ToolError("pull_number must be an integer")
        pr = repo.pulls.get(number)
        if pr is None:
            raise ToolError("Pull request not found")
        merge_sha, err = repo.squash_merge(pr)
        if err:
            raise ToolError(err)
        return {"merged": True, "sha": merge_sha, "number": number}

    def get_issue(self, args):
        _require(args, "owner", "repo", "issue_number")
        repo = self._repo(args)
        try:
            number = int(args["issue_number"])
        except (TypeError, ValueError):
            raise ToolError("issue_number must be an integer")
        issue = repo.issues.get(number)
        if issue is None:
            raise ToolError("Issue not found")
        return issue.shape()

    def list_repos(self, args):
        return {"repositories": [r.shape() for r in self.forge.repos.values()]}

    def get_file_content(self, args):
        _require(args, "owner", "repo", "path")
        from . import gitobj
        repo = self._repo(args)
        ref = args.get("ref") or repo.default_branch
        tip = repo.ref("refs/heads/" + ref)
        if tip is None:
            raise ToolError("Branch not found")
        commit = gitobj.parse_commit(repo.objects[tip][1])
        files = gitobj.flatten_tree(repo.objects, commit["tree"])
        entry = files.get(args["path"])
        if entry is None:
            raise ToolError("File not found")
        data = repo.objects[entry[1]][1]
        return {"path": args["path"], "sha": entry[1], "size": len(data),
                "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii")}


DISPATCH = {
    "create_repo": Tools.create_repo,
    "get_repo": Tools.get_repo,
    "create_issue": Tools.create_issue,
    "list_issues": Tools.list_issues,
    "create_pull_request": Tools.create_pull_request,
    "merge_pull_request": Tools.merge_pull_request,
    "get_issue": Tools.get_issue,
    "list_repos": Tools.list_repos,
    "get_file_content": Tools.get_file_content,
}


def handle(ctx, handler, body):
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        handler._send(400, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
        return
    if not isinstance(payload, dict):
        handler._send(400, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}})
        return
    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    # notifications (no id): acknowledge, no body
    if req_id is None:
        handler._send(202, b"", "application/json")
        return

    def ok(result):
        handler._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": result})

    def err(code, message):
        handler._send_json(200, {"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": code, "message": message}})

    try:
        if method == "server/discover":
            ok({
                "resultType": "DiscoverResult",
                "supportedVersions": PROTOCOL_VERSIONS,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "_meta": {"io.modelcontextprotocol/serverInfo":
                          {"name": SERVER_NAME, "version": SERVER_VERSION}},
            })
        elif method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else "2025-11-25"
            ok({"protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
        elif method == "ping":
            ok({})
        elif method == "tools/list":
            ok({"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            if name not in DISPATCH:
                raise McpError(-32602, "Unknown tool: %s" % name)
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise McpError(-32602, "arguments must be an object")
            tools = Tools(ctx.forge)
            try:
                result = DISPATCH[name](tools, args)
            except ToolError as exc:
                ok({"content": [{"type": "text", "text": str(exc)}],
                    "isError": True})
                return
            ok(_result_text(result))
        else:
            raise McpError(-32601, "Method not found: %s" % method)
    except McpError as exc:
        err(exc.code, exc.message)
