"""In-memory forge state: repos, git objects, refs, issues, PRs, labels.

Everything is deterministic: fixed timestamps, sequential counters, sorted
iteration. No wall-clock time, no randomness anywhere in stored state.
"""
import base64
import difflib

from . import gitobj

FIXED_DATE = "2026-01-01T00:00:00Z"
VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
VIEWER_IDENTITY = "%s <%s>" % (VIEWER_LOGIN, VIEWER_EMAIL)


def gid(kind, *parts):
    raw = (kind + ":" + ":".join(str(p) for p in parts)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def ungid(value):
    try:
        pad = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(value + pad).decode("utf-8")
        parts = raw.split(":")
        return parts[0], parts[1:]
    except Exception:
        return None, []


class Issue:
    def __init__(self, repo, number, title, body, labels):
        self.repo = repo
        self.number = number
        self.title = title
        self.body = body or ""
        self.state = "open"
        self.labels = list(labels)
        self.created_seq = repo.next_seq()
        self.comments = []
        self.id = repo.store.next_id()
        repo.issue_ids[number] = self.id

    @property
    def gid(self):
        return gid("I", self.repo.id, self.number)

    def shape(self):
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": [{"name": n} for n in self.labels],
            "user": {"login": VIEWER_LOGIN},
            "created_at": FIXED_DATE,
            "updated_at": FIXED_DATE,
            "url": "/api/v3/repos/%s/%s/issues/%d" % (self.repo.owner, self.repo.name, self.number),
            "html_url": "https://example.invalid/%s/%s/issues/%d" % (self.repo.owner, self.repo.name, self.number),
        }


class PullRequest:
    def __init__(self, repo, number, title, body, head, base):
        self.repo = repo
        self.number = number
        self.title = title
        self.body = body or ""
        self.head = head
        self.base = base
        self.state = "open"
        self.merged = False
        self.merged_at = None
        self.merge_commit_sha = None
        self.created_seq = repo.next_seq()
        self.id = repo.store.next_id()
        repo.issue_ids[number] = self.id

    @property
    def gid(self):
        return gid("P", self.repo.id, self.number)

    def head_sha(self):
        return self.repo.ref("refs/heads/" + self.head)

    def base_sha(self):
        return self.repo.ref("refs/heads/" + self.base)

    def shape(self):
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "merged": self.merged,
            "merged_at": self.merged_at,
            "merge_commit_sha": self.merge_commit_sha,
            "head": {"ref": self.head, "sha": self.head_sha()},
            "base": {"ref": self.base, "sha": self.base_sha()},
            "user": {"login": VIEWER_LOGIN},
            "created_at": FIXED_DATE,
            "updated_at": FIXED_DATE,
            "url": "/api/v3/repos/%s/%s/pulls/%d" % (self.repo.owner, self.repo.name, self.number),
            "html_url": "https://example.invalid/%s/%s/pull/%d" % (self.repo.owner, self.repo.name, self.number),
        }


