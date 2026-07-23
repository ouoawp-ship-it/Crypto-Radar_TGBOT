from __future__ import annotations

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import PushResult, TelegramGateway

from .config import OnchainSettings
from .constants import TEMPLATE_ID
from .db import OnchainStore
from .formatter import format_alert
from .models import OnchainAlert


class OnchainNotifier:
    def __init__(self, settings: OnchainSettings, store: OnchainStore):
        settings.assert_safe_paths()
        self.onchain_settings = settings
        self.store = store
        gateway_settings = Settings(
            base_dir=settings.base_dir,
            data_dir=settings.data_dir,
            tg_bot_token=settings.tg_bot_token,
            tg_chat_id=settings.tg_chat_id,
            tg_onchain_flow_topic_id=settings.tg_onchain_flow_topic_id,
            tg_use_topic=(
                settings.tg_use_topic or bool(settings.tg_onchain_flow_topic_id)
            ),
            tg_topic_routes_path=settings.tg_topic_routes_path,
            tg_push_history_path=settings.tg_push_history_path,
            tg_outbox_path=settings.tg_outbox_path,
            tg_global_hourly_limit=settings.tg_hourly_limit,
            tg_default_cooldown_sec=settings.alert_cooldown_sec,
            signal_events_path=settings.signal_events_path,
            signal_events_db_path=settings.signal_events_db_path,
            runtime_status_path=settings.runtime_status_path,
        )
        self.gateway = TelegramGateway(
            gateway_settings,
            JsonStore(settings.data_dir),
        )

    def notify(
        self,
        alert: OnchainAlert,
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> PushResult:
        result = self.gateway.send(
            format_alert(alert),
            TEMPLATE_ID,
            alert.alert_key,
            send=bool(send and self.onchain_settings.real_send),
            confirm_real_send=bool(confirm_real_send),
            cooldown_sec=self.onchain_settings.alert_cooldown_sec,
        )
        self.store.record_delivery(
            alert.alert_key,
            status=result.status,
            sent=result.sent,
            reason=result.reason,
            created_at=alert.created_at,
        )
        return result
