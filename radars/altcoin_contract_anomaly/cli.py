from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from config import Settings
from shared.atomic_json import atomic_write_text
from shared.cmc_data import CmcClientError

from .configuration import (
    AltcoinAnomalyConfig,
    AltcoinAnomalyConfigError,
    validate_output_path,
)
from .formatter import render_console, render_json, render_telegram_preview
from .mapping import MappingConfigError
from .radar import AltcoinAnomalyDataUnavailable, load_cached_pool, scan_candidate_pool
from .state import CandidateStatePartialUpdateError, CandidateStateSchemaError


EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_DATA_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130


def _safe_error(prefix: str, detail: str = "") -> None:
    suffix = f"：{detail}" if detail else ""
    print(f"{prefix}{suffix}", file=sys.stderr)


def _realtime_duration(args: Any) -> int | None:
    value = getattr(args, "realtime_duration_sec", None)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 30 <= value <= 3_600
    ):
        raise AltcoinAnomalyConfigError("P2 Dry-run时长必须在30到3600秒之间")
    conflicts = (
        ("cache_only", "--cache-only"),
        ("preview_telegram", "--preview-telegram"),
        ("send", "--send"),
        ("confirm_real_send", "--confirm-real-send"),
    )
    selected = [
        flag
        for attribute, flag in conflicts
        if bool(getattr(args, attribute, False))
    ]
    if selected:
        raise AltcoinAnomalyConfigError(
            "P2实时确认Dry-run不能与以下参数同时使用：" + "、".join(selected)
        )
    return value


def _emit_realtime_result(args: Any, result: dict[str, Any]) -> None:
    machine_output = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    output_path = getattr(args, "output", None)
    if output_path:
        atomic_write_text(Path(output_path), machine_output + "\n")
    print(machine_output)


def run_altcoin_anomaly_cli(args: Any, *, settings: Settings) -> int:
    cache_only = bool(getattr(args, "cache_only", False))
    try:
        validate_output_path(settings, getattr(args, "output", None))
        realtime_duration_sec = _realtime_duration(args)
        if realtime_duration_sec is not None:
            realtime_config = AltcoinAnomalyConfig.from_settings(
                settings,
                realtime=True,
            )
            if realtime_duration_sec > realtime_config.manifest_max_age_sec:
                raise AltcoinAnomalyConfigError(
                    "P2 Dry-run时长不能超过候选Manifest最大允许年龄"
                )
            from .realtime import run_realtime_confirmation_session

            result = run_realtime_confirmation_session(
                settings,
                duration_sec=realtime_duration_sec,
            )
            if not isinstance(result, dict):
                raise ValueError("P2实时确认返回值必须是字典")
            exit_code = result.get("exit_code", EXIT_OK)
            if (
                isinstance(exit_code, bool)
                or not isinstance(exit_code, int)
                or exit_code not in {
                    EXIT_OK,
                    EXIT_INTERNAL_ERROR,
                    EXIT_CONFIG_ERROR,
                    EXIT_DATA_UNAVAILABLE,
                    EXIT_INTERRUPTED,
                }
            ):
                raise ValueError("P2实时确认返回了无效退出码")
            _emit_realtime_result(args, result)
            return exit_code
        config = AltcoinAnomalyConfig.from_settings(settings, cache_only=cache_only)
        pool = (
            load_cached_pool(settings)
            if cache_only
            else scan_candidate_pool(settings)
        )
        preview_pages = (
            render_telegram_preview(
                pool,
                max_chars=config.telegram_preview_page_chars,
            )
            if bool(getattr(args, "preview_telegram", False))
            else None
        )
        machine_output = render_json(pool, telegram_pages=preview_pages)
        output_path = getattr(args, "output", None)
        if output_path:
            atomic_write_text(Path(output_path), machine_output + "\n")
        if bool(getattr(args, "json", False)):
            print(machine_output)
        else:
            print(render_console(pool))
            if preview_pages is not None:
                print("\n\nTelegram消息预览（不会发送）：")
                for page in preview_pages:
                    print("\n" + page)
            if output_path:
                print(f"\n机器可读结果已写入：{Path(output_path)}")
        return EXIT_OK
    except (AltcoinAnomalyConfigError, MappingConfigError) as exc:
        _safe_error("山寨合约异动雷达配置错误", str(exc))
        return EXIT_CONFIG_ERROR
    except CmcClientError as exc:
        if exc.kind in {"config_error", "authentication_error", "authorization_error"}:
            _safe_error("CoinMarketCap配置不可用", exc.kind)
            return EXIT_CONFIG_ERROR
        _safe_error("CoinMarketCap数据暂不可用", exc.kind)
        return EXIT_DATA_UNAVAILABLE
    except AltcoinAnomalyDataUnavailable as exc:
        _safe_error("候选池数据不可用", str(exc))
        return EXIT_DATA_UNAVAILABLE
    except CandidateStatePartialUpdateError:
        _safe_error("候选池数据部分缺失，已保留上一份完整快照")
        return EXIT_DATA_UNAVAILABLE
    except CandidateStateSchemaError:
        _safe_error("候选池状态Schema不兼容")
        return EXIT_INTERNAL_ERROR
    except (OSError, ValueError) as exc:
        _safe_error("候选池处理失败", type(exc).__name__)
        return EXIT_INTERNAL_ERROR
    except KeyboardInterrupt:
        if getattr(args, "realtime_duration_sec", None) is None:
            raise
        interrupted = {
            "schema_version": 1,
            "module": "altcoin_contract_anomaly",
            "mode": "realtime_confirmation_dry_run",
            "status": "interrupted",
        }
        _emit_realtime_result(args, interrupted)
        return EXIT_INTERRUPTED
    except Exception as exc:  # pragma: no cover - final process safety boundary
        _safe_error("候选池内部错误", type(exc).__name__)
        return EXIT_INTERNAL_ERROR


__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_DATA_UNAVAILABLE",
    "EXIT_INTERNAL_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "run_altcoin_anomaly_cli",
]
