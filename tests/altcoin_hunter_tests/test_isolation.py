"""Behavioral isolation checks in clean child interpreters, never production IO."""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "configuration", "models", "identity", "universe", "aggregation", "windows",
    "baselines", "quality", "storage", "read_model", "replay", "migrations",
)


class IsolationTests(unittest.TestCase):
    def test_all_public_imports_have_zero_runtime_side_effects(self):
        # Preload only the standard library so the guard distinguishes domain
        # startup from Python's import machinery. -B forbids bytecode writes.
        script = r'''
import builtins, importlib, importlib.abc, os, pathlib, socket, sqlite3, sys, threading
from unittest.mock import patch
class BlockClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'requests','httpx','aiohttp','websocket','websockets','telegram','ccxt'} or fullname in {'urllib.request','http.client','shared.telegram','config.settings'}:
            raise AssertionError('forbidden dependency: ' + fullname)
        return None
sys.meta_path.insert(0, BlockClients())
def deny(*args, **kwargs):
    raise AssertionError('import performed runtime IO')
def audit(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','sqlite3.connect','os.mkdir','os.remove','os.rename'}:
        raise AssertionError('import performed side effect: ' + event)
    if event == 'open':
        mode = args[1] or ''
        flags = args[2] if len(args) > 2 else 0
        if any(c in str(mode) for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError('import wrote a file')
sys.addaudithook(audit)
with patch.object(builtins, 'open', deny), patch.object(os, 'getenv', deny), patch.object(sqlite3, 'connect', deny), patch.object(socket, 'socket', deny), patch.object(threading.Thread, 'start', deny), patch.object(pathlib.Path, 'mkdir', deny):
    for name in MODULES:
        importlib.import_module('radars.altcoin_hunter.' + name)
    importlib.import_module('runtime.altcoin_hunter')
assert not any(n.startswith(('shared.telegram', 'shared.realtime_market', 'shared.signal_store')) for n in sys.modules)
'''.replace("MODULES", repr(MODULES))
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_domain_never_imports_legacy_runtime_or_network_clients(self):
        forbidden = ("shared", "config.settings", "runtime.cli", "requests", "aiohttp", "httpx",
                     "websocket", "websockets", "telegram", "ccxt", "urllib.request", "http.client")
        paths = list((ROOT / "radars" / "altcoin_hunter").rglob("*.py"))
        paths.append(ROOT / "runtime" / "altcoin_hunter.py")
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imports = [n.name for n in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
                for name in imports:
                    with self.subTest(path=path.name, imported=name):
                        self.assertFalse(any(name == item or name.startswith(item + ".") for item in forbidden))


if __name__ == "__main__":
    unittest.main()
