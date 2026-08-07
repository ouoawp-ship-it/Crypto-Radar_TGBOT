from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from config import Settings
from shared.atomic_json import atomic_write_text
from shared.cmc_data import CmcClientError

from .configuration import AltcoinAnomalyConfig, AltcoinAnomalyConfigError
from .formatter import render_console, render_json, render_telegram_preview
from .mapping import MappingConfigError
from .radar import AltcoinAnomalyDataUnavailable, load_cached_pool, scan_candidate_pool
from .state import CandidateStatePartialUpdateError, CandidateStateSchemaError


EXIT_OK = 0
EXIT_INTERNAL_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_DATA_UNAVAILABLE = 3


def _safe_error(prefix: str, detail: str = "") -> None:
    suffix = f"：{detail}" if detail else ""
    print(f"{prefix}{suffix}", file=sys.stderr)


def run_altcoin_anomaly_cli(args: Any, *, settings: Settings) -> int:
    cache_only = bool(getattr(args, "cache_only", False))
    try:
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
    except Exception as exc:  # pragma: no cover - final process safety boundary
        _safe_error("候选池内部错误", type(exc).__name__)
        return EXIT_INTERNAL_ERROR


__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_DATA_UNAVAILABLE",
    "EXIT_INTERNAL_ERROR",
    "EXIT_OK",
    "run_altcoin_anomaly_cli",
]
