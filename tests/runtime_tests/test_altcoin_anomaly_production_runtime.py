from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from config import Settings
import runtime.cli as cli
from shared.telegram import PushResult


def runtime(root: Path, *, production: bool = True):
    settings = Settings(
        data_dir=root,
        altcoin_contract_anomaly_enable=True,
        altcoin_contract_anomaly_realtime_enable=True,
        altcoin_contract_anomaly_production_enable=production,
    )
    return settings, MagicMock(), MagicMock(), MagicMock()


class MarketStreamProductionDispatchTests(unittest.TestCase):
    def test_manual_altcoin_topic_acceptance_message_uses_fixed_template(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                data_dir=root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="77",
            )
            gateway = MagicMock()
            gateway.send.return_value = PushResult("sent", "sent", True)
            with (
                patch(
                    "runtime.cli.make_runtime",
                    return_value=(settings, MagicMock(), MagicMock(), gateway),
                ),
                patch("runtime.cli.telegram_config_checks", return_value=[]),
            ):
                result = cli.main([
                    "telegram-test",
                    "--topic-template",
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "--send",
                    "--confirm-real-send",
                ])

        self.assertEqual(result, 0)
        text = str(gateway.send.call_args.args[0])
        self.assertIn("山寨合约异动｜验收测试", text)
        self.assertIn("不是交易信号", text)
        self.assertEqual(
            gateway.send.call_args.args[1],
            "TG_ALTCOIN_CONTRACT_ANOMALY",
        )
        self.assertTrue(gateway.send.call_args.kwargs["send"])
        self.assertTrue(gateway.send.call_args.kwargs["confirm_real_send"])
        self.assertEqual(gateway.send.call_args.kwargs["parse_mode"], "HTML")

    def test_plain_market_stream_is_legacy_even_when_all_module_switches_are_true(self) -> None:
        with TemporaryDirectory() as directory:
            values = runtime(Path(directory), production=True)
            with (
                patch("runtime.cli.Settings.load", return_value=values[0]),
                patch("runtime.cli.make_runtime") as make_runtime,
                patch("runtime.cli.cleanup_runtime_artifacts"),
                patch(
                    "shared.realtime_market.run_realtime_market_service",
                    return_value=0,
                ) as legacy,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.run_altcoin_production_service",
                    return_value=99,
                ) as production,
            ):
                result = cli.main(
                    ["market-stream", "--stream-duration-minutes", "2"]
                )

        self.assertEqual(result, 0)
        make_runtime.assert_not_called()
        production.assert_not_called()
        legacy.assert_called_once()
        self.assertIs(legacy.call_args.args[0], values[0])
        self.assertEqual(legacy.call_args.kwargs["duration_sec"], 120.0)
        self.assertIsNotNone(legacy.call_args.kwargs["process_lock"])

    def test_explicit_production_flag_is_the_only_controller_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            values = runtime(Path(directory), production=True)
            with (
                patch("runtime.cli.Settings.load", return_value=values[0]),
                patch("runtime.cli.make_runtime") as make_runtime,
                patch("runtime.cli.cleanup_runtime_artifacts"),
                patch(
                    "shared.realtime_market.run_realtime_market_service",
                    return_value=98,
                ) as legacy,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.run_altcoin_production_service",
                    return_value=7,
                ) as production,
            ):
                result = cli.main(
                    [
                        "market-stream",
                        "--altcoin-production",
                        "--stream-duration-minutes",
                        "1.5",
                    ]
                )

        self.assertEqual(result, 7)
        make_runtime.assert_not_called()
        legacy.assert_not_called()
        production.assert_called_once_with(
            values[0],
            duration_sec=90.0,
            real_send_requested=False,
        )

    def test_real_send_requires_cli_dual_gate_and_explicit_production(self) -> None:
        cases = (
            (["market-stream", "--send"], 2),
            (["market-stream", "--confirm-real-send"], 2),
            (["market-stream", "--send", "--confirm-real-send"], 2),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments), TemporaryDirectory() as directory:
                values = runtime(Path(directory), production=True)
                with (
                    patch("runtime.cli.Settings.load", return_value=values[0]),
                    patch("runtime.cli.make_runtime") as make_runtime,
                    patch("runtime.cli.cleanup_runtime_artifacts"),
                    patch(
                        "shared.realtime_market.run_realtime_market_service"
                    ) as legacy,
                    patch(
                        "radars.altcoin_contract_anomaly.production_runtime.run_altcoin_production_service"
                    ) as production,
                ):
                    result = cli.main(arguments)
            self.assertEqual(result, expected)
            make_runtime.assert_not_called()
            legacy.assert_not_called()
            production.assert_not_called()

        with TemporaryDirectory() as directory:
            values = runtime(Path(directory), production=True)
            with (
                patch("runtime.cli.Settings.load", return_value=values[0]),
                patch("runtime.cli.make_runtime") as make_runtime,
                patch("runtime.cli.cleanup_runtime_artifacts"),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.run_altcoin_production_service",
                    return_value=0,
                ) as production,
            ):
                result = cli.main([
                    "market-stream",
                    "--altcoin-production",
                    "--send",
                    "--confirm-real-send",
                ])

        self.assertEqual(result, 0)
        make_runtime.assert_not_called()
        production.assert_called_once_with(
            values[0],
            duration_sec=0.0,
            real_send_requested=True,
        )


if __name__ == "__main__":
    unittest.main()
