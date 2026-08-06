# AI Visibility Audit - can AI assistants read, quote and operate your site?
# Copyright (C) 2026  AI Visibility Audit contributors
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; see the GNU AGPL for
# details. You should have received a copy of the licence with this program;
# if not, see <https://www.gnu.org/licenses/>.

"""The local UI for bof-audit. One file, stdlib only, bound to loopback.

    python -m bof.serve [--port 8610]

Deliberately not FastAPI. The claude-seo venv has no fastapi, uvicorn or
pydantic, and adding them is not an option: runtime.py compares a
requirements_sha256 and any change to that venv forces a full re-setup. The
alternative was vendoring roughly 40 MB into bof/vendor, including
pydantic-core, a compiled wheel. For seven localhost endpoints serving one
person, ThreadingHTTPServer is the whole requirement.

Security, in the order it matters:

* Bound to 127.0.0.1. There is no auth anywhere in this stack and the bind is
  the only control. Never widen it.
* POST requires Content-Type: application/json. A cross-site form can only send
  urlencoded, multipart or plain text, so it cannot reach these routes without
  a preflight the browser will not grant. This is the CSRF guard the spec
  ascribes to a pydantic body model, written out rather than inherited as a
  side effect of a framework.
* Every submitted URL goes through url_safety.validate_url_strict before it is
  stored, which is DNS-pinned and refuses private ranges. This service takes a
  URL and fetches it, so it is an SSRF surface by definition.
* The report route resolves the file and refuses anything that does not land
  inside the audits directory, so a crafted run id cannot walk the filesystem.

It frames inside a parent dashboard because http.server sends no X-Frame-Options.
If a Content-Security-Policy is ever added here it must include
frame-ancestors for the parent origin, or the tool silently stops rendering
inside it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from bof import store
from bof import suite  # noqa: F401  # must precede every suite import

from url_safety import URLSafetyError, validate_url_strict  # noqa: E402

PORT = 8610
HOST = "127.0.0.1"
UI = pathlib.Path(__file__).with_name("ui.html")
AUDITS = (pathlib.Path.cwd() / "audits").resolve()

MAX_BODY = 8192  # a url and a brand; anything larger is not a real request


class Handler(BaseHTTPRequestHandler):
    server_version = "bof-audit"

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - quieten the default logger
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        # The CSRF guard. A cross-site <form> cannot set this content type, and
        # anything that can already had to pass a preflight.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise ValueError("this endpoint accepts application/json only")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("missing or oversized body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    @staticmethod
    def _row(row) -> dict:
        d = {k: row[k] for k in row.keys()}
        d["percent"] = int(100 * (d["step_no"] or 0) / (d["step_total"] or 1))
        return d

    # --- routes -----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/health":
                return self._json(200, {"ok": True, "db": str(store.db_path()),
                                        "steps": store.STEP_TOTAL})
            if path == "/api/runs":
                con = store.connect()
                rows = [self._row(r) for r in store.latest(con, limit=25)]
                con.close()
                return self._json(200, {"runs": rows})
            if path.startswith("/api/run/"):
                run_id = path.split("/")[3]
                con = store.connect()
                row = store.get(con, run_id)
                con.close()
                if row is None:
                    return self._json(404, {"error": "no such run"})
                return self._json(200, self._row(row))
            if path.startswith("/api/report/"):
                return self._report(path.split("/")[3])
            return self._json(404, {"error": "no such route"})
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the server
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/run":
                return self._start()
            if path.startswith("/api/run/") and path.endswith("/cancel"):
                run_id = path.split("/")[3]
                con = store.connect()
                ok = store.cancel(con, run_id)
                con.close()
                return self._json(200 if ok else 409,
                                  {"cancelled": ok,
                                   "error": None if ok else "not in flight"})
            return self._json(404, {"error": "no such route"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _start(self):
        body = self._body()
        url = (body.get("url") or "").strip()
        brand = (body.get("brand") or "").strip() or None
        if not url:
            return self._json(400, {"error": "a url is required"})
        if "://" not in url:
            url = f"https://{url}"
        # This service accepts a URL and then fetches it. Validate before it is
        # ever stored, not at fetch time.
        try:
            url, _ip = validate_url_strict(url)
        except (URLSafetyError, ValueError) as exc:
            return self._json(400, {"error": f"refused: {exc}"})

        con = store.connect()
        try:
            run_id = store.enqueue(con, url, brand)
        except store.Busy as exc:
            con.close()
            return self._json(409, {"error": "a run is already in flight",
                                    "run_id": exc.run_id})
        con.close()
        pid = store.spawn(run_id, str(AUDITS.parent / "audits"))
        return self._json(202, {"run_id": run_id, "pid": pid})

    def _report(self, run_id: str):
        con = store.connect()
        row = store.get(con, run_id)
        con.close()
        if row is None or not row["out_dir"]:
            return self._json(404, {"error": "no report for that run"})
        out = pathlib.Path(row["out_dir"])
        if not out.is_absolute():
            out = (pathlib.Path.cwd() / out)
        try:
            out = out.resolve()
            out.relative_to(AUDITS)
        except (ValueError, OSError):
            # A run id that resolves outside the audits tree is not a report.
            return self._json(403, {"error": "report is outside the audits directory"})
        pdfs = sorted(out.glob("*.pdf"))
        if not pdfs:
            return self._json(404, {"error": "no pdf in that run's directory"})
        return self._send(200, pdfs[0].read_bytes(), "application/pdf")


def serve(port: int = PORT) -> int:
    socketserver.TCPServer.allow_reuse_address = True
    AUDITS.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer((HOST, port), Handler) as httpd:
        print(f"bof-audit UI on http://{HOST}:{port}")
        print(f"  runs   {store.db_path()}")
        print(f"  audits {AUDITS}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def self_check() -> int:
    """Routes and guards, over a real socket on an ephemeral port."""
    import threading
    import urllib.error
    import urllib.request

    failures = []
    with ThreadingHTTPServer((HOST, 0), Handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://{HOST}:{port}"

        def call(path, data=None, ctype="application/json"):
            req = urllib.request.Request(base + path)
            if data is not None:
                req.data = json.dumps(data).encode()
                req.add_header("Content-Type", ctype)
                req.get_method = lambda: "POST"
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        code, body = call("/api/health")
        if code != 200 or not json.loads(body)["ok"]:
            failures.append(f"/api/health returned {code}")
        else:
            print("  ok    /api/health answers")

        code, _ = call("/")
        if code != 200:
            failures.append(f"the UI did not serve: {code}")
        else:
            print("  ok    / serves the UI")

        code, body = call("/api/run", {"url": "https://example.com"},
                          ctype="application/x-www-form-urlencoded")
        if code != 400:
            failures.append(f"a form-encoded POST was accepted ({code}): that is "
                            f"the CSRF guard gone")
        else:
            print("  ok    a form-encoded POST is refused, which is the CSRF guard")

        for bad, why in [("http://127.0.0.1:8600/", "loopback"),
                         ("http://192.168.1.1/", "private range"),
                         ("file:///c:/windows/win.ini", "non-http scheme")]:
            code, body = call("/api/run", {"url": bad})
            if code != 400:
                failures.append(f"{why} was not refused: {code} {body[:120]}")
        if not any("refused" in f for f in failures):
            print("  ok    loopback, private ranges and file:// are refused (SSRF)")

        code, body = call("/api/run/nosuchrun")
        if code != 404:
            failures.append(f"an unknown run id returned {code}, expected 404")
        else:
            print("  ok    an unknown run id is a clean 404")

        code, body = call("/api/report/nosuchrun")
        if code != 404:
            failures.append(f"a report for an unknown run returned {code}")
        else:
            print("  ok    a report for an unknown run is a clean 404")

        code, _ = call("/api/runs")
        if code != 200:
            failures.append(f"/api/runs returned {code}")
        else:
            print("  ok    /api/runs lists history")

        httpd.shutdown()

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nAll service self-checks passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)
    if args.self_check:
        return self_check()
    return serve(args.port)


if __name__ == "__main__":
    sys.exit(main())
