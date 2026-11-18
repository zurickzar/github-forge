#!/usr/bin/env python3
"""Entry point: lifecycle (mirrors starter/main.ts) + forge server.

- MAKEFUN_ENV_URL unset -> exit 1.
- Bind an ephemeral port (listen 0, never 8080).
- POST {"port": N} to $MAKEFUN_ENV_URL/register
  (200 ok; 409 = already registered, keep waiting; 400/405 = contract
  error, exit; anything else: retry with exponential backoff).
- Poll GET $MAKEFUN_ENV_URL/done until {"done": true}.
- Close the server, exit 0. Never exit before done.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request


def _post_json(url, payload, timeout=5):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("MAKEFUN_ENV_URL is not set", file=sys.stderr)
        return 1

    from forge.server import run
    server, _ctx = run("0.0.0.0", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # register (exponential backoff; 409 means already registered)
    delay = 0.25
    while True:
        try:
            status, _ = _post_json(env_url + "/register", {"port": port})
            if status in (200, 409):
                break
            if status in (400, 405):
                print("register rejected with HTTP %d" % status, file=sys.stderr)
                server.shutdown()
                return 1
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 405):
                print("register rejected with HTTP %d" % exc.code, file=sys.stderr)
                server.shutdown()
                return 1
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 5.0)

    # wait for done
    delay = 0.5
    while True:
        try:
            _status, data = _get_json(env_url + "/done")
            if isinstance(data, dict) and data.get("done") is True:
                break
            delay = 0.5
        except Exception:
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        time.sleep(1.0)

    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
