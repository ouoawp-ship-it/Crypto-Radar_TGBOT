"""Read-only temporary-root selection; no public transport is used."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from radars.altcoin_hunter import public_paths


class PublicTemporaryPathTests(unittest.TestCase):
    def test_import_does_not_lookup_environment_files_or_create_probe(self):
        with patch.object(os.environ, "get") as environment, patch.object(Path, "lstat") as metadata, \
                patch.object(os, "open") as opening, patch.object(os, "mkdir") as mkdir, \
                patch.object(tempfile, "gettempdir") as probing:
            importlib.reload(public_paths)
        for operation in (environment, metadata, opening, mkdir, probing):
            operation.assert_not_called()

    def test_existing_directory_selected_without_write_probe_or_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = tuple(root.iterdir())
            with patch.object(public_paths, "_configured_temporary_path", return_value=temporary), \
                    patch.object(tempfile, "gettempdir") as probing, patch.object(os, "open") as opening, \
                    patch.object(os, "mkdir") as mkdir:
                self.assertEqual(public_paths.safe_temporary_root(), root)
            for operation in (probing, opening, mkdir):
                operation.assert_not_called()
            self.assertEqual(tuple(root.iterdir()), before)

    def test_repository_root_and_children_rejected_before_filesystem_lookup(self):
        repository = Path(public_paths.__file__).absolute().parents[2]
        for candidate in (repository, repository / "runtime", repository / "must-not-create"):
            with self.subTest(candidate=candidate), \
                    patch.object(public_paths, "_configured_temporary_path", return_value=str(candidate)), \
                    patch.object(Path, "lstat") as metadata, patch.object(os, "open") as opening, \
                    patch.object(os, "mkdir") as mkdir:
                with self.assertRaisesRegex(ValueError, "repository_temporary_root_forbidden"):
                    public_paths.safe_temporary_root()
                metadata.assert_not_called()
                opening.assert_not_called()
                mkdir.assert_not_called()

    def test_unsafe_spelling_is_rejected_before_any_path_lookup(self):
        for value in ("", "relative-temp", r"\\server\share\tmp", "//server/share/tmp",
                      r"\\?\UNC\server\share\tmp", "https://host/tmp", "/tmp:data", "/tmp\n", "/tmp/../etc"):
            with self.subTest(value=value), \
                    patch.object(public_paths, "_configured_temporary_path", return_value=value), \
                    patch.object(Path, "lstat") as metadata, patch.object(os, "open") as opening:
                with self.assertRaises(ValueError):
                    public_paths.safe_temporary_root()
                metadata.assert_not_called()
                opening.assert_not_called()

    def test_missing_or_regular_file_root_is_not_created_or_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "file"
            regular.write_text("unchanged", encoding="utf-8")
            for candidate, error in ((regular, ValueError), (root / "missing", OSError)):
                with self.subTest(candidate=candidate), \
                        patch.object(public_paths, "_configured_temporary_path", return_value=str(candidate)), \
                        patch.object(os, "mkdir") as mkdir, patch.object(os, "open") as opening:
                    with self.assertRaises(error):
                        public_paths.safe_temporary_root()
                    mkdir.assert_not_called()
                    opening.assert_not_called()
            self.assertEqual(regular.read_text(encoding="utf-8"), "unchanged")

    def test_symlink_or_reparse_component_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            original = Path.lstat
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
                def fake_lstat(path, *, follow_symlinks=True):
                    if path == root:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(path)
                with self.subTest(mode=mode), \
                        patch.object(public_paths, "_configured_temporary_path", return_value=str(child)), \
                        patch.object(Path, "lstat", fake_lstat), patch.object(os, "open") as opening:
                    with self.assertRaisesRegex(ValueError, "linked_temporary_root_forbidden"):
                        public_paths.safe_temporary_root()
                    opening.assert_not_called()

    def test_windows_standard_variable_precedence_without_filesystem_probe(self):
        cases = (({"TEMP": r"C:\chosen", "TMP": r"C:\second", "LOCALAPPDATA": r"C:\local"}, r"C:\chosen"),
                 ({"TMP": r"C:\second", "LOCALAPPDATA": r"C:\local"}, r"C:\second"),
                 ({"LOCALAPPDATA": r"C:\local"}, r"C:\local\Temp"))
        for environment, expected in cases:
            with self.subTest(environment=environment), patch.object(public_paths.os, "name", "nt"), \
                    patch.dict(os.environ, environment, clear=True), patch.object(tempfile, "gettempdir") as probing:
                self.assertEqual(public_paths._configured_temporary_path(), expected)
                probing.assert_not_called()
        with patch.object(public_paths.os, "name", "nt"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "temporary_root_unavailable"):
                public_paths._configured_temporary_path()

    def test_posix_variable_or_literal_fallback_does_not_probe(self):
        for environment, expected in (({"TMPDIR": "/selected"}, "/selected"), ({}, "/tmp")):
            with self.subTest(environment=environment), patch.object(public_paths.os, "name", "posix"), \
                    patch.dict(os.environ, environment, clear=True), patch.object(tempfile, "gettempdir") as probing:
                self.assertEqual(public_paths._configured_temporary_path(), expected)
                probing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
