from __future__ import annotations

from typing import Any

from radars.announcement_risk.radar import AnnouncementRiskRadar
from radars.market_summary.radar import MarketSummaryRadar
from radars.pulse.radar import PulseRadar
from shared.binance_data import BinanceDataSource


class RadarEngine(MarketSummaryRadar, AnnouncementRiskRadar, PulseRadar):
    """Compatibility orchestrator over independently testable radar engines."""

    def run_once(
        self,
        include_announcements: bool = True,
    ) -> dict[str, Any]:
        summary_source = BinanceDataSource(self.settings)
        announcement_source = BinanceDataSource(self.settings)
        try:
            summary = self.build_money_radar_summary(summary_source)
            announcements = (
                self.build_announcement_alerts(announcement_source)
                if include_announcements
                else {
                    "template_id": "TG_ANNOUNCEMENT_ALERT",
                    "messages": [],
                    "alerts": [],
                    "status": "skipped",
                    "evidence": {
                        "status": "skipped",
                        "articles_scanned": 0,
                        "evidence_count": 0,
                    },
                }
            )
            return {
                "summary": summary,
                "announcements": announcements,
                "announcement_evidence": announcements.get("evidence", {}),
                "diagnostics": {
                    "summary": summary_source.diagnostics(),
                    "announcements": (
                        announcement_source.diagnostics()
                        if include_announcements
                        else {}
                    ),
                },
            }
        finally:
            summary_source.close()
            announcement_source.close()
