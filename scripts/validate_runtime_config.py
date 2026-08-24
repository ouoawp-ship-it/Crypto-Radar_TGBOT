from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings
from radars.altcoin_contract_anomaly.configuration import (
    AltcoinAnomalyConfig,
    AltcoinAnomalyConfigError,
    AltcoinAnomalyProductionConfig,
)
from runtime.cli import telegram_config_checks


def validate_runtime_config(settings: Any) -> dict[str, object]:
    failed_telegram_checks = [
        name
        for name, ok, _detail in telegram_config_checks(settings)
        if not ok
    ]
    if failed_telegram_checks:
        raise ValueError("telegram_runtime_config_invalid")
    production_enabled = bool(
        settings.altcoin_contract_anomaly_production_enable
    )
    send_enabled = bool(
        settings.altcoin_contract_anomaly_production_send_enable
    )
    if not production_enabled:
        if send_enabled:
            raise AltcoinAnomalyConfigError(
                "production_send_requires_production_mode"
            )
        return {
            "status": "ok",
            "altcoin_production_enabled": False,
            "altcoin_real_send_enabled": False,
        }

    AltcoinAnomalyConfig.from_settings(settings, realtime=True)
    AltcoinAnomalyProductionConfig.from_settings(
        settings,
        real_send_requested=send_enabled,
    )
    return {
        "status": "ok",
        "altcoin_production_enabled": True,
        "altcoin_real_send_enabled": send_enabled,
    }


def main() -> int:
    try:
        result = validate_runtime_config(Settings.load())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_class": type(exc).__name__,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
