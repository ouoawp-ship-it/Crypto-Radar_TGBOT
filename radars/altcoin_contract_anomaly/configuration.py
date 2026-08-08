from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
from typing import Any


class AltcoinAnomalyConfigError(ValueError):
    pass


def protected_runtime_paths(settings: Any) -> dict[Path, tuple[str, ...]]:
    """Return every configured runtime file path keyed by its canonical path."""

    discovered: dict[Path, list[str]] = {}
    for name, value in vars(settings).items():
        if not name.endswith("_path") or not isinstance(value, Path):
            continue
        resolved = value.resolve(strict=False)
        discovered.setdefault(resolved, []).append(name)
        if value.suffix.lower() in {".json", ".jsonl"}:
            lock_path = value.with_name(f"{value.name}.lock").resolve(strict=False)
            discovered.setdefault(lock_path, []).append(f"{name}.lock")
        if value.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            for sidecar_suffix in ("-wal", "-shm", "-journal"):
                sidecar = Path(f"{value}{sidecar_suffix}").resolve(strict=False)
                discovered.setdefault(sidecar, []).append(
                    f"{name}{sidecar_suffix}"
                )
    return {
        path: tuple(sorted(names))
        for path, names in discovered.items()
    }


def validate_output_path(settings: Any, output_path: str | Path | None) -> None:
    """Fail before network/state startup when an audit output aliases state."""

    if output_path is None:
        return
    resolved = Path(output_path).resolve(strict=False)
    conflicts = protected_runtime_paths(settings).get(resolved)
    if conflicts:
        raise AltcoinAnomalyConfigError(
            "--output不能覆盖运行数据路径：" + "、".join(conflicts)
        )


_INTEGER_ENV_RANGES = {
    "ALTCOIN_CONTRACT_ANOMALY_CMC_CONNECT_TIMEOUT_SEC": (1, 120),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_READ_TIMEOUT_SEC": (1, 180),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_RETRY": (0, 5),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_BATCH_SIZE": (1, 100),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_CACHE_TTL_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_MAX_DATA_AGE_SEC": (1, 604_800),
    "ALTCOIN_CONTRACT_ANOMALY_CANDIDATE_REFRESH_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_BINANCE_OI_MAX_AGE_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_AGE_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_TELEGRAM_PREVIEW_PAGE_CHARS": (512, 4096),
    "ALTCOIN_CONTRACT_ANOMALY_OI_WORKERS": (1, 16),
    "ALTCOIN_CONTRACT_ANOMALY_OI_REQUEST_BUDGET": (1, 5_000),
    "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_POLL_SEC": (1, 3_600),
    "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_MAX_AGE_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_BATCH_SIZE": (1, 200),
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_ACK_TIMEOUT_SEC": (1, 120),
    "ALTCOIN_CONTRACT_ANOMALY_MAX_STREAMS": (1, 1_024),
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_DATA_MAX_AGE_SEC": (1, 3_600),
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_GAP_SEC": (1, 300),
    "ALTCOIN_CONTRACT_ANOMALY_OI_REFRESH_SEC": (300, 300),
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_MAX_AGE_SEC": (1, 86_400),
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_WORKERS": (1, 16),
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_REQUEST_BUDGET": (1, 5_000),
    "ALTCOIN_CONTRACT_ANOMALY_FEATURE_1M_WINDOW_SEC": (1, 3_600),
    "ALTCOIN_CONTRACT_ANOMALY_FEATURE_5M_WINDOW_SEC": (1, 3_600),
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_BASELINE_BUCKETS": (2, 1_000),
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_SAMPLES": (1, 1_000),
    "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_WINDOWS": (1, 100),
    "ALTCOIN_CONTRACT_ANOMALY_SMOKE_DURATION_SEC": (30, 3_600),
}
_FLOAT_ENV_RANGES = {
    "ALTCOIN_CONTRACT_ANOMALY_CMC_BACKOFF_BASE_SEC": (0.0, 60.0),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_MIN_REQUEST_INTERVAL_SEC": (0.0, 60.0),
    "ALTCOIN_CONTRACT_ANOMALY_MARKET_CAP_MAX_USD": (0.0, 1e15),
    "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MIN_OI_MARKET_CAP_RATIO": (0.0, 100.0),
    "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MAX_FUNDING_RATE": (-1.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_HIGH_LEVERAGE_MIN_OI_MARKET_CAP_RATIO": (0.0, 100.0),
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_MIN_INTERVAL_SEC": (0.0, 60.0),
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_COVERAGE": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_1M_MOVE_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_5M_MOVE_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_EXPANSION_RATIO": (0.0, 100.0),
    "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_BUY_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_SELL_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_OPEN_INTEREST_MOVE_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_POSITIVE_RATE": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_CHANGE_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_LIQUIDATION_MIN_USD": (0.0, 1e15),
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_STALL_RATIO": (0.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_VOLUME_RATIO": (0.0, 100.0),
}

_BOOLEAN_ENV_NAMES = (
    "ALTCOIN_CONTRACT_ANOMALY_ENABLE",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE",
)


def _validate_raw_environment() -> None:
    for name in _BOOLEAN_ENV_NAMES:
        value = os.getenv(name)
        if value and value.strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
            "0",
            "false",
            "no",
            "off",
        }:
            raise AltcoinAnomalyConfigError(f"{name}格式无效")
    for name, (minimum, maximum) in _INTEGER_ENV_RANGES.items():
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError as exc:
            raise AltcoinAnomalyConfigError(f"{name}格式无效") from exc
        if not minimum <= value <= maximum:
            raise AltcoinAnomalyConfigError(f"{name}超出允许范围")
    for name, (minimum, maximum) in _FLOAT_ENV_RANGES.items():
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError as exc:
            raise AltcoinAnomalyConfigError(f"{name}格式无效") from exc
        below_minimum = value <= minimum if name.endswith("MARKET_CAP_MAX_USD") else value < minimum
        if not isfinite(value) or below_minimum or value > maximum:
            raise AltcoinAnomalyConfigError(f"{name}超出允许范围")


