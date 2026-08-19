import base64
import socketserver
import threading
import unittest
from unittest import mock

from backend.automation import session as browser_session
from backend.integrations import auth_exchange
from backend.integrations import network_checks
from backend.integrations.proxy import clear_sticky_proxy_session, current_sticky_proxy_id
from backend.registration import engine as gr


class ProxyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)

    def tearDown(self):
        clear_sticky_proxy_session()
        gr.config.clear()
        gr.config.update(self.original_config)
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )

    def test_camoufox_registration_keeps_configured_proxy(self):
        browser_session.configure(
            get_proxies=lambda: {
                "http": "http://127.0.0.1:7897",
                "https": "http://127.0.0.1:7897",
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["proxy"], {"server": "http://127.0.0.1:7897"})

    def test_camoufox_registration_uses_authenticated_http_proxy(self):
        browser_session.configure(
            get_proxies=lambda: {
                "http": "http://proxy-user:proxy-password@proxy.example.com:7897",
                "https": "http://proxy-user:proxy-password@proxy.example.com:7897",
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "proxy-user",
                "password": "proxy-password",
            },
        )

    def test_camoufox_decodes_percent_encoded_http_credentials(self):
        browser_session.configure(
            get_proxies=lambda: {
                "https": "http://user%40mail:p%40ss%3Aword@proxy.example.com:7897"
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "user@mail",
                "password": "p@ss:word",
            },
        )

    def test_cloakbrowser_reuses_authenticated_proxy_parsing(self):
        browser_session.configure(
            get_proxies=lambda: {
                "https": "http://user%40mail:p%40ss%3Aword@proxy.example.com:7897"
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "cloakbrowser",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "user@mail",
                "password": "p@ss:word",
            },
        )

    def test_http_client_sends_encoded_proxy_credentials_as_basic_auth(self):
        captured = {}

        class ProxyHandler(socketserver.StreamRequestHandler):
            def handle(self):
                lines = []
                while True:
                    line = self.rfile.readline().decode("iso-8859-1").rstrip("\r\n")
                    if not line:
                        break
                    lines.append(line)
                for line in lines[1:]:
                    if line.lower().startswith("proxy-authorization:"):
                        captured["authorization"] = line.split(":", 1)[1].strip()
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\nOK"
                )

        with socketserver.TCPServer(("127.0.0.1", 0), ProxyHandler) as proxy_server:
            thread = threading.Thread(target=proxy_server.handle_request, daemon=True)
            thread.start()
            port = proxy_server.server_address[1]
            proxy = f"http://user%40mail:p%40ss%3Aword@127.0.0.1:{port}"
            with mock.patch.object(gr, "registration_log"):
                response = gr.http_get(
                    "http://registration.test/probe",
                    proxies={"http": proxy},
                    timeout=5,
                )
            thread.join(timeout=5)

        expected = "Basic " + base64.b64encode(b"user@mail:p@ss:word").decode("ascii")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.get("authorization"), expected)

    def test_actual_http_route_log_deduplicates_query_variants(self):
        logs = []
        with mock.patch.object(gr, "registration_log", side_effect=logs.append):
            gr.reset_network_route_logs()
            gr._log_actual_http_route(
                "get",
                "https://accounts.x.ai/sign-up?step=1",
                proxies={"https": "http://127.0.0.1:7897"},
            )
            gr._log_actual_http_route(
                "GET",
                "https://accounts.x.ai/sign-up?step=2",
                proxies={"https": "http://127.0.0.1:7897"},
            )
            gr._log_actual_http_route("GET", "http://mail.test/api/emails", proxies={})

        self.assertEqual(len(logs), 2)
        self.assertIn("GET https://accounts.x.ai/sign-up -> 代理 http://127.0.0.1:7897", logs[0])
        self.assertIn("GET http://mail.test/api/emails -> 直连（不使用代理）", logs[1])

    def test_actual_http_route_log_redacts_proxy_credentials(self):
        logs = []
        proxy = "http://proxy-user:p%40ss@proxy.example.com:7897"
        with mock.patch.object(gr, "registration_log", side_effect=logs.append):
            gr.reset_network_route_logs()
            gr._log_actual_http_route(
                "GET",
                "https://accounts.x.ai/sign-up",
                proxies={"https": proxy},
            )

        self.assertEqual(len(logs), 1)
        self.assertNotIn("proxy-user", logs[0])
        self.assertNotIn("p%40ss", logs[0])
        self.assertIn("代理 http://***:***@proxy.example.com:7897", logs[0])

    def test_outlook_acquire_and_code_polling_use_direct_default_http(self):
        with mock.patch.object(
            gr.outlookemail_provider,
            "acquire_email",
            return_value=("fixture@outlook.com", "fixture-token"),
        ) as acquire:
            gr.outlookemail_get_email_and_token()
        self.assertIs(acquire.call_args.args[0], gr.http_get)
        self.assertIs(acquire.call_args.args[1], gr.direct_http_session)
        self.assertEqual(acquire.call_args.kwargs["proxies"], {})

        with mock.patch.object(
            gr.outlookemail_provider,
            "wait_for_code",
            return_value="ABC-123",
        ) as wait:
            gr.outlookemail_get_oai_code("fixture@outlook.com")
        self.assertIs(wait.call_args.args[0], gr.http_get)
        self.assertIs(wait.call_args.args[1], gr.direct_http_session)
        self.assertEqual(wait.call_args.kwargs["proxies"], {})

    def test_default_http_wrappers_disable_environment_and_project_proxy(self):
        gr.config["proxy"] = "http://127.0.0.1:7897"
        for method, request_fn in (
            ("GET", gr.http_get),
            ("POST", gr.http_post),
            ("DELETE", gr.http_delete),
        ):
            with self.subTest(method=method):
                response = mock.Mock()
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False
                session.request.return_value = response
                raw_request = session.request
                with mock.patch.object(
                    gr.requests, "Session", return_value=session
                ) as factory:
                    result = request_fn("http://mail-service.test/api")
                self.assertIs(result, response)
                factory.assert_called_once_with(trust_env=False)
                raw_request.assert_called_once_with(
                    method,
                    "http://mail-service.test/api",
                    proxies={},
                    timeout=15,
                )

    def test_xai_connectivity_check_explicitly_uses_configured_proxy(self):
        response = mock.Mock(status_code=200, text="<!doctype html>", headers={})
        http_get = mock.Mock(return_value=response)
        proxy = "http://127.0.0.1:7897"
        _, ok, detail = network_checks.check_xai_signup(proxy, http_get)
        self.assertTrue(ok, detail)
        self.assertEqual(
            http_get.call_args.kwargs["proxies"],
            {"http": proxy, "https": proxy},
        )

    def test_outlook_connectivity_check_uses_direct_default_http(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "accounts": []}
        direct_get = mock.Mock(return_value=response)
        name, ok, detail = network_checks.check_email_api(
            "outlookemail",
            {
                "outlookemail_api_base": "http://mail-pool.test",
                "outlookemail_source": "accounts",
                "outlookemail_api_key": "api-key",
                "outlookemail_group_id": "",
            },
            direct_get,
            mock.Mock(),
        )
        self.assertEqual(name, "邮箱API")
        self.assertTrue(ok, detail)
        self.assertEqual(direct_get.call_args.kwargs["proxies"], {})

    def test_outlook_disable_is_forced_direct(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        with mock.patch.object(
            gr.outlookemail_provider,
            "account_for_email",
            return_value={"id": 1, "email": "fixture@outlook.com"},
        ) as lookup, mock.patch.object(
            gr.outlookemail_provider,
            "disable_account",
            return_value={"success": True, "account_id": 1},
        ) as disable:
            detail = gr.disable_outlookemail_after_cpa_success(
                "fixture@outlook.com", {"status": "success"}
            )
        self.assertEqual(detail["status"], "success")
        self.assertIs(lookup.call_args.args[0], gr.http_get)
        self.assertIs(disable.call_args.args[0], gr.http_get)
        self.assertIs(disable.call_args.args[1], gr.direct_http_session)
        self.assertEqual(disable.call_args.kwargs["proxies"], {})

    def test_sso_token_exchange_uses_proxy_but_cpa_remote_upload_is_direct(self):
        proxy = "http://proxy-user:p%40ss@127.0.0.1:7897"
        gr.config.update(
            {
                "proxy": proxy,
                "cpa_auto_add": True,
                "cpa_token_mode": "device_protocol",
                "cpa_auth_dir": "",
                "cpa_remote_url": "http://cpa.internal:8317",
                "cpa_management_key": "management-key",
                "grok2api_auth_dir": "",
                "grok2api_remote_url": "",
                "grok2api_remote_username": "",
                "grok2api_remote_password": "",
            }
        )
        with mock.patch.object(
            gr._s2cpa,
            "sso_to_token",
            return_value={"access_token": "access", "refresh_token": "refresh"},
        ) as exchange, mock.patch.object(
            gr._s2cpa,
            "token_to_cpa_record",
            return_value={"access_token": "access", "email": "fixture@example.com"},
        ), mock.patch.object(
            gr._s2cpa,
            "decode_jwt_payload",
            return_value={},
        ), mock.patch.object(
            gr._s2cpa,
            "upload_cpa_auth_remote",
            return_value="xai-fixture.json",
        ) as upload:
            logs = []
            self.assertTrue(
                gr.add_sso_to_cpa(
                    "sso-value",
                    email="fixture@example.com",
                    log_callback=logs.append,
                )
            )

        self.assertEqual(exchange.call_args.kwargs["proxy"], proxy)
        self.assertEqual(upload.call_args.kwargs["proxy"], "")
        rendered_logs = "\n".join(logs)
        self.assertNotIn("proxy-user", rendered_logs)
        self.assertNotIn("p%40ss", rendered_logs)
        self.assertIn("proxy=http://***:***@127.0.0.1:7897", rendered_logs)

    def test_cpa_remote_http_session_does_not_inherit_environment_proxy(self):
        response = mock.Mock(status_code=200, reason="OK", text="")
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.post.return_value = response
        with mock.patch.object(auth_exchange.requests, "Session", return_value=session) as factory:
            name = auth_exchange.upload_cpa_auth_remote(
                "http://cpa.internal:8317",
                "management-key",
                {"email": "fixture@example.com"},
                proxy="",
            )
        self.assertEqual(name, "xai-fixture@example.com.json")
        factory.assert_called_once_with(trust_env=False)
        self.assertIsNone(session.post.call_args.kwargs["proxies"])


class StickyProxyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)
        clear_sticky_proxy_session()

    def tearDown(self):
        clear_sticky_proxy_session()
        gr.config.clear()
        gr.config.update(self.original_config)

    def test_disabled_sticky_proxy_keeps_placeholder_literal(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = False
        self.assertEqual(gr.get_proxies()["http"], "http://{id}@127.0.0.1:1080")
        self.assertEqual(gr._ensure_sticky_proxy_for_slot(0), "")

    def test_enabled_sticky_proxy_reuses_id_within_slot(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        first = gr._ensure_sticky_proxy_for_slot(0)
        again = gr._ensure_sticky_proxy_for_slot(0)
        next_slot = gr._ensure_sticky_proxy_for_slot(1)
        self.assertTrue(first)
        self.assertEqual(first, again)
        self.assertNotEqual(first, next_slot)
        self.assertEqual(gr.get_proxies()["http"], f"http://{next_slot}@127.0.0.1:1080")
        self.assertEqual(gr.get_proxies()["https"], f"http://{next_slot}@127.0.0.1:1080")
        self.assertEqual(gr._resolve_cpa_proxy(), f"http://{next_slot}@127.0.0.1:1080")

    def test_sticky_proxy_without_placeholder_is_unchanged(self):
        gr.config["proxy"] = "http://127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        self.assertEqual(gr._ensure_sticky_proxy_for_slot(0), "")
        self.assertEqual(gr.get_proxies()["http"], "http://127.0.0.1:1080")

    def test_sticky_proxy_survives_docker_host_rewrite(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        session_id = gr._ensure_sticky_proxy_for_slot(0)
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                gr.get_proxies()["http"],
                f"http://{session_id}@host.docker.internal:1080",
            )

    def test_camoufox_uses_substituted_sticky_username(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        session_id = gr._ensure_sticky_proxy_for_slot(0)
        browser_session.configure(
            get_proxies=gr.get_proxies,
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {"server": "http://127.0.0.1:1080", "username": session_id, "password": ""},
        )

    def test_start_browser_logs_redacted_sticky_username(self):
        gr.config["proxy"] = "http://user-{id}:secret@10.0.160.176:1110"
        gr.config["sticky_proxy"] = True
        session_id = gr._ensure_sticky_proxy_for_slot(0)
        logs = []
        launched = {}

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            def new_page(self):
                return FakePage()

            def close(self):
                pass

        def fake_launch(opts):
            launched["proxy"] = dict(opts.get("proxy") or {})
            return FakeContext(), None

        browser_session.configure(
            get_proxies=gr.get_proxies,
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        with mock.patch.object(
            browser_session,
            "_launch_camoufox_context",
            side_effect=fake_launch,
        ):
            try:
                browser_session.start_browser(log_callback=logs.append)
            finally:
                browser_session.stop_browser(force=True)
                browser_session.allow_browser_launches()
        network_lines = [line for line in logs if "网络:" in line]
        self.assertEqual(len(network_lines), 1)
        self.assertIn(f"user-{session_id}", network_lines[0])
        self.assertIn("10.0.160.176:1110", network_lines[0])
        self.assertIn("***", network_lines[0])
        self.assertNotIn("secret", network_lines[0])
        self.assertIn("本地转发 http://127.0.0.1:", network_lines[0])
        self.assertTrue(str(launched["proxy"].get("server") or "").startswith("http://127.0.0.1:"))
        self.assertNotIn("username", launched["proxy"])
        self.assertNotIn("password", launched["proxy"])

    def test_http_route_log_keeps_sticky_username(self):
        gr.config["proxy"] = "http://user-{id}:secret@10.0.160.176:1110"
        gr.config["sticky_proxy"] = True
        session_id = gr._ensure_sticky_proxy_for_slot(0)
        logs = []
        gr.reset_network_route_logs()
        with mock.patch.object(gr, "registration_log", side_effect=logs.append):
            gr._log_actual_http_route(
                "GET",
                "https://www.cloudflare.com/cdn-cgi/trace",
                proxy=gr.get_proxies()["https"],
            )
        self.assertEqual(len(logs), 1)
        self.assertIn(f"user-{session_id}", logs[0])
        self.assertIn("***", logs[0])
        self.assertNotIn("secret", logs[0])

    def test_connectivity_check_replaces_sticky_placeholder(self):
        seen = []

        def fake_check_proxy(proxy_url, http_get):
            seen.append(proxy_url)
            return "代理", True, "ok"

        with mock.patch.object(
            network_checks, "check_proxy", side_effect=fake_check_proxy
        ), mock.patch.object(
            network_checks, "check_xai_signup", return_value=("xAI注册页", True, "ok")
        ), mock.patch.object(
            network_checks, "check_email_api", return_value=("邮箱", True, "ok")
        ), mock.patch.object(
            network_checks, "check_cpa", return_value=("CPA", True, "ok")
        ):
            results = network_checks.run_connectivity_checks(
                {
                    "proxy": "http://{id}@127.0.0.1:1080",
                    "sticky_proxy": True,
                    "email_provider": "cloudflare",
                },
                http_get=mock.Mock(),
                http_post=mock.Mock(),
            )
        self.assertEqual(len(seen), 1)
        self.assertNotIn("{id}", seen[0])
        self.assertTrue(seen[0].endswith("@127.0.0.1:1080"))
        self.assertEqual(current_sticky_proxy_id(), "")
        probe = results[0]
        self.assertEqual(probe[0], network_checks.STICKY_PROBE_CHECK_NAME)
        self.assertTrue(probe[1])
        session_id = seen[0].split("://", 1)[1].split("@", 1)[0]
        self.assertIn(session_id, probe[2])
        self.assertIn("仅启动检查", probe[2])

    def test_connectivity_check_reports_missing_sticky_placeholder(self):
        with mock.patch.object(
            network_checks, "check_proxy", return_value=("代理", True, "ok")
        ), mock.patch.object(
            network_checks, "check_xai_signup", return_value=("xAI注册页", True, "ok")
        ), mock.patch.object(
            network_checks, "check_email_api", return_value=("邮箱", True, "ok")
        ), mock.patch.object(
            network_checks, "check_cpa", return_value=("CPA", True, "ok")
        ):
            results = network_checks.run_connectivity_checks(
                {
                    "proxy": "http://user:pass@127.0.0.1:1080",
                    "sticky_proxy": True,
                    "email_provider": "cloudflare",
                },
                http_get=mock.Mock(),
                http_post=mock.Mock(),
            )
        self.assertEqual(results[0][0], network_checks.STICKY_PROBE_CHECK_NAME)
        self.assertIn("没有 {id}", results[0][2])

    def test_worker_threads_get_independent_sticky_ids(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        results = []

        def worker():
            results.append(gr._ensure_sticky_proxy_for_slot(0))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(results))
        self.assertEqual(len(set(results)), 2)

    def test_camoufox_username_only_proxy_uses_local_forwarder(self):
        gr.config["proxy"] = "http://grok-reg-{id}@10.0.160.176:1110"
        gr.config["sticky_proxy"] = True
        session_id = gr._ensure_sticky_proxy_for_slot(0)
        launched = {}
        logs = []

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            def close(self):
                pass

        def fake_launch(opts):
            launched["proxy"] = dict(opts.get("proxy") or {})
            return FakeContext(), None

        browser_session.configure(
            get_proxies=gr.get_proxies,
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        with mock.patch.object(
            browser_session,
            "_launch_camoufox_context",
            side_effect=fake_launch,
        ):
            try:
                browser_session.start_browser(log_callback=logs.append)
            finally:
                browser_session.stop_browser(force=True)
                browser_session.allow_browser_launches()
        self.assertTrue(str(launched["proxy"].get("server") or "").startswith("http://127.0.0.1:"))
        self.assertNotIn("username", launched["proxy"])
        network_lines = [line for line in logs if "网络:" in line]
        self.assertEqual(len(network_lines), 1)
        self.assertIn(f"grok-reg-{session_id}", network_lines[0])
        self.assertIn("本地转发", network_lines[0])

    def test_camoufox_unauthenticated_proxy_is_not_forwarded(self):
        launched = {}

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            def close(self):
                pass

        def fake_launch(opts):
            launched["proxy"] = dict(opts.get("proxy") or {})
            return FakeContext(), None

        browser_session.configure(
            get_proxies=lambda: {"https": "http://10.0.160.176:1110"},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        with mock.patch.object(
            browser_session,
            "_launch_camoufox_context",
            side_effect=fake_launch,
        ):
            try:
                browser_session.start_browser()
            finally:
                browser_session.stop_browser(force=True)
                browser_session.allow_browser_launches()
        self.assertEqual(launched["proxy"], {"server": "http://10.0.160.176:1110"})

    def test_sticky_session_change_restarts_running_browser(self):
        gr.config["proxy"] = "http://{id}@127.0.0.1:1080"
        gr.config["sticky_proxy"] = True
        browser_session.configure(
            get_proxies=gr.get_proxies,
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        first = gr._ensure_sticky_proxy_for_slot(0)
        browser_session._tls.launch_proxy_url = gr.get_proxies()["https"]
        with mock.patch.object(
            browser_session, "active_browser", return_value=object()
        ), mock.patch.object(gr, "restart_browser") as restart:
            second = gr._rebind_sticky_proxy_for_account(1)
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first, second)
        restart.assert_called_once()

    def test_cloakbrowser_keeps_native_authenticated_proxy(self):
        launched = {}

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            def close(self):
                pass

        def fake_launch(opts):
            launched["proxy"] = dict(opts.get("proxy") or {})
            return FakeContext(), None

        browser_session.configure(
            get_proxies=lambda: {
                "https": "http://user-{id}:secret@proxy.example.com:8080".replace(
                    "{id}", "sess01"
                )
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "cloakbrowser",
        )
        with mock.patch.object(
            browser_session,
            "_launch_cloakbrowser_context",
            side_effect=fake_launch,
        ):
            try:
                browser_session.start_browser()
            finally:
                browser_session.stop_browser(force=True)
                browser_session.allow_browser_launches()
        self.assertEqual(
            launched["proxy"],
            {
                "server": "http://proxy.example.com:8080",
                "username": "user-sess01",
                "password": "secret",
            },
        )


if __name__ == "__main__":
    unittest.main()