class Repo:
    def __init__(self, store, repo_id, name, owner, private, description):
        self.store = store
        self.id = repo_id
        self.name = name
        self.owner = owner
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = "main"
        self.objects = {}       # oid -> (type, data)
        self.refs = {}          # full refname -> oid
        self.labels = {}        # name -> color
        self.issues = {}        # number -> Issue
        self.pulls = {}         # number -> PullRequest
        self.issue_ids = {}     # number -> stable int id (shared issue/PR counter)
        self.number_counter = 0
        self._seq = 0

    def next_seq(self):
        self._seq += 1
        return self._seq

    def next_number(self):
        self.number_counter += 1
        return self.number_counter

    def ref(self, refname):
        return self.refs.get(refname)

    def set_ref(self, refname, oid):
        if oid == gitobj.ZERO_OID:
            self.refs.pop(refname, None)
        else:
            self.refs[refname] = oid

    def branches(self):
        return sorted(n[11:] for n in self.refs if n.startswith("refs/heads/"))

    def store_object(self, type_name, data):
        oid = gitobj.hash_object(type_name, data)
        self.objects[oid] = (type_name, data)
        return oid

    def create_commit(self, branch, message, file_map, parent=None):
        """Build a commit on `branch` whose tree is exactly file_map.

        file_map: {path: bytes}. Returns the new commit oid.
        """
        blobs = {}
        flat = {}
        for path, content in sorted(file_map.items()):
            oid = self.store_object("blob", content)
            blobs[oid] = ("blob", content)
            flat[path] = ("100644", oid)
        tree_oid, new_trees = gitobj.build_tree_from_files(flat)
        self.objects.update(new_trees)
        parents = [parent] if parent else []
        data = gitobj.build_commit(tree_oid, parents, VIEWER_IDENTITY, message)
        commit_oid = self.store_object("commit", data)
        self.set_ref("refs/heads/" + branch, commit_oid)
        return commit_oid

    def commit_on_branch(self, branch, message, changes):
        """Apply `changes` {path: bytes|None} to the branch tip tree.

        Returns (commit_oid, new_tree_files). changes[path]=None deletes.
        """
        tip = self.ref("refs/heads/" + branch)
        if tip:
            tip_commit = gitobj.parse_commit(self.objects[tip][1])
            files = gitobj.flatten_tree(self.objects, tip_commit["tree"])
            parents = [tip]
        else:
            files = {}
            parents = []
        for path, content in changes.items():
            if content is None:
                files.pop(path, None)
            else:
                oid = self.store_object("blob", content)
                files[path] = ("100644", oid)
        tree_oid, new_trees = gitobj.build_tree_from_files(files)
        self.objects.update(new_trees)
        data = gitobj.build_commit(tree_oid, parents, VIEWER_IDENTITY, message)
        commit_oid = self.store_object("commit", data)
        self.set_ref("refs/heads/" + branch, commit_oid)
        return commit_oid, files

    def branch_tip_files(self, branch):
        tip = self.ref("refs/heads/" + branch)
        if not tip:
            return {}
        commit = gitobj.parse_commit(self.objects[tip][1])
        return gitobj.flatten_tree(self.objects, commit["tree"])

    def commits_from(self, tip):
        """List of commit oids newest-first from tip (parents only)."""
        out = []
        seen = set()
        stack = [tip]
        while stack:
            oid = stack.pop(0)
            if oid in seen or oid == gitobj.ZERO_OID or oid not in self.objects:
                continue
            typ, data = self.objects[oid]
            if typ != "commit":
                continue
            seen.add(oid)
            out.append(oid)
            stack.extend(gitobj.parse_commit(data)["parents"])
        return out

    def merge_base(self, a, b):
        """First common ancestor of two commits (newest common)."""
        if not a or not b:
            return None
        anc_b = gitobj.commit_ancestors(self.objects, b)
        for oid in self.commits_from(a):
            if oid in anc_b:
                return oid
        return None

    def diff_files(self, base_tree_files, head_tree_files):
        """Compare two flat file maps -> list of file-change dicts."""
        out = []
        for path in sorted(set(base_tree_files) | set(head_tree_files)):
            base = base_tree_files.get(path)
            head = head_tree_files.get(path)
            if base is None:
                content = self.objects[head[1]][1]
                lines = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
                out.append({"filename": path, "status": "added",
                            "additions": max(lines, 1), "deletions": 0,
                            "changes": max(lines, 1)})
            elif head is None:
                content = self.objects[base[1]][1]
                lines = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
                out.append({"filename": path, "status": "removed",
                            "additions": 0, "deletions": lines, "changes": lines})
            elif base[1] != head[1]:
                old = self.objects[base[1]][1].decode("utf-8", "replace").splitlines()
                new = self.objects[head[1]][1].decode("utf-8", "replace").splitlines()
                sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
                additions = deletions = 0
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag in ("insert", "replace"):
                        additions += j2 - j1
                    if tag in ("delete", "replace"):
                        deletions += i2 - i1
                out.append({"filename": path, "status": "modified",
                            "additions": additions, "deletions": deletions,
                            "changes": additions + deletions})
        return out

    def merge_conflict(self, pr):
        """True when head and base both changed the same file differently."""
        head_sha = pr.head_sha()
        base_sha = pr.base_sha()
        if not head_sha or not base_sha:
            return False
        base = self.merge_base(head_sha, base_sha)
        if base is None:
            return False
        base_files = gitobj.flatten_tree(
            self.objects, gitobj.parse_commit(self.objects[base][1])["tree"])
        head_files = gitobj.flatten_tree(
            self.objects, gitobj.parse_commit(self.objects[head_sha][1])["tree"])
        base_tip_files = gitobj.flatten_tree(
            self.objects, gitobj.parse_commit(self.objects[base_sha][1])["tree"])
        for path in set(head_files) | set(base_tip_files):
            h = head_files.get(path)
            b = base_tip_files.get(path)
            o = base_files.get(path)
            if h != o and b != o and h != b:
                return True
        return False

    def squash_merge(self, pr):
        """Squash-merge an open PR onto its base branch.

        Returns (merge_sha, None) on success or (None, error_message).
        """
        if pr.state != "open":
            return None, "Pull request is not open"
        if self.merge_conflict(pr):
            return None, "Merge conflict"
        head_sha = pr.head_sha()
        base_sha = pr.base_sha()
        merge_base = self.merge_base(head_sha, base_sha)
        head_files = self.branch_tip_files(pr.head)
        if merge_base:
            mb_files = gitobj.flatten_tree(
                self.objects, gitobj.parse_commit(self.objects[merge_base][1])["tree"])
        else:
            mb_files = {}
        changes = {}
        for path in set(head_files) | set(mb_files):
            if head_files.get(path) != mb_files.get(path):
                if path in head_files:
                    changes[path] = self.objects[head_files[path][1]][1]
                else:
                    changes[path] = None
        message = "Merge pull request #%d from %s" % (pr.number, pr.head)
        merge_sha, _ = self.commit_on_branch(pr.base, message, changes)
        pr.state = "closed"
        pr.merged = True
        pr.merged_at = FIXED_DATE
        pr.merge_commit_sha = merge_sha
        return merge_sha, None

    def shape(self):
        return {
            "id": self.id,
            "name": self.name,
            "full_name": "%s/%s" % (self.owner, self.name),
            "private": self.private,
            "description": self.description,
            "default_branch": self.default_branch,
            "owner": {"login": self.owner, "id": VIEWER_ID, "type": "User"},
            "html_url": "https://example.invalid/%s/%s" % (self.owner, self.name),
            "url": "/api/v3/repos/%s/%s" % (self.owner, self.name),
            "created_at": FIXED_DATE,
            "updated_at": FIXED_DATE,
            "has_issues": True,
        }


