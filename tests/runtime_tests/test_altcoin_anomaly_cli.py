from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from config import Settings
from radars.altcoin_contract_anomaly import cli as anomaly_cli
from radars.altcoin_contract_anomaly.radar import AltcoinAnomalyDataUnavailable
import runtime.cli as cli


class AltcoinAnomalyCommandTests(unittest.TestCase):
    @staticmethod
    def fake_business_module(run: Mock) -> types.ModuleType:
        module = types.ModuleType("radars.altcoin_contract_anomaly.cli")
        module.run_altcoin_anomaly_cli = run
        return module

    def test_parser_exposes_one_shot_command_and_preview_flags(self) -> None:
        parser = cli.build_parser()
        command_action = next(
            action for action in parser._actions if action.dest == "command"
        )

        self.assertIn("altcoin-anomaly", command_action.choices)
        args = parser.parse_args(
            [
                "altcoin-anomaly",
                "--json",
                "--cache-only",
                "--preview-telegram",
                "--output",
                "result.json",
            ]
        )
        self.assertTrue(args.json)
        self.assertTrue(args.cache_only)
        self.assertTrue(args.preview_telegram)
        self.assertEqual(args.output, Path("result.json"))

    def test_command_calls_only_the_dedicated_business_cli(self) -> None:
        settings = Settings(altcoin_contract_anomaly_enable=False)
        run = Mock(return_value=0)
        fake_module = self.fake_business_module(run)

        with TemporaryDirectory() as tmp, patch.dict(
            sys.modules,
            {"radars.altcoin_contract_anomaly.cli": fake_module},
        ), patch.object(
            cli.Settings,
            "load",
            return_value=settings,
        ) as load_settings, patch.object(
            cli,
            "make_runtime",
        ) as make_runtime, patch.object(
            cli,
            "run_loop",
        ) as run_loop:
            output = Path(tmp) / "candidate-pool.json"
            code = cli.main(
                [
                    "altcoin-anomaly",
                    "--cache-only",
                    "--preview-telegram",
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(code, 0)
        load_settings.assert_called_once_with()
        make_runtime.assert_not_called()
        run_loop.assert_not_called()
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertTrue(args.cache_only)
        self.assertTrue(args.preview_telegram)
        self.assertEqual(args.output, output)
        self.assertIs(run.call_args.kwargs["settings"], settings)

    def test_business_exit_codes_are_preserved(self) -> None:
        for expected in (0, 1, 2, 3):
            with self.subTest(exit_code=expected):
                run = Mock(return_value=expected)
                fake_module = self.fake_business_module(run)
                with patch.dict(
                    sys.modules,
                    {"radars.altcoin_contract_anomaly.cli": fake_module},
                ), patch.object(
                    cli.Settings,
                    "load",
                    return_value=Settings(),
                ):
                    code = cli.main(["altcoin-anomaly", "--json"])

                self.assertEqual(code, expected)


class AltcoinAnomalyBusinessCliTests(unittest.TestCase):
    @staticmethod
    def pool() -> dict[str, object]:
        snapshot = {
            "schema_version": 1,
            "symbol": "COTIUSDT",
            "market_cap_usd": 23_370_000.0,
            "oi_market_cap_ratio": 0.355,
            "binance_oi_usd": 8_296_350.0,
            "binance_oi_market_cap_ratio": 0.355,
            "global_oi_usd": None,
            "global_oi_market_cap_ratio": None,
            "global_oi_source": None,
            "funding_rate": -0.000352,
            "candidate_tags": ["short_squeeze_candidate"],
            "data_quality": "complete",
        }
        return {
            "schema_version": 1,
            "module": "altcoin_contract_anomaly",
            "generated_at": "2026-08-07T12:00:00+00:00",
            "candidate_pool_hash": "abc123",
            "universe": {
                "loaded_usdt_perpetuals": 526,
                "eligible_altcoin_contracts": 500,
                "excluded_contracts": 26,
            },
            "mapping_stats": {
                "trusted_count": 364,
                "diagnostic_count": 120,
                "conflict_count": 4,
                "unmapped_count": 12,
                "reason_counts": {"missing_cmc_id": 12},
            },
            "stats": {
                "short_squeeze_count": 1,
                "high_leverage_count": 0,
                "dual_match_count": 0,
                "merged_candidate_count": 1,
            },
            "short_squeeze_symbols": ["COTIUSDT"],
            "high_leverage_symbols": [],
            "dual_match_symbols": [],
            "candidate_symbols": ["COTIUSDT"],
            "delta": {"added": ["COTIUSDT"], "retained": [], "removed": []},
            "snapshots": [snapshot],
            "data_sources": {
                "market_cap": "CoinMarketCap官方API",
                "open_interest": "Binance USDⓈ-M Futures",
            },
            "diagnostics": {"network_status": "测试夹具"},
        }

    @staticmethod
    def args(**updates: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "cache_only": False,
            "preview_telegram": False,
            "json": False,
            "output": None,
        }
        values.update(updates)
        return argparse.Namespace(**values)

    def test_json_preview_and_output_file_use_the_same_structured_result(self) -> None:
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "export.json"
            settings = Settings(
                data_dir=Path(tmp),
                altcoin_contract_anomaly_enable=True,
                altcoin_contract_anomaly_cmc_api_key="fake-cmc-key",
            )
            with patch.object(
                anomaly_cli,
                "scan_candidate_pool",
                return_value=self.pool(),
            ) as scan, redirect_stdout(StringIO()) as stdout:
                code = anomaly_cli.run_altcoin_anomaly_cli(
                    self.args(
                        json=True,
                        preview_telegram=True,
                        output=output_path,
                    ),
                    settings=settings,
                )

            rendered = stdout.getvalue().strip()
            payload = json.loads(rendered)
            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(code, anomaly_cli.EXIT_OK)
        scan.assert_called_once_with(settings)
        self.assertEqual(payload, saved)
        self.assertEqual(payload["candidate_symbols"], ["COTIUSDT"])
        self.assertIn("telegram_preview_pages", payload)
        self.assertIn(
            "🔎【山寨合约异动雷达｜监控池更新】",
            payload["telegram_preview_pages"][0],
        )
        self.assertIn("第1/1页", payload["telegram_preview_pages"][0])

    def test_default_human_output_is_chinese_and_has_no_telegram_send(self) -> None:
        settings = Settings(
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_cmc_api_key="fake-cmc-key",
        )
        with patch.object(
            anomaly_cli,
            "scan_candidate_pool",
            return_value=self.pool(),
        ), redirect_stdout(StringIO()) as stdout:
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=settings,
            )

        text = stdout.getvalue()
        self.assertEqual(code, anomaly_cli.EXIT_OK)
        self.assertIn("山寨合约异动雷达｜候选池扫描", text)
        self.assertIn(
            "COTIUSDT｜市值 $23.37M｜OI/市值 35.5%｜费率 -0.0352%",
            text,
        )
        self.assertNotIn("Telegram消息预览", text)

    def test_cache_only_uses_only_the_cached_pool_loader(self) -> None:
        settings = Settings()
        with patch.object(
            anomaly_cli,
            "load_cached_pool",
            return_value=self.pool(),
        ) as load_cached, patch.object(
            anomaly_cli,
            "scan_candidate_pool",
        ) as scan, redirect_stdout(StringIO()):
            code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(cache_only=True),
                settings=settings,
            )

        self.assertEqual(code, anomaly_cli.EXIT_OK)
        load_cached.assert_called_once_with(settings)
        scan.assert_not_called()

    def test_config_data_and_internal_failures_have_distinct_safe_exit_codes(self) -> None:
        with patch.object(anomaly_cli, "scan_candidate_pool") as disabled_scan, redirect_stderr(StringIO()):
            disabled_code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=Settings(
                    altcoin_contract_anomaly_enable=False,
                    altcoin_contract_anomaly_cmc_api_key="fake-key",
                ),
            )
        self.assertEqual(disabled_code, anomaly_cli.EXIT_CONFIG_ERROR)
        disabled_scan.assert_not_called()

        with redirect_stderr(StringIO()) as stderr:
            config_code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=Settings(
                    altcoin_contract_anomaly_enable=True,
                    altcoin_contract_anomaly_cmc_api_key="",
                ),
            )
        self.assertEqual(config_code, anomaly_cli.EXIT_CONFIG_ERROR)
        self.assertIn("配置错误", stderr.getvalue())

        settings = Settings(
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_cmc_api_key="fake-cmc-key-never-log",
        )
        with patch.object(
            anomaly_cli,
            "scan_candidate_pool",
            side_effect=AltcoinAnomalyDataUnavailable("夹具数据不可用"),
        ), redirect_stderr(StringIO()) as stderr:
            data_code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=settings,
            )
        self.assertEqual(data_code, anomaly_cli.EXIT_DATA_UNAVAILABLE)
        self.assertIn("数据不可用", stderr.getvalue())

        with patch.object(
            anomaly_cli,
            "scan_candidate_pool",
            side_effect=anomaly_cli.CandidateStatePartialUpdateError(
                "must-not-overwrite"
            ),
        ), redirect_stderr(StringIO()) as stderr:
            partial_code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=settings,
            )
        self.assertEqual(partial_code, anomaly_cli.EXIT_DATA_UNAVAILABLE)
        self.assertIn("已保留上一份完整快照", stderr.getvalue())

        secret = "provider-body-secret-never-log"
        with patch.object(
            anomaly_cli,
            "scan_candidate_pool",
            side_effect=RuntimeError(secret),
        ), redirect_stderr(StringIO()) as stderr:
            internal_code = anomaly_cli.run_altcoin_anomaly_cli(
                self.args(),
                settings=settings,
            )
        self.assertEqual(internal_code, anomaly_cli.EXIT_INTERNAL_ERROR)
        self.assertIn("RuntimeError", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
