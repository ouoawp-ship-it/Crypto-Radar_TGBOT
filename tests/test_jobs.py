from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.web_services import jobs
from paopao_radar.web_services.ops import parse_update_check_output


def temp_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(data_dir=base, web_jobs_db_path=base / "jobs.db")


class JobStoreTests(unittest.TestCase):
    def test_initializes_jobs_table_and_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            with store.connect() as conn:
                objects = conn.execute(
                    "SELECT type, name FROM sqlite_master WHERE name LIKE 'jobs' OR name LIKE 'idx_jobs_%'"
                ).fetchall()

        names = {row["name"] for row in objects}
        self.assertIn("jobs", names)
        self.assertIn("idx_jobs_created_at", names)
        self.assertIn("idx_jobs_status", names)
        self.assertIn("idx_jobs_type_created", names)

    def test_create_list_and_get_job(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("api-self-test", {"source": "unit"})

            listed = store.list_jobs()
            loaded = store.get_job(int(job["id"]))

        self.assertEqual(job["status"], "queued")
        self.assertEqual(len(listed), 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["job_type"], "api-self-test")  # type: ignore[index]
        self.assertEqual(loaded["metadata"]["source"], "unit")  # type: ignore[index]

    def test_success_job_records_stdout(self) -> None:
        spec = jobs.JobSpec("test-success", "test success", [sys.executable, "-c", "print('ok')"], 10)
        with TemporaryDirectory() as tmp, patch.dict(jobs.JOB_SPECS, {"test-success": spec}):
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("test-success")
            finished = jobs.run_job_sync_for_tests(store, int(job["id"]))

        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], "success")  # type: ignore[index]
        self.assertEqual(finished["returncode"], 0)  # type: ignore[index]
        self.assertIn("ok", finished["stdout_tail"])  # type: ignore[index]

    def test_failed_job_records_returncode(self) -> None:
        spec = jobs.JobSpec(
            "test-fail",
            "test fail",
            [sys.executable, "-c", "import sys; print('bad'); sys.exit(2)"],
            10,
        )
        with TemporaryDirectory() as tmp, patch.dict(jobs.JOB_SPECS, {"test-fail": spec}):
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("test-fail")
            finished = jobs.run_job_sync_for_tests(store, int(job["id"]))

        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], "failed")  # type: ignore[index]
        self.assertEqual(finished["returncode"], 2)  # type: ignore[index]
        self.assertIn("bad", finished["stdout_tail"])  # type: ignore[index]

    def test_stable_check_returncode_one_is_attention(self) -> None:
        stable_output = (
            "泡泡雷达稳定版自检\n"
            "状态: 基本可运行，建议关注\n"
            "摘要: 1 个警告项，不一定阻断运行，但建议确认。\n"
            "网络重试噪声: 关注 - 近期可自动重试的网络超时 11 条。\n"
        )
        spec = jobs.JobSpec(
            "stable-check",
            "stable check",
            [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({stable_output.encode('utf-8')!r}); sys.exit(1)"],
            10,
        )
        with TemporaryDirectory() as tmp, patch.dict(jobs.JOB_SPECS, {"stable-check": spec}):
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("stable-check")
            finished = jobs.run_job_sync_for_tests(store, int(job["id"]))
            enriched = jobs.enrich_job(finished or {})
            stats = store.stats()

        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], "attention")  # type: ignore[index]
        self.assertEqual(finished["returncode"], 1)  # type: ignore[index]
        self.assertEqual(enriched["status"], "attention")
        self.assertIn("稳定版自检", enriched["error_summary"])
        self.assertIn("非阻断", enriched["error_summary"])
        self.assertIn("观察项", enriched["next_action"])
        self.assertEqual(stats["attention"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(stats["recent_failed"]), 0)
        self.assertEqual(len(stats["recent_attention"]), 1)
        self.assertIn("stable-check", stats["last_attention_by_type"])

    def test_legacy_stable_check_failed_returncode_one_displays_attention(self) -> None:
        stable_output = (
            "状态: 基本可运行，建议关注\n"
            "摘要: 1 个警告项，不一定阻断运行，但建议确认。\n"
        )
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("stable-check")
            finished = store.finish_job(
                int(job["id"]),
                status="failed",
                returncode=1,
                stdout_tail=stable_output,
                error=stable_output,
            )
            enriched = jobs.enrich_job(finished or {})
            report = jobs.job_report_payload_from_item(finished or {})
            stats = store.stats()
            filtered_attention = store.list_jobs(status="attention")
            filtered_failed = store.list_jobs(status="failed")

        self.assertEqual(enriched["status"], "attention")
        self.assertEqual(report["status"], "attention")
        self.assertEqual(stats["attention"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(stats["recent_failed"]), 0)
        self.assertEqual(len(filtered_attention), 1)
        self.assertEqual(len(filtered_failed), 0)

    def test_success_job_has_no_error_summary_even_with_stderr_noise(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("update-check")
            finished = store.finish_job(
                int(job["id"]),
                status="success",
                returncode=0,
                stdout_tail="当前已经是最新版本，不需要更新。",
                stderr_tail="* branch            main       -> FETCH_HEAD",
            )
            enriched = jobs.enrich_job(finished or {})

        self.assertEqual(enriched["status"], "success")
        self.assertEqual(enriched["error_summary"], "")

    def test_timeout_job_records_timeout_status(self) -> None:
        spec = jobs.JobSpec(
            "test-timeout",
            "test timeout",
            [sys.executable, "-c", "import time; time.sleep(5)"],
            1,
        )
        with TemporaryDirectory() as tmp, patch.dict(jobs.JOB_SPECS, {"test-timeout": spec}):
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("test-timeout")
            finished = jobs.run_job_sync_for_tests(store, int(job["id"]))

        self.assertIsNotNone(finished)
        self.assertEqual(finished["status"], "timeout")  # type: ignore[index]
        self.assertEqual(finished["returncode"], 124)  # type: ignore[index]

    def test_output_is_redacted_before_persisting(self) -> None:
        spec = jobs.JobSpec(
            "test-redact",
            "test redact",
            [
                sys.executable,
                "-c",
                "import sys; print('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'); print('AI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz', file=sys.stderr)",
            ],
            10,
        )
        with TemporaryDirectory() as tmp, patch.dict(jobs.JOB_SPECS, {"test-redact": spec}):
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("test-redact")
            finished = jobs.run_job_sync_for_tests(store, int(job["id"]))

        text = f"{finished['stdout_tail']} {finished['stderr_tail']}"  # type: ignore[index]
        self.assertIn("<redacted", text)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", text)

    def test_rejects_non_whitelisted_job_type(self) -> None:
        with TemporaryDirectory() as tmp:
            result = jobs.create_job_payload("bad; rm -rf /", settings=temp_settings(tmp), start=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_job_type")

    def test_payload_helpers_create_detail_and_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = temp_settings(tmp)
            created = jobs.create_job_payload("api-self-test", settings=settings, start=False)
            job_id = int(created["job"]["id"])
            listed = jobs.jobs_payload(settings=settings)
            detail = jobs.job_detail_payload(job_id, settings=settings)
            cancelled = jobs.cancel_job_payload(job_id, settings=settings)

        self.assertTrue(created["ok"])
        self.assertEqual(listed["count"], 1)
        self.assertEqual(detail["job"]["id"], job_id)
        self.assertTrue(cancelled["ok"])
        self.assertEqual(cancelled["job"]["status"], "cancelled")

    def test_jobs_payload_supports_filters_sort_and_pagination_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = temp_settings(tmp)
            store = jobs.JobStore(settings.web_jobs_db_path)
            first = store.create_job("api-self-test", allow_reuse=False)
            second = store.create_job("doctor", allow_reuse=False)
            store.finish_job(int(first["id"]), status="success", returncode=0)
            store.finish_job(int(second["id"]), status="failed", returncode=2)

            payload = jobs.jobs_payload(
                limit=10,
                status="failed",
                sort_field="id",
                sort_direction="asc",
                pagination={"limit": 10, "cursor": None},
                filters={"status": "failed"},
                sort={"field": "id", "direction": "asc", "raw": "id"},
                settings=settings,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["jobs"][0]["job_type"], "doctor")
        self.assertEqual(payload["filters"]["status"], "failed")
        self.assertEqual(payload["sort"]["direction"], "asc")
        self.assertIn("pagination", payload)

    def test_cancel_running_job_returns_clear_message(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = temp_settings(tmp)
            created = jobs.create_job_payload("api-self-test", settings=settings, start=False)
            job_id = int(created["job"]["id"])
            store = jobs.JobStore(settings.web_jobs_db_path)
            store.mark_running(job_id)
            result = jobs.cancel_job_payload(job_id, settings=settings)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "running_not_cancelable")

    def test_database_file_is_created(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.db"
            store = jobs.JobStore(db)
            with store.connect():
                pass

            conn = sqlite3.connect(db)
            try:
                count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            finally:
                conn.close()
            exists = db.exists()

        self.assertEqual(count, 0)
        self.assertTrue(exists)

    def test_stats_counts_statuses_and_recent_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            success = store.create_job("api-self-test", allow_reuse=False)
            failed = store.create_job("doctor", allow_reuse=False)
            timeout = store.create_job("readiness", allow_reuse=False)
            running = store.create_job("cleanup", allow_reuse=False)
            store.finish_job(int(success["id"]), status="success", returncode=0, stdout_tail="ok")
            store.finish_job(int(failed["id"]), status="failed", returncode=2, stderr_tail="bad")
            store.finish_job(int(timeout["id"]), status="timeout", returncode=124, error="timeout")
            store.mark_running(int(running["id"]))

            stats = store.stats()

        self.assertEqual(stats["success"], 1)
        self.assertEqual(stats["attention"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["timeout"], 1)
        self.assertEqual(stats["running"], 1)
        self.assertIn("doctor", stats["by_type"])
        self.assertEqual(len(stats["recent_failed"]), 2)
        self.assertIn("api-self-test", stats["last_success_by_type"])
        self.assertIn("doctor", stats["last_failed_by_type"])

    def test_stats_can_exclude_archived_job_types_without_deleting_history(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            active = store.create_job("doctor", allow_reuse=False)
            archived_spec = jobs.JobSpec(
                "lifecycle-outcome-reconcile",
                "archived",
                [sys.executable, "-c", "print('archived')"],
                30,
            )
            with patch.dict(jobs.JOB_SPECS, {"lifecycle-outcome-reconcile": archived_spec}):
                archived = store.create_job("lifecycle-outcome-reconcile", allow_reuse=False)
            store.finish_job(int(active["id"]), status="failed", returncode=2, stderr_tail="active failed")
            store.finish_job(int(archived["id"]), status="failed", returncode=1, stderr_tail="archived failed")

            scoped = store.stats(job_types={"doctor"})
            all_stats = store.stats()

        self.assertEqual(scoped["total"], 1)
        self.assertEqual(scoped["by_type"], {"doctor": 1})
        self.assertEqual([item["job_type"] for item in scoped["recent_failed"]], ["doctor"])
        self.assertEqual(all_stats["total"], 2)
        self.assertIn("lifecycle-outcome-reconcile", all_stats["by_type"])

    def test_job_report_is_redacted_and_has_next_action(self) -> None:
        fake_token_suffix = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        fake_api_suffix = "abcdefghijklmnopqrstuvwxyz"
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            job = store.create_job("doctor")
            finished = store.finish_job(
                int(job["id"]),
                status="failed",
                returncode=1,
                stdout_tail="TG_BOT_TOKEN=" + "123456789:" + fake_token_suffix + "\n",
                stderr_tail="AI_API_KEY=" + "sk-" + fake_api_suffix + "\nTraceback: bad",
                error="password=secret",
            )

            report = jobs.job_report_payload_from_item(finished or {})

        text = report["text"]
        self.assertIn("doctor", text)
        self.assertIn("failed", text)
        self.assertIn("<redacted", text)
        self.assertNotIn(fake_token_suffix, text)
        self.assertNotIn("sk-" + fake_api_suffix, text)
        self.assertNotIn("password=secret", text)
        self.assertTrue(report["next_action"])

    def test_duplicate_guard_reuses_running_same_type_job(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = temp_settings(tmp)
            first = jobs.create_job_payload("stable-check", settings=settings, start=False)
            second = jobs.create_job_payload("stable-check", settings=settings, start=False)
            listed = jobs.jobs_payload(settings=settings)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(listed["count"], 1)

    def test_cleanup_jobs_prunes_old_finished_but_keeps_active(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            old = store.create_job("doctor", allow_reuse=False)
            active = store.create_job("readiness", allow_reuse=False)
            recent = store.create_job("cleanup", allow_reuse=False)
            store.finish_job(int(old["id"]), status="success", returncode=0)
            store.mark_running(int(active["id"]))
            store.finish_job(int(recent["id"]), status="success", returncode=0)
            with store.connect() as conn:
                conn.execute("UPDATE jobs SET created_at = 1, updated_at = 1 WHERE id = ?", (int(old["id"]),))

            result = store.cleanup_jobs(retention_days=1, limit=500)
            remaining = {int(item["id"]) for item in store.list_jobs(limit=10)}

        self.assertEqual(result["deleted_count"], 1)
        self.assertNotIn(int(old["id"]), remaining)
        self.assertIn(int(active["id"]), remaining)
        self.assertIn(int(recent["id"]), remaining)

    def test_cleanup_jobs_respects_limit_for_finished_jobs(self) -> None:
        with TemporaryDirectory() as tmp:
            store = jobs.JobStore(Path(tmp) / "jobs.db")
            for _ in range(55):
                job = store.create_job("api-self-test", allow_reuse=False)
                store.finish_job(int(job["id"]), status="success", returncode=0)

            result = store.cleanup_jobs(retention_days=365, limit=50)
            remaining_count = len(store.list_jobs(limit=100))

        self.assertEqual(result["deleted_count"], 5)
        self.assertEqual(remaining_count, 50)

    def test_rerun_job_creates_whitelisted_new_job_only(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = temp_settings(tmp)
            created = jobs.create_job_payload("api-self-test", settings=settings, start=False)
            store = jobs.JobStore(settings.web_jobs_db_path)
            store.finish_job(int(created["job"]["id"]), status="success", returncode=0)
            rerun = jobs.rerun_job_payload(int(created["job"]["id"]), settings=settings, start=False)

            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (job_type, label, status, command_json, created_at, updated_at)
                    VALUES ('not-allowed', 'bad', 'failed', '["rm","-rf","/"]', 1, 1)
                    """
                )
                bad_id = int(conn.execute("SELECT MAX(id) FROM jobs").fetchone()[0])
            rejected = jobs.rerun_job_payload(bad_id, settings=settings, start=False)

        self.assertTrue(rerun["ok"])
        self.assertEqual(rerun["rerun_from"], int(created["job"]["id"]))
        self.assertEqual(rerun["job"]["job_type"], "api-self-test")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["code"], "invalid_job_type")

    def test_update_check_parser_extracts_versions_and_is_lenient(self) -> None:
        stdout = (
            "[paopao-update] 检查 GitHub 最新版本\n"
            "当前版本 : v1.61.0 (6b9d485) feat\n"
            "GitHub版本: v1.62.0 (abc1234) feat\n"
            "发现新版本，可以更新\n"
        )
        parsed = parse_update_check_output(stdout, "")
        unknown = parse_update_check_output("unrelated output", "")

        self.assertEqual(parsed["current_version"], "v1.61.0")
        self.assertEqual(parsed["current_commit"], "6b9d485")
        self.assertEqual(parsed["remote_version"], "v1.62.0")
        self.assertEqual(parsed["remote_commit"], "abc1234")
        self.assertIs(parsed["update_available"], True)
        self.assertIsNone(unknown["update_available"])

    def test_api_self_test_uses_api_contract_checks(self) -> None:
        returncode, stdout, stderr = jobs._run_api_self_test()

        self.assertEqual(returncode, 0)
        self.assertEqual(stderr, "")
        self.assertIn("signals", stdout)
        self.assertIn("jobs", stdout)
        self.assertIn("update-status", stdout)


if __name__ == "__main__":
    unittest.main()
