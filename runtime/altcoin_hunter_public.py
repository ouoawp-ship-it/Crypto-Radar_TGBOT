"""Explicit, bounded public-read smoke entry point, separate from offline CLI.

Import, help and disabled invocations create neither output nor worker processes.
Only a freshly allocated local temporary directory can receive smoke output.
The spawned worker has an absolute parent deadline, including network setup.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import tempfile
import time
from typing import Any


MAX_REPORT_BYTES = 65_536
WORKER_RESULT_NAME = "cli-result.json"
FIRST_SYMBOLS = "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"


class CliError(ValueError):
    """Only locally assigned stable codes cross the CLI error boundary."""


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        # argparse's normal message may echo arbitrary user-provided values.
        raise CliError("invalid_cli_arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Altcoin Hunter bounded public-read smoke; disabled by default")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    smoke = commands.add_parser("smoke", help="explicitly authorize a temporary public-data smoke")
    smoke.add_argument("--enable-public-read", action="store_true")
    smoke.add_argument("--tier", choices=("first", "expanded"), default="first")
    smoke.add_argument("--symbols", default=FIRST_SYMBOLS, help="comma-separated uppercase symbols")
    smoke.add_argument("--duration-sec", type=int, default=180)
    smoke.add_argument("--first-success-report", help="existing first-run report under local temporary storage")
    return parser


def _local_absolute_path(value: str | Path) -> Path:
    raw = os.fspath(value)
    if (type(raw) is not str or not raw or len(raw) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise CliError("invalid_local_path")
    windows = PureWindowsPath(raw)
    if (raw.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\")
            or "://" in raw or ":" in raw[len(windows.drive):]):
        raise CliError("nonlocal_path_forbidden")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise CliError("absolute_local_path_required")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) not in (2, 3, 5, 6):
            raise CliError("nonlocal_path_forbidden")
    return path


def _no_links(path: Path) -> os.stat_result:
    last = None
    for component in (*reversed(path.parents), path):
        last = component.lstat()
        if stat.S_ISLNK(last.st_mode) or getattr(last, "st_file_attributes", 0) & 0x400:
            raise CliError("linked_path_forbidden")
    assert last is not None
    return last


def _temporary_root() -> Path:
    from radars.altcoin_hunter.public_paths import safe_temporary_root
    try:
        return safe_temporary_root()
    except (ValueError, OSError) as exc:
        raise CliError("invalid_temporary_root") from exc


def _read_temporary_json(value: str | Path, temporary_root: Path) -> dict[str, Any]:
    path = _local_absolute_path(value)
    if path == temporary_root or temporary_root not in path.parents:
        raise CliError("report_outside_temporary_root")
    before = _no_links(path)
    if not stat.S_ISREG(before.st_mode):
        raise CliError("report_must_be_regular_file")
    if before.st_size > MAX_REPORT_BYTES:
        raise CliError("report_size_limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode)
                or getattr(opened, "st_file_attributes", 0) & 0x400
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise CliError("report_changed_before_read")
        _no_links(path)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(MAX_REPORT_BYTES + 1)
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(raw) > MAX_REPORT_BYTES:
        raise CliError("report_size_limit")

    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise CliError("invalid_report_json")
            result[key] = child
        return result

    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(CliError("invalid_report_json")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CliError("invalid_report_json") from exc
    if type(result) is not dict:
        raise CliError("invalid_report_json")
    return result


def _failure(code: str) -> dict[str, Any]:
    return {"status": "failed", "mode": "public_read_only_smoke", "real_send": False,
            "error_code": code}


def _smoke_worker(config, output_dir: str, deadline_ns: int) -> None:
    # Spawn target: no real transport can be imported by a disabled invocation.
    try:
        from runtime.altcoin_hunter_smoke import run_public_smoke
        result = run_public_smoke(config, output_dir=Path(output_dir), deadline_ns=deadline_ns)
        if type(result) is not dict:
            raise ValueError("invalid_worker_result")
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("ascii")
        if len(encoded) > MAX_REPORT_BYTES:
            raise ValueError("worker_result_limit")
    except Exception:
        encoded = json.dumps(_failure("public_smoke_failed"), sort_keys=True).encode("ascii")
    result_path = Path(output_dir) / WORKER_RESULT_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(result_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)


def _stop_worker(process) -> bool:
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.25)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.25)
    return not process.is_alive()


def _run_guarded(config, output_dir: Path, *, process_factory=None) -> dict[str, Any]:
    """Parent never blocks reading a pipe whose child may stall halfway through."""
    deadline_ns = config.deadline_ns(time.monotonic_ns())
    if process_factory is None:
        import multiprocessing
        process_factory = multiprocessing.get_context("spawn").Process
    process = process_factory(target=_smoke_worker,
                              args=(config, str(output_dir), deadline_ns), daemon=True)
    started = False
    try:
        process.start()
        started = True
        while process.is_alive():
            remaining = (deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining <= 0:
                result = _failure("smoke_hard_deadline")
                result["worker_cleanup_complete"] = _stop_worker(process)
                return result
            process.join(timeout=min(0.25, remaining))
        if time.monotonic_ns() > deadline_ns:
            return _failure("smoke_hard_deadline")
        if process.exitcode != 0:
            return _failure("public_smoke_worker_failed")
        try:
            return _read_temporary_json(output_dir / WORKER_RESULT_NAME, output_dir)
        except (ValueError, OSError):
            return _failure("invalid_worker_result")
    finally:
        if started:
            if process.is_alive():
                _stop_worker(process)
            if not process.is_alive():
                process.close()
        else:
            process.close()


def main(argv: list[str] | None = None) -> int:
    output_dir = None
    try:
        args = build_parser().parse_args(argv)
        if not args.enable_public_read:
            raise CliError("public_smoke_disabled")
        from radars.altcoin_hunter.smoke_policy import SmokeConfig
        temporary_root = _temporary_root()
        proof = (_read_temporary_json(args.first_success_report, temporary_root)
                 if args.first_success_report else None)
        try:
            config = SmokeConfig(enable=True, tier=args.tier,
                                 symbols=tuple(args.symbols.split(",")), duration_sec=args.duration_sec,
                                 first_success_proof=proof)
        except ValueError as exc:
            raise CliError("invalid_smoke_configuration") from exc
        output_dir = Path(tempfile.mkdtemp(prefix="altcoin-hunter-smoke-", dir=temporary_root))
        result = _run_guarded(config, output_dir)
        # Locations belong to the CLI wrapper, not to the integrity-digested proof.
        output = {"status": "passed" if result.get("status") == "passed" else "failed",
                  "mode": "public_read_only_smoke", "real_send": False,
                  "output_dir": str(output_dir), "report_path": str(output_dir / "report.json"),
                  "result": result}
    except CliError as exc:
        output = _failure(str(exc))
    except KeyboardInterrupt:
        output = _failure("public_smoke_interrupted")
    except Exception:
        output = _failure("public_smoke_failed")
    if output_dir is not None:
        output.setdefault("output_dir", str(output_dir))
        output.setdefault("report_path", str(output_dir / "report.json"))
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, allow_nan=False))
    return 0 if output["status"] == "passed" else 2


if __name__ == "__main__":
    sys.exit(main())
