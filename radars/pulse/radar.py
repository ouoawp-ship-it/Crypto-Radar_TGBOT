from __future__ import annotations

from typing import Any

from radars.common import RadarComponent
from shared.telegram import TelegramGateway

from .divergence import DivergenceConfig, run_cycle as run_divergence_cycle
from .review_store import backfill_outcomes, send_review_replies
from .simple_alert import SimpleAlertConfig, run_cycle as run_simple_cycle


class PulseRadar(RadarComponent):
    """Production adapter for the two pulse signal windows.

    The 15-minute alert and 2-hour divergence engines own their signal logic.
    Telegram delivery stays on the main project's gateway and therefore keeps
    its topic routing, dual real-send gate, deduplication, and outbox handling.
    """

    def run_simple_pulse(
        self,
        gateway: TelegramGateway,
        *,
        send: bool,
        confirm_real_send: bool,
        scan_limit: int | None = None,
    ) -> dict[str, Any]:
        config = SimpleAlertConfig.from_env(self.settings)
        return run_simple_cycle(
            self.settings,
            gateway,
            config,
            send=send,
            confirm_real_send=confirm_real_send,
            scan_limit=(
                scan_limit
                if scan_limit is not None
                else self.settings.pulse_simple_scan_limit
            ),
        )

    def run_divergence_pulse(
        self,
        gateway: TelegramGateway,
        *,
        send: bool,
        confirm_real_send: bool,
        scan_limit: int | None = None,
    ) -> dict[str, Any]:
        config = DivergenceConfig.from_env(self.settings)
        return run_divergence_cycle(
            self.settings,
            gateway,
            config,
            send=send,
            confirm_real_send=confirm_real_send,
            scan_limit=(
                scan_limit
                if scan_limit is not None
                else self.settings.pulse_divergence_scan_limit
            ),
        )

    def maintain_pulse_reviews(
        self,
        gateway: TelegramGateway,
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, Any]:
        if not (send and confirm_real_send):
            return {
                "status": "dry_run",
                "backfilled": 0,
                "replies": 0,
            }
        try:
            backfilled = backfill_outcomes(self.settings)
            replies = send_review_replies(
                self.settings,
                gateway,
                send=send,
                confirm_real_send=confirm_real_send,
            )
        except Exception as exc:
            return {
                "status": "degraded",
                "error": type(exc).__name__,
                "backfilled": 0,
                "replies": 0,
            }
        failed_replies = sum(
            1
            for reply in replies
            if str(reply.get("status") or "") != "sent"
        )
        return {
            "status": "degraded" if failed_replies else "ok",
            "backfilled": len(backfilled),
            "replies": len(replies),
            "failed_replies": failed_replies,
        }


__all__ = ["PulseRadar"]
