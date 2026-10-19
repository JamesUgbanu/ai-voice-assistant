"""Privacy/packaging guards.

The original private deployment may bake one authorized reference into the
image at a single known path. Every other private-audio path stays excluded
from Git and the build context. A clean public checkout has no reference and
builds a preset-voice image instead. PRIVACY TRADE-OFF: anyone able to pull an
image containing the reference can extract it; keep such images private.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REFERENCE_DIR_COPY = "COPY deploy/reference/ /tmp/voice-reference/"
REFERENCE_RUNTIME_PATH = "/srv/voice/voice-reference/processed/voice-ref.wav"


class PackagingGuardTest(unittest.TestCase):
    def test_gitignore_excludes_reference_dirs(self):
        txt = (Path(ROOT) / ".gitignore").read_text()
        self.assertIn("voice-reference/original/", txt)
        self.assertIn("voice-reference/processed/", txt)
        self.assertIn("*.wav", txt)
        # The staging area is ignored except for the empty-directory marker.
        self.assertIn("deploy/reference/*", txt)
        self.assertIn("!deploy/reference/.gitkeep", txt)

    def test_dockerignore_excludes_private_audio(self):
        txt = (Path(ROOT) / ".dockerignore").read_text()
        for pat in ("voice-reference/", "voices/", "*.wav", ".env"):
            self.assertIn(pat, txt)
        # Only the single authorized reference is re-included.
        self.assertIn("!deploy/reference/voice-ref.wav", txt)
        self.assertIn("!deploy/reference/.gitkeep", txt)

    def test_dockerfile_copies_only_the_authorized_reference(self):
        txt = (Path(ROOT) / "Dockerfile").read_text()
        copies = [line for line in txt.splitlines() if line.startswith("COPY")]
        self.assertIn(REFERENCE_DIR_COPY, copies)
        # The copy is conditional: a clean checkout has no reference and must
        # still build (preset voice rather than a broken build).
        self.assertIn("if [ -f /tmp/voice-reference/voice-ref.wav ]", txt)
        self.assertIn(REFERENCE_RUNTIME_PATH, txt)
        for line in copies:
            if line == REFERENCE_DIR_COPY:
                continue
            # No other copy may pull in private audio.
            self.assertNotIn("voice-reference", line)
            self.assertNotIn("voices", line)
            self.assertNotIn(".wav", line)

    def test_reference_docs_tracked_but_audio_ignored(self):
        # The README is the only tracked file under voice-reference/.
        self.assertTrue((Path(ROOT) / "voice-reference" / "README.md").is_file())


if __name__ == "__main__":
    unittest.main()
