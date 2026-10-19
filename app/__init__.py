"""Self-hosted realtime voice service.

Audio transport, speech recognition, turn lifecycle, and speech synthesis.
A pluggable responder connects it to a conversational backend; the portfolio
RAG adapter is the reference deployment. The service owns microphone audio,
STT, TTS, turn state, sessions, and quotas — not the application's knowledge.
"""
