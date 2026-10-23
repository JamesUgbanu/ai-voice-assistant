"""Structural guards for the scheduled-deployment assets (no cloud calls).

These do not run Docker or Hetzner; they pin the safety-critical properties of
the release/bootstrap files so a future edit cannot silently break them.
"""
import json
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class ReleaseScriptTest(unittest.TestCase):
    def test_refuses_mutable_tags_and_uses_amd64(self):
        src = read("scripts/release.sh")
        self.assertIn("latest", src)  # explicitly refused
        self.assertIn("stable", src)  # explicitly refused as VERSION
        self.assertIn("linux/amd64", src)
        self.assertRegex(src, r"buildx build")
        self.assertRegex(src, r"--push")

    def test_runs_checks_before_pushing(self):
        src = read("scripts/release.sh")
        # tests must appear before the build/push line
        self.assertLess(src.index("unittest discover"), src.index("buildx build"))
        self.assertIn("py_compile", src)

    def test_emits_immutable_digest(self):
        src = read("scripts/release.sh")
        self.assertIn("Manifest.Digest", src)
        self.assertIn("VOICE_IMAGE_DIGEST", src)


class CloudInitTest(unittest.TestCase):
    def setUp(self):
        self.src = read("deploy/cloud-init.yaml")

    def test_pulls_a_pinned_placeholder_image_not_latest(self):
        self.assertIn("__IMAGE__", self.src)
        self.assertNotRegex(self.src, r"__IMAGE__:latest")

    def test_downloads_all_three_model_families(self):
        for needle in ("WhisperModel(", "pocket_tts", "silero_vad"):
            self.assertIn(needle, self.src)

    def test_models_use_persistent_volume(self):
        self.assertIn("voice-models:/srv/voice/models", self.src)
        self.assertIn("HF_HOME=/srv/voice/models", self.src)
        self.assertIn("TORCH_HOME=/srv/voice/models/torch", self.src)

    def test_readiness_is_real_and_fails_closed(self):
        # Requires db + warm, and clone-active when in clone mode.
        self.assertIn("\\\"db\\\":true", self.src)
        self.assertIn("\\\"warm\\\":true", self.src)
        self.assertIn("cloning_active", self.src)
        self.assertIn("readiness timeout", self.src)
        self.assertIn("exit 1", self.src)

    def test_does_not_bake_secrets_and_drops_registry_credential(self):
        self.assertIn("docker, logout, ghcr.io", self.src)
        self.assertIn("permissions: '0600'", self.src)
        # Secrets are placeholders, never literals.
        self.assertNotRegex(self.src, r"hf_[A-Za-z0-9]{10}")
        self.assertNotRegex(self.src, r"sk-[A-Za-z0-9]{20}")

    def test_preserves_clone_mode_and_rag_client(self):
        self.assertIn("VOICE_TTS_MODE=__TTS_MODE__", self.src)
        self.assertIn("VOICE_RAG_API_URL=__RAG_URL__", self.src)
        self.assertIn("VOICE_RAG_SERVICE_TOKEN=__RAG_TOKEN__", self.src)


class ComposeTest(unittest.TestCase):
    def test_pinned_image_no_latest_and_no_secrets(self):
        src = read("deploy/docker-compose.deploy.yml")
        self.assertIn("${VOICE_IMAGE:?", src)
        self.assertNotIn(":latest", src)
        self.assertIn("env_file: /etc/voice/runtime.env", src)
        self.assertNotRegex(src, r"hf_[A-Za-z0-9]{10}")
        self.assertNotRegex(src, r"sk-[A-Za-z0-9]{20}")


class ReferenceBakeTest(unittest.TestCase):
    """Optional authorized reference baked into a PRIVATE image."""

    def test_dockerfile_bakes_reference_at_runtime_path(self):
        src = read("Dockerfile")
        self.assertIn("COPY deploy/reference/ /tmp/voice-reference/", src)
        self.assertIn("if [ -f /tmp/voice-reference/voice-ref.wav ]", src)
        self.assertIn(
            "cp /tmp/voice-reference/voice-ref.wav "
            "/srv/voice/voice-reference/processed/voice-ref.wav",
            src,
        )

    def test_dockerignore_excludes_private_audio_but_reincludes_reference(self):
        src = read(".dockerignore")
        self.assertIn("voice-reference/", src)
        self.assertIn("*.wav", src)
        self.assertIn("!deploy/reference/voice-ref.wav", src)
        self.assertIn("!deploy/reference/.gitkeep", src)

    def test_gitignore_keeps_reference_out_of_git(self):
        src = read(".gitignore")
        self.assertIn("deploy/reference/*", src)
        self.assertIn("!deploy/reference/.gitkeep", src)

    def test_release_refuses_missing_or_untracked_reference(self):
        src = read("scripts/release.sh")
        self.assertIn("deploy/reference/voice-ref.wav", src)
        self.assertIn("git check-ignore", src)
        self.assertIn("truncated", src)
        # Never cat/encode the reference contents in the release log.
        self.assertNotRegex(src, r"cat\s+\"?\$REF")
        self.assertNotRegex(src, r"base64.*\$REF")


class ModelManifestReleaseTest(unittest.TestCase):
    def test_dockerfile_ships_and_points_at_the_manifest(self):
        src = read("Dockerfile")
        self.assertIn("COPY deploy/model-manifest.json ./deploy/model-manifest.json", src)
        self.assertIn(
            "VOICE_MODEL_MANIFEST=/srv/voice/deploy/model-manifest.json", src
        )

    def test_manifest_is_valid_json_with_pinned_assets(self):
        with open(os.path.join(ROOT, "deploy", "model-manifest.json"),
                  encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["pocket_tts_version"], "4.0.0")
        for key in ("gated_model", "non_gated_model", "tokenizer"):
            asset = manifest[key]
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(asset["size_bytes"], 0)
            self.assertTrue(asset["revision"])


class CostEstimatorTest(unittest.TestCase):
    def test_requires_current_rates_and_does_not_hardcode_prices(self):
        src = read("deploy/cost-estimate.py")
        self.assertIn("--hourly", src)
        self.assertIn("--ipv4-month", src)
        # No fabricated default hourly price baked in.
        self.assertNotRegex(src, r"hourly.*default=")
        self.assertIn("budget-usd", src)


if __name__ == "__main__":
    unittest.main()
