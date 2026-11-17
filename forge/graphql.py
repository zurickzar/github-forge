"""GitHub GraphQL v4 subset: a small parser + executor for exactly the
documents the gh CLI and the github-mcp-server send.

Supports: queries/mutations with names, variables (incl. defaults, null),
aliases, inline fragments (`... on Type`), inline input objects, enums,
lists, batched operation arrays. Unknown fields produce HTTP 200 with an
`errors` array naming the field (never HTTP 400). Unknown repository ->
data.repository = null with no errors. Global ids are opaque base64.
"""
import base64
import json

from .store import gid, ungid, Issue, PullRequest

# ---------------------------------------------------------------------------
# Lexer / parser
# ---------------------------------------------------------------------------

_NAME_START = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
_NAME_CHARS = _NAME_START + "0123456789"


class ParseError(Exception):
    pass


def _tokenize(src):
    toks = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\r\n,":
            i += 1
        elif c == "#":
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("...", i):
            toks.append(("...", "..."))
            i += 3
        elif c in "{}()[]:!=$@":
            toks.append((c, c))
            i += 1
        elif c == '"':
            j = i + 1
            out = []
            while j < n and src[j] != '"':
                if src[j] == "\\" and j + 1 < n:
                    esc = src[j + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"',
                                "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    out.append(src[j])
                    j += 1
            toks.append(("STRING", "".join(out)))
            i = j + 1
        elif c in _NAME_START:
            j = i
            while j < n and src[j] in _NAME_CHARS:
                j += 1
            toks.append(("NAME", src[i:j]))
            i = j
        elif c.isdigit() or c == "-":
            j = i
            while j < n and (src[j].isdigit() or src[j] in ".eE+-"):
                j += 1
            raw = src[i:j]
            try:
                toks.append(("INT", int(raw)))
            except ValueError:
                toks.append(("FLOAT", float(raw)))
            i = j
        else:
            raise ParseError("unexpected character %r" % c)
    toks.append(("EOF", None))
    return toks


class Parser:
    def __init__(self, src):
        self.toks = _tokenize(src)
        self.pos = 0

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise ParseError("expected %s, got %r" % (kind, tok))
        return tok

    def skip(self, kind):
        if self.peek()[0] == kind:
            self.pos += 1
            return True
        return False

    def parse_document(self):
        defs = []
        while self.peek()[0] != "EOF":
            defs.append(self.parse_definition())
        return defs

    def parse_definition(self):
        kind, val = self.peek()
        if kind == "{":
            return {"kind": "operation", "op": "query", "name": None,
                    "variables": {}, "selections": self.parse_selections()}
        if kind == "NAME" and val in ("query", "mutation", "subscription"):
            self.next()
            name = None
            if self.peek()[0] == "NAME":
                name = self.next()[1]
            variables = {}
            if self.peek()[0] == "(":
                variables = self.parse_variable_defs()
            return {"kind": "operation", "op": val, "name": name,
                    "variables": variables, "selections": self.parse_selections()}
        if kind == "NAME" and val == "fragment":
            self.next()
            name = self.expect("NAME")[1]
            self.expect("NAME")  # 'on'
            type_cond = self.expect("NAME")[1]
            return {"kind": "fragment", "name": name, "type": type_cond,
                    "selections": self.parse_selections()}
        raise ParseError("unexpected definition %r" % (self.peek(),))

    def parse_variable_defs(self):
        self.expect("(")
        out = {}
        while not self.skip(")"):
            self.expect("$")
            name = self.expect("NAME")[1]
            self.expect(":")
            self.parse_type()
            if self.skip("="):
                out[name] = self.parse_value()
            else:
                out[name] = None
        return out

    def parse_type(self):
        if self.skip("["):
            self.parse_type()
            self.expect("]")
        else:
            self.expect("NAME")
        self.skip("!")

    def parse_selections(self):
        self.expect("{")
        sels = []
        while not self.skip("}"):
            sels.append(self.parse_selection())
        return sels

    def parse_selection(self):
        if self.skip("..."):
            if self.peek()[0] == "NAME" and self.peek()[1] == "on":
                self.next()
                type_cond = self.expect("NAME")[1]
                return {"kind": "inline", "type": type_cond,
                        "selections": self.parse_selections()}
            name = self.expect("NAME")[1]
            return {"kind": "spread", "name": name}
        name = self.expect("NAME")[1]
        alias = name
        if self.skip(":"):
            alias = name
            name = self.expect("NAME")[1]
        args = {}
        if self.peek()[0] == "(":
            args = self.parse_arguments()
        if self.peek()[0] == "{":
            sels = self.parse_selections()
        else:
            sels = None
        return {"kind": "field", "name": name, "alias": alias,
                "args": args, "selections": sels}

    def parse_arguments(self):
        self.expect("(")
        out = {}
        while not self.skip(")"):
            name = self.expect("NAME")[1]
            self.expect(":")
            out[name] = self.parse_value()
        return out

    def parse_value(self):
        kind, val = self.next()
        if kind == "$":
            return ("var", self.expect("NAME")[1])
        if kind == "STRING":
            return val
        if kind == "INT":
            return val
        if kind == "FLOAT":
            return val
        if kind == "NAME":
            if val == "true":
                return True
            if val == "false":
                return False
            if val == "null":
                return None
            return ("enum", val)
        if kind == "[":
            out = []
            while not self.skip("]"):
                out.append(self.parse_value())
            return out
        if kind == "{":
            out = {}
            while not self.skip("}"):
                name = self.expect("NAME")[1]
                self.expect(":")
                out[name] = self.parse_value()
            return out
        raise ParseError("unexpected value token %r" % (kind,))


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

TYPE_FIELDS = {
    "Issue": ["id", "number", "title", "state", "body", "url", "labels",
              "user", "comments"],
    "PullRequest": ["id", "number", "title", "body", "state", "merged",
                    "headRefName", "baseRefName", "mergeable", "files", "url",
                    "headRef", "baseRef"],
    "Repository": ["id", "databaseId", "name", "nameWithOwner", "owner",
                   "defaultBranchRef", "viewerPermission", "hasIssuesEnabled",
                   "issue", "issues", "pullRequests", "pullRequest",
                   "description", "url"],
    "SearchType": [],
}


def _cursor(number):
    raw = ("c:%d" % number).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _uncursor(value):
    try:
        pad = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + pad).decode("utf-8")
        if raw.startswith("c:"):
            return int(raw[2:])
    except Exception:
        pass
    return None


