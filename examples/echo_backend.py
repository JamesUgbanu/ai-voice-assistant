"""Minimal conversational backend for the voice service's generic responder.

This is a reference adapter, not a production server. It shows the entire
contract the voice service needs:

  POST /respond  {"question", "history", "sessionId"} -> SSE token frames
  GET  /healthz  -> {"status": "ok"}

Run it with the voice service in generic-backend mode:

  python examples/echo_backend.py --port 9000

Then set in the voice service's environment:

  VOICE_RESPONDER=http
  VOICE_RESPONDER_URL=http://127.0.0.1:9000/respond

No third-party packages, no API keys, no personal data.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def build_reply(question: str, history: list[dict]) -> str:
    """A deterministic, context-aware reply used by the example server."""
    q = (question or "").strip()
    reply = f'You said: "{q}".'
    if history:
        previous = str(history[-1].get("question", "")).strip()
        if previous:
            reply += f' Earlier you asked: "{previous}".'
    reply += " This answer came from the example echo backend."
    return reply


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 keeps the SSE response simple: the body ends when the socket
    # closes, which httpx reads as end-of-stream.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        # Do not log questions or transcripts.
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path == "/healthz":
            self._json({"status": "ok"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802 (http.server API)
        if self.path != "/respond":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("content-length", "0") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._json({"error": "invalid JSON"}, 400)
            return
        if not isinstance(body, dict) or not isinstance(body.get("question"), str):
            self._json({"error": "question is required"}, 400)
            return
        history = body.get("history") if isinstance(body.get("history"), list) else []
        text = build_reply(body["question"], history)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # Stream word-by-word so TTS can start before the reply is complete.
        for word in text.split(" "):
            frame = {"text": word + " "}
            self.wfile.write(f"event: token\ndata: {json.dumps(frame)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"event: done\ndata: {}\n\n")
        self.wfile.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"echo backend listening on http://{args.host}:{args.port}/respond")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
