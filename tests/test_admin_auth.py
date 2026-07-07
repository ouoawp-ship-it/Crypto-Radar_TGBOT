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
        client_ip: str = "127.0.0.1",
    ):
        handler = object.__new__(web.WebHandler)
        handler.path = path
        handler.headers = dict(headers or {})
        raw = json.dumps(method_body or {}).encode("utf-8")
        handler.headers.setdefault("Content-Length", str(len(raw)))
        handler.rfile = BytesIO(raw)
        handler.wfile = BytesIO()
        handler.client_address = (client_ip, 12345)
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
            self.assertEqual(status_body["username"], "admin")
            self.assertTrue(status_body["expires_at"])
            self.assertGreater(status_body["remaining_sec"], 0)
            self.assertEqual(status_body["lockout_config"]["max_failures"], 5)

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

            audit, statuses, _headers = self.make_handler(
                "/api/auth/audit?limit=5",
                settings,
                headers={"Cookie": cookie_pair},
            )
            web.WebHandler.do_GET(audit)
            audit_body = json.loads(audit.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 200)
            self.assertTrue(audit_body["ok"])
            events = [item.get("event") for item in audit_body.get("items", [])]
            self.assertIn("login_success", events)
            self.assertIn("logout", events)

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

    def test_login_lockout_is_scoped_to_username_and_ip(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
                web_auth_max_failures=5,
                web_auth_lockout_sec=600,
                web_auth_failure_window_sec=900,
            )
            with patch("paopao_radar.auth.time.time", return_value=1000):
                for attempt in range(5):
                    handler, statuses, _headers = self.make_handler(
                        "/api/auth/login",
                        settings,
                        method_body={"username": "admin", "password": "bad"},
                        client_ip="198.51.100.10",
                    )
                    web.WebHandler.do_POST(handler)
                    self.assertEqual(statuses[-1], 429 if attempt == 4 else 401)

                locked, statuses, _headers = self.make_handler(
                    "/api/auth/login",
                    settings,
                    method_body={"username": "admin", "password": "secret-pass"},
                    client_ip="198.51.100.10",
                )
                web.WebHandler.do_POST(locked)
                locked_body = json.loads(locked.wfile.getvalue().decode("utf-8"))
                self.assertEqual(statuses[-1], 429)
                self.assertEqual(locked_body["error"]["code"], "locked")
                self.assertGreater(locked_body["retry_after_sec"], 0)

                other_ip, statuses, _headers = self.make_handler(
                    "/api/auth/login",
                    settings,
                    method_body={"username": "admin", "password": "secret-pass"},
                    client_ip="203.0.113.20",
                )
                web.WebHandler.do_POST(other_ip)
                self.assertEqual(statuses[-1], 200)

            with patch("paopao_radar.auth.time.time", return_value=1601):
                unlocked, statuses, _headers = self.make_handler(
                    "/api/auth/login",
                    settings,
                    method_body={"username": "admin", "password": "secret-pass"},
                    client_ip="198.51.100.10",
                )
                web.WebHandler.do_POST(unlocked)
                self.assertEqual(statuses[-1], 200)

    def test_auth_audit_records_safe_events_and_applies_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for index in range(4):
                auth.append_auth_audit(
                    data_dir,
                    event="login_failed",
                    username="admin",
                    ip=f"198.51.100.{index}",
                    user_agent=f"agent-{index}",
                    result="failed",
                    reason="bad_credentials",
                    limit=2,
                    secret="unit-session-secret",
                )
            payload = auth.auth_audit_payload(data_dir, limit=10)
            text = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(payload["count"], 2)
            self.assertNotIn("198.51.100", text)
            self.assertNotIn("agent-", text)
            self.assertNotIn("secret-pass", text)
            self.assertNotIn("WEB_ADMIN_PASSWORD_HASH", text)
            self.assertNotIn("unit-session-secret", text)

    def test_auth_status_rejects_expired_and_tampered_cookie(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
            )
            session_value, _csrf = auth.create_session_value("admin", settings.web_session_secret, ttl_sec=60, now=1000)
            expired_cookie = f"{settings.web_auth_cookie_name}={session_value}"
            with patch("paopao_radar.auth.time.time", return_value=2000):
                status, statuses, _headers = self.make_handler(
                    "/api/auth/status",
                    settings,
                    headers={"Cookie": expired_cookie},
                )
                web.WebHandler.do_GET(status)
            body = json.loads(status.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 200)
            self.assertFalse(body["logged_in"])

            tampered, statuses, _headers = self.make_handler(
                "/api/auth/status",
                settings,
                headers={"Cookie": expired_cookie + "x"},
            )
            web.WebHandler.do_GET(tampered)
            body = json.loads(tampered.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 200)
            self.assertFalse(body["logged_in"])

    def test_session_refresh_sets_cookie_when_near_expiration(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                web_admin_username="admin",
                web_admin_password_hash=auth.generate_password_hash("secret-pass"),
                web_session_secret="unit-session-secret",
                web_session_ttl_sec=100,
                web_session_refresh_threshold_ratio=0.5,
            )
            session_value, csrf = auth.create_session_value("admin", settings.web_session_secret, ttl_sec=100, now=1000)
            cookie_pair = f"{settings.web_auth_cookie_name}={session_value}"
            private, statuses, headers = self.make_handler(
                "/api/dashboard",
                settings,
                headers={"Cookie": cookie_pair, "X-Forwarded-Proto": "https"},
            )
            with patch("paopao_radar.auth.time.time", return_value=1060), patch("paopao_radar.web.time.time", return_value=1060), patch(
                "paopao_radar.web.dashboard_payload",
                return_value={"ok": True, "data": {"version": {}}},
            ):
                web.WebHandler.do_GET(private)
            self.assertEqual(statuses[-1], 200)
            refreshed_cookie = next(value for key, value in headers if key == "Set-Cookie")
            self.assertIn("HttpOnly", refreshed_cookie)
            self.assertIn("SameSite=Lax", refreshed_cookie)
            self.assertIn("Secure", refreshed_cookie)
            self.assertEqual(private.auth_session.get("csrf"), csrf)

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

            wrong, statuses, _headers = self.make_handler(
                "/api/action",
                settings,
                method_body={"name": "doctor"},
                headers={"Cookie": cookie_pair, "X-CSRF-Token": "wrong"},
            )
            web.WebHandler.do_POST(wrong)
            body = json.loads(wrong.wfile.getvalue().decode("utf-8"))
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

            missing_scan, statuses, _headers = self.make_handler(
                "/api/outcomes/scan",
                settings,
                method_body={},
                headers={"Cookie": cookie_pair},
            )
            web.WebHandler.do_POST(missing_scan)
            body = json.loads(missing_scan.wfile.getvalue().decode("utf-8"))
            self.assertEqual(statuses[-1], 403)
            self.assertEqual(body["error"]["code"], "forbidden")

            outcome_scan, statuses, _headers = self.make_handler(
                "/api/outcomes/scan",
                settings,
                method_body={},
                headers={"Cookie": cookie_pair, "X-CSRF-Token": csrf},
            )
            with patch("paopao_radar.web.create_job_payload", return_value={"ok": True, "job": {"id": 2, "job_type": "outcome-scan", "status": "queued"}}), patch("paopao_radar.web.append_web_audit"):
                web.WebHandler.do_POST(outcome_scan)
            self.assertEqual(statuses[-1], 200)
            self.assertIn("outcome-scan", outcome_scan.wfile.getvalue().decode("utf-8"))

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
