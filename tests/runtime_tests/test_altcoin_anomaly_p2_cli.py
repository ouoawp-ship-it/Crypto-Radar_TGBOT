from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
import unittest
from unittest.mock import Mock, patch

from config import Settings
from radars.altcoin_contract_anomaly import cli as anomaly_cli
import runtime.cli as runtime_cli


class AltcoinAnomalyP2DispatchTests(unittest.TestCase):
    def test_parser_and_dispatch_keep_p2_before_the_main_runtime(self) -> None:
        parser = runtime_cli.build_parser()
        args = parser.parse_args(
            ["altcoin-anomaly", "--realtime-duration-sec", "300", "--json"]
        )
        self.assertEqual(args.realtime_duration_sec, 300)

        business_run = Mock(return_value=0)
        business_module = types.ModuleType("radars.altcoin_contract_anomaly.cli")
        business_module.run_altcoin_anomaly_cli = business_run
        settings = Settings()
        with patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.cli": business_module},
        ), patch.object(
            runtime_cli.Settings,
            "load",
            return_value=settings,
        ), patch.object(
            runtime_cli,
            "make_runtime",
        ) as make_runtime, patch.object(
            runtime_cli,
            "TelegramGateway",
        ) as telegram_gateway:
            code = runtime_cli.main(
                ["altcoin-anomaly", "--realtime-duration-sec", "300", "--json"]
            )

        self.assertEqual(code, 0)
        make_runtime.assert_not_called()
        telegram_gateway.assert_not_called()
        business_run.assert_called_once()
        self.assertEqual(
            business_run.call_args.args[0].realtime_duration_sec,
            300,
        )