@dataclass(frozen=True)
class AltcoinAnomalyConfig:
    enabled: bool
    cmc_api_key: str
    cmc_connect_timeout_sec: float
    cmc_read_timeout_sec: float
    cmc_retry: int
    cmc_backoff_base_sec: float
    cmc_min_request_interval_sec: float
    cmc_batch_size: int
    cmc_cache_ttl_sec: int
    cmc_max_data_age_sec: int
    cmc_cache_path: Path
    candidate_snapshot_path: Path
    mapping_overrides_path: Path
    market_cap_max_usd: float
    short_squeeze_min_ratio: float
    short_squeeze_max_funding_rate: float
    high_leverage_min_ratio: float
    candidate_refresh_sec: int
    binance_oi_max_age_sec: int
    funding_max_age_sec: int
    telegram_preview_page_chars: int
    oi_workers: int
    oi_request_budget: int
    realtime_enabled: bool
    manifest_poll_sec: int
    manifest_max_age_sec: int
    subscription_batch_size: int
    subscription_min_interval_sec: float
    subscription_ack_timeout_sec: int
    max_streams: int
    realtime_data_max_age_sec: int
    funding_max_gap_sec: int
    oi_refresh_sec: int
    realtime_oi_max_age_sec: int
    realtime_oi_workers: int
    realtime_oi_request_budget: int
    feature_1m_window_sec: int
    feature_5m_window_sec: int
    volume_baseline_buckets: int
    volume_min_samples: int
    volume_min_coverage: float
    price_1m_move_ratio: float
    price_5m_move_ratio: float
    volume_expansion_ratio: float
    aggressive_buy_ratio: float
    aggressive_sell_ratio: float
    open_interest_move_ratio: float
    funding_positive_rate: float
    funding_change_ratio: float
    liquidation_min_usd: float
    price_stall_ratio: float
    weakening_volume_ratio: float
    weakening_windows: int
    realtime_state_path: Path
    realtime_event_path: Path
    smoke_duration_sec: int

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        cache_only: bool = False,
        realtime: bool = False,
    ) -> "AltcoinAnomalyConfig":
        _validate_raw_environment()
        config = cls(
            enabled=bool(settings.altcoin_contract_anomaly_enable),
            cmc_api_key=str(settings.altcoin_contract_anomaly_cmc_api_key or "").strip(),
            cmc_connect_timeout_sec=float(settings.altcoin_contract_anomaly_cmc_connect_timeout_sec),
            cmc_read_timeout_sec=float(settings.altcoin_contract_anomaly_cmc_read_timeout_sec),
            cmc_retry=int(settings.altcoin_contract_anomaly_cmc_retry),
            cmc_backoff_base_sec=float(
                settings.altcoin_contract_anomaly_cmc_backoff_base_sec
            ),
            cmc_min_request_interval_sec=float(
                settings.altcoin_contract_anomaly_cmc_min_request_interval_sec
            ),
            cmc_batch_size=int(settings.altcoin_contract_anomaly_cmc_batch_size),
            cmc_cache_ttl_sec=int(settings.altcoin_contract_anomaly_cmc_cache_ttl_sec),
            cmc_max_data_age_sec=int(settings.altcoin_contract_anomaly_cmc_max_data_age_sec),
            cmc_cache_path=Path(settings.altcoin_contract_anomaly_cmc_cache_path),
            candidate_snapshot_path=Path(settings.altcoin_contract_anomaly_candidate_snapshot_path),
            mapping_overrides_path=Path(settings.altcoin_contract_anomaly_mapping_overrides_path),
            market_cap_max_usd=float(settings.altcoin_contract_anomaly_market_cap_max_usd),
            short_squeeze_min_ratio=float(
                settings.altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio
            ),
            short_squeeze_max_funding_rate=float(
                settings.altcoin_contract_anomaly_short_squeeze_max_funding_rate
            ),
            high_leverage_min_ratio=float(
                settings.altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio
            ),
            candidate_refresh_sec=int(settings.altcoin_contract_anomaly_candidate_refresh_sec),
            binance_oi_max_age_sec=int(settings.altcoin_contract_anomaly_binance_oi_max_age_sec),
            funding_max_age_sec=int(settings.altcoin_contract_anomaly_funding_max_age_sec),
            telegram_preview_page_chars=int(
                settings.altcoin_contract_anomaly_telegram_preview_page_chars
            ),
            oi_workers=int(settings.altcoin_contract_anomaly_oi_workers),
            oi_request_budget=int(settings.altcoin_contract_anomaly_oi_request_budget),
            realtime_enabled=bool(
                settings.altcoin_contract_anomaly_realtime_enable
            ),
            manifest_poll_sec=int(
                settings.altcoin_contract_anomaly_manifest_poll_sec
            ),
            manifest_max_age_sec=int(
                settings.altcoin_contract_anomaly_manifest_max_age_sec
            ),
            subscription_batch_size=int(
                settings.altcoin_contract_anomaly_subscription_batch_size
            ),
            subscription_min_interval_sec=float(
                settings.altcoin_contract_anomaly_subscription_min_interval_sec
            ),
            subscription_ack_timeout_sec=int(
                settings.altcoin_contract_anomaly_subscription_ack_timeout_sec
            ),
            max_streams=int(settings.altcoin_contract_anomaly_max_streams),
            realtime_data_max_age_sec=int(
                settings.altcoin_contract_anomaly_realtime_data_max_age_sec
            ),
            funding_max_gap_sec=int(
                settings.altcoin_contract_anomaly_funding_max_gap_sec
            ),
            oi_refresh_sec=int(
                settings.altcoin_contract_anomaly_oi_refresh_sec
            ),
            realtime_oi_max_age_sec=int(
                settings.altcoin_contract_anomaly_realtime_oi_max_age_sec
            ),
            realtime_oi_workers=int(
                settings.altcoin_contract_anomaly_realtime_oi_workers
            ),
            realtime_oi_request_budget=int(
                settings.altcoin_contract_anomaly_realtime_oi_request_budget
            ),
            feature_1m_window_sec=int(
                settings.altcoin_contract_anomaly_feature_1m_window_sec
            ),
            feature_5m_window_sec=int(
                settings.altcoin_contract_anomaly_feature_5m_window_sec
            ),
            volume_baseline_buckets=int(
                settings.altcoin_contract_anomaly_volume_baseline_buckets
            ),
            volume_min_samples=int(
                settings.altcoin_contract_anomaly_volume_min_samples
            ),
            volume_min_coverage=float(
                settings.altcoin_contract_anomaly_volume_min_coverage
            ),
            price_1m_move_ratio=float(
                settings.altcoin_contract_anomaly_price_1m_move_ratio
            ),
            price_5m_move_ratio=float(
                settings.altcoin_contract_anomaly_price_5m_move_ratio
            ),
            volume_expansion_ratio=float(
                settings.altcoin_contract_anomaly_volume_expansion_ratio
            ),
            aggressive_buy_ratio=float(
                settings.altcoin_contract_anomaly_aggressive_buy_ratio
            ),
            aggressive_sell_ratio=float(
                settings.altcoin_contract_anomaly_aggressive_sell_ratio
            ),
            open_interest_move_ratio=float(
                settings.altcoin_contract_anomaly_open_interest_move_ratio
            ),
            funding_positive_rate=float(
                settings.altcoin_contract_anomaly_funding_positive_rate
            ),
            funding_change_ratio=float(
                settings.altcoin_contract_anomaly_funding_change_ratio
            ),
            liquidation_min_usd=float(
                settings.altcoin_contract_anomaly_liquidation_min_usd
            ),
            price_stall_ratio=float(
                settings.altcoin_contract_anomaly_price_stall_ratio
            ),
            weakening_volume_ratio=float(
                settings.altcoin_contract_anomaly_weakening_volume_ratio
            ),
            weakening_windows=int(
                settings.altcoin_contract_anomaly_weakening_windows
            ),
            realtime_state_path=Path(
                settings.altcoin_contract_anomaly_realtime_state_path
            ),
            realtime_event_path=Path(
                settings.altcoin_contract_anomaly_realtime_event_path
            ),
            smoke_duration_sec=int(
                settings.altcoin_contract_anomaly_smoke_duration_sec
            ),
        )
        if realtime or config.realtime_enabled:
            protected_paths = protected_runtime_paths(settings)
            for label, path in (
                ("P2状态文件", config.realtime_state_path),
                ("P2事件文件", config.realtime_event_path),
            ):
                own_field = (
                    "altcoin_contract_anomaly_realtime_state_path"
                    if label == "P2状态文件"
                    else "altcoin_contract_anomaly_realtime_event_path"
                )
                conflict = tuple(
                    name
                    for name in protected_paths.get(path.resolve(strict=False), ())
                    if name != own_field
                )
                if conflict:
                    raise AltcoinAnomalyConfigError(
                        f"{label}不能与现有运行路径冲突：{'、'.join(conflict)}"
                    )
        config.validate(cache_only=cache_only, realtime=realtime)
        return config

    def validate(
        self,
        *,
        cache_only: bool = False,
        realtime: bool = False,
    ) -> None:
        if not cache_only and not self.enabled:
            raise AltcoinAnomalyConfigError("山寨合约异动雷达未启用")
        if realtime and not self.realtime_enabled:
            raise AltcoinAnomalyConfigError("山寨合约异动雷达P2实时确认未启用")
        if not cache_only and not realtime and not self.cmc_api_key:
            raise AltcoinAnomalyConfigError("CMC API Key未配置")
        numeric = (
            self.cmc_connect_timeout_sec,
            self.cmc_read_timeout_sec,
            self.cmc_backoff_base_sec,
            self.cmc_min_request_interval_sec,
            self.market_cap_max_usd,
            self.short_squeeze_min_ratio,
            self.short_squeeze_max_funding_rate,
            self.high_leverage_min_ratio,
        )
        if not all(isfinite(value) for value in numeric):
            raise AltcoinAnomalyConfigError("配置包含非有限数值")
        if not 0.1 <= self.cmc_connect_timeout_sec <= 120:
            raise AltcoinAnomalyConfigError("CMC连接超时超出范围")
        if not 0.1 <= self.cmc_read_timeout_sec <= 180:
            raise AltcoinAnomalyConfigError("CMC读取超时超出范围")
        if not 0 <= self.cmc_retry <= 5:
            raise AltcoinAnomalyConfigError("CMC重试次数超出范围")
        if not 0 <= self.cmc_backoff_base_sec <= 60:
            raise AltcoinAnomalyConfigError("CMC退避基数超出范围")
        if not 0 <= self.cmc_min_request_interval_sec <= 60:
            raise AltcoinAnomalyConfigError("CMC主动限流间隔超出范围")
        if not 1 <= self.cmc_batch_size <= 100:
            raise AltcoinAnomalyConfigError("CMC批大小必须在1到100之间")
        if self.cmc_cache_ttl_sec <= 0 or self.cmc_max_data_age_sec <= 0:
            raise AltcoinAnomalyConfigError("CMC缓存时间必须为正数")
        if self.market_cap_max_usd <= 0:
            raise AltcoinAnomalyConfigError("市值上限必须为正数")
        if not 0 <= self.short_squeeze_min_ratio <= 100:
            raise AltcoinAnomalyConfigError("潜在逼空比例阈值无效")
        if not -1 <= self.short_squeeze_max_funding_rate <= 1:
            raise AltcoinAnomalyConfigError("潜在逼空资金费率阈值无效")
        if not 0 <= self.high_leverage_min_ratio <= 100:
            raise AltcoinAnomalyConfigError("高杠杆比例阈值无效")
        if min(
            self.candidate_refresh_sec,
            self.binance_oi_max_age_sec,
            self.funding_max_age_sec,
            self.oi_request_budget,
        ) <= 0:
            raise AltcoinAnomalyConfigError("刷新、新鲜度和请求预算必须为正数")
        if not 1 <= self.oi_workers <= 16:
            raise AltcoinAnomalyConfigError("OI并发必须在1到16之间")
        if not 512 <= self.telegram_preview_page_chars <= 4096:
            raise AltcoinAnomalyConfigError("Telegram预览分页长度必须在512到4096之间")
        realtime_numeric = (
            self.subscription_min_interval_sec,
            self.volume_min_coverage,
            self.price_1m_move_ratio,
            self.price_5m_move_ratio,
            self.volume_expansion_ratio,
            self.aggressive_buy_ratio,
            self.aggressive_sell_ratio,
            self.open_interest_move_ratio,
            self.funding_positive_rate,
            self.funding_change_ratio,
            self.liquidation_min_usd,
            self.price_stall_ratio,
            self.weakening_volume_ratio,
        )
        if not all(isfinite(value) for value in realtime_numeric):
            raise AltcoinAnomalyConfigError("P2配置包含非有限数值")
        if not 1 <= self.manifest_poll_sec <= 3_600:
            raise AltcoinAnomalyConfigError("候选清单轮询间隔超出范围")
        if not self.manifest_poll_sec <= self.manifest_max_age_sec <= 86_400:
            raise AltcoinAnomalyConfigError("候选清单新鲜度必须不小于轮询间隔")
        if not 1 <= self.subscription_batch_size <= 200:
            raise AltcoinAnomalyConfigError("订阅批大小必须在1到200之间")
        if not 0 <= self.subscription_min_interval_sec <= 60:
            raise AltcoinAnomalyConfigError("订阅最小间隔超出范围")
        if not 1 <= self.subscription_ack_timeout_sec <= 120:
            raise AltcoinAnomalyConfigError("订阅确认超时超出范围")
        if not 1 <= self.max_streams <= 1_024:
            raise AltcoinAnomalyConfigError("实时订阅流数量超出范围")
        if not 1 <= self.realtime_data_max_age_sec <= 3_600:
            raise AltcoinAnomalyConfigError("实时数据新鲜度超出范围")
        if not 1 <= self.funding_max_gap_sec <= 300:
            raise AltcoinAnomalyConfigError("资金费率窗口最大间隔超出范围")
        if self.oi_refresh_sec != 300 or not 300 <= self.realtime_oi_max_age_sec <= 86_400:
            raise AltcoinAnomalyConfigError("实时OI刷新或新鲜度配置无效")
        if not 1 <= self.realtime_oi_workers <= 16:
            raise AltcoinAnomalyConfigError("实时OI并发必须在1到16之间")
        if not 1 <= self.realtime_oi_request_budget <= 5_000:
            raise AltcoinAnomalyConfigError("实时OI请求预算超出范围")
        if self.feature_1m_window_sec != 60 or self.feature_5m_window_sec != 300:
            raise AltcoinAnomalyConfigError(
                "P2当前只支持60秒闭合桶和300秒连续聚合窗口"
            )
        if not 2 <= self.volume_baseline_buckets <= 1_000:
            raise AltcoinAnomalyConfigError("成交量基线桶数量超出范围")
        if not 1 <= self.volume_min_samples <= self.volume_baseline_buckets:
            raise AltcoinAnomalyConfigError("成交量最小样本数不能超过基线桶数量")
        if not 0 < self.volume_min_coverage <= 1:
            raise AltcoinAnomalyConfigError("成交量覆盖率必须在0到1之间")
        if not 0 < self.price_1m_move_ratio <= 1:
            raise AltcoinAnomalyConfigError("1分钟价格阈值超出范围")
        if not 0 < self.price_5m_move_ratio <= 1:
            raise AltcoinAnomalyConfigError("5分钟价格阈值超出范围")
        if not 0 < self.volume_expansion_ratio <= 100:
            raise AltcoinAnomalyConfigError("成交量放大阈值超出范围")
        if not 0 <= self.aggressive_sell_ratio < self.aggressive_buy_ratio <= 1:
            raise AltcoinAnomalyConfigError("主动买卖比例阈值无效")
        if not 0 < self.open_interest_move_ratio <= 1:
            raise AltcoinAnomalyConfigError("OI变化阈值超出范围")
        if not 0 <= self.funding_positive_rate <= 1:
            raise AltcoinAnomalyConfigError("正资金费率阈值超出范围")
        if not 0 < self.funding_change_ratio <= 1:
            raise AltcoinAnomalyConfigError("资金费率变化阈值超出范围")
        if self.liquidation_min_usd <= 0:
            raise AltcoinAnomalyConfigError("爆仓金额阈值必须为正数")
        if not 0 < self.price_stall_ratio <= 1:
            raise AltcoinAnomalyConfigError("价格停滞阈值超出范围")
        if not 0 < self.weakening_volume_ratio <= 100:
            raise AltcoinAnomalyConfigError("减弱成交量阈值超出范围")
        if not 1 <= self.weakening_windows <= 100:
            raise AltcoinAnomalyConfigError("减弱确认窗口数量超出范围")
        if self.realtime_state_path == self.realtime_event_path:
            raise AltcoinAnomalyConfigError("P2状态和事件文件必须相互隔离")
        if self.realtime_state_path.suffix.lower() != ".json":
            raise AltcoinAnomalyConfigError("P2状态文件必须使用.json后缀")
        if self.realtime_event_path.suffix.lower() != ".jsonl":
            raise AltcoinAnomalyConfigError("P2事件文件必须使用.jsonl后缀")
        if not 30 <= self.smoke_duration_sec <= 3_600:
            raise AltcoinAnomalyConfigError("P2 Dry-run时长必须在30到3600秒之间")
        if self.manifest_max_age_sec < self.smoke_duration_sec:
            raise AltcoinAnomalyConfigError("候选清单新鲜度不能短于P2 Smoke时长")


__all__ = [
    "AltcoinAnomalyConfig",
    "AltcoinAnomalyConfigError",
    "protected_runtime_paths",
    "validate_output_path",
]