class Executor:
    def __init__(self, forge):
        self.forge = forge
        self.errors = []

    # -- value resolution ---------------------------------------------------
    def resolve_value(self, value, variables):
        if isinstance(value, tuple) and value[0] == "var":
            return variables.get(value[1])
        if isinstance(value, tuple) and value[0] == "enum":
            return value[1]
        if isinstance(value, list):
            return [self.resolve_value(v, variables) for v in value]
        if isinstance(value, dict):
            return {k: self.resolve_value(v, variables) for k, v in value.items()}
        return value

    def resolve_args(self, args, variables):
        return {k: self.resolve_value(v, variables) for k, v in args.items()}

    # -- selection execution -------------------------------------------------
    def execute(self, selections, obj, type_name, variables, fragments):
        if obj is None:
            return None
        out = {}
        for sel in selections:
            if sel["kind"] == "inline":
                if sel["type"] == type_name or sel["type"] in ("Node",):
                    sub = self.execute(sel["selections"], obj, type_name, variables, fragments)
                    if sub:
                        out.update(sub)
                continue
            if sel["kind"] == "spread":
                frag = fragments.get(sel["name"])
                if frag:
                    sub = self.execute(frag["selections"], obj, type_name, variables, fragments)
                    if sub:
                        out.update(sub)
                continue
            field = sel["name"]
            alias = sel["alias"]
            if field == "__typename":
                out[alias] = type_name
                continue
            resolver = self.field_resolver(type_name, field)
            if resolver is None:
                self.errors.append({
                    "message": 'Cannot query field "%s" on type "%s".' % (field, type_name)})
                out[alias] = None
                continue
            args = self.resolve_args(sel["args"], variables)
            value = resolver(obj, args)
            if sel["selections"] is not None:
                value = self.execute_child(value, sel["selections"], variables, fragments)
            out[alias] = value
        return out

    def execute_child(self, value, selections, variables, fragments):
        if value is None:
            return None
        if isinstance(value, list):
            return [self.execute(selections, item, item["__type"], variables, fragments)
                    if isinstance(item, dict) and "__type" in item else item
                    for item in value]
        if isinstance(value, dict) and "__type" in value:
            return self.execute(selections, value, value["__type"], variables, fragments)
        return value

    def field_resolver(self, type_name, field):
        table = getattr(self, "fields_" + type_name, None)
        if table is None:
            return None
        return table.get(field)

    # -- type wrappers ---------------------------------------------------------
    def wrap_repo(self, repo):
        return {"__type": "Repository", "__repo": repo}

    def wrap_issue(self, issue):
        return {"__type": "Issue", "__issue": issue}

    def wrap_pr(self, pr):
        return {"__type": "PullRequest", "__pr": pr}

    def wrap_user(self, login):
        return {"__type": "User", "__login": login}

    # -- Query -----------------------------------------------------------------
    @property
    def fields_Query(self):
        return {
            "viewer": lambda _o, _a: {"__type": "Viewer",
                                      "__login": self.forge.known_owner},
            "repository": self._q_repository,
            "node": self._q_node,
            "__type": self._q_type,
            "__schema": lambda _o, _a: {"__type": "__Schema"},
        }

    def _q_repository(self, _obj, args):
        owner = args.get("owner")
        name = args.get("name")
        repo = self.forge.get_repo(owner, name) if owner and name else None
        return self.wrap_repo(repo) if repo else None

    def _q_node(self, _obj, args):
        kind, parts = ungid(args.get("id") or "")
        if kind == "R" and len(parts) == 1:
            repo = self.forge.repo_by_id(int(parts[0])) if parts[0].isdigit() else None
            return self.wrap_repo(repo) if repo else None
        if kind == "I" and len(parts) == 2:
            repo = self.forge.repo_by_id(int(parts[0])) if parts[0].isdigit() else None
            issue = repo.issues.get(int(parts[1])) if repo and parts[1].isdigit() else None
            return self.wrap_issue(issue) if issue else None
        if kind == "P" and len(parts) == 2:
            repo = self.forge.repo_by_id(int(parts[0])) if parts[0].isdigit() else None
            pr = repo.pulls.get(int(parts[1])) if repo and parts[1].isdigit() else None
            return self.wrap_pr(pr) if pr else None
        return None

    def _q_type(self, _obj, args):
        name = args.get("name")
        if name in ("Issue", "PullRequest", "Repository"):
            return {"__type": "__Type", "__name": name,
                    "__fields": TYPE_FIELDS[name], "__kind": "OBJECT"}
        if name == "SearchType":
            return {"__type": "__Type", "__name": name,
                    "__fields": [], "__kind": "ENUM",
                    "__enum": ["ISSUE", "REPOSITORY", "USER"]}
        return None

    # -- Viewer / User ---------------------------------------------------------
    @property
    def fields_Viewer(self):
        return {"login": lambda o, _a: o["__login"]}

    @property
    def fields_User(self):
        return {"login": lambda o, _a: o["__login"]}

    # -- Repository --------------------------------------------------------------
    @property
    def fields_Repository(self):
        return {
            "id": lambda o, _a: gid("R", o["__repo"].id),
            "databaseId": lambda o, _a: o["__repo"].id,
            "name": lambda o, _a: o["__repo"].name,
            "nameWithOwner": lambda o, _a: "%s/%s" % (o["__repo"].owner, o["__repo"].name),
            "owner": lambda o, _a: self.wrap_user(o["__repo"].owner),
            "defaultBranchRef": self._r_default_branch,
            "viewerPermission": lambda _o, _a: "ADMIN",
            "hasIssuesEnabled": lambda _o, _a: True,
            "description": lambda o, _a: o["__repo"].description,
            "url": lambda o, _a: "https://example.invalid/%s/%s" % (o["__repo"].owner, o["__repo"].name),
            "issue": self._r_issue,
            "issues": self._r_issues,
            "pullRequests": self._r_pull_requests,
            "pullRequest": self._r_pull_request,
        }

    def _r_default_branch(self, obj, _args):
        repo = obj["__repo"]
        tip = repo.ref("refs/heads/" + repo.default_branch)
        if tip is None:
            return None
        return {"__type": "Ref", "__name": repo.default_branch, "__oid": tip}

    def _r_issue(self, obj, args):
        repo = obj["__repo"]
        issue = repo.issues.get(args.get("number"))
        return self.wrap_issue(issue) if issue else None

    def _r_issues(self, obj, args):
        repo = obj["__repo"]
        items = list(repo.issues.values())
        states = args.get("states")
        if states:
            want = {"OPEN": "open", "CLOSED": "closed"}
            items = [i for i in items if i.state in
                     {want.get(s, s.lower()) for s in states}]
        order = args.get("orderBy") or {}
        direction = (order.get("direction") or "ASC").upper()
        items.sort(key=lambda i: i.created_seq, reverse=(direction == "DESC"))
        return self._connection(items, args, lambda i: self.wrap_issue(i),
                                "IssueConnection")

    def _r_pull_requests(self, obj, args):
        repo = obj["__repo"]
        items = sorted(repo.pulls.values(), key=lambda p: p.created_seq)
        return self._connection(items, args, lambda p: self.wrap_pr(p),
                                "PullRequestConnection")

    def _r_pull_request(self, obj, args):
        repo = obj["__repo"]
        pr = repo.pulls.get(args.get("number"))
        return self.wrap_pr(pr) if pr else None

    def _connection(self, items, args, wrap, conn_type):
        after = args.get("after")
        if after:
            cur = _uncursor(after)
            if cur is not None:
                idx = next((i for i, it in enumerate(items) if it.number == cur), None)
                if idx is not None:
                    items = items[idx + 1:]
        first = args.get("first")
        total_after = len(items)
        if first is not None:
            items = items[:first]
        end_cursor = _cursor(items[-1].number) if items else None
        return {
            "__type": conn_type,
            "__nodes": [wrap(it) for it in items],
            "__page": {"__type": "PageInfo",
                       "__has_next": total_after > len(items),
                       "__end_cursor": end_cursor},
        }

    # -- Issue ---------------------------------------------------------------------
    @property
    def fields_Issue(self):
        return {
            "id": lambda o, _a: o["__issue"].gid,
            "number": lambda o, _a: o["__issue"].number,
            "title": lambda o, _a: o["__issue"].title,
            "state": lambda o, _a: o["__issue"].state.upper(),
            "body": lambda o, _a: o["__issue"].body,
            "url": lambda o, _a: "https://example.invalid/%s/%s/issues/%d" % (
                o["__issue"].repo.owner, o["__issue"].repo.name, o["__issue"].number),
            "labels": lambda o, _a: [{"__type": "Label", "__name": n}
                                     for n in o["__issue"].labels],
            "user": lambda o, _a: self.wrap_user(o["__issue"].repo.owner),
            "comments": lambda o, _a: [{"__type": "Comment", "__c": c}
                                       for c in o["__issue"].comments],
        }

    # -- PullRequest -----------------------------------------------------------------
    @property
    def fields_PullRequest(self):
        return {
            "id": lambda o, _a: o["__pr"].gid,
            "number": lambda o, _a: o["__pr"].number,
            "title": lambda o, _a: o["__pr"].title,
            "body": lambda o, _a: o["__pr"].body,
            "state": lambda o, _a: ("MERGED" if o["__pr"].merged
                                    else o["__pr"].state.upper()),
            "merged": lambda o, _a: o["__pr"].merged,
            "headRefName": lambda o, _a: o["__pr"].head,
            "baseRefName": lambda o, _a: o["__pr"].base,
            "mergeable": self._pr_mergeable,
            "files": self._pr_files,
            "url": lambda o, _a: "https://example.invalid/%s/%s/pull/%d" % (
                o["__pr"].repo.owner, o["__pr"].repo.name, o["__pr"].number),
            "headRef": lambda o, _a: {"__type": "Ref", "__name": o["__pr"].head,
                                      "__oid": o["__pr"].head_sha()},
            "baseRef": lambda o, _a: {"__type": "Ref", "__name": o["__pr"].base,
                                      "__oid": o["__pr"].base_sha()},
        }

    def _pr_mergeable(self, obj, _args):
        pr = obj["__pr"]
        if pr.state != "open":
            return "UNKNOWN"
        return "CONFLICTING" if pr.repo.merge_conflict(pr) else "MERGEABLE"

    def _pr_files(self, obj, args):
        pr = obj["__pr"]
        head_files = pr.repo.branch_tip_files(pr.head)
        base_files = pr.repo.branch_tip_files(pr.base)
        diff = pr.repo.diff_files(base_files, head_files)
        first = args.get("first")
        if first is not None:
            diff = diff[:first]
        return {"__type": "PullRequestFiles",
                "__nodes": [{"__type": "File", "__path": d["filename"]} for d in diff]}

    # -- connections / misc -----------------------------------------------------------
    @property
    def fields_IssueConnection(self):
        return {
            "nodes": lambda o, _a: o["__nodes"],
            "pageInfo": lambda o, _a: o["__page"],
            "totalCount": lambda o, _a: len(o["__nodes"]),
        }

    @property
    def fields_PullRequestConnection(self):
        return {
            "nodes": lambda o, _a: o["__nodes"],
            "pageInfo": lambda o, _a: o["__page"],
            "totalCount": lambda o, _a: len(o["__nodes"]),
        }

    @property
    def fields_PullRequestFiles(self):
        return {"nodes": lambda o, _a: o["__nodes"]}

    @property
    def fields_PageInfo(self):
        return {
            "hasNextPage": lambda o, _a: o["__has_next"],
            "endCursor": lambda o, _a: o["__end_cursor"],
            "hasPreviousPage": lambda _o, _a: False,
            "startCursor": lambda _o, _a: None,
        }

    @property
    def fields_Ref(self):
        return {"name": lambda o, _a: o["__name"],
                "target": lambda o, _a: {"__type": "Commit", "__oid": o["__oid"]}}

    @property
    def fields_Commit(self):
        return {"oid": lambda o, _a: o["__oid"]}

    @property
    def fields_File(self):
        return {"path": lambda o, _a: o["__path"]}

    @property
    def fields_Label(self):
        return {"name": lambda o, _a: o["__name"]}

    @property
    def fields_Comment(self):
        return {"id": lambda o, _a: o["__c"].get("gid", ""),
                "body": lambda o, _a: o["__c"]["body"]}

    @property
    def fields___Type(self):
        return {
            "name": lambda o, _a: o["__name"],
            "kind": lambda o, _a: o["__kind"],
            "fields": lambda o, _a: [{"__type": "__Field", "__name": f}
                                     for f in o.get("__fields", [])],
            "enumValues": lambda o, _a: [{"__type": "__EnumValue", "__name": v}
                                         for v in o.get("__enum", [])],
        }

    @property
    def fields___Field(self):
        return {"name": lambda o, _a: o["__name"]}

    @property
    def fields___EnumValue(self):
        return {"name": lambda o, _a: o["__name"]}

    @property
    def fields___Schema(self):
        return {}

    # -- mutations ---------------------------------------------------------------------
    @property
    def fields_Mutation(self):
        return {
            "createIssue": self._m_create_issue,
            "closeIssue": self._m_close_issue,
            "createPullRequest": self._m_create_pr,
            "mergePullRequest": self._m_merge_pr,
            "addComment": self._m_add_comment,
        }

    def _resolve_repo_gid(self, value):
        kind, parts = ungid(value or "")
        if kind == "R" and len(parts) == 1 and parts[0].isdigit():
            return self.forge.repo_by_id(int(parts[0]))
        return None

    def _m_create_issue(self, _obj, args):
        inp = args.get("input") or {}
        repo = self._resolve_repo_gid(inp.get("repositoryId"))
        if repo is None:
            return None
        number = repo.next_number()
        issue = Issue(repo, number, inp.get("title", ""), inp.get("body", ""), [])
        repo.issues[number] = issue
        return {"__type": "CreateIssuePayload", "__issue": self.wrap_issue(issue)}

    def _m_close_issue(self, _obj, args):
        inp = args.get("input") or {}
        kind, parts = ungid(inp.get("issueId") or "")
        issue = None
        if kind == "I" and len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            repo = self.forge.repo_by_id(int(parts[0]))
            if repo:
                issue = repo.issues.get(int(parts[1]))
        if issue is None:
            return None
        issue.state = "closed"
        return {"__type": "CloseIssuePayload", "__issue": self.wrap_issue(issue)}

    def _m_create_pr(self, _obj, args):
        inp = args.get("input") or {}
        repo = self._resolve_repo_gid(inp.get("repositoryId"))
        if repo is None:
            return None
        head = inp.get("headRefName")
        base = inp.get("baseRefName")
        if repo.ref("refs/heads/" + head) is None or repo.ref("refs/heads/" + base) is None:
            return None
        for existing in repo.pulls.values():
            if existing.head == head and existing.state == "open":
                return {"__type": "CreatePullRequestPayload",
                        "__pr": self.wrap_pr(existing)}
        number = repo.next_number()
        pr = PullRequest(repo, number, inp.get("title", ""), "", head, base)
        repo.pulls[number] = pr
        return {"__type": "CreatePullRequestPayload", "__pr": self.wrap_pr(pr)}

    def _m_merge_pr(self, _obj, args):
        inp = args.get("input") or {}
        kind, parts = ungid(inp.get("pullRequestId") or "")
        pr = None
        if kind == "P" and len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            repo = self.forge.repo_by_id(int(parts[0]))
            if repo:
                pr = repo.pulls.get(int(parts[1]))
        if pr is None:
            return None
        pr.repo.squash_merge(pr)
        return {"__type": "MergePullRequestPayload", "__pr": self.wrap_pr(pr)}

    def _m_add_comment(self, _obj, args):
        inp = args.get("input") or {}
        kind, parts = ungid(inp.get("subjectId") or "")
        issue = None
        if kind == "I" and len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            repo = self.forge.repo_by_id(int(parts[0]))
            if repo:
                issue = repo.issues.get(int(parts[1]))
        if issue is None:
            return None
        comment = {"id": self.forge.next_id(), "body": inp.get("body", ""),
                   "user": {"login": issue.repo.owner}}
        comment["gid"] = gid("C", issue.repo.id, issue.number, comment["id"])
        issue.comments.append(comment)
        return {"__type": "AddCommentPayload",
                "__edge": {"__type": "CommentEdge",
                           "__node": {"__type": "Comment", "__c": comment}}}

    @property
    def fields_CreateIssuePayload(self):
        return {"issue": lambda o, _a: o["__issue"]}

    @property
    def fields_CloseIssuePayload(self):
        return {"issue": lambda o, _a: o["__issue"]}

    @property
    def fields_CreatePullRequestPayload(self):
        return {"pullRequest": lambda o, _a: o["__pr"]}

    @property
    def fields_MergePullRequestPayload(self):
        return {"pullRequest": lambda o, _a: o["__pr"]}

    @property
    def fields_AddCommentPayload(self):
        return {"commentEdge": lambda o, _a: o["__edge"]}

    @property
    def fields_CommentEdge(self):
        return {"node": lambda o, _a: o["__node"]}

    # -- top-level -----------------------------------------------------------------------
    def run_operation(self, doc, variables, operation_name=None):
        self.errors = []
        fragments = {d["name"]: d for d in doc if d["kind"] == "fragment"}
        ops = [d for d in doc if d["kind"] == "operation"]
        if not ops:
            return {"data": None, "errors": [{"message": "No operation in document"}]}
        op = None
        if operation_name:
            for candidate in ops:
                if candidate["name"] == operation_name:
                    op = candidate
                    break
        if op is None:
            op = ops[0]
        variables = dict(variables or {})
        for name, default in op["variables"].items():
            if name not in variables and default is not None:
                variables[name] = self.resolve_value(default, variables)
        root_type = "Query" if op["op"] == "query" else "Mutation"
        root = {"__type": root_type}
        data = self.execute(op["selections"], root, root_type, variables, fragments)
        result = {"data": data}
        if self.errors:
            result["errors"] = self.errors
        return result


def handle(ctx, handler, body):
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError):
        handler._send_json(400, {"errors": [{"message": "Invalid JSON body"}]})
        return
    if payload is None:
        handler._send_json(400, {"errors": [{"message": "Empty body"}]})
        return
    ops = payload if isinstance(payload, list) else [payload]
    results = []
    for item in ops:
        if not isinstance(item, dict) or "query" not in item:
            results.append({"data": None,
                            "errors": [{"message": "Missing query"}]})
            continue
        try:
            doc = Parser(item["query"]).parse_document()
        except ParseError as exc:
            results.append({"data": None,
                            "errors": [{"message": "GraphQL syntax error: %s" % exc}]})
            continue
        executor = Executor(ctx.forge)
        results.append(executor.run_operation(
            doc, item.get("variables") or {}, item.get("operationName")))
    handler._send_json(200, results if isinstance(payload, list) else results[0])
