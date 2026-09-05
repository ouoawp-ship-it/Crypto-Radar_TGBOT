"""No network, databases, threads, credentials or files created by new imports."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("adapters", "adapters.base", "adapters.binance_usdm", "adapters.binance_protocol",
           "adapters.fixtures", "subscription_plan", "connection", "rest_budget", "rest_scheduler", "ingestion")


class BinanceIsolationTests(unittest.TestCase):
    def test_all_new_imports_have_zero_side_effects(self):
        script = r'''
import builtins, importlib, importlib.abc, os, pathlib, socket, sqlite3, sys, threading
from unittest.mock import patch
class BlockClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'requests','httpx','aiohttp','websocket','websockets','telegram','ccxt'} or fullname in {'urllib.request','http.client','shared.telegram','config.settings'}:
            raise AssertionError('forbidden dependency: ' + fullname)
        return None
sys.meta_path.insert(0, BlockClients())
def deny(*args, **kwargs): raise AssertionError('runtime IO attempted')
def audit(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','sqlite3.connect','os.mkdir','os.remove','os.rename'}:
        raise AssertionError('side effect: ' + event)
    if event == 'open':
        mode, flags = args[1] or '', args[2] if len(args) > 2 else 0
        if any(c in str(mode) for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError('file write')
sys.addaudithook(audit)
with patch.object(builtins,'open',deny), patch.object(os,'getenv',deny), patch.object(sqlite3,'connect',deny), patch.object(socket,'socket',deny), patch.object(threading.Thread,'start',deny), patch.object(pathlib.Path,'mkdir',deny):
    for name in MODULES:
        importlib.import_module('radars.altcoin_hunter.' + name)
    importlib.import_module('runtime.altcoin_hunter')
assert not any(name.startswith(('shared.', 'config.settings')) for name in sys.modules)
print('network=0 dns=0 telegram=0 database=0 writes=0')
'''.replace("MODULES", repr(MODULES))
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network=0 dns=0 telegram=0 database=0 writes=0", result.stdout)

    def test_cli_has_no_online_commands(self):
        from runtime.altcoin_hunter import build_parser
        parser = build_parser()
        text = parser.format_help()
        for allowed in ("validate-binance-fixture", "plan-binance-subscriptions", "simulate-binance-connection"):
            self.assertIn(allowed, text)
        action = next(action for action in parser._actions if hasattr(action, "choices") and isinstance(action.choices, dict))
        self.assertTrue({"live", "connect", "smoke", "daemon", "send"}.isdisjoint(action.choices))
