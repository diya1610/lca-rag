"""Loopback-only JSON API and web interface for the LCA RAG index (standard library only).

    python server.py [--port 8765] [--dense] [--open]

Web UI: GET /  (static files in web/)
Endpoints: GET /health, GET /meta, GET /hierarchy[?parent=...], GET /record?id=...,
           POST /query, POST /calculate, POST /ebom
"""

from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ai_fallback
import ebom
import rag

HOST = "127.0.0.1"
MAX_BODY_BYTES = 64 * 1024
MAX_EBOM_BYTES = 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/sample_ebom.csv": ("sample_ebom.csv", "text/csv; charset=utf-8"),
}


def make_handler(engine: rag.Engine, fallback=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LCARAG/1.0"

        def log_message(self, fmt, *args):  # keep test output quiet
            pass

        def _headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; script-src 'self'; "
                             "frame-ancestors 'none'")
            self.end_headers()

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self._headers(status, "application/json; charset=utf-8", len(data))
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            """Reject requests addressed to other host names (DNS-rebinding guard)."""
            port = self.server.server_address[1]
            if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
                return True
            self._send(403, {"error": "forbidden_host"})
            return False

        def _body(self, max_bytes: int = MAX_BODY_BYTES) -> dict | None:
            # Requiring JSON forces a CORS preflight for cross-site requests, which this
            # server never approves, so other web pages cannot trigger queries or AI calls.
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "unsupported_media_type",
                                 "message": "Content-Type must be application/json."})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > max_bytes:
                self._send(400, {"error": "bad_request",
                                 "message": f"JSON body required (max {max_bytes // 1024} KiB)."})
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, {"error": "bad_request", "message": "Body is not valid JSON."})
                return None
            if not isinstance(body, dict):
                self._send(400, {"error": "bad_request", "message": "Body must be a JSON object."})
                return None
            return body

        def _meta(self) -> dict:
            return {
                "sources": engine.health()["sources"],
                "kinds": engine.distinct("kind"),
                "stages": engine.distinct("stage"),
                "units": engine.distinct("unit"),
                "indicators": {s: engine.indicators(s) for s in rag.SOURCES},
                "providers": ai_fallback.provider_status(),
                "dense": engine.dense is not None,
            }

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                data = (WEB_DIR / name).read_bytes()
                self._headers(200, ctype, len(data))
                self.wfile.write(data)
            elif url.path == "/health":
                self._send(200, engine.health())
            elif url.path == "/meta":
                self._send(200, self._meta())
            elif url.path == "/hierarchy":
                self._send(200, {"parent": qs.get("parent"), "nodes": engine.hierarchy(qs.get("parent"))})
            elif url.path == "/record":
                rec = engine.record(qs.get("id", ""))
                if rec is None:
                    self._send(404, {"error": "not_found", "id": qs.get("id")})
                else:
                    self._send(200, rec)
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            if not self._host_ok():
                return
            path = urlparse(self.path).path
            if path not in ("/query", "/calculate", "/ebom"):
                self._send(404, {"error": "not_found"})
                return
            body = self._body(MAX_EBOM_BYTES if path == "/ebom" else MAX_BODY_BYTES)
            if body is None:
                return
            if path == "/ebom":
                if not isinstance(body.get("csv"), str):
                    self._send(400, {"error": "bad_request", "message": "'csv' text is required."})
                    return
                try:
                    out = ebom.analyse(engine, body["csv"], body.get("indicator") or "Climate change",
                                       body.get("source") or None, body.get("default_unit") or None,
                                       bool(body.get("use_ai", False)), body.get("provider"), fallback)
                except ebom.EbomError as exc:
                    self._send(400, {"error": "invalid_ebom", "message": str(exc)})
                    return
                self._send(200, out)
            elif path == "/query":
                text = body.get("query")
                if not isinstance(text, str) or not text.strip():
                    self._send(400, {"error": "bad_request", "message": "'query' is required."})
                    return
                try:
                    out = engine.query(
                        text, body.get("filters") or {}, body.get("indicator"),
                        top_k=max(1, min(int(body.get("top_k", 5)), 50)),
                        use_ai=bool(body.get("use_ai", False)), provider=body.get("provider"),
                        use_dense=bool(body.get("dense", False)), fallback=fallback)
                except (ValueError, TypeError) as exc:
                    self._send(400, {"error": "bad_request", "message": str(exc)})
                    return
                self._send(200, out)
            else:
                try:
                    out = engine.calculate(body.get("record_id"), body.get("indicator"),
                                           body.get("quantity"), body.get("unit"))
                except rag.CalculationError as exc:
                    status = 404 if exc.code == "unknown_record" else 400
                    self._send(status, {"error": exc.code, "message": str(exc)})
                    return
                self._send(200, out)

    return Handler


def make_server(engine: rag.Engine, port: int = 8765, fallback=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((HOST, port), make_handler(engine, fallback))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dense", action="store_true", help="load optional dense index")
    parser.add_argument("--open", action="store_true", help="open the web UI in a browser")
    args = parser.parse_args()
    if not rag.INDEX_PATH.exists():
        rag.build()
    engine = rag.Engine()
    if args.dense:
        import dense
        engine.dense = dense.DenseIndex.load()
    server = make_server(engine, args.port)
    url = f"http://{HOST}:{args.port}/"
    print(f"LCA RAG on {url} (loopback only). Press Ctrl+C to stop.")
    for name, ok in ai_fallback.provider_status().items():
        print(f"  AI provider {name:7} {'ready' if ok else 'not configured'}")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
