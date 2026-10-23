"""Fail-closed verification of the pinned Pocket TTS model assets.

The manifest records the exact upstream repository revisions and SHA-256
digests that ``pocket_tts`` is expected to use. It is verification only: a
missing or mismatched asset raises before the model is used, and this module
never substitutes another checkpoint. Token values are never read or logged.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path


class ModelIntegrityError(RuntimeError):
    """A pinned model/tokenizer asset is missing, unreadable, or mismatched."""


_ASSETS = ("gated_model", "non_gated_model", "tokenizer")
_HEX = set("0123456789abcdef")


def default_manifest_path() -> Path:
    override = (os.environ.get("VOICE_MODEL_MANIFEST", "") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "deploy" / "model-manifest.json"


def _require_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelIntegrityError(f"model manifest has no valid {label}")
    return value.strip()


def _validate_asset(asset, label: str) -> None:
    if not isinstance(asset, dict):
        raise ModelIntegrityError(f"model manifest has no valid {label} entry")
    for key in ("repo", "revision", "path", "sha256"):
        _require_text(asset.get(key), f"{label}.{key}")
    size = asset.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ModelIntegrityError(f"model manifest has no valid {label}.size_bytes")
    digest = asset["sha256"].strip().lower()
    if len(digest) != 64 or any(ch not in _HEX for ch in digest):
        raise ModelIntegrityError(f"model manifest has no valid {label}.sha256")
    if ".." in asset["path"].split("/"):
        raise ModelIntegrityError(f"model manifest has no valid {label}.path")


def load_manifest(path: str | Path) -> dict:
    """Read and structurally validate the manifest. Never returns unverified
    values for use in a model load decision."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise ModelIntegrityError(
            f"model manifest is missing or unreadable ({type(exc).__name__})"
        ) from exc
    except (ValueError, TypeError) as exc:
        raise ModelIntegrityError("model manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ModelIntegrityError("model manifest is not a JSON object")
    _require_text(manifest.get("pocket_tts_version"), "pocket_tts_version")
    _require_text(manifest.get("config"), "config")
    for key in _ASSETS:
        _validate_asset(manifest.get(key), key)
    return manifest


def parse_hf_uri(source: str) -> dict:
    """Parse ``hf://repo/path[@revision]`` into its three manifest fields."""
    source = _require_text(source, "model source")
    if not source.startswith("hf://"):
        raise ModelIntegrityError("model source is not an hf:// revision")
    rest = source[5:]
    if "@" not in rest:
        raise ModelIntegrityError("model source has no pinned revision")
    location, revision = rest.rsplit("@", 1)
    parts = location.split("/")
    if len(parts) < 3 or not parts[0] or not parts[1] or not revision:
        raise ModelIntegrityError("model source is not a valid hf:// revision")
    return {
        "repo": "/".join(parts[:2]),
        "path": "/".join(parts[2:]),
        "revision": revision,
    }


def verify_asset_source(source: str, expected: dict, label: str) -> None:
    """Reject a config whose checkpoint is not exactly the manifest revision.

    ``pocket_tts`` itself falls back to another checkpoint when the gated one
    is unavailable. That behavior is intentional for preset mode, but which
    checkpoint was selected must still be the one the manifest pins.
    """
    actual = parse_hf_uri(source)
    if any(actual[key] != expected[key] for key in ("repo", "path", "revision")):
        raise ModelIntegrityError(f"{label} source does not match the manifest revision")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ModelIntegrityError(
            f"model asset is unreadable ({type(exc).__name__})"
        ) from exc
    return digest.hexdigest()


def verify_file(path: str | Path, expected: dict, label: str) -> None:
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise ModelIntegrityError(
            f"{label} is missing or inaccessible ({type(exc).__name__})"
        ) from exc
    if not os.path.isfile(path):
        raise ModelIntegrityError(f"{label} is not a regular file")
    if stat.st_size != expected["size_bytes"]:
        raise ModelIntegrityError(f"{label} size does not match the manifest")
    if sha256_file(path) != expected["sha256"]:
        raise ModelIntegrityError(f"{label} SHA-256 does not match the manifest")


def resolve_cached_hf_asset(source: str) -> Path:
    """Resolve an exact pinned revision from the local HF cache only.

    No network access and no re-download: the model loader has already fetched
    the selected checkpoint. A cache miss fails closed instead of allowing a
    different revision to be used silently.
    """
    asset = parse_hf_uri(source)
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception as exc:
        raise ModelIntegrityError("huggingface_hub is unavailable") from exc
    try:
        cached = try_to_load_from_cache(
            repo_id=asset["repo"],
            filename=asset["path"],
            revision=asset["revision"],
        )
    except Exception as exc:
        raise ModelIntegrityError(
            f"model asset cache lookup failed ({type(exc).__name__})"
        ) from exc
    if not isinstance(cached, (str, os.PathLike)) or not str(cached):
        raise ModelIntegrityError("model asset is not present in the local cache")
    return Path(cached)


def verify_runtime_manifest(
    *,
    manifest_path: str | Path | None,
    cloning_available: bool,
    model_source: str | None,
    tokenizer_source: str | None,
    resolver: Callable[[str], Path] | None = None,
) -> dict:
    """Verify the selected checkpoint and tokenizer against the manifest.

    Returns a small, path-free summary for logs/tests. Raises
    :class:`ModelIntegrityError` on any mismatch or missing asset.
    """
    manifest = load_manifest(manifest_path or default_manifest_path())
    try:
        from importlib.metadata import PackageNotFoundError, version

        installed = version("pocket-tts")
    except PackageNotFoundError:
        installed = None
    if installed is not None and installed != manifest["pocket_tts_version"]:
        raise ModelIntegrityError(
            "pocket-tts version does not match the model manifest"
        )

    selected = "gated_model" if cloning_available else "non_gated_model"
    expected = manifest[selected]
    if not model_source:
        raise ModelIntegrityError(f"model manifest has no {selected} source")
    if not tokenizer_source:
        raise ModelIntegrityError("model manifest has no tokenizer source")

    verify_asset_source(model_source, expected, selected)
    verify_asset_source(tokenizer_source, manifest["tokenizer"], "tokenizer")
    resolve = resolver or resolve_cached_hf_asset
    verify_file(resolve(model_source), expected, selected)
    verify_file(resolve(tokenizer_source), manifest["tokenizer"], "tokenizer")
    return {
        "selected": selected,
        "sha256": expected["sha256"],
        "size_bytes": expected["size_bytes"],
    }
