"""Public entry-point safety tests: all runners and worker processes are fake."""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from runtime import altcoin_hunter_public as cli
from radars.altcoin_hunter.smoke_policy import FIRST_SMOKE_SYMBOLS, SmokeConfig
from .test_public_smoke_policy import first_report


def invoke(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = cli.main(arguments)
    return code, json.loads(output.getvalue())


class FakeProcess:
    """Do not create a subprocess or call its potentially live target."""
    def __init__(self, *, target, args, daemon, body=None, exitcode=0,
                 hanging=False, ignore_terminate=False):
        self.target, self.args, self.daemon = target, args, daemon
        self.body, self.exitcode = body, exitcode
        self.alive = hanging
        self.ignore_terminate = ignore_terminate
        self.started = self.closed = False
        self.terminations = self.kills = 0
        self.joins = []

    def start(self):
        self.started = True
        if self.body is not None:
            (Path(self.args[1]) / cli.WORKER_RESULT_NAME).write_bytes(self.body)

    def is_alive(self):
        return self.alive

    def join(self, *, timeout):
        self.joins.append(timeout)

    def terminate(self):
        self.terminations += 1
        if not self.ignore_terminate:
            self.alive = False

    def kill(self):
        self.kills += 1
        self.alive = False

    def close(self):
        self.closed = True


class PublicSmokeCliTests(unittest.TestCase):
    def test_temporary_root_wrapper_uses_read_only_selector_not_gettempdir_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("radars.altcoin_hunter.public_paths.safe_temporary_root", return_value=root) as selector, \
                    patch.object(cli.tempfile, "gettempdir") as probing:
                self.assertEqual(cli._temporary_root(), root)
                selector.assert_called_once_with()
                probing.assert_not_called()

    def test_unsafe_temporary_root_rejection_never_allocates_output(self):
        with patch("radars.altcoin_hunter.public_paths.safe_temporary_root", side_effect=ValueError("repository_temporary_root_forbidden")), \
                patch.object(cli.tempfile, "gettempdir") as probing, patch.object(cli.tempfile, "mkdtemp") as create, \
                patch.object(cli, "_run_guarded") as run:
            code, result = invoke(["smoke", "--enable-public-read"])
        self.assertEqual(code, 2)
        self.assertEqual(result["error_code"], "invalid_temporary_root")
        for operation in (probing, create, run):
            operation.assert_not_called()

    def test_disabled_entrypoint_never_reads_proof_creates_output_or_starts_worker(self):
        with patch.object(cli, "_temporary_root") as root, patch.object(cli, "_read_temporary_json") as read, \
                patch.object(cli.tempfile, "mkdtemp") as create, patch.object(cli, "_run_guarded") as run:
            for args in (["smoke"], ["smoke", "--tier", "expanded", "--first-success-report", "PRIVATE"]):
                code, result = invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(result["error_code"], "public_smoke_disabled")
            root.assert_not_called()
            read.assert_not_called()
            create.assert_not_called()
            run.assert_not_called()

    def test_help_has_no_worker_output_or_directory_side_effect(self):
        with patch.object(cli, "_temporary_root") as root, patch.object(cli, "_run_guarded") as run, \
                patch.object(cli.tempfile, "mkdtemp") as create, redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            cli.main(["--help"])
        self.assertEqual(stop.exception.code, 0)
        root.assert_not_called()
        run.assert_not_called()
        create.assert_not_called()

    def test_import_does_not_create_files_threads_network_or_database(self):
        import threading
        with patch.object(socket, "socket") as sockets, patch.object(socket, "getaddrinfo") as dns, \
                patch.object(sqlite3, "connect") as database, patch.object(threading.Thread, "start") as thread, \
                patch.object(cli.tempfile, "mkdtemp") as output, patch.object(cli.os, "open") as files:
            importlib.reload(cli)
        for mocked in (sockets, dns, database, thread, output, files):
            mocked.assert_not_called()

    def test_only_smoke_and_declared_flags_are_accepted_without_echoing_input(self):
        secret = "private-token-never-echo"
        for args in ([], ["live"], ["connect"], ["send"], ["daemon"],
                     ["smoke", "--enable-public"],
                     ["smoke", "--db", secret], ["smoke", "--output", secret],
                     ["smoke", "--env", secret], ["smoke", "--duration-sec", secret],
                     ["smoke", "--tier", secret]):
            with self.subTest(args=args), patch.object(cli, "_run_guarded") as run, \
                    patch.object(cli.tempfile, "mkdtemp") as create:
                code, result = invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(result["error_code"], "invalid_cli_arguments")
                self.assertNotIn(secret, json.dumps(result))
                run.assert_not_called()
                create.assert_not_called()

    def test_enabled_first_run_gets_only_fresh_temporary_output_and_fixed_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = first_report()
            with patch.object(cli, "_temporary_root", return_value=root), \
                    patch.object(cli, "_run_guarded", return_value=report) as run, \
                    patch.object(socket, "socket") as sockets, patch.object(socket, "getaddrinfo") as dns, \
                    patch.object(sqlite3, "connect") as database:
                code, result = invoke(["smoke", "--enable-public-read"])
            self.assertEqual(code, 0)
            config, output_dir = run.call_args.args
            self.assertTrue(config.enable)
            self.assertEqual(set(config.symbols), set(FIRST_SMOKE_SYMBOLS))
            self.assertEqual(config.duration_sec, 180)
            self.assertEqual(output_dir.parent, root)
            self.assertTrue(output_dir.name.startswith("altcoin-hunter-smoke-"))
            self.assertEqual(list(output_dir.iterdir()), [])  # runner owns report.json
            self.assertEqual(result["report_path"], str(output_dir / "report.json"))
            self.assertEqual(result["result"], report)  # proof digest is not rewritten
            for mocked in (sockets, dns, database):
                mocked.assert_not_called()

    def test_policy_failure_does_not_allocate_output(self):
        for extra in (["--duration-sec", "181"], ["--duration-sec", "601"],
                      ["--symbols", "BTCUSDT"], ["--symbols", "BTCUSDT,BTCUSDT"],
                      ["--tier", "expanded"]):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(cli, "_temporary_root", return_value=Path(temporary)), \
                    patch.object(cli.tempfile, "mkdtemp") as create, patch.object(cli, "_run_guarded") as run:
                code, result = invoke(["smoke", "--enable-public-read", *extra])
                self.assertEqual(code, 2)
                self.assertEqual(result["error_code"], "invalid_smoke_configuration")
                create.assert_not_called()
                run.assert_not_called()

    def test_expanded_run_loads_only_bounded_existing_first_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            proof = root / "report.json"
            original = json.dumps(first_report()).encode()
            proof.write_bytes(original)
            with patch.object(cli, "_temporary_root", return_value=root), \
                    patch.object(cli, "_run_guarded", return_value={"status": "passed"}) as run:
                code, _ = invoke(["smoke", "--enable-public-read", "--tier", "expanded", "--symbols", "BTCUSDT",
                                  "--duration-sec", "600", "--first-success-report", str(proof)])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[0].duration_sec, 600)
            self.assertIsNotNone(run.call_args.args[0].first_success_digest)
            self.assertEqual(proof.read_bytes(), original)

    def test_nonlocal_or_relative_proof_is_rejected_before_stat_or_open(self):
        temporary_root = Path(tempfile.gettempdir())
        for path in (r"\\server\share\x.json", "//server/share/x.json", r"\\?\UNC\server\x.json",
                     "https://host/report.json", "report.json", "/tmp/report.json:stream"):
            with self.subTest(path=path), patch.object(Path, "lstat") as metadata, patch.object(cli.os, "open") as opening:
                with self.assertRaises(cli.CliError):
                    cli._read_temporary_json(path, temporary_root)
                metadata.assert_not_called()
                opening.assert_not_called()

    def test_report_must_be_under_temp_and_regular(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(cli.CliError, "outside_temporary_root"):
                cli._read_temporary_json(root.parent / "outside.json", root)
            directory = root / "directory"
            directory.mkdir()
            with self.assertRaisesRegex(cli.CliError, "regular_file"):
                cli._read_temporary_json(directory, root)
            with self.assertRaises(OSError):
                cli._read_temporary_json(root / "missing.json", root)

    def test_symlink_and_reparse_ancestors_are_rejected_without_opening(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "sub"
            parent.mkdir()
            report = parent / "report.json"
            report.write_text("{}", encoding="utf-8")
            original = Path.lstat
            for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
                def fake_lstat(path, *, follow_symlinks=True):
                    if path == parent:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(path)
                with self.subTest(mode=mode), patch.object(Path, "lstat", fake_lstat), patch.object(cli.os, "open") as opening:
                    with self.assertRaisesRegex(cli.CliError, "linked_path_forbidden"):
                        cli._read_temporary_json(report, root)
                    opening.assert_not_called()

    def test_report_size_is_checked_before_open_and_after_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            report.write_bytes(b" " * (cli.MAX_REPORT_BYTES + 1))
            with patch.object(cli.os, "open") as opening, self.assertRaisesRegex(cli.CliError, "report_size_limit"):
                cli._read_temporary_json(report, root)
            opening.assert_not_called()
            actual = report.stat()
            smaller = SimpleNamespace(st_mode=actual.st_mode, st_size=1, st_dev=actual.st_dev,
                                      st_ino=actual.st_ino, st_file_attributes=0)
            with patch.object(cli, "_no_links", return_value=smaller), \
                    self.assertRaisesRegex(cli.CliError, "report_size_limit"):
                cli._read_temporary_json(report, root)

    def test_report_json_rejects_duplicates_nonfinite_invalid_and_nonobject(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "report.json"
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'false', b'\xff', b'{'):
                with self.subTest(raw=raw):
                    report.write_bytes(raw)
                    with self.assertRaisesRegex(cli.CliError, "invalid_report_json"):
                        cli._read_temporary_json(report, root)

    def test_proof_read_failure_is_safe_and_does_not_start_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(cli, "_temporary_root", return_value=Path(temporary)), \
                    patch.object(cli, "_read_temporary_json", side_effect=OSError("secret path credentials")), \
                    patch.object(cli.tempfile, "mkdtemp") as create, patch.object(cli, "_run_guarded") as run:
                code, result = invoke(["smoke", "--enable-public-read", "--tier", "expanded",
                                      "--first-success-report", str(Path(temporary) / "missing.json")])
            self.assertEqual(code, 2)
            self.assertNotIn("secret", json.dumps(result))
            create.assert_not_called()
            run.assert_not_called()

    def test_runner_failure_is_not_reported_as_passed_and_preserves_output_location(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(cli, "_temporary_root", return_value=Path(temporary)), \
                patch.object(cli, "_run_guarded", return_value={"status": "failed", "error_code": "protocol_incompatible"}):
            code, result = invoke(["smoke", "--enable-public-read"])
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "failed")
            self.assertIn("report_path", result)

    def test_watchdog_success_passes_absolute_deadline_to_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            made = []
            def factory(**kwargs):
                process = FakeProcess(**kwargs, body=b'{"status":"passed"}')
                made.append(process)
                return process
            with patch.object(cli.time, "monotonic_ns", return_value=1000):
                result = cli._run_guarded(SmokeConfig(enable=True), Path(temporary), process_factory=factory)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(made[0].args[2], 180_000_001_000)
            self.assertTrue(made[0].daemon)
            self.assertTrue(made[0].closed)
            self.assertEqual(made[0].terminations, 0)

    def test_watchdog_deadline_terminates_without_trusting_existing_success_report(self):
        for ignore_terminate in (False, True):
            with self.subTest(ignore=ignore_terminate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = root / "report.json"
                report.write_text('{"status":"passed"}', encoding="utf-8")
                made = []
                def factory(**kwargs):
                    process = FakeProcess(**kwargs, body=b'{"status":"passed"}', hanging=True,
                                          ignore_terminate=ignore_terminate)
                    made.append(process)
                    return process
                with patch.object(cli.time, "monotonic_ns", side_effect=(0, 180_000_000_001)):
                    result = cli._run_guarded(SmokeConfig(enable=True), root, process_factory=factory)
                self.assertEqual(result["error_code"], "smoke_hard_deadline")
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["worker_cleanup_complete"])
                self.assertEqual(made[0].terminations, 1)
                self.assertEqual(made[0].kills, int(ignore_terminate))
                self.assertTrue(made[0].closed)
                self.assertTrue(all(timeout <= 0.25 for timeout in made[0].joins))
                self.assertEqual(report.read_text(encoding="utf-8"), '{"status":"passed"}')

    def test_watchdog_worker_failure_or_invalid_result_cannot_pass(self):
        for body, exitcode, expected in ((None, 1, "public_smoke_worker_failed"),
                                        (None, 0, "invalid_worker_result"),
                                        (b"invalid", 0, "invalid_worker_result"),
                                        (b" " * (cli.MAX_REPORT_BYTES + 1), 0, "invalid_worker_result")):
            with self.subTest(exitcode=exitcode, expected=expected), tempfile.TemporaryDirectory() as temporary:
                factory = lambda **kwargs: FakeProcess(**kwargs, body=body, exitcode=exitcode)
                with patch.object(cli.time, "monotonic_ns", return_value=0):
                    result = cli._run_guarded(SmokeConfig(enable=True), Path(temporary), process_factory=factory)
                self.assertEqual(result["error_code"], expected)

    def test_worker_uses_lazy_runner_and_bounded_local_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = SmokeConfig(enable=True)
            runner = Mock(return_value={"status": "passed"})
            module = SimpleNamespace(run_public_smoke=runner)
            with patch.dict(cli.sys.modules, {"runtime.altcoin_hunter_smoke": module}), \
                    patch.object(socket, "socket") as sockets, patch.object(socket, "getaddrinfo") as dns, \
                    patch.object(sqlite3, "connect") as database:
                cli._smoke_worker(config, temporary, 123)
            runner.assert_called_once_with(config, output_dir=Path(temporary), deadline_ns=123)
            self.assertEqual(json.loads((Path(temporary) / cli.WORKER_RESULT_NAME).read_text()), {"status": "passed"})
            for mocked in (sockets, dns, database):
                mocked.assert_not_called()

    def test_worker_exception_or_oversized_result_never_leaks_raw_details(self):
        for failure in (True, False):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                runner = Mock(side_effect=ValueError("Authorization secret private-url") if failure else None,
                              return_value={"status": "passed", "payload": "x" * cli.MAX_REPORT_BYTES})
                with patch.dict(cli.sys.modules, {"runtime.altcoin_hunter_smoke": SimpleNamespace(run_public_smoke=runner)}):
                    cli._smoke_worker(SmokeConfig(enable=True), temporary, 123)
                body = (Path(temporary) / cli.WORKER_RESULT_NAME).read_text()
                self.assertNotIn("Authorization", body)
                self.assertNotIn("private-url", body)
                self.assertEqual(json.loads(body)["error_code"], "public_smoke_failed")


if __name__ == "__main__":
    unittest.main()
