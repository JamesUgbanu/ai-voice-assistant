"""Minimal voice-service client (text or WAV turns).

Creates a session, connects to the documented WebSocket protocol, sends one or
more turns, prints transcript/answer events, optionally saves synthesized audio,
and ends the session cleanly.

Quick text demo (works with VOICE_USE_STUBS=1, no models):

    python examples/voice_client.py \
      --base http://127.0.0.1:8000 \
      --text "Hello" --text "What did I just ask?"

Audio turn (needs real STT models; 16 kHz mono PCM16 WAV):

    python examples/voice_client.py --base http://127.0.0.1:8000 --wav hello.wav

Requires httpx and websockets (both are installed with the service's
uvicorn[standard] dependency). No browser and no personal recordings.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import wave

import httpx
import websockets
from websockets.exceptions import WebSocketException


def read_wav_pcm16(path: str) -> bytes:
    """Read a 16 kHz mono 16-bit PCM WAV and return raw little-endian bytes."""
    with wave.open(path, "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit("WAV must be mono 16-bit PCM")
        if wav.getframerate() != 16000:
            raise SystemExit("WAV must be 16 kHz (the service's protocol rate)")
        return wav.readframes(wav.getnframes())


def frames(pcm: bytes, frame_ms: int = 100) -> list[bytes]:
    size = 16000 * frame_ms // 1000 * 2
    return [pcm[i:i + size] for i in range(0, len(pcm), size)]


async def receive_json(ws, timeout: float = 180.0) -> dict | None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


class Client:
    def __init__(self, ws, save_audio: str | None) -> None:
        self.ws = ws
        self.seq = 0
        self.save_audio = save_audio
        self._audio: list[bytes] = []

    async def send(self, kind: str, payload: dict) -> None:
        self.seq += 1
        await self.ws.send(json.dumps({
            "v": 1, "type": kind, "seq": self.seq, "turn_id": None, "payload": payload,
        }))

    async def wait_listening(self) -> None:
        for _ in range(50):
            message = await receive_json(self.ws)
            if message is None:
                continue
            if message.get("type") == "listening":
                return
            if message.get("type") == "error":
                raise SystemExit(f"server error: {message.get('payload')}")
        raise SystemExit("no listening event; check the session ticket and origin")

    async def run_turn(self, *, text: str | None = None, pcm: bytes | None = None) -> None:
        self._audio = []
        if text is not None:
            print(f"you (text): {text}")
            await self.send("text_message", {"text": text})
        else:
            assert pcm is not None
            print(f"you (audio): {len(pcm)} bytes")
            for frame in frames(pcm):
                await self.send("audio_frame", {
                    "audio_b64": base64.b64encode(frame).decode(),
                    "final": False,
                })
            await self.send("end_utterance", {})

        audio_frames = 0
        while True:
            message = await receive_json(self.ws)
            if message is None:
                raise SystemExit("timed out waiting for turn_end")
            kind = message.get("type")
            payload = message.get("payload") or {}
            if kind == "transcript_final":
                print(f"transcript: {payload.get('text', '')}")
            elif kind == "assistant_text":
                print(f"assistant: {payload.get('text', '')}")
            elif kind == "audio_frame":
                audio_frames += 1
                data = payload.get("audio_b64")
                if data:
                    self._audio.append(base64.b64decode(data))
            elif kind == "error":
                print(f"error: {payload.get('code')} {payload.get('message', '')}")
            elif kind == "turn_end":
                print(f"turn_end: {payload.get('reason')} ({audio_frames} audio frames)")
                if self.save_audio and self._audio:
                    with open(self.save_audio, "wb") as fh:
                        fh.write(b"".join(self._audio))
                    print(f"saved PCM16 audio to {self.save_audio}")
                return

    async def end(self) -> None:
        await self.send("end_session", {"reason": "user_ended"})


def resolve_ws_url(base: str, ws_url: str) -> str:
    if ws_url.startswith(("ws://", "wss://")):
        return ws_url
    scheme = "wss" if base.startswith("https") else "ws"
    return f"{scheme}://{base.split('://', 1)[1].rstrip('/')}/{ws_url.lstrip('/')}"


async def run(args) -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(f"{args.base.rstrip('/')}/v1/sessions", json={})
        if response.status_code != 200:
            raise SystemExit(f"session failed: {response.status_code} {response.text[:200]}")
        session = response.json()

    history = []
    if args.history_file:
        with open(args.history_file, encoding="utf-8") as fh:
            history = json.load(fh)
        if not isinstance(history, list):
            raise SystemExit("history file must contain a JSON list")

    ws_url = resolve_ws_url(args.base, session["ws_url"])
    async with websockets.connect(ws_url, max_size=None) as ws:
        client = Client(ws, args.save_audio)
        await client.send("hello", {
            "session_id": session["session_id"],
            "ticket": session["ticket"],
            "history": history,
        })
        await client.wait_listening()
        try:
            for text in args.text:
                await client.run_turn(text=text)
            for path in args.wav:
                await client.run_turn(pcm=read_wav_pcm16(path))
            if not args.text and not args.wav:
                await client.run_turn(text="Hello from the example client.")
        finally:
            await client.end()
            await asyncio.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal voice-service WebSocket client")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="service HTTP origin")
    parser.add_argument("--text", action="append", default=[], help="a text turn (repeatable)")
    parser.add_argument(
        "--wav", action="append", default=[], help="a 16 kHz mono WAV turn (repeatable)"
    )
    parser.add_argument("--history-file", help="optional JSON list of {question, answer} turns")
    parser.add_argument("--save-audio", help="write synthesized PCM16 audio (24 kHz) to this path")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (httpx.HTTPError, WebSocketException) as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
