from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import os
from pathlib import Path
from typing import Any


class AltcoinAnomalyConfigError(ValueError):
    pass


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
}
_FLOAT_ENV_RANGES = {
    "ALTCOIN_CONTRACT_ANOMALY_CMC_BACKOFF_BASE_SEC": (0.0, 60.0),
    "ALTCOIN_CONTRACT_ANOMALY_CMC_MIN_REQUEST_INTERVAL_SEC": (0.0, 60.0),
    "ALTCOIN_CONTRACT_ANOMALY_MARKET_CAP_MAX_USD": (0.0, 1e15),
    "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MIN_OI_MARKET_CAP_RATIO": (0.0, 100.0),
    "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MAX_FUNDING_RATE": (-1.0, 1.0),
    "ALTCOIN_CONTRACT_ANOMALY_HIGH_LEVERAGE_MIN_OI_MARKET_CAP_RATIO": (0.0, 100.0),
}


def _validate_raw_environment() -> None:
    enabled = os.getenv("ALTCOIN_CONTRACT_ANOMALY_ENABLE")
    if enabled and enabled.strip().lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise AltcoinAnomalyConfigError("ALTCOIN_CONTRACT_ANOMALY_ENABLE格式无效")
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

    @classmethod
    def from_settings(cls, settings: Any, *, cache_only: bool = False) -> "AltcoinAnomalyConfig":
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
        )
        config.validate(cache_only=cache_only)
        return config

    def validate(self, *, cache_only: bool = False) -> None:
        if not cache_only and not self.enabled:
            raise AltcoinAnomalyConfigError("山寨合约异动雷达未启用")
        if not cache_only and not self.cmc_api_key:
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


__all__ = ["AltcoinAnomalyConfig", "AltcoinAnomalyConfigError"]
