from __future__ import annotations


# Source group: test_web.py

import unittest

from paopao_radar import web
from paopao_radar.web_services.jobs import JOB_SPECS, LONG_ACTION_JOB_TYPES


class WebSurfaceTests(unittest.TestCase):
    def test_content_length_must_be_a_non_negative_integer(self) -> None:
        self.assertEqual(web.parse_content_length(None), 0)
        self.assertEqual(web.parse_content_length("2048"), 2048)
        for value in ("-1", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                web.parse_content_length(value)

    def test_public_surface_only_exposes_signals(self) -> None:
        self.assertIn("/public-api/signals", web.PUBLIC_INDEX_HTML)
        self.assertIn("/admin", web.PUBLIC_INDEX_HTML)

    def test_admin_surface_contains_only_operational_pages(self) -> None:
        for page in ("运行总览", "信号记录", "雷达服务", "任务中心", "日志中心", "配置中心", "审计记录"):
            self.assertIn(page, web.INDEX_HTML)

    def test_config_surface_has_core_signal_keys(self) -> None:
        keys = {field.key for field in web.EDITABLE_CONFIG_FIELDS}
        self.assertIn("SIGNAL_EVENTS_DB_FILE", keys)

    def test_job_surface_is_operational_only(self) -> None:
        self.assertEqual(set(JOB_SPECS), {"stable-check", "doctor", "readiness", "cleanup", "update-check", "api-self-test"})
        self.assertEqual(LONG_ACTION_JOB_TYPES, {"stable-check", "doctor", "readiness", "cleanup"})


if __name__ == "__main__":
    unittest.main()


# Source group: public market context contracts

import json
import threading
import time
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.realtime_market import RealtimeFeatureStore
from paopao_radar.signal_store import SignalEventStore, append_from_push
from paopao_radar.signal_intelligence import absolute_metric, build_radar_intelligence
from paopao_radar.web_observability import PublicApiMetrics, PublicStreamMetrics, PublicTelemetry, SlidingWindowRateLimiter
from paopao_radar.web_services.public import (
    public_api_health_payload,
    PUBLIC_CONTEXT_SCHEMA_VERSION,
    public_market_snapshot_payload,
    public_coin_context_payload,
    public_radar_intelligence_payload,
    public_signal_item,
    public_signals_payload,
    public_watchlist_market_payload,
    public_signal_context_payload,
)


class PublicContextContractTests(unittest.TestCase):
    @staticmethod
    def settings_for(tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
            ai_bot_username="paopao_ai_bot",
        )

    @staticmethod
    def snapshot(_settings: Settings, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "coin": symbol[:-4],
            "updated_at": 2_000,
            "price": 60_000.0,
            "price_1h_pct": 2.5,
            "price_4h_pct": 4.25,
            "quote_volume": 1_000_000_000.0,
            "volume_ratio": 1.8,
            "oi_value": 500_000_000.0,
            "oi_1h_pct": 3.2,
            "funding_pct": 0.012,
            "market_cap": 1_200_000_000_000.0,
            "market_cap_source": "CoinPaprika",
            "market_cap_tier": "large",
            "liquidity_tier": "deep",
            "funding_exchanges": [
                {
                    "exchange": "Binance",
                    "funding_pct": 0.012,
                    "interval_hours": 8,
                    "next_funding_time": "2026-07-16 16:00:00",
                    "api_key": "must-not-leak",
                }
            ],
            "data_quality": {"authorization": "must-not-leak"},
        }

    def test_market_snapshot_exposes_versioned_metric_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = public_market_snapshot_payload(
                "btc",
                settings=self.settings_for(tmp),
                snapshot_loader=self.snapshot,
                now_ts=2_010,
            )

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["schema_version"], PUBLIC_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(data["symbol"], "BTCUSDT")
        self.assertEqual(data["status"], "fresh")
        self.assertEqual(data["metrics"]["price"]["unit"], "usd")
        self.assertEqual(data["metrics"]["price"]["age_sec"], 10)
        self.assertEqual(data["metrics"]["price_1h_pct"]["quality"], "derived")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)

    def test_market_snapshot_rejects_empty_or_invalid_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            empty = public_market_snapshot_payload("", settings=settings, snapshot_loader=self.snapshot)
            invalid = public_market_snapshot_payload("-", settings=settings, snapshot_loader=self.snapshot)

        self.assertFalse(empty["ok"])
        self.assertEqual(empty["code"], "invalid_symbol")
        self.assertFalse(invalid["ok"])

    def test_signal_context_combines_signal_market_evidence_and_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:btc:context",
                status="sent",
                sent=True,
                text="BTCUSDT\n启动雷达\n分数: 88\n阶段: active",
                ts=1_990,
                topic_id="private-topic",
                message_ids=[123],
            )
            stored_signal = SignalEventStore(settings.signal_events_db_path).list_signals()["items"][0]
            signal_id = stored_signal["id"]
            public_ref = stored_signal["public_ref"]
            payload = public_signal_context_payload(
                public_ref,
                settings=settings,
                snapshot_loader=self.snapshot,
                now_ts=2_010,
            )

        self.assertTrue(payload["ok"])
        context = payload["data"]
        self.assertEqual(context["signal"]["id"], signal_id)
        self.assertEqual(context["market"]["symbol"], "BTCUSDT")
        self.assertGreaterEqual(len(context["evidence"]), 5)
        self.assertEqual(context["signal"]["public_ref"], public_ref)
        self.assertEqual(context["actions"]["signal_url"], f"/radar?signal={public_ref}")
        self.assertEqual(context["actions"]["symbol_url"], "/radar?symbol=BTCUSDT")
        self.assertEqual(context["actions"]["ai_url"], "https://t.me/paopao_ai_bot?start=analyze_BTC")
        self.assertEqual(context["actions"]["alert_url"], "https://t.me/paopao_ai_bot?start=alert_BTC")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private-topic", serialized)
        self.assertNotIn("message_ids", serialized)

    def test_signal_context_loads_market_and_intelligence_concurrently(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:btc:parallel-context",
                status="sent",
                sent=True,
                text="BTCUSDT\n启动雷达\n分数: 88",
                ts=1_990,
            )
            stored_signal = SignalEventStore(settings.signal_events_db_path).list_signals()["items"][0]
            barrier = threading.Barrier(2)

            def snapshot_loader(loaded: Settings, symbol: str) -> dict[str, object]:
                barrier.wait(timeout=2)
                return self.snapshot(loaded, symbol)

            def intelligence_loader(*_args: object, **_kwargs: object) -> dict[str, object]:
                barrier.wait(timeout=2)
                return {"items": []}

            with patch(
                "paopao_radar.web_services.public._radar_intelligence_targets",
                side_effect=intelligence_loader,
            ):
                payload = public_signal_context_payload(
                    stored_signal["public_ref"],
                    settings=settings,
                    snapshot_loader=snapshot_loader,
                    now_ts=2_010,
                )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["market"]["symbol"], "BTCUSDT")

    def test_public_routes_include_context_and_snapshot(self) -> None:
        source = __import__("inspect").getsource(web.WebHandler.do_GET)
        self.assertIn("/public-api/signals/context", source)
        self.assertIn("/public-api/market/snapshot", source)
        self.assertIn("/public-api/market/realtime", source)
        self.assertIn("/public-api/radar/intelligence", source)
        self.assertIn("/public-api/radar/realtime-intelligence", source)
        self.assertIn("/public-api/workstation/radar/momentum", source)
        self.assertIn("/public-api/workstation/radar/momentum-windows", source)
        self.assertIn("/public-api/workstation/radar/anomalies", source)
        self.assertIn("/public-api/workstation/radar/surge", source)
        self.assertIn("/public-api/workstation/radar/rank", source)
        self.assertIn("/public-api/workstation/radar/briefs", source)
        self.assertIn("/public-api/workstation/funds/open-interest", source)
        self.assertIn("/public-api/coin/context", source)
        self.assertIn("/public-api/market/watchlist", source)
        self.assertIn("/public-api/funds/sectors", source)
        self.assertIn("/public-api/funds/assets", source)
        self.assertIn("/public-api/info/feed", source)
        self.assertIn("/public-api/agents/overview", source)
        self.assertIn("/public-api/stream", source)
        self.assertIn("/public-api/health", source)
        self.assertIn("require_public_rate_limit", source)

    def test_public_rate_limiter_uses_sliding_window(self) -> None:
        clock = [0.0]
        limiter = SlidingWindowRateLimiter(window_sec=60, clock=lambda: clock[0])

        self.assertTrue(limiter.allow("client", 2).allowed)
        self.assertTrue(limiter.allow("client", 2).allowed)
        blocked = limiter.allow("client", 2)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.remaining, 0)
        clock[0] = 61.0
        self.assertTrue(limiter.allow("client", 2).allowed)
        self.assertEqual(limiter.stats()["blocked"], 1)

    def test_public_rate_limiter_bounds_inactive_client_keys(self) -> None:
        clock = [0.0]
        limiter = SlidingWindowRateLimiter(window_sec=60, max_keys=100, clock=lambda: clock[0])
        for index in range(100):
            self.assertTrue(limiter.allow(f"client-{index}", 1).allowed)

        clock[0] = 61.0
        self.assertTrue(limiter.allow("replacement", 1).allowed)
        self.assertLessEqual(limiter.stats()["active_keys"], 100)

    def test_public_telemetry_only_accepts_bounded_event_names(self) -> None:
        telemetry = PublicTelemetry()

        self.assertTrue(telemetry.record("frontend_api_error"))
        self.assertFalse(telemetry.record("password=secret"))
        self.assertEqual(telemetry.stats()["counts"], {"frontend_api_error": 1})

    def test_public_api_metrics_report_bounded_route_p95(self) -> None:
        metrics = PublicApiMetrics(sample_limit=20)
        for duration in range(1, 21):
            metrics.record("/public-api/example", 200 if duration < 20 else 503, duration)

        stats = metrics.stats()
        self.assertEqual(stats["routes"]["/public-api/example"]["count"], 20)
        self.assertEqual(stats["routes"]["/public-api/example"]["p50_ms"], 10.0)
        self.assertEqual(stats["routes"]["/public-api/example"]["p95_ms"], 19.0)
        self.assertEqual(stats["routes"]["/public-api/example"]["status_classes"], {"2xx": 19, "5xx": 1})
        self.assertEqual(stats["status_classes"], {"2xx": 19, "5xx": 1})

    def test_critical_public_routes_report_performance_budget_status(self) -> None:
        metrics = PublicApiMetrics(sample_limit=20)
        route_durations = {
            "/public-api/signals/context": 799,
            "/public-api/coin/context": 800,
            "/public-api/agents/overview": 801,
        }
        for path, duration in route_durations.items():
            for _ in range(20):
                metrics.record(path, 200, duration)

        stats = metrics.stats()

        self.assertEqual(stats["performance_budget"]["status"], "breaching")
        self.assertEqual(stats["performance_budget"]["observed_routes"], 3)
        self.assertEqual(stats["performance_budget"]["breaching_routes"], ["/public-api/agents/overview"])
        self.assertTrue(stats["routes"]["/public-api/signals/context"]["within_p95_budget"])
        self.assertTrue(stats["routes"]["/public-api/coin/context"]["within_p95_budget"])
        self.assertFalse(stats["routes"]["/public-api/agents/overview"]["within_p95_budget"])
        self.assertEqual(stats["routes"]["/public-api/agents/overview"]["p95_budget_ms"], 800)

    def test_public_api_metrics_bound_unknown_route_cardinality(self) -> None:
        metrics = PublicApiMetrics(sample_limit=20, route_limit=3)
        for index in range(8):
            metrics.record(f"/public-api/unknown-{index}", 404, index)

        stats = metrics.stats()
        self.assertLessEqual(len(stats["routes"]), 3)
        self.assertIn("/public-api/_other", stats["routes"])
        self.assertEqual(stats["collapsed_routes"], 6)
        self.assertEqual(stats["route_limit"], 3)

    def test_stream_metrics_tracks_connections_and_events(self) -> None:
        metrics = PublicStreamMetrics()
        metrics.opened()
        metrics.event("status")
        metrics.event("signal")
        metrics.closed()
        self.assertEqual(metrics.stats()["active"], 0)
        self.assertEqual(metrics.stats()["events"]["signal"], 1)

    def test_public_health_exposes_aggregates_without_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="health:latest",
                status="sent",
                sent=True,
                text="BTCUSDT health",
                ts=int(time.time()),
            )
            payload = public_api_health_payload(settings=settings)

        self.assertTrue(payload["ok"])
        self.assertIn(payload["data"]["status"], {"ok", "degraded"})
        self.assertEqual(payload["data"]["database"]["signals"], 1)
        self.assertTrue(payload["data"]["database"]["latest_at"])
        self.assertEqual(payload["data"]["realtime_market"]["status"], "empty")
        self.assertEqual(payload["data"]["realtime_market"]["symbols"], 0)
        self.assertGreater(payload["data"]["cache"]["max_entries"], 0)
        self.assertIn("evictions", payload["data"]["cache"])
        self.assertGreater(payload["data"]["requests"]["route_limit"], 0)
        self.assertEqual(payload["data"]["upstreams"]["scope"], "process")
        self.assertIn(payload["data"]["upstreams"]["status"], {"ready", "degraded", "unobserved"})
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("bot_token", serialized)
        self.assertNotIn("password", serialized)

    def test_public_health_reports_enabled_exchange_gaps_as_partial(self) -> None:
        now = int(time.time())
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            RealtimeFeatureStore(settings.realtime_features_db_path).replace_many([{
                "exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                "bucket_start": now - 60, "bucket_sec": 60,
                "trade_buy_usd": 100, "trade_sell_usd": 50, "cvd_usd": 50,
                "trade_count": 1, "price_open": 100, "price_high": 100,
                "price_low": 100, "price_close": 100,
                "long_liquidation_usd": 0, "short_liquidation_usd": 0,
                "liquidation_count": 0, "last_event_ms": now * 1000,
            }])
            payload = public_api_health_payload(settings=settings)

        realtime = payload["data"]["realtime_market"]
        self.assertEqual(realtime["status"], "partial")
        self.assertEqual(realtime["exchanges"]["binance"]["status"], "ready")
        self.assertEqual(realtime["exchanges"]["bybit"]["status"], "empty")
        self.assertEqual(realtime["exchanges"]["okx"]["status"], "empty")

    def test_operational_audit_scope_keeps_auth_failures_separate(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            web.append_web_audit(
                "/api/auth/login",
                {"username": "paopao"},
                {"ok": False, "error": "bad credentials"},
                status=401,
                started_at=time.time(),
                data_dir=data_dir,
            )
            web.append_web_audit(
                "/api/service",
                {"name": "restart-main"},
                {"ok": False, "error": "restart failed"},
                status=500,
                started_at=time.time(),
                data_dir=data_dir,
            )
            operations = web.web_audit_payload(data_dir=data_dir, result="failed", scope="operations")
            auth = web.web_audit_payload(data_dir=data_dir, result="failed", scope="auth")

        self.assertEqual(operations["matched"], 1)
        self.assertEqual(operations["records"][0]["path"], "/api/service")
        self.assertEqual(auth["matched"], 1)
        self.assertEqual(auth["records"][0]["path"], "/api/auth/login")

    def test_log_error_excerpt_ignores_json_failure_counters(self) -> None:
        source = "\n".join([
            'Jul 16 host python[1]:       "failed": 156,',
            'Jul 16 host python[1]:       "errors": 3,',
            "Jul 16 host python[1]: RuntimeError: actual failure",
        ])
        with patch(
            "paopao_radar.web.logs_payload",
            return_value={"source": "test", "text": source, "ok": True},
        ):
            payload = web.log_error_excerpt("main")

        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["transient_count"], 0)
        self.assertIn("RuntimeError", payload["lines"][0])

    def test_log_error_excerpt_only_reads_the_active_service_run(self) -> None:
        active_since = "Sat 2026-07-18 07:02:14 UTC"

        def fake_run(argv: list[str], **_kwargs: object) -> dict[str, object]:
            if argv[0] == "systemctl":
                return {"ok": True, "returncode": 0, "stdout": active_since, "stderr": ""}
            self.assertEqual(argv[0], "journalctl")
            self.assertIn("--since", argv)
            self.assertEqual(argv[argv.index("--since") + 1], active_since)
            return {"ok": True, "returncode": 0, "stdout": "service ready", "stderr": ""}

        with (
            patch("paopao_radar.web.command_exists", return_value=True),
            patch("paopao_radar.web.run_subprocess", side_effect=fake_run),
        ):
            payload = web.log_error_excerpt("web")

        self.assertEqual(payload["source"], "journalctl:paopao-web:active-run")
        self.assertEqual(payload["error_count"], 0)

    def test_json_responses_add_browser_security_headers(self) -> None:
        source = __import__("inspect").getsource(web.WebHandler.send_payload)
        self.assertIn("X-Content-Type-Options", source)
        self.assertIn("X-Frame-Options", source)
        self.assertIn("Permissions-Policy", source)

    def test_public_json_exposes_the_same_request_id_in_header_and_meta(self) -> None:
        handler = object.__new__(web.WebHandler)
        handler.path = "/public-api/example"
        handler.request_started_at = 0.0
        handler.public_metrics_recorded = True
        captured: dict[str, object] = {}

        def capture(payload: bytes, status: int, content_type: str, *, extra_headers: dict[str, str] | None = None) -> None:
            captured.update(payload=payload, status=status, content_type=content_type, headers=extra_headers or {})

        handler.send_payload = capture  # type: ignore[method-assign]
        handler.send_json({"ok": True})

        body = json.loads(captured["payload"])
        headers = captured["headers"]
        self.assertEqual(headers["X-Request-ID"], body["_meta"]["request_id"])
        self.assertEqual(handler.request_id(), body["_meta"]["request_id"])

    def test_coin_context_combines_snapshot_timeline_intelligence_and_actions(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="coin:btc:launch",
                status="sent",
                sent=True,
                text="BTCUSDT\n启动雷达\n分数: 80\n24h成交额: $100M",
                ts=99_500,
            )
            payload = public_coin_context_payload(
                "BTC",
                settings=settings,
                snapshot_loader=self.snapshot,
                now_ts=100_000,
            )

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["symbol"], "BTCUSDT")
        self.assertEqual(data["market"]["symbol"], "BTCUSDT")
        self.assertEqual(data["summary"]["signal_count"], 1)
        self.assertEqual(data["timeline"][0]["intelligence"]["lifecycle"]["state"], "new")
        reference = data["timeline"][0]["public_ref"]
        self.assertEqual(data["actions"]["ai_url"], f"https://t.me/paopao_ai_bot?start=analyze_BTC_{reference}")
        self.assertIn(f"signal={reference}", data["actions"]["share_url"])

    def test_watchlist_market_normalizes_deduplicates_and_isolates_invalid_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            payload = public_watchlist_market_payload(
                "btc,ETHUSDT,BTC,-",
                settings=settings,
                snapshot_loader=self.snapshot,
                now_ts=100_000,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual([item["symbol"] for item in payload["data"]["items"]], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(payload["data"]["invalid"], ["-"])

    def test_watchlist_reuses_market_snapshot_flow_without_fabricating_missing_assets(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            MarketSnapshotStore(settings.market_snapshots_db_path).append_many([{
                "symbol": "BTCUSDT", "observed_at": 100_000, "source": "flow_radar",
                "price": 60_000, "spot_flow_usd": 2_000_000,
                "futures_flow_usd": -1_000_000, "oi_change_pct": 3.5, "funding_pct": 0.01,
            }])
            payload = public_watchlist_market_payload(
                "BTC,ETH",
                settings=settings,
                snapshot_loader=self.snapshot,
                now_ts=100_000,
            )

        by_symbol = {item["symbol"]: item for item in payload["data"]["items"]}
        self.assertEqual(by_symbol["BTCUSDT"]["flow"]["spot_net_flow_usd"], 2_000_000)
        self.assertEqual(by_symbol["BTCUSDT"]["flow"]["futures_net_flow_usd"], -1_000_000)
        self.assertIsNone(by_symbol["ETHUSDT"]["flow"])

    def test_intelligence_ranks_resonance_lifecycle_and_opportunity_boards(self) -> None:
        now = 100_000
        events = [
            {
                "id": 1, "public_ref": "sig_btc_old", "ts": now - 10_000, "time": "old",
                "module": "launch", "symbol": "BTCUSDT", "status": "sent", "score": 60,
                "excerpt": "24h成交额: $80M", "payload": {}, "severity": "info",
            },
            {
                "id": 2, "public_ref": "sig_eth", "ts": now - 700, "time": "eth",
                "module": "launch", "symbol": "ETHUSDT", "status": "sent", "score": 50,
                "excerpt": "24h成交额: $20M", "payload": {}, "severity": "info",
            },
            {
                "id": 3, "public_ref": "sig_btc_launch", "ts": now - 600, "time": "launch",
                "module": "launch", "symbol": "BTCUSDT", "status": "sent", "score": 70,
                "excerpt": "24h成交额: $100M", "payload": {}, "severity": "info",
            },
            {
                "id": 4, "public_ref": "sig_btc_funding", "ts": now - 300, "time": "funding",
                "module": "funding", "symbol": "BTCUSDT", "status": "sent", "score": 82,
                "excerpt": "资金费率警报", "payload": {}, "severity": "warning",
            },
        ]

        payload = build_radar_intelligence(events, now_ts=now, window_sec=86400, board_limit=5)
        by_ref = {entry["signal"]["public_ref"]: entry["intelligence"] for entry in payload["items"]}
        launch = by_ref["sig_btc_launch"]

        self.assertEqual(launch["self_rank"]["rank"], 1)
        self.assertEqual(launch["market_strength_rank"]["rank"], 1)
        self.assertEqual(launch["market_absolute_rank"]["rank"], 1)
        self.assertEqual(launch["lifecycle"]["state"], "enhancing")
        self.assertGreater(by_ref["sig_btc_funding"]["resonance"]["active_count"], 0)
        boards = {board["key"]: board for board in payload["boards"]}
        self.assertEqual(boards["launch"]["items"][0]["signal"]["public_ref"], "sig_btc_launch")
        self.assertEqual(boards["funding"]["items"][0]["signal"]["public_ref"], "sig_btc_funding")
        self.assertEqual(boards["resonance"]["items"][0]["signal"]["symbol"], "BTCUSDT")

    def test_absolute_metric_prefilter_preserves_structured_and_text_parsing(self) -> None:
        structured = absolute_metric({
            "payload": {"quote_volume": 125_000_000},
            "text_html": "unrelated text",
        })
        parsed = absolute_metric({
            "payload": {},
            "text_html": "Market snapshot · quote volume: $12.5M",
        })
        unrelated = absolute_metric({
            "payload": {},
            "text_html": "price and momentum context " * 500,
        })

        self.assertEqual(structured["value"], 125_000_000)
        self.assertEqual(structured["quality"], "structured")
        self.assertEqual(parsed["value"], 12_500_000)
        self.assertEqual(parsed["quality"], "parsed")
        self.assertIsNone(unrelated)

    def test_intelligence_cold_build_stays_within_production_scale_budget(self) -> None:
        now = int(time.time())
        modules = ("launch", "flow", "funding", "announcement")
        events = [
            {
                "id": index + 1,
                "public_ref": f"sig_{index:020x}",
                "ts": now - index * 60 if index < 1200 else now - 86400 - (index - 1200) * 60,
                "time": "2026-07-16T12:00:00+00:00",
                "module": modules[index % len(modules)],
                "symbol": f"T{index % 180:03d}USDT",
                "status": "sent",
                "score": 50 + index % 50,
                "excerpt": "24h成交额: $100M",
                "payload": {"quote_volume": 1_000_000 + index},
            }
            for index in range(2000)
        ]
        timings = []
        result = None
        for _ in range(3):
            started = time.perf_counter()
            result = build_radar_intelligence(events, now_ts=now, window_sec=86400, board_limit=5)
            timings.append(time.perf_counter() - started)

        self.assertIsNotNone(result)
        self.assertEqual(len(result["items"]), 1201)
        self.assertLess(median(timings), 1.0, f"2,000 条冷计算中位数超出 1 秒保护线: {timings}")

    def test_intelligence_target_projection_keeps_full_history_context(self) -> None:
        now = 2_000_000
        events = [
            {
                "id": 1,
                "public_ref": "sig_00000000000000000001",
                "ts": now - 172800,
                "time": "old",
                "module": "launch",
                "symbol": "BTCUSDT",
                "status": "sent",
                "score": 50,
            },
            {
                "id": 2,
                "public_ref": "sig_00000000000000000002",
                "ts": now - 60,
                "time": "current",
                "module": "launch",
                "symbol": "BTCUSDT",
                "status": "sent",
                "score": 70,
            },
        ]

        current = build_radar_intelligence(events, now_ts=now, window_sec=86400)
        targeted = build_radar_intelligence(
            events,
            now_ts=now,
            window_sec=2_592_000,
            target_refs={"sig_00000000000000000001"},
        )

        self.assertEqual([entry["signal"]["id"] for entry in current["items"]], [2])
        self.assertEqual(current["items"][0]["intelligence"]["lifecycle"]["state"], "restarted")
        self.assertEqual([entry["signal"]["id"] for entry in targeted["items"]], [1])

    def test_public_intelligence_is_redacted_and_reports_empty_state(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            payload = public_radar_intelligence_payload(settings=settings, now_ts=100_000)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["data_status"], "empty")
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("bot_token", serialized)
        self.assertNotIn("dedup_key", serialized)

    def test_public_radar_payloads_are_single_envelope_projected_and_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            now = int(time.time())
            for index in range(45):
                append_from_push(
                    settings,
                    template_id="TG_LAUNCH_ALERT",
                    dedup_key=f"performance:{index}",
                    status="sent",
                    sent=True,
                    text=(
                        f"T{index}USDT\n启动雷达\n分数: {50 + index % 40}\n"
                        + "价格与 OI 同步增强，进入启动观察。" * 20
                    ),
                    ts=now - index,
                )

            signals_payload = public_signals_payload(limit=40, settings=settings)
            signal_items = signals_payload["data"]["items"]
            refs = [item["public_ref"] for item in signal_items[:3]]
            projected = public_radar_intelligence_payload(
                settings=settings,
                now_ts=now,
                signal_refs=",".join(refs),
            )
            default_projection = public_radar_intelligence_payload(settings=settings, now_ts=now)

        self.assertNotIn("items", signals_payload)
        self.assertEqual(signals_payload["data"]["count"], 40)
        self.assertEqual(len(signal_items), 40)
        self.assertLess(len(json.dumps(signals_payload, ensure_ascii=False, separators=(",", ":"))), 100_000)
        self.assertEqual(projected["data"]["projection"]["requested"], 3)
        self.assertEqual(projected["data"]["projection"]["returned"], 3)
        self.assertEqual(
            [entry["signal"]["public_ref"] for entry in projected["data"]["items"]],
            refs,
        )
        self.assertLess(len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"))), 60_000)
        self.assertEqual(len(default_projection["data"]["items"]), 40)
        self.assertLess(len(json.dumps(default_projection, ensure_ascii=False, separators=(",", ":"))), 200_000)
        invalid = public_radar_intelligence_payload(settings=settings, signal_refs="../../etc/passwd")
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["code"], "invalid_refs")

    def test_public_signal_card_uses_a_bounded_display_projection(self) -> None:
        item = public_signal_item({
            "id": 1,
            "public_ref": "sig_1234567890abcdef1234",
            "time": "2026-07-16T12:00:00+00:00",
            "module": "launch",
            "symbol": "BTCUSDT",
            "status": "sent",
            "signal_type": "启动雷达",
            "excerpt": "市场摘要" * 200,
        })

        self.assertNotIn("badges", item["display"])
        self.assertLessEqual(len(item["excerpt"]), 180)
        self.assertLessEqual(len(item["display"]["summary"]), 180)


# Source group: test_admin_auth.py

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar import web
from paopao_radar.auth import create_session_value, generate_password_hash, generate_session_secret, verify_password, verify_session_value
from paopao_radar.config import Settings


class AdminAuthTests(unittest.TestCase):
    def test_password_hash_and_session_round_trip(self) -> None:
        password_hash = generate_password_hash("strong-password")
        self.assertTrue(verify_password("strong-password", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))
        secret = generate_session_secret()
        value, csrf = create_session_value("admin", secret, ttl_sec=3600)
        payload = verify_session_value(value, secret)
        self.assertEqual(payload["username"], "admin")
        self.assertEqual(payload["csrf"], csrf)

    def test_password_mode_requires_hash_and_secret(self) -> None:
        settings = Settings(web_auth_mode="password", web_admin_password_hash="", web_session_secret="")
        self.assertEqual(web.auth_mode(settings), "password")
        self.assertFalse(bool(settings.web_admin_password_hash and settings.web_session_secret))

    def test_auth_audit_uses_runtime_data_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            self.assertEqual(settings.data_dir, Path(tmp))


if __name__ == "__main__":
    unittest.main()


# Source group: test_api_core.py

import unittest

from paopao_radar.web_services.api_core import (
    api_error,
    api_ok,
    normalize_symbol_filter,
    pagination_params,
    redact_api_payload,
    sort_params,
    time_range_params,
)


class ApiCoreTests(unittest.TestCase):
    def test_pagination_params_clamps_limit_and_reads_cursor(self) -> None:
        params = pagination_params({"limit": ["999"], "cursor": ["42"], "offset": ["3"], "page": ["2"]}, default_limit=50, max_limit=200)
        fallback = pagination_params({"limit": ["bad"]}, default_limit=25, max_limit=100)

        self.assertEqual(params["limit"], 200)
        self.assertEqual(params["cursor"], 42)
        self.assertEqual(params["offset"], 3)
        self.assertEqual(params["page"], 2)
        self.assertEqual(fallback["limit"], 25)

    def test_sort_params_accepts_direction_and_falls_back(self) -> None:
        desc = sort_params({"sort": ["-id"]}, {"id", "ts"}, default="-ts")
        asc = sort_params({"sort": ["ts"]}, {"id", "ts"}, default="-id")
        fallback = sort_params({"sort": ["bad"]}, {"id", "ts"}, default="-id")

        self.assertEqual(desc, {"field": "id", "direction": "desc", "raw": "-id"})
        self.assertEqual(asc, {"field": "ts", "direction": "asc", "raw": "ts"})
        self.assertEqual(fallback, {"field": "id", "direction": "desc", "raw": "-id"})

    def test_time_range_params_supports_window_and_explicit_range(self) -> None:
        defaulted = time_range_params({})
        explicit = time_range_params({"start_ts": ["100"], "end_ts": ["200"], "window_sec": ["60"]})
        window = time_range_params({"window_sec": ["60"]})

        self.assertEqual(defaulted["window_sec"], 86400)
        self.assertFalse(defaulted["applied"])
        self.assertEqual(explicit["start_ts"], 100)
        self.assertEqual(explicit["end_ts"], 200)
        self.assertTrue(window["applied"])
        self.assertIsNotNone(window["start_ts"])
        self.assertIsNotNone(window["end_ts"])

    def test_normalize_symbol_filter_supports_coin_and_pair(self) -> None:
        self.assertEqual(normalize_symbol_filter("BTC")["symbol"], "BTCUSDT")
        self.assertEqual(normalize_symbol_filter("BTCUSDT")["coin"], "BTC")
        self.assertEqual(normalize_symbol_filter("btc/usd")["symbol"], "BTCUSDT")

    def test_api_ok_error_and_redaction_shape(self) -> None:
        ok = api_ok({"value": 1}, message="done")
        err = api_error("bad", code="bad_request", details={"token": "secret"})
        redacted = redact_api_payload({"AI_API_KEY": "sk-" + "abcdefghijklmnopqrstuvwxyz", "text": "ok"})

        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"], {"value": 1})
        self.assertFalse(err["ok"])
        self.assertEqual(err["code"], "bad_request")
        self.assertEqual(err["details"]["token"], "<redacted>")
        self.assertEqual(redacted["AI_API_KEY"], "<redacted>")


if __name__ == "__main__":
    unittest.main()
