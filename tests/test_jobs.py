from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.web_services import jobs


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


if __name__ == "__main__":
    unittest.main()
