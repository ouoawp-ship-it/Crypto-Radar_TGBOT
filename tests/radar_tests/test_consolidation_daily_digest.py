from __future__ import annotations

import json
import unittest

from radars.consolidation_breakout.daily_digest import (
    CANDIDATE_GATE_VERSION,
    ConsolidationDailyDigestAccumulator,
    DEFAULT_DIGEST_TEXT_LIMIT,
    DELIVERY_RETRY_MAX_SEC,
    TELEGRAM_TEXT_LIMIT,
    empty_daily_digest_state,
    select_digest_signal_structures,
)


DAY_MS = 86_400_000
TARGET_MS = 1_700_006_399_999


def structure(
    symbol: str,
    *,
    quality: str = "standard",
    age: int = 72,
    horizon: str = "medium",
    reason: str = "边界稳定",
) -> dict[str, object]:
    return {
        "box_id": f"{symbol}:{horizon}:{TARGET_MS}",
        "horizon": horizon,
        "horizon_label": {
            "short": "短期",
            "medium": "中期",
            "long": "长期",
        }.get(horizon, horizon),
        "base_bars": age,
        "box_age": age,
        "formed_close_time": TARGET_MS,
        "box_lower": 90.0,
        "box_upper": 110.0,
        "width_pct": 20.0,
        "width_atr": 8.0,
        "upper_touches": 3,
        "lower_touches": 3,
        "efficiency": 0.1,
        "current_close": 108.0,
        "distance_upper_atr": 0.2,
        "distance_lower_atr": 2.0,
        "structure_quality": quality,
        "quality_reasons": [reason],
        "lifecycle_state": "continuing",
        "score": 99,
    }


def observation(
    symbol: str,
    *,
    target: int = TARGET_MS,
    status: str = "success",
    structures: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "target_close_time": target,
        "status": status,
        "structures": structures or [],
    }


