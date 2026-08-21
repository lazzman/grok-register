import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.registration.store import RegistrationRepository


OLD_SCHEMA = """
CREATE TABLE registration_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT UNIQUE,
    batch_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'gui',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL DEFAULT 0,
    email TEXT NOT NULL DEFAULT '',
    password TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'failure',
    success INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    worker_id INTEGER NOT NULL DEFAULT 0,
    cpa_enabled INTEGER NOT NULL DEFAULT 0,
    cpa_status TEXT NOT NULL DEFAULT 'disabled',
    auth_info TEXT NOT NULL DEFAULT '',
    auth_path TEXT NOT NULL DEFAULT '',
    failure_type TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    account_file TEXT NOT NULL DEFAULT '',
    sso_saved INTEGER NOT NULL DEFAULT 0,
    nsfw_status TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}'
);
PRAGMA user_version = 1;
"""


class RegistrationRepositoryMigrationTests(unittest.TestCase):
    def test_old_database_migrates_and_filters_disable_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.sqlite3"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(OLD_SCHEMA)
                conn.execute(
                    """
                    INSERT INTO registration_results
                    (started_at, finished_at, email, status, success, provider)
                    VALUES ('2026-08-01 00:00:00', '2026-08-01 00:00:01',
                            'old@example.com', 'success', 1, 'cloudflare')
                    """
                )
                conn.commit()

            store = RegistrationRepository(path)
            with closing(sqlite3.connect(path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(registration_results)")}
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 8)
            self.assertIn("bot_risk", columns)
            self.assertIn("bfs", columns)
            with closing(sqlite3.connect(path)) as outbox_conn:
                outbox_tables = {
                    row[0]
                    for row in outbox_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("grokiq_outbox", outbox_tables)
            self.assertTrue(
                {
                    "email_account_id",
                    "email_disable_status",
                    "email_disabled_at",
                    "email_disable_error",
                    "cpa_auth_path",
                    "grok2api_auth_path",
                    "screenshot_path",
                    "cpa_remote_status",
                    "cpa_remote_imported_at",
                    "cpa_remote_error",
                    "grok2api_remote_status",
                    "grok2api_remote_imported_at",
                    "grok2api_remote_error",
                }.issubset(columns)
            )
            self.assertEqual(store.list_results()[0]["email_disable_status"], "not_applicable")

            store.add_result(
                {
                    "email": "disabled@outlook.com",
                    "status": "success",
                    "provider": "outlookemail",
                    "cpa_enabled": True,
                    "cpa_status": "success",
                    "email_account_id": "367",
                    "email_disable_status": "success",
                    "email_disabled_at": "2026-08-01 01:02:03",
                    "screenshot_path": "/tmp/failure.png",
                }
            )
            store.add_result(
                {
                    "email": "failed@outlook.com",
                    "status": "success",
                    "provider": "outlookemail",
                    "cpa_enabled": True,
                    "cpa_status": "success",
                    "email_disable_status": "failed",
                    "email_disable_error": "fixture error",
                }
            )

            filtered = store.list_results(email_disable_status="failed")
            self.assertEqual([row["email"] for row in filtered], ["failed@outlook.com"])
            self.assertEqual(store.count_results(), 3)
            self.assertEqual(len(store.list_results(limit=1, offset=1)), 1)
            self.assertEqual(
                store.count_results(email_disable_status="failed"), 1
            )
            stats = store.stats()
            self.assertEqual(stats["email_disabled"], 1)
            self.assertEqual(stats["email_disable_failed"], 1)
            disabled = next(row for row in store.list_results() if row["email"] == "disabled@outlook.com")
            self.assertEqual(disabled["screenshot_path"], "/tmp/failure.png")
            self.assertEqual(disabled["grok2api_remote_status"], "not_configured")

            self.assertTrue(
                store.update_remote_import_status(
                    disabled["id"], "grok2api", status="success"
                )
            )
            refreshed = store.get_results_by_ids([disabled["id"]])[0]
            self.assertEqual(refreshed["grok2api_remote_status"], "success")
            self.assertTrue(refreshed["grok2api_remote_imported_at"])

    def test_pagination_filters_and_large_id_batches_share_consistent_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            for index in range(5):
                store.add_result(
                    {
                        "email": f"user-{index}@example.com",
                        "status": "success" if index < 4 else "failure",
                        "provider": "fixture",
                        "finished_at": f"2026-08-04 00:00:0{index}",
                    }
                )

            self.assertEqual(store.count_results(status="success", keyword="user-"), 4)
            page = store.list_results(
                status="success",
                keyword="user-",
                limit=2,
                offset=2,
            )
            self.assertEqual(
                [row["email"] for row in page],
                ["user-1@example.com", "user-0@example.com"],
            )
            records = store.get_results_by_ids(range(1, 1006))
            self.assertEqual([row["id"] for row in records], [1, 2, 3, 4, 5])
            self.assertEqual(len(store.delete_results(range(1, 1006))), 5)
            self.assertEqual(store.count_results(), 0)

    def test_bot_risk_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "email": "risk@example.com",
                    "status": "success",
                    "bot_risk": True,
                    "bfs": 1,
                }
            )
            store.add_result(
                {
                    "email": "safe@example.com",
                    "status": "success",
                    "bot_risk": False,
                    "bfs": 0,
                }
            )
            store.add_result(
                {
                    "email": "unknown@example.com",
                    "status": "success",
                    "bot_risk": False,
                }
            )
            store.add_result(
                {
                    "email": "source-only-risk@example.com",
                    "status": "success",
                    "bot_risk": False,
                    "bfs": 4,
                }
            )
            store.add_result(
                {
                    "email": "legacy-clean@example.com",
                    "status": "success",
                    "bot_risk": False,
                    "extra_json": json.dumps({"sso_check_status": "clean"}),
                }
            )
            store.add_result(
                {
                    "email": "grokiq-degraded@example.com",
                    "status": "success",
                    "bot_risk": False,
                    "extra_json": json.dumps({"grokiq_result": {"degraded": True}}),
                }
            )
            risk_rows = store.list_results(bot_risk="1")
            self.assertEqual(
                [row["email"] for row in risk_rows],
                [
                    "grokiq-degraded@example.com",
                    "source-only-risk@example.com",
                    "risk@example.com",
                ],
            )
            self.assertEqual(store.count_results(bot_risk="risk"), 3)
            safe_rows = store.list_results(bot_risk="0")
            self.assertEqual(
                [row["email"] for row in safe_rows],
                ["legacy-clean@example.com", "safe@example.com"],
            )
            self.assertEqual(store.count_results(bot_risk="normal"), 2)
            unknown_rows = store.list_results(bot_risk="unknown")
            self.assertEqual([row["email"] for row in unknown_rows], ["unknown@example.com"])

    def test_remote_import_status_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "email": "cpa-ready@example.com",
                    "status": "success",
                    "cpa_remote_status": "ready",
                    "grok2api_remote_status": "not_configured",
                }
            )
            store.add_result(
                {
                    "email": "g2a-success@example.com",
                    "status": "success",
                    "cpa_remote_status": "not_configured",
                    "grok2api_remote_status": "success",
                }
            )
            store.add_result(
                {
                    "email": "both-failed@example.com",
                    "status": "success",
                    "cpa_remote_status": "failed",
                    "grok2api_remote_status": "failed",
                }
            )

            cpa_ready = store.list_results(cpa_remote_status="ready")
            self.assertEqual([row["email"] for row in cpa_ready], ["cpa-ready@example.com"])
            self.assertEqual(store.count_results(cpa_remote_status="ready"), 1)

            g2a_ok = store.list_results(grok2api_remote_status="success")
            self.assertEqual([row["email"] for row in g2a_ok], ["g2a-success@example.com"])
            self.assertEqual(store.count_results(grok2api_remote_status="success"), 1)

            both_failed = store.list_results(
                cpa_remote_status="failed",
                grok2api_remote_status="failed",
            )
            self.assertEqual([row["email"] for row in both_failed], ["both-failed@example.com"])
            expected_ids = [
                row["id"] for row in store.list_results(cpa_remote_status="failed")
            ]
            self.assertEqual(store.list_result_ids(cpa_remote_status="failed"), expected_ids)
            self.assertEqual(store.count_results(cpa_remote_status="not_configured"), 1)

    def test_list_result_ids_matches_filters_and_list_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            first = store.add_result(
                {"email": "first@example.com", "status": "success", "provider": "fixture"}
            )
            second = store.add_result(
                {"email": "second@example.com", "status": "failure", "provider": "fixture"}
            )
            third = store.add_result(
                {"email": "third@example.com", "status": "success", "provider": "other"}
            )

            expected = [row["id"] for row in store.list_results(status="success", keyword="fixture")]
            self.assertEqual(store.list_result_ids(status="success", keyword="fixture"), expected)
            self.assertEqual(expected, [first])
            self.assertNotIn(second, expected)
            self.assertNotIn(third, expected)

    def test_actionable_ids_apply_task_and_risk_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            safe = store.add_result(
                {
                    "email": "safe@example.com",
                    "password": "secret",
                    "status": "success",
                    "sso_saved": True,
                    "bot_risk": False,
                    "bfs": 0,
                    "auth_info": "batch-action-needle",
                }
            )
            risky = store.add_result(
                {
                    "email": "risk@example.com",
                    "password": "secret",
                    "status": "success",
                    "sso_saved": True,
                    "bot_risk": True,
                    "bfs": 2,
                }
            )
            unknown = store.add_result(
                {
                    "email": "unknown@example.com",
                    "password": "secret",
                    "status": "success",
                    "sso_saved": True,
                    "bot_risk": False,
                }
            )
            store.add_result(
                {
                    "email": "missing-password@example.com",
                    "password": "",
                    "status": "success",
                    "sso_saved": True,
                    "bot_risk": False,
                }
            )

            self.assertEqual(store.list_actionable_result_ids("relogin", bot_risk="0"), [safe])
            self.assertEqual(store.list_actionable_result_ids("sso_check", bot_risk="1"), [risky])
            self.assertEqual(store.list_actionable_result_ids("relogin", bot_risk="unknown"), [unknown])
            self.assertEqual(store.list_actionable_result_ids("sso_check", keyword="safe@"), [safe])
            self.assertEqual(
                store.list_actionable_result_ids("sso_check", keyword="batch-action-needle"),
                [safe],
            )

    def test_registration_risk_email_is_treated_as_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            store.add_result(
                {
                    "email": "risk@outlook.com",
                    "status": "failure",
                    "failure_type": "registration_risk",
                    "failure_reason": "注册风控拒绝",
                }
            )
            store.add_result(
                {
                    "email": "sso@outlook.com",
                    "status": "failure",
                    "failure_type": "sso_timeout",
                    "failure_reason": "未获取到 sso cookie",
                }
            )
            store.add_result(
                {
                    "email": "timeout@outlook.com",
                    "status": "failure",
                    "failure_type": "code_timeout",
                    "failure_reason": "未收到验证码",
                }
            )

            self.assertTrue(store.has_registered_or_consumed("risk@outlook.com"))
            self.assertTrue(store.has_registered_or_consumed("sso@outlook.com"))
            self.assertFalse(store.has_registered_or_consumed("timeout@outlook.com"))

    def test_successful_relogin_marks_registration_success_and_clears_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            account_id = store.add_result(
                {
                    "email": "failed@example.com",
                    "password": "secret",
                    "status": "failure",
                    "success": False,
                    "failure_type": "login_error",
                    "failure_reason": "Turnstile 未通过",
                    "screenshot_path": "/tmp/old-failure.png",
                    "extra_json": json.dumps(
                        {
                            "exception_type": "RuntimeError: fixture",
                            "exception_traceback": "Traceback fixture",
                            "relogin_diagnostics": {"stage": "填写邮箱和密码"},
                        }
                    ),
                }
            )

            self.assertTrue(
                store.update_relogin_result(
                    account_id,
                    account_file="/tmp/failed@example.com.txt",
                    cpa_detail={
                        "enabled": True,
                        "status": "success",
                        "auth_info": "CPA 本地: /tmp/cpa.json",
                        "auth_path": "/tmp/cpa.json",
                        "cpa_auth_path": "/tmp/cpa.json",
                        "grok2api_auth_path": "/tmp/g2a.json",
                        "grok2api_remote_status": "success",
                        "grok2api_remote_imported_at": "2026-08-14 00:00:00",
                    },
                    status="success",
                    error="",
                )
            )

            refreshed = store.get_results_by_ids([account_id])[0]
            extra = json.loads(refreshed["extra_json"])
            self.assertEqual(refreshed["status"], "success")
            self.assertEqual(refreshed["success"], 1)
            self.assertEqual(refreshed["failure_type"], "")
            self.assertEqual(refreshed["failure_reason"], "")
            self.assertEqual(refreshed["screenshot_path"], "")
            self.assertEqual(refreshed["grok2api_auth_path"], "/tmp/g2a.json")
            self.assertEqual(refreshed["grok2api_remote_status"], "success")
            self.assertEqual(extra["relogin_status"], "success")
            self.assertNotIn("exception_traceback", extra)
            self.assertNotIn("exception_type", extra)
            self.assertNotIn("relogin_diagnostics", extra)

    def test_failed_relogin_does_not_rewrite_registration_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            account_id = store.add_result(
                {
                    "email": "still-failed@example.com",
                    "status": "failure",
                    "success": False,
                    "failure_type": "login_error",
                    "failure_reason": "原始注册失败",
                }
            )

            self.assertTrue(
                store.update_relogin_result(
                    account_id,
                    status="failed",
                    error="登录超时",
                )
            )

            refreshed = store.get_results_by_ids([account_id])[0]
            extra = json.loads(refreshed["extra_json"])
            self.assertEqual(refreshed["status"], "failure")
            self.assertEqual(refreshed["success"], 0)
            self.assertEqual(refreshed["failure_type"], "login_error")
            self.assertEqual(refreshed["failure_reason"], "原始注册失败")
            self.assertEqual(extra["relogin_status"], "failed")
            self.assertEqual(extra["relogin_error"], "登录超时")

    def test_flagged_partial_relogin_replaces_sso_timeout_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            account_id = store.add_result(
                {
                    "email": "risk-after-relogin@example.com",
                    "password": "secret",
                    "status": "failure",
                    "success": False,
                    "failure_type": "sso_timeout",
                    "failure_reason": "等待 SSO 超时",
                }
            )

            self.assertTrue(
                store.update_relogin_result(
                    account_id,
                    account_file="/tmp/risk-after-relogin@example.com.txt",
                    cpa_detail={
                        "enabled": True,
                        "status": "not_attempted",
                        "bot_risk": True,
                        "bfs": "3",
                    },
                    status="partial",
                    error="SSO 风控异常，已停止授权重建: botFlagSource=3",
                    failure_type="registration_risk",
                    failure_reason="SSO 风控异常，已停止授权重建: botFlagSource=3",
                )
            )

            refreshed = store.get_results_by_ids([account_id])[0]
            self.assertEqual(refreshed["status"], "failure")
            self.assertEqual(refreshed["success"], 0)
            self.assertEqual(refreshed["failure_type"], "registration_risk")
            self.assertIn("botFlagSource=3", refreshed["failure_reason"])
            self.assertEqual(refreshed["sso_saved"], 1)
            self.assertEqual(refreshed["bot_risk"], 1)
            self.assertEqual(refreshed["bfs"], "3")

    def test_backfill_repairs_existing_relogin_risk_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            account_id = store.add_result(
                {
                    "email": "existing-loop@example.com",
                    "status": "failure",
                    "success": False,
                    "failure_type": "sso_timeout",
                    "failure_reason": "等待 SSO 超时",
                    "bot_risk": True,
                    "bfs": "7",
                    "extra_json": json.dumps(
                        {
                            "relogin_status": "partial",
                            "sso_check_status": "flagged",
                        }
                    ),
                }
            )

            self.assertEqual(store.backfill_registration_risk_bot_risk(), 1)
            refreshed = store.get_results_by_ids([account_id])[0]
            self.assertEqual(refreshed["failure_type"], "registration_risk")
            self.assertEqual(
                refreshed["failure_reason"],
                "重新登录 SSO 风控异常: botFlagSource=7",
            )
            self.assertEqual(store.backfill_registration_risk_bot_risk(), 0)

    def test_backfill_marks_grokiq_degraded_as_bot_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RegistrationRepository(Path(tmp) / "results.sqlite3")
            account_id = store.add_result(
                {
                    "email": "grokiq-risk@example.com",
                    "status": "success",
                    "bot_risk": False,
                    "extra_json": json.dumps({"grokiq_result": {"degraded": True}}),
                }
            )
            self.assertEqual(store.backfill_grokiq_degraded_bot_risk(), 1)
            refreshed = store.get_results_by_ids([account_id])[0]
            self.assertEqual(int(refreshed["bot_risk"] or 0), 1)
            self.assertEqual(store.backfill_grokiq_degraded_bot_risk(), 0)


if __name__ == "__main__":
    unittest.main()
