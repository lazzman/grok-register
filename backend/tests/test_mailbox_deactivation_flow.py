import unittest
from unittest import mock

from backend.registration import engine as gr


class OutlookWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)

    def tearDown(self):
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_all_email_providers_require_exact_cpa_success(self):
        providers = ("duckmail", "yyds", "cloudflare", "mailnest", "mailhub", "outlookemail", "cloudmail")
        for provider in providers:
            gr.config["email_provider"] = provider
            self.assertTrue(gr.registration_counts_as_success({"status": "success"}))
            for status in (
                "failed",
                "disabled",
                "skipped",
                "not_attempted",
                "",
                "SUCCESS",
                " success ",
                None,
            ):
                self.assertFalse(gr.registration_counts_as_success({"status": status}))

    def test_cpa_conversion_is_enabled_by_default(self):
        self.assertTrue(gr.DEFAULT_CONFIG["cpa_auto_add"])

    def test_cpa_failure_skips_remote_disable(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        with mock.patch.object(gr.outlookemail_provider, "account_for_email") as lookup:
            detail = gr.disable_outlookemail_after_cpa_success(
                "fixture@outlook.com", {"status": "failed", "error": "fixture"}
            )
        self.assertEqual(detail["status"], "skipped_cpa")
        lookup.assert_not_called()

    def test_feature_disabled_and_temp_source_are_recorded(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": False,
            }
        )
        self.assertEqual(
            gr.default_email_disable_detail("outlookemail", {"status": "success"})["status"],
            "feature_disabled",
        )
        gr.config["outlookemail_disable_after_cpa_success"] = True
        gr.config["outlookemail_source"] = "temp"
        self.assertEqual(
            gr.default_email_disable_detail("outlookemail", {"status": "success"})["status"],
            "unsupported_source",
        )

    def test_registration_risk_disables_outlook_account(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        logs = []
        with mock.patch.object(
            gr.outlookemail_provider,
            "account_for_email",
            return_value={"id": 88, "email": "risk@outlook.com"},
        ), mock.patch.object(
            gr.outlookemail_provider,
            "disable_account",
            return_value={"success": True, "account_id": 88},
        ) as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_RISK,
                "risk@outlook.com",
                reason="注册风控: botFlagSource=1",
                log_callback=logs.append,
            )
        self.assertEqual(detail["status"], "success")
        self.assertEqual(detail["account_id"], "88")
        disable.assert_called_once()
        self.assertTrue(any("注册风控" in item and "停用完成" in item for item in logs))

    def test_registration_risk_skips_disable_for_non_outlook(self):
        gr.config["email_provider"] = "cloudflare"
        with mock.patch.object(gr, "disable_outlookemail_consumed") as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_RISK,
                "risk@example.com",
                reason="注册风控",
            )
        self.assertIsNone(detail)
        disable.assert_not_called()

    def test_sso_timeout_disables_outlook_account(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        logs = []
        with mock.patch.object(
            gr, "disable_outlookemail_consumed", return_value={"status": "success"}
        ) as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_SSO,
                "sso@outlook.com",
                reason="SSO超时: 未获取到 sso cookie",
                log_callback=logs.append,
            )
        self.assertEqual(detail["status"], "success")
        disable.assert_called_once()
        self.assertTrue(any("SSO超时" in item and "停用完成" in item for item in logs))

    def test_already_registered_still_disables_outlook_account(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        with mock.patch.object(
            gr, "disable_outlookemail_consumed", return_value={"status": "success"}
        ) as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_ALREADY_REGISTERED,
                "dup@outlook.com",
                reason="账号已注册",
            )
        self.assertEqual(detail["status"], "success")
        disable.assert_called_once()

    def test_other_failures_do_not_disable_outlook_account(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        with mock.patch.object(gr, "disable_outlookemail_consumed") as disable:
            detail = gr.maybe_disable_outlookemail_for_consumed_failure(
                gr.FAIL_CODE,
                "timeout@outlook.com",
                reason="未收到验证码",
            )
        self.assertIsNone(detail)
        disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
