"""HTTP server: routing, auth, JSON helpers. One ThreadingHTTPServer.

Routes (auth = Bearer $MAKEFUN_SHOP_TOKEN, except /healthz):
  GET  /healthz
  GET  /api/v3/                      API root
  /api/v3/...                       REST v3
  POST /api/graphql
  POST /mcp
  /<owner>/<repo>.git/...           git smart HTTP
  anything else                     404 {"message": "Not Found"}
"""
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import git as git_handlers
from . import graphql as graphql_handlers
from . import mcp as mcp_handlers
from . import rest as rest_handlers
from .store import Forge


class Context:
    def __init__(self, forge, token):
        self.forge = forge
        self.token = token


class ForgeServer(ThreadingHTTPServer):
    # 50 concurrent clones arrive at once; the default listen backlog (5)
    # drops SYNs and the client waits out ~1 s retransmit timers.
    request_queue_size = 128
    daemon_threads = True


def make_server(ctx, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "github-forge/1.0"

        def setup(self):
            super().setup()
            # Disable Nagle: http.server writes headers and body in separate
            # sends; without TCP_NODELAY each keep-alive reply waits out the
            # client's ~40 ms delayed ACK and the benchmark stalls.
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        def log_message(self, fmt, *args):  # keep stdout clean
            pass

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _send(self, status, body, content_type="application/json"):
            if isinstance(body, (dict, list)):
                raw = json.dumps(body).encode("utf-8")
            elif isinstance(body, str):
                raw = body.encode("utf-8")
            else:
                raw = body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-GitHub-Request-Id", ctx.forge.next_request_id())
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _send_json(self, status, obj):
            self._send(status, obj)

        def _auth_ok(self):
            """True when the Authorization header carries the shop token."""
            if not ctx.token:
                auth = self.headers.get("Authorization") or ""
                return auth.startswith("Bearer ") and len(auth) > 7
            return (self.headers.get("Authorization") or "") == "Bearer " + ctx.token

        def _unauthorized(self):
            self._send_json(401, {"message": "Requires authentication"})

        def _handle(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            body = self._read_body()
            method = self.command

            if path == "/healthz":
                if method == "GET":
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(404, {"message": "Not Found"})
                return

            # ---- git smart HTTP -------------------------------------------------
            if path.endswith(".git/info/refs") or path.endswith(".git/git-upload-pack") \
                    or path.endswith(".git/git-receive-pack"):
                if not self._auth_ok():
                    self._unauthorized()
                    return
                git_handlers.handle(ctx, self, method, path, query, body)
                return

            # ---- /api/v3/ root --------------------------------------------------
            if path == "/api/v3/" or path == "/api/v3":
                if not self._auth_ok():
                    self._unauthorized()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Oauth-Scopes", "repo, read:org")
                self.send_header("X-GitHub-Request-Id", ctx.forge.next_request_id())
                host = self.headers.get("Host") or "localhost"
                root = {
                    "current_user_url": "http://%s/api/v3/user" % host,
                    "rate_limit_url": "http://%s/api/v3/rate_limit" % host,
                    "current_user_authorizations_html_url": "http://%s/settings/applications" % host,
                    "authorizations_url": "http://%s/api/v3/authorizations" % host,
                }
                raw = json.dumps(root).encode("utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            # ---- REST / GraphQL / MCP -------------------------------------------
            if path.startswith("/api/") or path == "/mcp":
                if not self._auth_ok():
                    self._unauthorized()
                    return
                if path == "/api/graphql" and method == "POST":
                    graphql_handlers.handle(ctx, self, body)
                    return
                if path == "/mcp" and method == "POST":
                    mcp_handlers.handle(ctx, self, body)
                    return
                if path.startswith("/api/v3/"):
                    rest_handlers.handle(ctx, self, method, path, query, body)
                    return
                self._send_json(404, {"message": "Not Found"})
                return

            self._send_json(404, {"message": "Not Found"})

        do_GET = _handle
        do_POST = _handle
        do_PUT = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    return ForgeServer((host, port), Handler)


def run(host="127.0.0.1", port=0):
    token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    ctx = Context(Forge(), token)
    server = make_server(ctx, host, port)
    return server, ctx
