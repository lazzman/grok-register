# -*- coding: utf-8 -*-
"""Mail Hub OTP 提供商单元测试（不访问真实网络）。"""

from __future__ import annotations

import unittest
from unittest import mock
from typing import Any, Dict, List, Optional

from backend.integrations import network_checks
from backend.mailbox import mail_hub as mailhub_provider
from backend.registration import engine as gr


class DummyResponse:
    def __init__(self, payload: Any, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class MailHubProviderTests(unittest.TestCase):
    def test_otp_url_compat(self):
        self.assertEqual(
            mailhub_provider.otp_url("https://mail-hub.example.com/", "/otp"),
            "https://mail-hub.example.com/api/v1/otp",
        )
        self.assertEqual(
            mailhub_provider.otp_url("https://mail-hub.example.com/api/v1", "/otp"),
            "https://mail-hub.example.com/api/v1/otp",
        )

    def test_create_session_uses_api_key_and_omits_empty_hints(self):
        captured: Dict[str, Any] = {}

        def fake_post(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return DummyResponse({"email": "alpha@example.com"})

        email = mailhub_provider.create_otp_session(
            fake_post,
            "https://mail-hub.example.com/",
            "test-key",
            session_ttl_seconds=300,
            verification_pattern="([A-Z0-9]{3}-[A-Z0-9]{3})",
            hint_from_contains="",
            hint_subject_contains="",
        )
        self.assertEqual(email, "alpha@example.com")
        self.assertEqual(captured["url"], "https://mail-hub.example.com/api/v1/otp")
        self.assertEqual(captured["headers"]["X-API-Key"], "test-key")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(captured["json"]["session_ttl_seconds"], 300)
        self.assertEqual(captured["json"]["verification_pattern"], "([A-Z0-9]{3}-[A-Z0-9]{3})")
        self.assertNotIn("hint", captured["json"])

    def test_status_uses_versioned_base_encoded_email_and_wait_seconds(self):
        captured: Dict[str, Any] = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["params"] = kwargs.get("params")
            captured["timeout"] = kwargs.get("timeout")
            return DummyResponse({"status": "active"})

        data = mailhub_provider.get_session_status(
            fake_get,
            "https://mail-hub.example.com/api/v1",
            "test-key",
            "name+tag@example.com",
            wait_seconds=3,
        )
        self.assertEqual(data["status"], "active")
        self.assertEqual(
            captured["url"],
            "https://mail-hub.example.com/api/v1/otp/name%2Btag%40example.com",
        )
        self.assertEqual(captured["headers"]["X-API-Key"], "test-key")
        self.assertEqual(captured["params"]["wait_seconds"], 3)
        self.assertGreaterEqual(captured["timeout"], 8)

    def test_default_pattern_skips_html_attribute_fragments(self):
        messages = [
            {
                "subject": "",
                "html_content": (
                    '<meta http-equiv="X-UA-Compatible">'
                    '<td style="font-weight: bold;">W7J-00I</td>'
                ),
            }
        ]
        code = mailhub_provider.extract_code_from_messages(
            messages, mailhub_provider.DEFAULT_VERIFICATION_PATTERN
        )
        self.assertEqual(code, "W7J-00I")

    def test_wait_for_code_uses_positive_wait_and_falls_back_to_messages(self):
        calls: List[str] = []
        waits: List[int] = []
        released = {"done": False}
        logs: List[str] = []

        def fake_get(url, **kwargs):
            calls.append(url)
            params = kwargs.get("params") or {}
            if "wait_seconds" in params:
                waits.append(int(params["wait_seconds"]))
            if url.endswith("/messages"):
                return DummyResponse(
                    {
                        "messages": [
                            {
                                "subject": "W7J-00I xAI confirmation code",
                                "html_content": "<p>验证码：<strong>W7J-00I</strong></p>",
                            }
                        ]
                    }
                )
            return DummyResponse({"status": "active", "code": ""})

        def fake_delete(url, **kwargs):
            released["done"] = True
            return DummyResponse({}, status_code=204)

        code = mailhub_provider.wait_for_code(
            fake_get,
            fake_delete,
            "https://mail-hub.example.com",
            "test-key",
            "alpha@example.com",
            verification_pattern=mailhub_provider.DEFAULT_VERIFICATION_PATTERN,
            timeout=30,
            poll_interval=2,
            raise_if_cancelled=lambda cb: None,
            sleep_with_cancel=lambda seconds, cb: None,
            log_callback=logs.append,
        )
        self.assertEqual(code, "W7J-00I")
        self.assertTrue(released["done"])
        self.assertTrue(waits)
        self.assertTrue(all(w >= 1 for w in waits))
        self.assertGreaterEqual(mailhub_provider.poll_wait_seconds(0), 1)
        self.assertTrue(any("回退提取到验证码: W7J-00I" in line for line in logs))
        self.assertTrue(
            any(u.endswith("/api/v1/otp/alpha%40example.com") for u in calls)
        )
        self.assertTrue(
            any(u.endswith("/api/v1/otp/alpha%40example.com/messages") for u in calls)
        )

    def test_create_mailbox_token_is_mailhub_prefix(self):
        def fake_post(url, **kwargs):
            return DummyResponse({"email": "a@example.com"})

        email, token = mailhub_provider.create_mailbox(
            fake_post, "https://mail-hub.example.com", "k"
        )
        self.assertEqual(email, "a@example.com")
        self.assertEqual(token, "mailhub:a@example.com")


class MailHubIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)

    def tearDown(self):
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_engine_dispatches_mailhub_creation_and_code_wait(self):
        gr.config["email_provider"] = "mailhub"
        with (
            mock.patch.object(
                gr,
                "mailhub_get_email_and_token",
                return_value=("alpha@example.com", "mailhub:alpha@example.com"),
            ) as create,
            mock.patch.object(gr, "mailhub_get_oai_code", return_value="W7J-00I") as wait,
        ):
            self.assertEqual(
                gr.get_email_and_token(),
                ("alpha@example.com", "mailhub:alpha@example.com"),
            )
            self.assertEqual(
                gr.get_oai_code("mailhub:alpha@example.com", "alpha@example.com", timeout=45),
                "W7J-00I",
            )

        create.assert_called_once_with()
        wait.assert_called_once_with(
            "mailhub:alpha@example.com",
            "alpha@example.com",
            timeout=45,
            poll_interval=3,
            log_callback=None,
            cancel_callback=None,
            resend_callback=None,
        )

    def test_connectivity_uses_versioned_ping_and_api_key(self):
        http_get = mock.Mock()
        http_get.return_value.status_code = 204
        result = network_checks.check_email_api(
            "mailhub",
            {
                "mailhub_api_base": "https://mailhub.example.com/api/v1",
                "mailhub_api_key": "test-key",
            },
            http_get,
            mock.Mock(),
        )

        self.assertEqual(result, ("邮箱API", True, "Mail Hub HTTP 204"))
        http_get.assert_called_once_with(
            "https://mailhub.example.com/api/v1/ping",
            headers={"X-API-Key": "test-key"},
            timeout=12,
        )

    def test_connectivity_requires_base_and_key(self):
        http_get = mock.Mock()
        self.assertEqual(
            network_checks.check_email_api("mailhub", {}, http_get, mock.Mock()),
            ("邮箱API", False, "未配置 mailhub_api_base"),
        )
        self.assertEqual(
            network_checks.check_email_api(
                "mailhub", {"mailhub_api_base": "https://mailhub.example.com"}, http_get, mock.Mock()
            ),
            ("邮箱API", False, "未配置 mailhub_api_key"),
        )
        http_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
