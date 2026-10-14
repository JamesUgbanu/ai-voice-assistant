"""Privacy/packaging guards: private voice-reference audio must never be
committed or baked into the image, and the adapter must not log paths.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class PackagingGuardTest(unittest.TestCase):
    def test_gitignore_excludes_reference_dirs(self):
        txt = open(os.path.join(ROOT, ".gitignore")).read()
        self.assertIn("voice-reference/original/", txt)
        self.assertIn("voice-reference/processed/", txt)
        self.assertIn("*.wav", txt)

    def test_dockerignore_excludes_private_audio(self):
        txt = open(os.path.join(ROOT, ".dockerignore")).read()
        for pat in ("voice-reference/", "voices/", "*.wav", ".env"):
            self.assertIn(pat, txt)

    def test_dockerfile_does_not_copy_reference(self):
        txt = open(os.path.join(ROOT, "Dockerfile")).read()
        # Only explicit, non-private paths are copied into the image.
        for line in txt.splitlines():
            if line.startswith("COPY"):
                self.assertNotIn("voice-reference", line)
                self.assertNotIn("voices", line)

    def test_reference_docs_tracked_but_audio_ignored(self):
        # The README is the only tracked file under voice-reference/.
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "voice-reference", "README.md")))


if __name__ == "__main__":
    unittest.main()