class Forge:
    def __init__(self):
        self.repos = {}          # name -> Repo (single owner space)
        self.repo_counter = 0
        self.id_counter = 0      # stable int ids for issues/comments
        self.request_counter = 0
        self.known_owner = VIEWER_LOGIN
        self.owner_observed = False

    def next_id(self):
        self.id_counter += 1
        return self.id_counter

    def next_request_id(self):
        self.request_counter += 1
        return "req-%d" % self.request_counter

    def resolve_owner(self, owner):
        """Learn/confirm the token owner's login from a repo path.

        The hardcoded viewer login wins once observed; before any repo-path
        request has been seen, adopt the observed owner (protects against a
        scenario-seed change). Any other owner afterwards is foreign.
        Returns True when `owner` is the viewer, False when foreign.
        """
        if owner == self.known_owner:
            self.owner_observed = True
            return True
        if not self.owner_observed:
            self.known_owner = owner
            self.owner_observed = True
            return True
        return False

    def get_repo(self, owner, name):
        if not self.resolve_owner(owner):
            return None
        return self.repos.get(name)

    def create_repo(self, name, private=False, description=""):
        if name in self.repos:
            return None
        self.repo_counter += 1
        repo = Repo(self, self.repo_counter, name, self.known_owner, private, description)
        self.repos[name] = repo
        return repo

    def repo_by_id(self, repo_id):
        for repo in self.repos.values():
            if repo.id == repo_id:
                return repo
        return None
