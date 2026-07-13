from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar import web
from paopao_radar.auth import create_session_value, generate_password_hash, generate_session_secret, verify_password, verify_session_value
from paopao_radar.config import Settings


class AdminAuthTests(unittest.TestCase):
    def test_password_hash_and_session_round_trip(self) -> None:
        password_hash = generate_password_hash("strong-password")
        self.assertTrue(verify_password("strong-password", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))
        secret = generate_session_secret()
        value, csrf = create_session_value("admin", secret, ttl_sec=3600)
        payload = verify_session_value(value, secret)
        self.assertEqual(payload["username"], "admin")
        self.assertEqual(payload["csrf"], csrf)

    def test_password_mode_requires_hash_and_secret(self) -> None:
        settings = Settings(web_auth_mode="password", web_admin_password_hash="", web_session_secret="")
        self.assertEqual(web.auth_mode(settings), "password")
        self.assertFalse(bool(settings.web_admin_password_hash and settings.web_session_secret))

    def test_auth_audit_uses_runtime_data_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            self.assertEqual(settings.data_dir, Path(tmp))


if __name__ == "__main__":
    unittest.main()
