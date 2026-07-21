from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit


def canonical_request_path(value: str) -> str:
    parsed = urlsplit(value)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return f"{parsed.path}?{query}" if query else parsed.path


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve an exported Mercu workstation visual fixture corpus.")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()

    payload = json.loads(args.corpus.read_text(encoding="utf-8"))
    responses = payload.get("responses")
    if not isinstance(responses, dict) or not responses:
        raise SystemExit("fixture corpus does not contain any responses")

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            entry = responses.get(canonical_request_path(self.path))
            if not isinstance(entry, dict):
                body = json.dumps({"ok": False, "message": "fixture response not found"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            else:
                body = str(entry.get("body", "")).encode("utf-8")
                self.send_response(int(entry.get("status", 200)))
                self.send_header("Content-Type", str(entry.get("contentType") or "application/json; charset=utf-8"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving {len(responses)} fixture responses for {payload.get('viewport', 'unknown')} on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
