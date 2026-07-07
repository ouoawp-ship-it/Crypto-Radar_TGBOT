from __future__ import annotations

import inspect
import json
import os
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import auth, cli, web
from paopao_radar.config import Settings


class AdminAuthTests(unittest.TestCase):
    def test_password_hash_verification_uses_safe_compare(self) -> None:
        hashed = auth.generate_password_hash("correct-password")

        self.assertNotEqual(hashed, "correct-password")
        self.assertTrue(hashed.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.verify_password("correct-password", hashed))
        self.assertFalse(auth.verify_password("wrong-password", hashed))
        self.assertIn("hmac.compare_digest", inspect.getsource(auth.verify_password))

    def test_signed_session_rejects_expired_and_tampered_cookie(self) -> None:
        value, csrf = auth.create_session_value("admin", "secret", ttl_sec=60, now=1000)

        payload = auth.verify_session_value(value, "secret", expected_username="admin", now=1010)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["csrf"], csrf)
        self.assertIsNone(auth.verify_session_value(value, "secret", expected_username="admin", now=2000))
        self.assertIsNone(auth.verify_session_value(value + "x", "secret", expected_username="admin", now=1010))

    def make_handler(
        self,
        path: str,
        settings: Settings,
        *,
        method_body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ):
        handler = object.__new__(web.WebHandler)
        handler.path = path
        handler.headers = dict(headers or {})
        raw = json.dumps(method_body or {}).encode("utf-8")
        handler.headers.setdefault("Content-Length", str(len(raw)))
        handler.rfile = BytesIO(raw)
        handler.wfile = BytesIO()
        handler.server = type("Server", (), {"settings": settings, "admin_token": ""})()
        statuses: list[int] = []
        sent_headers: list[tuple[str, str]] = []
        handler.send_response = lambda status: statuses.append(int(status))
        handler.send_header = lambda key, value: sent_headers.append((key, value))
        handler.end_headers = lambda: None
        return handler, statuses, sent_headers

    def test_login_status_logout_and_private_api_cookie_auth(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
            )

            login, statuses, headers = self.make_handler(
                "/api/auth/login",
                settings,
                method_body={"username": "admin", "password": "secret-pass"},
                headers={"X-Forwarded-Proto": "https"},
            )
            web.WebHandler.do_POST(login)
            body = json.loads(login.wfile.getvalue().decode("utf-8"))

            self.assertEqual(statuses[-1], 200)
            self.assertTrue(body["ok"])
            self.assertTrue(body["csrf_token"])
            cookie_header = next(value for key, value in headers if key == "Set-Cookie")
            self.assertIn("HttpOnly", cookie_header)
            self.assertIn("SameSite=Lax", cookie_header)
            self.assertIn("Secure", cookie_header)
            cookie_pair = cookie_header.split(";", 1)[0]

            status, statuses, _headers = self.make_handler(
                "/api/auth/status",
                settings,
                headers={"Cookie": cookie_pair},
            )
            web.WebHandler.do_GET(status)
            status_body = json.loads(status.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 200)
            self.assertTrue(status_body["logged_in"])

            private, statuses, _headers = self.make_handler(
                "/api/dashboard",
                settings,
                headers={"Cookie": cookie_pair},
            )
            with patch("paopao_radar.web.dashboard_payload", return_value={"ok": True, "data": {"version": {}}}):
                web.WebHandler.do_GET(private)
            self.assertEqual(statuses[-1], 200)

            logout, statuses, headers = self.make_handler(
                "/api/auth/logout",
                settings,
                headers={"Cookie": cookie_pair, "X-CSRF-Token": body["csrf_token"]},
            )
            web.WebHandler.do_POST(logout)
            self.assertEqual(statuses[-1], 200)
            clear_cookie = next(value for key, value in headers if key == "Set-Cookie")
            self.assertIn("Max-Age=0", clear_cookie)

    def test_login_wrong_password_and_unconfigured_password_fail(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
            )
            handler, statuses, _headers = self.make_handler(
                "/api/auth/login",
                settings,
                method_body={"username": "admin", "password": "bad"},
            )
            web.WebHandler.do_POST(handler)
            body = json.loads(handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 401)
            self.assertEqual(body["error"]["code"], "unauthorized")

            unconfigured = Settings(data_dir=Path(tmp))
            handler, statuses, _headers = self.make_handler(
                "/api/auth/login",
                unconfigured,
                method_body={"username": "admin", "password": "anything"},
            )
            web.WebHandler.do_POST(handler)
            body = json.loads(handler.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 400)
            self.assertEqual(body["error"]["code"], "auth_not_configured")

    def test_private_api_requires_login_but_public_api_does_not(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            private, statuses, _headers = self.make_handler("/api/dashboard", settings)
            web.WebHandler.do_GET(private)
            body = json.loads(private.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 401)
            self.assertEqual(body["error"]["code"], "unauthorized")

            public, statuses, _headers = self.make_handler("/public-api/signals?limit=1", settings)
            with patch("paopao_radar.web.public_signals_payload", return_value={"ok": True, "items": [], "count": 0}):
                web.WebHandler.do_GET(public)
            self.assertEqual(statuses[-1], 200)

    def test_private_write_api_requires_csrf_token(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
            )
            session_value, csrf = auth.create_session_value("admin", settings.web_session_secret)
            cookie_pair = f"{settings.web_auth_cookie_name}={session_value}"

            missing, statuses, _headers = self.make_handler(
                "/api/action",
                settings,
                method_body={"name": "doctor"},
                headers={"Cookie": cookie_pair},
            )
            web.WebHandler.do_POST(missing)
            body = json.loads(missing.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 403)
            self.assertEqual(body["error"]["code"], "forbidden")

            allowed, statuses, _headers = self.make_handler(
                "/api/action",
                settings,
                method_body={"name": "doctor"},
                headers={"Cookie": cookie_pair, "X-CSRF-Token": csrf},
            )
            with patch("paopao_radar.web.create_job_payload", return_value={"ok": True, "job": {"id": 1, "job_type": "doctor", "status": "queued"}}), patch("paopao_radar.web.append_web_audit"):
                web.WebHandler.do_POST(allowed)
            self.assertEqual(statuses[-1], 200)

    def test_token_mode_is_only_used_when_explicitly_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            password_mode = Settings(data_dir=Path(tmp), web_auth_mode="password")
            handler, _statuses, _headers = self.make_handler(
                "/api/dashboard",
                password_mode,
                headers={"X-Admin-Token": "legacy"},
            )
            handler.server.admin_token = "legacy"
            self.assertFalse(web.check_auth(handler))

            token_mode = Settings(data_dir=Path(tmp), web_auth_mode="token")
            handler, _statuses, _headers = self.make_handler(
                "/api/dashboard",
                token_mode,
                headers={"X-Admin-Token": "legacy"},
            )
            handler.server.admin_token = "legacy"
            self.assertTrue(web.check_auth(handler))

    def test_admin_password_set_defaults_to_visible_input_and_writes_hash_only(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            env_path = Path(tmp) / ".env.oi"
            args = SimpleNamespace(admin_action="set", hidden=False)
            output = StringIO()
            with patch.object(cli, "ENV_FILE", env_path), patch(
                "builtins.input",
                side_effect=["paopao", "visible-password", "visible-password"],
            ), patch("getpass.getpass", side_effect=AssertionError("hidden input should not be used")), patch(
                "sys.stdout",
                output,
            ):
                code = cli.run_admin_password(args)

            text = env_path.read_text(encoding="utf-8")
            stdout = output.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("提示：当前密码输入会明文显示，请确认终端环境安全。", stdout)
            self.assertIn("WEB_ADMIN_USERNAME=paopao", text)
            self.assertIn("WEB_ADMIN_PASSWORD_HASH=pbkdf2_sha256$", text)
            self.assertNotIn("WEB_ADMIN_PASSWORD=", text)
            self.assertNotIn("visible-password", text)
            self.assertNotIn("visible-password", stdout)
            stored_hash = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("WEB_ADMIN_PASSWORD_HASH="))
            self.assertTrue(auth.verify_password("visible-password", stored_hash))

    def test_admin_password_mismatch_does_not_write_new_hash(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            env_path = Path(tmp) / ".env.oi"
            original = "WEB_ADMIN_PASSWORD_HASH=old-hash\nWEB_SESSION_SECRET=keep-secret\n"
            env_path.write_text(original, encoding="utf-8")
            args = SimpleNamespace(admin_action="set", hidden=False)
            output = StringIO()
            with patch.object(cli, "ENV_FILE", env_path), patch(
                "builtins.input",
                side_effect=["paopao", "one-password", "two-password"],
            ), patch("sys.stdout", output):
                code = cli.run_admin_password(args)

            self.assertEqual(code, 2)
            self.assertEqual(env_path.read_text(encoding="utf-8"), original)
            self.assertIn("两次输入的密码不一致，请重新执行设置命令。", output.getvalue())

    def test_admin_password_hidden_option_uses_getpass(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            env_path = Path(tmp) / ".env.oi"
            args = SimpleNamespace(admin_action="set", hidden=True)
            output = StringIO()
            with patch.object(cli, "ENV_FILE", env_path), patch(
                "builtins.input",
                side_effect=["paopao"],
            ), patch("getpass.getpass", side_effect=["hidden-password", "hidden-password"]) as getpass_mock, patch(
                "sys.stdout",
                output,
            ):
                code = cli.run_admin_password(args)

            text = env_path.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertEqual(getpass_mock.call_count, 2)
            self.assertNotIn("当前密码输入会明文显示", output.getvalue())
            self.assertNotIn("hidden-password", text)
            self.assertNotIn("hidden-password", output.getvalue())


if __name__ == "__main__":
    unittest.main()
