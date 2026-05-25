from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.storage import JsonStore


class JsonStoreTests(unittest.TestCase):
    def test_save_and_load_json(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            path = Path(tmp) / "state.json"

            store.save(path, {"symbol": "BTCUSDT", "count": 2})

            self.assertEqual(store.load(path, {}), {"symbol": "BTCUSDT", "count": 2})

    def test_corrupt_json_is_renamed_and_default_returned(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            path = Path(tmp) / "state.json"
            path.write_text("{bad json", encoding="utf-8")

            self.assertEqual(store.load(path, {"ok": False}), {"ok": False})
            self.assertFalse(path.exists())
            self.assertTrue(list(Path(tmp).glob("state.json.corrupt.*")))


if __name__ == "__main__":
    unittest.main()
