"""Widget token verification (HS256 JWT shape) — stdlib only."""
import base64
import hashlib
import hmac
import json
import time
import unittest

from app.widget_token import verify_widget_token

SECRET = "x" * 32
NOW = 1_800_000_000


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def make_token(secret: str, claims: dict) -> str:
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    body = f"{header}.{payload}"
    signature = b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def claims(**overrides) -> dict:
    value = {
        "project": "portfolio",
        "origin": "https://widget.example",
        "scope": "widget:chat widget:voice",
        "aud": "widget",
        "iat": NOW,
        "exp": NOW + 300,
        "jti": "test",
    }
    value.update(overrides)
    return value


class WidgetTokenTest(unittest.TestCase):
    def verify(self, token, **overrides):
        return verify_widget_token(SECRET, token, now=NOW, **overrides)

    def test_valid_token_returns_claims(self):
        result = self.verify(make_token(SECRET, claims()))
        self.assertIsNotNone(result)
        self.assertEqual(result["project"], "portfolio")
        self.assertEqual(result["origin"], "https://widget.example")

    def test_wrong_secret_is_rejected(self):
        self.assertIsNone(self.verify(make_token("y" * 32, claims())))

    def test_expired_token_is_rejected(self):
        self.assertIsNone(self.verify(make_token(SECRET, claims(exp=NOW - 31))))

    def test_missing_scope_is_rejected(self):
        token = make_token(SECRET, claims(scope="widget:chat"))
        self.assertIsNone(self.verify(token, required_scope="widget:voice"))

    def test_wrong_audience_or_missing_project_is_rejected(self):
        self.assertIsNone(self.verify(make_token(SECRET, claims(aud="other"))))
        self.assertIsNone(self.verify(make_token(SECRET, claims(project=""))))

    def test_tampered_payload_is_rejected(self):
        token = make_token(SECRET, claims())
        header, payload, signature = token.split(".")
        forged = b64(json.dumps(claims(project="other")).encode())
        self.assertIsNone(self.verify(f"{header}.{forged}.{signature}"))

    def test_short_secret_and_non_string_tokens_are_rejected(self):
        self.assertIsNone(verify_widget_token("short", make_token(SECRET, claims()), now=NOW))
        self.assertIsNone(verify_widget_token(SECRET, None, now=NOW))
        self.assertIsNone(verify_widget_token(SECRET, 123, now=NOW))

    def test_malformed_tokens_fail_closed_without_raising(self):
        for token in ("", "a", "a.b", "a.b.c.d", "a.b.é", "a.%%%.sig", "..", "a.b." + chr(0)):
            self.assertIsNone(verify_widget_token(SECRET, token, now=NOW), repr(token))

    def test_future_issued_at_and_wrong_algorithm_are_rejected(self):
        self.assertIsNone(self.verify(make_token(SECRET, claims(iat=NOW + 120))))
        header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload = b64(json.dumps(claims()).encode())
        body = f"{header}.{payload}"
        signature = b64(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
        self.assertIsNone(self.verify(f"{body}.{signature}"))


if __name__ == "__main__":
    unittest.main()
