"""Voice-reference audio validation and normalization.

Pure helpers (unit-testable without ffmpeg) plus thin ffmpeg/ffprobe wrappers
for real audio. Used by ``scripts/prepare-voice-reference.py`` and by the TTS
adapter's preflight. Never logs or returns the recording's audio content;
callers must not print full private paths in normal logs.

Pocket TTS constraint (installed 3.3.0): the first 30 seconds of the reference
are processed (``export-voice`` docs; ``get_state_for_audio_prompt`` truncates
to 30 s). The README recommends a ~5 s clip for efficiency. We normalize to
mono PCM WAV at the model's 24 kHz and cap at the documented 30 s.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

#: Installed Pocket TTS processes only the first 30 seconds of a reference.
POCKET_TTS_REF_MAX_SECONDS = 30.0
#: Mono, matching the model's output sample rate (it resamples regardless).
NORMALIZED_SAMPLE_RATE = 24000


class VoiceReferenceError(Exception):
    """Actionable reference-audio failure; ``code`` is stable for tests/UI."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class AudioInfo:
    exists: bool
    duration_s: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    codec: str = ""
    container: str = ""
    mean_db: float | None = None
    peak_db: float | None = None
    issues: list[str] = field(default_factory=list)


# -- pure policy ---------------------------------------------------------------


def assess_levels(mean_db: float | None, peak_db: float | None) -> list[str]:
    """Return human-readable problems with a recording's level.

    Conservative thresholds: only clearly-broken audio is flagged, so a quiet
    but usable phone memo is not rejected."""
    issues: list[str] = []
    if peak_db is not None and peak_db > -0.3:
        issues.append("clipped")
    if mean_db is not None and mean_db < -45.0:
        issues.append("excessively quiet")
    if peak_db is not None and peak_db < -25.0:
        issues.append("level far too low")
    return issues


def clamp_reference_seconds(duration_s: float, max_seconds: float = POCKET_TTS_REF_MAX_SECONDS) -> float:
    """The reference length that will actually be used (never exceeds the
    documented Pocket TTS 30 s processing limit)."""
    if duration_s <= 0:
        return 0.0
    return min(duration_s, max_seconds)


def validate_info(info: AudioInfo, *, min_seconds: float = 1.0) -> AudioInfo:
    """Raise VoiceReferenceError for unusable audio; return info (with
    ``issues`` populated) otherwise."""
    if not info.exists:
        raise VoiceReferenceError("missing", "reference file does not exist")
    if info.duration_s <= 0:
        raise VoiceReferenceError("empty", "reference has no decodable audio")
    if info.duration_s < min_seconds:
        raise VoiceReferenceError(
            "too_short", f"reference is {info.duration_s:.2f}s; need at least {min_seconds:.0f}s"
        )
    info.issues = assess_levels(info.mean_db, info.peak_db)
    return info


# -- ffmpeg/ffprobe wrappers ---------------------------------------------------


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise VoiceReferenceError("no_ffmpeg", f"{name} is required but not installed")
    return path


def probe(path: str) -> AudioInfo:
    """Inspect a recording with ffprobe + volumedetect. Raises on unreadable
    or undecodable input. Reads no audio content beyond level statistics."""
    import os

    if not path or not os.path.isfile(path):
        return AudioInfo(exists=False)
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-show_entries",
             "format=duration,format_name:stream=codec_name,sample_rate,channels",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise VoiceReferenceError("decode_failed", "ffprobe could not read the reference") from exc
    data = json.loads(out)
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    info = AudioInfo(
        exists=True,
        duration_s=float(fmt.get("duration") or 0.0),
        sample_rate=int(streams[0].get("sample_rate") or 0),
        channels=int(streams[0].get("channels") or 0),
        codec=str(streams[0].get("codec_name") or ""),
        container=str(fmt.get("format_name") or ""),
    )
    # Level stats (best-effort; missing values are not fatal).
    try:
        proc = subprocess.run(
            [_tool("ffmpeg"), "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
        for line in proc.stderr.splitlines():
            if "mean_volume:" in line:
                info.mean_db = float(line.split("mean_volume:")[1].split("dB")[0].strip())
            elif "max_volume:" in line:
                info.peak_db = float(line.split("max_volume:")[1].split("dB")[0].strip())
    except Exception:
        pass
    return info


def normalize(
    src: str,
    dst: str,
    *,
    sample_rate: int = NORMALIZED_SAMPLE_RATE,
    max_seconds: float = POCKET_TTS_REF_MAX_SECONDS,
) -> AudioInfo:
    """Write a mono PCM WAV at ``sample_rate``, loudness-normalized, capped at
    ``max_seconds`` (the Pocket TTS limit). Never touches ``src``. Returns the
    probe of the produced file."""
    import os

    _tool("ffmpeg")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        subprocess.run(
            [_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
             "-i", src, "-t", f"{max_seconds:.3f}",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
             "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", dst],
            capture_output=True, text=True, timeout=300, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise VoiceReferenceError("normalize_failed", "ffmpeg normalization failed") from exc
    return probe(dst)
