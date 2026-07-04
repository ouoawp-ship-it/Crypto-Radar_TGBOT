from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import load_env_file


class ConfigLoadTests(unittest.TestCase):
    def test_load_env_file_overrides_empty_process_value_with_file_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_ADMIN_TOKEN=file-token\n", encoding="utf-8")

            with patch.dict(os.environ, {"WEB_ADMIN_TOKEN": ""}):
                env = load_env_file(env_path)

                self.assertEqual(env["WEB_ADMIN_TOKEN"], "file-token")
                self.assertEqual(os.environ["WEB_ADMIN_TOKEN"], "file-token")

    def test_load_env_file_preserves_non_empty_process_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_ADMIN_TOKEN=file-token\n", encoding="utf-8")

            with patch.dict(os.environ, {"WEB_ADMIN_TOKEN": "process-token"}):
                env = load_env_file(env_path)

                self.assertEqual(env["WEB_ADMIN_TOKEN"], "file-token")
                self.assertEqual(os.environ["WEB_ADMIN_TOKEN"], "process-token")


if __name__ == "__main__":
    unittest.main()
