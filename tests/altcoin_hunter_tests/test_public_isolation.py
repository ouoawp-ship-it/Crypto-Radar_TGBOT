"""The opt-in public entry points retain zero-I/O imports and old-domain isolation."""
from pathlib import Path
import subprocess
import sys
import unittest


class PublicImportIsolationTests(unittest.TestCase):
    def test_all_new_imports_do_not_load_network_clients_or_read_configuration(self):
        script = r'''
import builtins, importlib, importlib.abc, os, pathlib, socket, sqlite3, sys, threading
from unittest.mock import patch
class BlockClients(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'requests','httpx','aiohttp','websocket','websockets','telegram','ccxt'} or fullname in {'urllib.request','http.client','shared.telegram','config.settings'}:
            raise AssertionError('forbidden import: ' + fullname)
        return None
sys.meta_path.insert(0, BlockClients())
def deny(*args, **kwargs): raise AssertionError('import IO forbidden')
def audit(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','sqlite3.connect','os.mkdir','os.remove','os.rename'}:
        raise AssertionError('side effect: ' + event)
    if event == 'open':
        mode, flags = args[1] or '', args[2] if len(args) > 2 else 0
        if any(c in str(mode) for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError('write forbidden')
sys.addaudithook(audit)
with patch.object(builtins,'open',deny), patch.object(os,'getenv',deny), patch.object(sqlite3,'connect',deny), patch.object(socket,'socket',deny), patch.object(threading.Thread,'start',deny), patch.object(pathlib.Path,'mkdir',deny):
    for name in ('radars.altcoin_hunter.smoke_policy', 'radars.altcoin_hunter.public_paths',
                 'runtime.altcoin_hunter_transport', 'runtime.altcoin_hunter_smoke',
                 'runtime.altcoin_hunter_public'):
        importlib.import_module(name)
assert not any(name.startswith(('shared.', 'config.settings')) for name in sys.modules)
print('network=0 dns=0 telegram=0 database=0 writes=0')
'''
        result = subprocess.run([sys.executable, "-B", "-c", script],
                                cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("network=0 dns=0 telegram=0 database=0 writes=0", result.stdout)
