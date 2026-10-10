"""The browser view: a read-only page that draws /v1/dashboard.

The sync server serves it at / (people sign in with a join code from `savetokens pair`).
`savetokens web` serves it on this machine: 127.0.0.1 only, behind a key made for that run,
showing every machine when this one is connected to a server, else this machine.
"""
from __future__ import annotations

import hmac
import json
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).with_name("web")
FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8")}
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';"
                               " img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def static(path):
    """(body, content type) for one of the page's files, or None."""
    f = FILES.get(urllib.parse.urlparse(path).path)
    return ((ROOT / f[0]).read_bytes(), f[1]) if f else None


def send_static(handler: BaseHTTPRequestHandler, path) -> bool:
    got = static(path)
    if not got:
        return False
    body, kind = got
    handler.send_response(200)
    handler.send_header("Content-Type", kind)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in HEADERS.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)
    return True


def local_handler(key: str, snapshot):
    """The page and this machine's dashboard (snapshot() gives it), for requests carrying the run's key."""
    class Handler(BaseHTTPRequestHandler):
        server_version = "savetokens"

        def log_message(self, fmt, *args):
            pass

        def _json(self, code, body):
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            # a page on another site can't read this: it would need the key, and the Host check stops
            # DNS rebinding
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def do_GET(self):
            if not self._local():
                return self._json(403, {"error": "local only"})
            if send_static(self, self.path):
                return
            if urllib.parse.urlparse(self.path).path != "/v1/dashboard":
                return self._json(404, {"error": "not found"})
            auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(auth, key):
                return self._json(401, {"error": "open the full link savetokens web printed (it ends in #key=...)"})
            try:
                self._json(200, snapshot())
            except Exception as e:
                self._json(500, {"error": str(e)[:200]})

        def do_POST(self):
            # join codes are for a server's address; this view opens with the link `savetokens web` printed
            self._json(404, {"error": "This is the view on your own machine: it doesn't take codes. Open the full"
                                      " link savetokens web printed (it ends in #key=...), or enter the code at"
                                      " your server's address."})
    return Handler


def serve_local(snapshot, port=8788, open_browser=True, log=print):
    key = secrets.token_urlsafe(18)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), local_handler(key, snapshot))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#key={key}"
    log(f"savetokens in your browser: {url}\n(this address works until you stop this with Ctrl-C)", flush=True)
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return url


def local_snapshot():
    """What `savetokens web` shows: fresh capture, then the server's view when connected, else this machine's."""
    from . import capture, cli, maintain, setup
    from .store import Store
    with Store() as s:
        capture.backfill(s)
        maintain.kick(s)
        snap = cli._snapshot(s)
        snap["setup"] = setup.steps(s)   # this machine's: what it detects, and its own key and connection
    snap.setdefault("now", time.time())
    return snap