class ConsolidationDailyDigestAccumulatorTests(unittest.TestCase):
    def test_expected_universe_is_frozen_and_batches_merge_idempotently(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator(max_items=10)

        self.assertIsNone(accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["BBBUSDT", "AAAUSDT"],
            observations=[
                observation(
                    "AAAUSDT",
                    structures=[structure("AAAUSDT", quality="strong")],
                )
            ],
            now_ts=100,
        ))
        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT", "NEWUSDT"],
            observations=[
                observation("NEWUSDT", structures=[structure("NEWUSDT")]),
                observation(
                    "AAAUSDT",
                    structures=[structure("AAAUSDT", quality="strong")],
                ),
            ],
            now_ts=101,
        )
        active = accumulator.snapshot()["active"]
        self.assertEqual(active["expected_symbols"], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(sorted(active["observations"]), ["AAAUSDT"])
        self.assertEqual(
            active["candidate_gate_version"],
            CANDIDATE_GATE_VERSION,
        )

        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["BBBUSDT", "AAAUSDT"],
            observations=[
                observation("BBBUSDT", structures=[structure("BBBUSDT")])
            ],
            now_ts=102,
        )

        self.assertIsNotNone(pending)
        self.assertEqual(pending["coverage"], {
            "expected": 2,
            "attempted": 2,
            "successful": 2,
            "failed": 0,
            "missing": 0,
        })
        self.assertEqual(
            [item["symbol"] for item in pending["structures"]],
            ["AAAUSDT", "BBBUSDT"],
        )
        self.assertEqual(
            pending["candidate_gate_version"],
            CANDIDATE_GATE_VERSION,
        )
        self.assertIsNone(accumulator.snapshot()["active"])

        # Replaying the same close cannot create a second digest.
        replay = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[observation("AAAUSDT"), observation("BBBUSDT")],
            now_ts=103,
        )
        self.assertEqual(replay["digest_id"], pending["digest_id"])
        self.assertEqual(len(accumulator.snapshot()["pending_digests"]), 1)

    def test_new_daily_close_never_mixes_old_close_observations(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[
                observation("AAAUSDT", structures=[structure("AAAUSDT")])
            ],
            now_ts=100,
        )

        new_target = TARGET_MS + DAY_MS
        accumulator.ingest_batch(
            target_close_time=new_target,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[
                observation("AAAUSDT", target=TARGET_MS),
                observation("BBBUSDT", target=new_target),
            ],
            now_ts=200,
        )
        state = accumulator.snapshot()

        self.assertEqual(state["pending_digests"], [])
        self.assertEqual(len(state["recent_snapshots"]), 1)
        old = state["recent_snapshots"][0]
        self.assertEqual(old["target_close_time"], TARGET_MS)
        self.assertTrue(old["degraded"])
        self.assertEqual(old["finalize_reason"], "superseded_by_new_target")
        self.assertEqual(old["archive"]["status"], "superseded")
        self.assertEqual(state["active"]["target_close_time"], new_target)
        self.assertEqual(
            sorted(state["active"]["observations"]),
            ["BBBUSDT"],
        )

        accumulator.ingest_batch(
            target_close_time=new_target,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[observation("AAAUSDT", target=new_target)],
            now_ts=201,
        )
        pending = accumulator.snapshot()["pending_digests"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["target_close_time"], new_target)
        self.assertEqual(
            accumulator.snapshot()["recent_snapshots"][0][
                "target_close_time"
            ],
            TARGET_MS,
        )

    def test_failed_full_coverage_gets_finite_retries_and_round_replay_is_idempotent(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator(
            max_retry_rounds=2,
            max_wait_sec=10_000,
        )
        first = [
            observation("AAAUSDT"),
            observation("BBBUSDT", status="request_error"),
        ]
        self.assertIsNone(accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=first,
            now_ts=100,
            round_completed=True,
            round_token="round-1",
        ))
        self.assertEqual(accumulator.snapshot()["active"]["failed_rounds"], 1)

        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=first,
            now_ts=101,
            round_completed=True,
            round_token="round-1",
        )
        self.assertEqual(accumulator.snapshot()["active"]["failed_rounds"], 1)

        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[],
            now_ts=102,
            round_completed=True,
            round_token="round-2",
        )
        self.assertIsNone(accumulator.pending_digest())

        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[],
            now_ts=103,
            round_completed=True,
            round_token="round-3",
        )
        self.assertTrue(pending["degraded"])
        self.assertEqual(pending["finalize_reason"], "retry_rounds_exhausted")
        self.assertEqual(pending["coverage"]["failed"], 1)

    def test_successful_retry_replaces_failure_without_later_downgrade(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[
                observation("AAAUSDT"),
                observation("BBBUSDT", status="request_error"),
            ],
            now_ts=100,
            round_completed=True,
            round_token="round-1",
        )
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[observation("BBBUSDT")],
            now_ts=101,
        )
        self.assertFalse(pending["degraded"])
        self.assertEqual(pending["coverage"]["successful"], 2)

    def test_timeout_freezes_partial_coverage_as_degraded(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator(max_wait_sec=10)
        accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
            observations=[observation("AAAUSDT")],
            now_ts=100,
        )
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT", "CCCUSDT"],
            observations=[],
            now_ts=110,
        )

        self.assertTrue(pending["degraded"])
        self.assertEqual(pending["finalize_reason"], "coverage_timeout")
        self.assertEqual(pending["coverage"]["attempted"], 1)
        self.assertEqual(pending["coverage"]["missing"], 2)

    def test_only_sent_or_dedup_completes_pending_delivery(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT"],
            observations=[observation("AAAUSDT")],
            now_ts=100,
        )
        digest_id = pending["digest_id"]
        frozen_text = pending["text"]

        self.assertFalse(accumulator.mark_delivery(
            digest_id,
            status="failed",
            reason="telegram_api_failed",
            now_ts=101,
        ))
        self.assertEqual(accumulator.pending_digest()["text"], frozen_text)
        self.assertIsNone(accumulator.pending_digest(now_ts=400))
        self.assertEqual(
            accumulator.pending_digest(now_ts=401)["digest_id"],
            digest_id,
        )
        self.assertEqual(
            accumulator.snapshot()["last_delivered_close_time"],
            0,
        )
        self.assertTrue(accumulator.mark_delivery(
            digest_id,
            status="skipped",
            reason="dedup_cooldown",
            now_ts=401,
        ))
        self.assertIsNone(accumulator.pending_digest())
        self.assertEqual(
            accumulator.snapshot()["last_delivered_close_time"],
            TARGET_MS,
        )
        archived = accumulator.snapshot()["recent_snapshots"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["archive"]["status"], "already_delivered")

        next_target = TARGET_MS + DAY_MS
        sent = accumulator.ingest_batch(
            target_close_time=next_target,
            expected_symbols=["AAAUSDT"],
            observations=[observation("AAAUSDT", target=next_target)],
            now_ts=200,
        )
        self.assertTrue(accumulator.mark_delivery(
            sent["digest_id"],
            status="sent",
            now_ts=201,
        ))
        self.assertFalse(accumulator.mark_delivery(
            "missing",
            status="sent",
            now_ts=202,
        ))
        self.assertEqual(len(accumulator.snapshot()["recent_snapshots"]), 2)

    def test_text_is_deterministic_escaped_scoreless_and_within_limit(self) -> None:
        symbols = [f"S{index:03d}USDT" for index in range(100)]

        def build(order: list[str]) -> dict[str, object]:
            accumulator = ConsolidationDailyDigestAccumulator(max_items=100)
            observations = []
            for symbol in order:
                index = int(symbol[1:4])
                item = structure(
                    symbol,
                    quality=("strong", "standard", "observe")[index % 3],
                    age=20 + index,
                    reason="<script>alert(1)</script>" + "X" * 100,
                )
                item["horizon_label"] = "<b>伪标签</b>"
                observations.append(observation(
                    symbol,
                    structures=[item],
                ))
            return accumulator.ingest_batch(
                target_close_time=TARGET_MS,
                expected_symbols=symbols,
                observations=observations,
                now_ts=100,
            )

        first = build(symbols)
        second = build(list(reversed(symbols)))

        self.assertEqual(
            [item["box_id"] for item in first["structures"]],
            [item["box_id"] for item in second["structures"]],
        )
        self.assertLessEqual(len(first["text"]), DEFAULT_DIGEST_TEXT_LIMIT)
        self.assertLessEqual(len(first["text"]), TELEGRAM_TEXT_LIMIT)
        self.assertNotIn("/100", first["text"])
        self.assertNotIn("<script>", first["text"])
        self.assertIn("&lt;script&gt;", first["text"])
        self.assertIn("结构周期 1D｜触发周期 1D", first["text"])
        for item in first["structures"]:
            self.assertNotIn("score", item)
            self.assertEqual(item["structure_timeframe"], "1d")
            self.assertEqual(item["trigger_timeframe"], "1d")
            self.assertEqual(item["trigger_kind"], "daily_close_digest")

    def test_empty_structure_report_is_still_a_complete_market_digest(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[observation("AAAUSDT"), observation("BBBUSDT")],
            now_ts=100,
        )

        self.assertFalse(pending["degraded"])
        self.assertEqual(pending["structures"], [])
        self.assertIn("没有达到硬门槛", pending["text"])

    def test_zero_display_items_keeps_structures_and_state_is_strict_json(self) -> None:
        item = structure("AAAUSDT")
        item.pop("distance_upper_atr")
        item.pop("distance_lower_atr")
        accumulator = ConsolidationDailyDigestAccumulator(max_items=0)
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT"],
            observations=[observation("AAAUSDT", structures=[item])],
            now_ts=100,
        )

        self.assertEqual(len(pending["structures"]), 1)
        self.assertIn("重点结构展示已关闭", pending["text"])
        self.assertNotIn("没有达到硬门槛", pending["text"])
        json.dumps(accumulator.snapshot(), allow_nan=False)

    def test_invalid_state_schema_resets_without_reusing_foreign_state(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator({
            "schema_version": 99,
            "active": {"target_close_time": TARGET_MS},
            "pending_digests": [{"digest_id": "unsafe"}],
        })
        self.assertEqual(accumulator.snapshot(), empty_daily_digest_state())

    def test_completed_round_requires_stable_token(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        with self.assertRaises(ValueError):
            accumulator.ingest_batch(
                target_close_time=TARGET_MS,
                expected_symbols=["AAAUSDT"],
                observations=[observation("AAAUSDT")],
                now_ts=100,
                round_completed=True,
            )

    def test_custom_text_limit_is_single_message_and_records_only_best_symbol(self) -> None:
        aaa_long = structure(
            "AAAUSDT",
            quality="strong",
            age=240,
            horizon="long",
        )
        aaa_short = structure(
            "AAAUSDT",
            quality="observe",
            age=40,
            horizon="short",
        )
        bbb = structure("BBBUSDT", quality="standard")
        accumulator = ConsolidationDailyDigestAccumulator(
            max_items=3,
            text_limit=1000,
        )
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT", "BBBUSDT"],
            observations=[
                observation("AAAUSDT", structures=[aaa_short, aaa_long]),
                observation("BBBUSDT", structures=[bbb]),
            ],
            now_ts=100,
        )

        self.assertLessEqual(len(pending["text"]), 1000)
        selected = select_digest_signal_structures(pending, max_items=3)
        self.assertEqual([item["symbol"] for item in selected], [
            "AAAUSDT",
            "BBBUSDT",
        ])
        self.assertEqual(selected[0]["horizon"], "long")

    def test_retry_backoff_is_exponential_and_bounded(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        pending = accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["AAAUSDT"],
            observations=[observation("AAAUSDT")],
            now_ts=100,
        )
        digest_id = pending["digest_id"]
        now_ts = 101
        delays: list[int] = []
        for _attempt in range(12):
            self.assertFalse(accumulator.mark_delivery(
                digest_id,
                status="failed",
                reason="telegram_api_failed",
                now_ts=now_ts,
            ))
            delivery = accumulator.snapshot()["pending_digests"][0]["delivery"]
            delay = delivery["next_attempt_at"] - now_ts
            delays.append(delay)
            self.assertIsNone(
                accumulator.pending_digest(now_ts=delivery["next_attempt_at"] - 1)
            )
            now_ts = delivery["next_attempt_at"]
            self.assertIsNotNone(accumulator.pending_digest(now_ts=now_ts))
        self.assertEqual(delays[:3], [300, 600, 1200])
        self.assertLessEqual(max(delays), DELIVERY_RETRY_MAX_SEC)
        self.assertEqual(delays[-1], DELIVERY_RETRY_MAX_SEC)

    def test_reconcile_prunes_legacy_active_and_preserves_current_frozen_universe(self) -> None:
        state = empty_daily_digest_state()
        state["active"] = {
            "target_close_time": TARGET_MS,
            "expected_symbols": ["AAAUSDT", "USDCUSDT"],
            "started_at": 100,
            "observations": {
                "AAAUSDT": observation("AAAUSDT"),
                "USDCUSDT": observation("USDCUSDT"),
            },
            "completed_round_tokens": [],
            "failed_rounds": 0,
        }
        accumulator = ConsolidationDailyDigestAccumulator(state)

        result = accumulator.reconcile_symbols(["AAAUSDT"], now_ts=200)
        active = accumulator.snapshot()["active"]

        self.assertTrue(result["active_reconciled"])
        self.assertEqual(result["removed_symbols"], 1)
        self.assertFalse(result["active_reset"])
        self.assertEqual(active["expected_symbols"], ["AAAUSDT"])
        self.assertEqual(sorted(active["observations"]), ["AAAUSDT"])
        self.assertEqual(
            active["candidate_gate_version"],
            CANDIDATE_GATE_VERSION,
        )

        current = accumulator.reconcile_symbols(["BBBUSDT"], now_ts=201)
        self.assertTrue(current["active_current"])
        self.assertFalse(current["active_reconciled"])
        self.assertFalse(current["active_reset"])
        self.assertEqual(
            accumulator.snapshot()["active"]["expected_symbols"],
            ["AAAUSDT"],
        )

        legacy_only_removed = empty_daily_digest_state()
        legacy_only_removed["active"] = {
            "target_close_time": TARGET_MS,
            "expected_symbols": ["USDCUSDT"],
            "started_at": 100,
            "observations": {
                "USDCUSDT": observation("USDCUSDT"),
            },
            "completed_round_tokens": [],
            "failed_rounds": 0,
        }
        reset_accumulator = ConsolidationDailyDigestAccumulator(
            legacy_only_removed
        )
        reset = reset_accumulator.reconcile_symbols(
            ["BBBUSDT"],
            now_ts=201,
        )
        self.assertTrue(reset["active_reset"])
        self.assertIsNone(reset_accumulator.snapshot()["active"])
        self.assertEqual(
            reset_accumulator.snapshot()["pending_digests"],
            [],
        )

        rebuilt = reset_accumulator.ingest_batch(
            target_close_time=TARGET_MS,
            expected_symbols=["BBBUSDT"],
            observations=[observation("BBBUSDT")],
            now_ts=202,
        )
        self.assertEqual(rebuilt["coverage"]["expected"], 1)
        self.assertEqual(rebuilt["coverage"]["successful"], 1)
        self.assertEqual(
            rebuilt["candidate_gate_version"],
            CANDIDATE_GATE_VERSION,
        )

    def test_legacy_pending_is_invalidated_and_preserved_for_audit(self) -> None:
        state = empty_daily_digest_state()
        state["pending_digests"] = [{
            "digest_id": "unsafe",
            "target_close_time": TARGET_MS,
            "structures": [],
        }]

        accumulator = ConsolidationDailyDigestAccumulator(
            state,
            migration_now_ts=777,
        )
        normalized = accumulator.snapshot()

        self.assertEqual(normalized["pending_digests"], [])
        self.assertEqual(len(normalized["recent_snapshots"]), 1)
        archived = normalized["recent_snapshots"][0]
        self.assertEqual(archived["digest_id"], "unsafe")
        self.assertEqual(archived["archive"], {
            "status": "invalidated",
            "reason": "candidate_universe_tightened",
            "archived_at": 777,
        })

    def test_current_gate_pending_queue_keeps_latest_and_archives_older(self) -> None:
        state = empty_daily_digest_state()
        state["pending_digests"] = [
            {
                "digest_id": "old",
                "target_close_time": TARGET_MS,
                "structures": [],
                "candidate_gate_version": CANDIDATE_GATE_VERSION,
            },
            {
                "digest_id": "new",
                "target_close_time": TARGET_MS + DAY_MS,
                "structures": [],
                "candidate_gate_version": CANDIDATE_GATE_VERSION,
            },
        ]
        accumulator = ConsolidationDailyDigestAccumulator(state)
        normalized = accumulator.snapshot()

        self.assertEqual(
            [item["digest_id"] for item in normalized["pending_digests"]],
            ["new"],
        )
        self.assertEqual(
            [item["digest_id"] for item in normalized["recent_snapshots"]],
            ["old"],
        )
        self.assertEqual(
            normalized["recent_snapshots"][0]["archive"]["status"],
            "superseded",
        )

    def test_completed_snapshot_archive_is_capped_at_seven(self) -> None:
        accumulator = ConsolidationDailyDigestAccumulator()
        for index in range(9):
            target = TARGET_MS + index * DAY_MS
            pending = accumulator.ingest_batch(
                target_close_time=target,
                expected_symbols=["AAAUSDT"],
                observations=[observation("AAAUSDT", target=target)],
                now_ts=100 + index,
            )
            self.assertTrue(accumulator.mark_delivery(
                pending["digest_id"],
                status="sent",
                reason="telegram_api",
                now_ts=200 + index,
            ))
        snapshots = accumulator.snapshot()["recent_snapshots"]
        self.assertEqual(len(snapshots), 7)
        self.assertEqual(
            [item["target_close_time"] for item in snapshots],
            [TARGET_MS + index * DAY_MS for index in range(2, 9)],
        )


if __name__ == "__main__":
    unittest.main()
