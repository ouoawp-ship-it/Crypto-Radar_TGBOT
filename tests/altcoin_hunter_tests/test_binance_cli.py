"""Execute all new CLI commands with hard runtime I/O prohibitions."""
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "altcoin_hunter_tests" / "fixtures" / "binance"
TIME = 1788518401000


def invoke(arguments):
    script = r'''
import importlib.abc, os, socket, sqlite3, sys, threading
from unittest.mock import patch
class NoClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'requests','httpx','aiohttp','websocket','websockets','telegram','ccxt'} or fullname.startswith('shared.'):
            raise AssertionError('forbidden import: ' + fullname)
        return None
sys.meta_path.insert(0, NoClients())
def deny(*args, **kwargs): raise AssertionError('forbidden runtime call')
def audit(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','socket.gethostbyname','sqlite3.connect','os.mkdir','os.remove','os.rename'}:
        raise AssertionError('forbidden runtime action: ' + event)
    if event == 'open':
        mode, flags = args[1] or '', args[2] if len(args) > 2 else 0
        if any(c in str(mode) for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError('file write')
sys.addaudithook(audit)
with patch.object(socket,'socket',deny), patch.object(socket,'getaddrinfo',deny), patch.object(sqlite3,'connect',deny), patch.object(threading.Thread,'start',deny), patch.object(os,'getenv',deny):
    from runtime.altcoin_hunter import main
    raise SystemExit(main(ARGUMENTS))
'''.replace("ARGUMENTS", repr(arguments))
    return subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=30)


class BinanceCliTests(unittest.TestCase):
    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "offline_dry_run")
        self.assertEqual(payload["network_calls"], 0)
        self.assertEqual(payload["dns_calls"], 0)
        self.assertFalse(payload["real_send"])
        for field in ("protocol_version", "parsed_events", "rejected_items", "route", "stream_coverage",
                      "connection_states", "epochs", "subscription_shards", "budget_status", "deterministic_digest"):
            self.assertIn(field, payload)
        return payload

    def validate_args(self, kind, fixture):
        return ["validate-binance-fixture", "--fixture", str(FIXTURES / fixture), "--kind", kind,
                "--receive-time-ms", str(TIME), "--receive-monotonic-ns", "123456789"]

    def test_validate_fixture_actual_entrypoint_and_determinism(self):
        args = self.validate_args("agg_trade", "agg_trade.json")
        first, second = self.assert_success(invoke(args)), self.assert_success(invoke(args))
        self.assertEqual(first, second)
        self.assertEqual(first["parsed_events"], 1)

    def test_metadata_cli_does_not_invent_events(self):
        for kind, name in (("exchange_info", "exchange_info.json"), ("funding_info", "funding_info.json"),
                           ("server_time", "server_time.json"), ("open_interest", "open_interest.json")):
            with self.subTest(kind=kind):
                result = self.assert_success(invoke(self.validate_args(kind, name)))
                self.assertEqual(result["parsed_events"], int(kind == "open_interest"))

    def test_plan_entrypoint(self):
        result = self.assert_success(invoke(["plan-binance-subscriptions", "--universe",
             str(FIXTURES / "exchange_info.json"), "--max-streams-per-connection", "800",
             "--promoted-symbols", "AAAUSDT"]))
        self.assertEqual(len(result["subscription_shards"]), 2)
        self.assertEqual(result["stream_coverage"]["eligible_instruments"], 3)

    def test_simulation_entrypoint_includes_budget_and_no_fake_oi_value(self):
        args = ["simulate-binance-connection", "--scenario", "normal", "--seed", "42"]
        result = self.assert_success(invoke(args))
        self.assertEqual(result, self.assert_success(invoke(args)))
        self.assertEqual(result["budget_status"]["oi_coverage"]["missing_instruments"], 1)
        self.assertFalse(result["budget_status"]["transport_executed"])
        self.assertEqual(result["budget_status"]["after_cleanup"]["inflight"], 0)
        self.assertEqual(result["budget_status"]["after_cleanup"]["queue_depth"], 0)

    def test_network_paths_rejected_before_file_open(self):
        from runtime.altcoin_hunter import _read_offline_json
        for value in (r"\\server\share\fixture.json", "//server/share/fixture.json", r"\\?\UNC\server\share\x.json"):
            with self.subTest(path=value), patch.object(Path, "open") as opening:
                with self.assertRaises(ValueError):
                    _read_offline_json(Path(value))
                opening.assert_not_called()

    def test_invalid_kind_is_degraded_without_network_or_writes(self):
        result = invoke(self.validate_args("not_a_protocol", "agg_trade.json"))
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["rejected_items"], 1)
        self.assertEqual(payload["network_calls"], 0)
        self.assertEqual(payload["parser_rejected_count"], 1)
        self.assertEqual(payload["admission_rejected_count"], 0)
        self.assertEqual(payload["duplicate_count"], 0)
        self.assertEqual(payload["total_rejected_count"], 1)

    def test_explicit_string_ack_strategy_is_deterministic_offline(self):
        args = ["simulate-binance-connection", "--scenario", "normal", "--seed", "42",
                "--ack-id-strategy", "STRING"]
        first, second = self.assert_success(invoke(args)), self.assert_success(invoke(args))
        self.assertEqual(first, second)
        self.assertEqual(first["ack_id_strategy"], "STRING")
        self.assertEqual(first["state"], "ACTIVE")

    def test_directory_limit_is_explicit_and_not_a_live_measurement(self):
        args = self.validate_args("exchange_info", "exchange_info.json")
        result = self.assert_success(invoke(args + ["--exchange-info-max-bytes", "9000000"]))
        self.assertEqual(result["exchange_info_max_bytes"], 9000000)
        self.assertFalse(result["exchange_info_preflight_executed"])
        for bad in ("0", "-1", "999999999", "1"):
            with self.subTest(limit=bad):
                result = invoke(args + ["--exchange-info-max-bytes", bad])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(json.loads(result.stdout)["real_send"])
