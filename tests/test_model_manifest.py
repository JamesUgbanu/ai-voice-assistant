"""Deterministic tests for fail-closed model-manifest verification."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_manifest import (
    ModelIntegrityError,
    default_manifest_path,
    load_manifest,
    parse_hf_uri,
    verify_runtime_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def _installed_version() -> str:
    try:
        from importlib.metadata import version

        return version("pocket-tts")
    except Exception:  # noqa: BLE001 - stdlib-only test environments
        return "4.0.0"


def _asset(repo: str, path: str, revision: str, data: bytes) -> dict:
    return {
        "repo": repo,
        "revision": revision,
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _uri(asset: dict) -> str:
    return f"hf://{asset['repo']}/{asset['path']}@{asset['revision']}"


class ManifestVerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.model_data = b"pinned gated weights"
        self.public_data = b"pinned public weights"
        self.tok_data = b"pinned tokenizer"
        self.gated = _asset("kyutai/pocket-tts", "languages/english/model.safetensors",
                            "a" * 40, self.model_data)
        self.public = _asset("kyutai/pocket-tts-without-voice-cloning",
                             "languages/english/model.safetensors", "b" * 40,
                             self.public_data)
        self.tokenizer = _asset("kyutai/pocket-tts-without-voice-cloning",
                                "languages/english/tokenizer.json", "c" * 40,
                                self.tok_data)
        (self.root / "gated.safetensors").write_bytes(self.model_data)
        (self.root / "public.safetensors").write_bytes(self.public_data)
        (self.root / "tokenizer.json").write_bytes(self.tok_data)
        self.paths = {
            _uri(self.gated): self.root / "gated.safetensors",
            _uri(self.public): self.root / "public.safetensors",
            _uri(self.tokenizer): self.root / "tokenizer.json",
        }
        self.manifest = {
            "pocket_tts_version": _installed_version(),
            "config": "pocket_tts/config/english.yaml",
            "gated_model": self.gated,
            "non_gated_model": self.public,
            "tokenizer": self.tokenizer,
        }
        self.manifest_path = self.root / "model-manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _verify(self, *, cloning=True, model_source=None, tokenizer_source=None,
                resolver=None):
        return verify_runtime_manifest(
            manifest_path=self.manifest_path,
            cloning_available=cloning,
            model_source=_uri(self.gated) if model_source is None else model_source,
            tokenizer_source=(
                _uri(self.tokenizer) if tokenizer_source is None else tokenizer_source
            ),
            resolver=resolver or (lambda source: self.paths[source]),
        )

    def test_valid_gated_assets_pass_and_report_no_paths(self):
        result = self._verify()
        self.assertEqual(result["selected"], "gated_model")
        self.assertEqual(result["sha256"], self.gated["sha256"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_public_fallback_is_selected_when_cloning_is_unavailable(self):
        result = self._verify(cloning=False, model_source=_uri(self.public))
        self.assertEqual(result["selected"], "non_gated_model")
        self.assertEqual(result["sha256"], self.public["sha256"])

    def test_revision_mismatch_fails_before_cache_resolution(self):
        resolver_calls = []

        def resolver(source):
            resolver_calls.append(source)
            return self.paths[source]

        wrong = _uri({**self.gated, "revision": "d" * 40})
        with self.assertRaises(ModelIntegrityError):
            self._verify(model_source=wrong, resolver=resolver)
        self.assertEqual(resolver_calls, [])

    def test_wrong_repository_or_path_fails(self):
        wrong_repo = _uri({**self.gated, "repo": "other/model"})
        wrong_path = _uri({**self.gated, "path": "languages/other/model.safetensors"})
        for source in (wrong_repo, wrong_path):
            with self.assertRaises(ModelIntegrityError):
                self._verify(model_source=source)

    def test_size_mismatch_fails(self):
        self.manifest["gated_model"]["size_bytes"] += 1
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaises(ModelIntegrityError) as ctx:
            self._verify()
        self.assertIn("size", str(ctx.exception))

    def test_hash_mismatch_fails(self):
        (self.root / "gated.safetensors").write_bytes(b"X" * len(self.model_data))
        with self.assertRaises(ModelIntegrityError) as ctx:
            self._verify()
        self.assertIn("SHA-256", str(ctx.exception))

    def test_missing_asset_fails_closed(self):
        with self.assertRaises(ModelIntegrityError) as ctx:
            self._verify(resolver=lambda _source: self.root / "absent.bin")
        self.assertIn("missing", str(ctx.exception).lower())

    def test_directory_is_not_an_asset(self):
        with self.assertRaises(ModelIntegrityError):
            self._verify(resolver=lambda _source: self.root)

    def test_tokenizer_mismatch_fails(self):
        (self.root / "tokenizer.json").write_bytes(b"wrong tokenizer")
        with self.assertRaises(ModelIntegrityError):
            self._verify()

    def test_missing_tokenizer_source_fails(self):
        with self.assertRaises(ModelIntegrityError):
            self._verify(tokenizer_source="")

    def test_missing_model_source_fails(self):
        with self.assertRaises(ModelIntegrityError):
            self._verify(model_source="")

    def test_invalid_json_manifest_fails(self):
        self.manifest_path.write_text("{", encoding="utf-8")
        with self.assertRaises(ModelIntegrityError):
            load_manifest(self.manifest_path)

    def test_invalid_sha_manifest_fails(self):
        self.manifest["tokenizer"]["sha256"] = "not-a-hash"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        with self.assertRaises(ModelIntegrityError):
            load_manifest(self.manifest_path)

    def test_missing_manifest_fails(self):
        with self.assertRaises(ModelIntegrityError):
            load_manifest(self.root / "absent.json")

    def test_hf_uri_requires_a_pinned_revision(self):
        with self.assertRaises(ModelIntegrityError):
            parse_hf_uri("hf://kyutai/pocket-tts/model.safetensors")
        with self.assertRaises(ModelIntegrityError):
            parse_hf_uri("https://example.invalid/model.safetensors")

    def test_default_manifest_path_can_be_overridden(self):
        with patch.dict(os.environ, {"VOICE_MODEL_MANIFEST": "/tmp/voice-manifest.json"}):
            self.assertEqual(default_manifest_path(), Path("/tmp/voice-manifest.json"))


class RepositoryManifestTest(unittest.TestCase):
    def test_shipped_manifest_is_structurally_valid(self):
        manifest = load_manifest(ROOT / "deploy" / "model-manifest.json")
        self.assertEqual(manifest["pocket_tts_version"], "4.0.0")
        self.assertEqual(manifest["gated_model"]["sha256"],
                         "7767ce79b1b8db523d867e70855a00627aacfe44aaa5c1fd7ebe135fba71fff9")
        self.assertEqual(manifest["non_gated_model"]["sha256"],
                         "e106c451108e066119f070eb7cf6c9ff4a2ad93f3bf04c9914cc94c66b7a4eeb")
        self.assertEqual(manifest["tokenizer"]["sha256"],
                         "f498428e1eafee50492f7be13dc9bfafcfc12e508cd0eb1b01c92ecd5d8c6687")


if __name__ == "__main__":
    unittest.main()