class AltcoinAnomalyP2BusinessCliTests(unittest.TestCase):
    @staticmethod
    def args(**updates: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "cache_only": False,
            "preview_telegram": False,
            "send": False,
            "confirm_real_send": False,
            "json": True,
            "output": None,
            "realtime_duration_sec": 300,
        }
        values.update(updates)
        return argparse.Namespace(**values)

    @staticmethod
    def settings(**updates: object) -> Settings:
        values: dict[str, object] = {
            "altcoin_contract_anomaly_enable": True,
            "altcoin_contract_anomaly_realtime_enable": True,
            "altcoin_contract_anomaly_cmc_api_key": "fake-key",
        }
        values.update(updates)
        return Settings(**values)

    @staticmethod
    def fake_realtime_module(run: Mock) -> types.ModuleType:
        module = types.ModuleType("radars.altcoin_contract_anomaly.realtime")
        module.run_realtime_confirmation_session = run
        return module

    def test_bounded_session_emits_one_json_and_atomically_exports_same_result(self) -> None:
        result = {
            "schema_version": 1,
            "module": "altcoin_contract_anomaly",
            "mode": "realtime_confirmation_dry_run",
            "status": "completed",
            "telegram": {"enabled": False, "sent": 0},
        }
        run = Mock(return_value=result)
        module = self.fake_realtime_module(run)
        settings = self.settings(altcoin_contract_anomaly_cmc_api_key="")
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "p2-session.json"
            with patch.dict(os.environ, {}, clear=True), patch.dict(
                sys.modules,
                {"radars.altcoin_contract_anomaly.realtime": module},
            ), patch.object(
                anomaly_cli,
                "scan_candidate_pool",
            ) as scan, patch.object(
                anomaly_cli,
                "load_cached_pool",
            ) as load_cached, redirect_stdout(StringIO()) as stdout:
                code = anomaly_cli.run_altcoin_anomaly_cli(
                    self.args(output=output_path),
                    settings=settings,
                )

            rendered = stdout.getvalue().strip()
            payload = json.loads(rendered)
            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(code, anomaly_cli.EXIT_OK)
        self.assertEqual(payload, result)
        self.assertEqual(saved, result)
        self.assertEqual(rendered.count('"schema_version"'), 1)
        run.assert_called_once_with(settings, duration_sec=300)
        scan.assert_not_called()
        load_cached.assert_not_called()

    def test_realtime_mode_rejects_unbounded_duration_and_conflicting_flags(self) -> None:
        cases = (
            {"realtime_duration_sec": 0},
            {"realtime_duration_sec": 29},
            {"realtime_duration_sec": 1_201},
            {"realtime_duration_sec": 3_601},
            {"cache_only": True},
            {"preview_telegram": True},
            {"send": True},
            {"confirm_real_send": True},
        )
        run = Mock(return_value={})
        module = self.fake_realtime_module(run)
        for updates in cases:
            with self.subTest(updates=updates), patch.dict(
                os.environ,
                {},
                clear=True,
            ), patch.dict(
                sys.modules,
                {"radars.altcoin_contract_anomaly.realtime": module},
            ), redirect_stderr(StringIO()) as stderr:
                code = anomaly_cli.run_altcoin_anomaly_cli(
                    self.args(**updates),
                    settings=self.settings(),
                )
            self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)
            self.assertIn("配置错误", stderr.getvalue())
        run.assert_not_called()

    def test_realtime_gate_is_independent_and_checked_before_runner_import(self) -> None:
        run = Mock(return_value={})
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), redirect_stderr(StringIO()) as stderr:
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=self.settings(
                    altcoin_contract_anomaly_realtime_enable=False,
                ),
            )

        self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)
        self.assertIn("P2实时确认未启用", stderr.getvalue())
        run.assert_not_called()

    def test_realtime_output_rejects_every_configured_runtime_path_before_runner(self) -> None:
        settings = self.settings()
        protected = [
            (name, value)
            for name, value in vars(settings).items()
            if name.endswith("_path") and isinstance(value, Path)
        ]
        self.assertGreaterEqual(len(protected), 10)
        run = Mock(return_value={})
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), patch.object(anomaly_cli, "atomic_write_text") as writer, patch.object(
            anomaly_cli,
            "scan_candidate_pool",
        ) as scan:
            for name, protected_path in protected:
                with self.subTest(path_field=name), redirect_stderr(StringIO()):
                    code = anomaly_cli.run_altcoin_anomaly_cli(
                        self.args(output=protected_path),
                        settings=settings,
                    )
                self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)

        run.assert_not_called()
        writer.assert_not_called()
        scan.assert_not_called()

    def test_realtime_output_path_alias_is_compared_after_resolution(self) -> None:
        with TemporaryDirectory() as tmp:
            protected_path = Path(tmp) / "runtime" / "events.jsonl"
            settings = self.settings(
                altcoin_contract_anomaly_realtime_event_path=protected_path,
            )
            alias = protected_path.parent / "unused" / ".." / protected_path.name
            run = Mock(return_value={})
            module = self.fake_realtime_module(run)
            with patch.dict(os.environ, {}, clear=True), patch.dict(
                sys.modules,
                {"radars.altcoin_contract_anomaly.realtime": module},
            ), patch.object(anomaly_cli, "atomic_write_text") as writer, redirect_stderr(
                StringIO()
            ):
                code = anomaly_cli.run_altcoin_anomaly_cli(
                    self.args(output=alias),
                    settings=settings,
                )

        self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)
        run.assert_not_called()
        writer.assert_not_called()

    def test_realtime_output_rejects_sqlite_sidecars_before_runner(self) -> None:
        settings = self.settings()
        database_paths = [
            value
            for name, value in vars(settings).items()
            if name.endswith("_path")
            and isinstance(value, Path)
            and value.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        ]
        self.assertTrue(database_paths)
        run = Mock(return_value={})
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), patch.object(anomaly_cli, "atomic_write_text") as writer, redirect_stderr(
            StringIO()
        ):
            for database_path in database_paths:
                for suffix in ("-wal", "-shm", "-journal"):
                    with self.subTest(database=database_path, suffix=suffix):
                        code = anomaly_cli.run_altcoin_anomaly_cli(
                            self.args(output=Path(f"{database_path}{suffix}")),
                            settings=settings,
                        )
                        self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)

        run.assert_not_called()
        writer.assert_not_called()

    def test_realtime_output_rejects_atomic_json_lock_paths_before_runner(self) -> None:
        settings = self.settings()
        json_paths = [
            value
            for name, value in vars(settings).items()
            if name.endswith("_path")
            and isinstance(value, Path)
            and value.suffix.lower() in {".json", ".jsonl"}
        ]
        self.assertTrue(json_paths)
        run = Mock(return_value={})
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), patch.object(anomaly_cli, "atomic_write_text") as writer, redirect_stderr(
            StringIO()
        ):
            for json_path in json_paths:
                with self.subTest(path=json_path):
                    lock_path = json_path.with_name(f"{json_path.name}.lock")
                    code = anomaly_cli.run_altcoin_anomaly_cli(
                        self.args(output=lock_path),
                        settings=settings,
                    )
                    self.assertEqual(code, anomaly_cli.EXIT_CONFIG_ERROR)

        run.assert_not_called()
        writer.assert_not_called()

    def test_keyboard_interrupt_produces_final_json_and_exit_130(self) -> None:
        run = Mock(side_effect=KeyboardInterrupt)
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), redirect_stdout(StringIO()) as stdout:
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=self.settings(),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, anomaly_cli.EXIT_INTERRUPTED)
        self.assertEqual(payload["status"], "interrupted")
        self.assertEqual(payload["mode"], "realtime_confirmation_dry_run")

    def test_non_finite_runner_result_is_rejected_without_invalid_json(self) -> None:
        run = Mock(return_value={"score": float("nan")})
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=self.settings(),
            )

        self.assertEqual(code, anomaly_cli.EXIT_INTERNAL_ERROR)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("ValueError", stderr.getvalue())

    def test_runner_exit_code_is_preserved_after_emitting_structured_result(self) -> None:
        result = {
            "schema_version": 1,
            "module": "altcoin_contract_anomaly",
            "mode": "realtime_confirmation_dry_run",
            "status": "data_unavailable",
            "exit_code": anomaly_cli.EXIT_DATA_UNAVAILABLE,
            "failures": ["candidate_manifest_unavailable"],
        }
        run = Mock(return_value=result)
        module = self.fake_realtime_module(run)
        with patch.dict(os.environ, {}, clear=True), patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.realtime": module},
        ), redirect_stdout(StringIO()) as stdout:
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=self.settings(),
            )

        self.assertEqual(code, anomaly_cli.EXIT_DATA_UNAVAILABLE)
        self.assertEqual(json.loads(stdout.getvalue()), result)


if __name__ == "__main__":
    unittest.main()
