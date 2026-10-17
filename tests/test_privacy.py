"""Privacy/packaging guards.

Option A (explicitly authorized): the authorized reference IS baked into the
PRIVATE image at a single, known path. Every other private-audio path stays
excluded from Git and the build context. PRIVACY TRADE-OFF: anyone able to pull
the private image can extract the biometric reference; registry access must stay
tightly restricted.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AUTHORIZED_COPY = (
    "COPY deploy/reference/voice-ref.wav "
    "/srv/voice/voice-reference/processed/voice-ref.wav"
)


class PackagingGuardTest(unittest.TestCase):
    def test_gitignore_excludes_reference_dirs(self):
        txt = open(os.path.join(ROOT, ".gitignore")).read()
        self.assertIn("voice-reference/original/", txt)
        self.assertIn("voice-reference/processed/", txt)
        self.assertIn("*.wav", txt)
        # Option A staging area is git-ignored too.
        self.assertIn("deploy/reference/", txt)

    def test_dockerignore_excludes_private_audio(self):
        txt = open(os.path.join(ROOT, ".dockerignore")).read()
        for pat in ("voice-reference/", "voices/", "*.wav", ".env"):
            self.assertIn(pat, txt)
        # Only the single authorized reference is re-included.
        self.assertIn("!deploy/reference/voice-ref.wav", txt)

    def test_dockerfile_copies_only_the_authorized_reference(self):
        txt = open(os.path.join(ROOT, "Dockerfile")).read()
        copies = [line for line in txt.splitlines() if line.startswith("COPY")]
        self.assertIn(AUTHORIZED_COPY, copies)  # Option A
        for line in copies:
            if line == AUTHORIZED_COPY:
                continue
            # No other copy may pull in private audio.
            self.assertNotIn("voice-reference", line)
            self.assertNotIn("voices", line)
            self.assertNotIn(".wav", line)

    def test_reference_docs_tracked_but_audio_ignored(self):
        # The README is the only tracked file under voice-reference/.
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "voice-reference", "README.md")))


if __name__ == "__main__":
    unittest.main()
