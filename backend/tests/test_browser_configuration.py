import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.automation import session as browser_session
from backend.registration import engine as gr


class BrowserHeadlessConfigTests(unittest.TestCase):
    def tearDown(self):
        browser_session.stop_browser(force=True)
        browser_session.allow_browser_launches()
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "camoufox",
            is_low_traffic=lambda: False,
            get_traffic_savings_level=lambda: "standard",
        )

    def test_camoufox_remains_default_browser_engine(self):
        browser_session.configure(get_engine=None)
        self.assertEqual(browser_session.selected_browser_engine(), "camoufox")

    def test_invalid_browser_engine_falls_back_to_camoufox(self):
        browser_session.configure(get_engine=lambda: "unknown")
        self.assertEqual(browser_session.selected_browser_engine(), "camoufox")

    def test_browser_options_follow_headless_setting(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: True,
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertIs(options["headless"], True)

        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertIs(options["headless"], False)

    def test_container_force_headed_overrides_config(self):
        with mock.patch.dict(gr.os.environ, {"GROK_FORCE_HEADED": "1"}, clear=False):
            with mock.patch.dict(gr.config, {"browser_headless": True}, clear=False):
                self.assertFalse(gr.is_browser_headless())

    def test_browser_options_force_configured_locale(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "zh-CN",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["locale"], "zh-CN")

    def test_invalid_browser_locale_falls_back_to_english(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "fr-FR",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["locale"], "en-US")

    def test_low_traffic_mode_excludes_default_camoufox_addons(self):
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            is_low_traffic=lambda: True,
        )
        sentinel = [object()]
        with mock.patch.object(
            browser_session,
            "_ensure_default_addons_or_exclude",
            return_value=sentinel,
        ) as exclude:
            options = browser_session.create_camoufox_options(unique_profile=False)

        exclude.assert_called_once_with(disable_defaults=True)
        self.assertIs(options["exclude_addons"], sentinel)
        self.assertEqual(options["timeout"], browser_session._BROWSER_LAUNCH_TIMEOUT_MS)

    def test_low_traffic_request_rules_preserve_registration_and_turnstile(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_block(
                "https://cdn.grok.com/assets/hero.webp", "image"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_block(
                "https://media.x.ai/video.mp4", "media"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://challenges.cloudflare.com/turnstile/v0/api.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_block(
                "https://accounts.x.ai/api/register", "fetch"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/sign-up", "document"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/cdn-cgi/challenge-platform/main.js", "script"
            )
        )

    def test_more_savings_caches_accounts_hashed_static_resources(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "standard",
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )

        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/sign-up", "document"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/cdn-cgi/challenge-platform/main.js", "script"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/api/register", "fetch"
            )
        )

    def test_accounts_resource_diagnostics_logs_real_response_size_in_debug(self):
        browser_session.configure(is_debug=lambda: True)
        context = mock.Mock()
        logs = []
        browser_session._install_accounts_resource_diagnostics(context, logs.append)
        callback = context.on.call_args.args[1]
        response = mock.Mock(
            status=200, headers={"content-length": "999"}
        )
        request = mock.Mock(
            url="https://accounts.x.ai/assets/app.js?build=secret",
            resource_type="script",
            response=mock.Mock(return_value=response),
            sizes=mock.Mock(return_value={"responseBodySize": 5}),
        )

        callback(request)

        self.assertEqual(len(logs), 1)
        self.assertIn("type=script status=200 bytes=5", logs[0])
        self.assertIn("url=https://accounts.x.ai/assets/app.js", logs[0])
        self.assertNotIn("build=secret", logs[0])

    def test_accounts_resource_diagnostics_ignores_other_hosts(self):
        browser_session.configure(is_debug=lambda: True)
        context = mock.Mock()
        logs = []
        browser_session._install_accounts_resource_diagnostics(context, logs.append)
        callback = context.on.call_args.args[1]

        callback(
            mock.Mock(
                url="https://cdn.grok.com/assets/app.js",
                status=200,
                headers={},
                request=mock.Mock(resource_type="script"),
            )
        )

        self.assertEqual(logs, [])

    def test_low_traffic_does_not_intercept_signup_document_or_turnstile(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        self.assertFalse(
            browser_session.low_traffic_should_intercept(
                "https://accounts.x.ai/sign-up?redirect=grok-com"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_intercept(
                "https://accounts.x.ai/api/register"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_intercept(
                "https://challenges.cloudflare.com/turnstile/v0/api.js"
            )
        )
        self.assertFalse(
            browser_session.low_traffic_should_intercept("https://grok.com/")
        )
        self.assertTrue(
            browser_session.low_traffic_should_intercept(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_intercept(
                "https://cdn.grok.com/assets/app.js"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_intercept(
                "https://cdn.grok.com/assets/hero.webp"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_intercept(
                "https://grok.com/assets/hero.webp"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_intercept(
                "https://cdn.cookielaw.org/script.js"
            )
        )

        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "standard",
        )
        self.assertFalse(
            browser_session.low_traffic_should_intercept(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js"
            )
        )

    def test_low_traffic_routing_uses_native_network_for_uncached_assets(self):
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )
        context = mock.Mock()
        browser_session._install_low_traffic_routing(context)

        matcher, handler = context.route.call_args.args
        self.assertTrue(callable(matcher))
        self.assertFalse(matcher("https://accounts.x.ai/sign-up?redirect=grok-com"))
        self.assertTrue(matcher("https://cdn.grok.com/assets/app.js"))
        context.on.assert_called()

        route = mock.Mock()
        request = mock.Mock(
            url="https://cdn.grok.com/assets/app.js",
            resource_type="script",
            method="GET",
            headers={},
        )
        with mock.patch.object(browser_session, "_cached_response", return_value=None):
            handler(route, request)
        route.continue_.assert_called_once()
        route.fetch.assert_not_called()
        route.fulfill.assert_not_called()

        route.reset_mock()
        with mock.patch.object(
            browser_session,
            "_cached_response",
            return_value=(200, {"content-type": "application/javascript"}, b"ok"),
        ):
            handler(route, request)
        route.fulfill.assert_called_once()
        route.fetch.assert_not_called()
        route.continue_.assert_not_called()

        route.reset_mock()
        image = mock.Mock(
            url="https://cdn.grok.com/assets/hero.webp",
            resource_type="image",
            method="GET",
            headers={},
        )
        handler(route, image)
        route.abort.assert_called_once()

        route.reset_mock()
        document = mock.Mock(
            url="https://accounts.x.ai/sign-up?redirect=grok-com",
            resource_type="document",
            method="GET",
            headers={},
        )
        handler(route, document)
        route.continue_.assert_called_once()
        route.fetch.assert_not_called()

    def test_accounts_resource_diagnostics_is_disabled_outside_debug(self):
        browser_session.configure(is_debug=lambda: False)
        context = mock.Mock()

        browser_session._install_accounts_resource_diagnostics(context, mock.Mock())

        context.on.assert_not_called()

    def test_cloakbrowser_options_share_proxy_locale_and_headless_settings(self):
        browser_session.configure(
            get_proxies=lambda: {"https": "http://user:pass@proxy.example.com:8080"},
            is_debug=lambda: False,
            is_headless=lambda: True,
            get_locale=lambda: "zh-CN",
            get_engine=lambda: "cloakbrowser",
        )

        options = browser_session.create_browser_options(unique_profile=False)

        self.assertIs(options["headless"], True)
        self.assertIs(options["humanize"], True)
        self.assertIs(options["geoip"], True)
        self.assertEqual(options["locale"], "zh-CN")
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:8080",
                "username": "user",
                "password": "pass",
            },
        )

    def test_start_browser_dispatches_to_cloakbrowser_backend(self):
        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]
                self.closed = False

            def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

            def close(self):
                self.closed = True

        context = FakeContext()
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "cloakbrowser",
        )
        with mock.patch.object(
            browser_session,
            "_launch_cloakbrowser_context",
            return_value=(context, None),
        ) as launch:
            browser, page = browser_session.start_browser()

        self.assertEqual(browser.engine_name, "cloakbrowser")
        self.assertIs(page.raw_page, context.pages[0])
        launch.assert_called_once()


class LowTrafficCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"GROK_BROWSER_CACHE_DIR": str(self.cache_dir)})
        self._env.start()
        browser_session._low_traffic_cache_pruned = False
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "more",
        )

    def tearDown(self):
        self._env.stop()
        browser_session._low_traffic_cache_pruned = False
        browser_session.configure(
            is_low_traffic=lambda: False,
            get_traffic_savings_level=lambda: "standard",
        )
        self._tmp.cleanup()

    def _script_response(self, body=b"console.log(1)"):
        return mock.Mock(
            status=200,
            headers={"content-type": "application/javascript"},
            body=mock.Mock(return_value=body),
        )

    def test_routing_still_intercepts_all_requests(self):
        context = mock.Mock()
        browser_session._install_low_traffic_routing(context)
        matcher, _handler = context.route.call_args.args
        self.assertIs(matcher, browser_session.low_traffic_should_intercept)

    def test_cache_miss_fetches_stores_and_refills_after_clear(self):
        context = mock.Mock()
        logs = []
        browser_session._install_low_traffic_routing(context, logs.append)
        _matcher, handler = context.route.call_args.args
        url = "https://accounts.x.ai/_next/static/chunks/app-hash.js"
        route = mock.Mock()
        request = mock.Mock(url=url, resource_type="script", method="GET", headers={})
        response_cb = context.on.call_args.args[1]
        fake_response = mock.Mock(
            status=200,
            headers={"content-type": "application/javascript"},
            body=mock.Mock(return_value=b"console.log('bundle')"),
            request=request,
        )

        handler(route, request)
        route.continue_.assert_called_once()
        route.fetch.assert_not_called()
        response_cb(fake_response)
        snapshot = browser_session.inspect_low_traffic_cache()
        self.assertEqual(snapshot["entry_count"], 1)
        self.assertEqual(snapshot["entries"][0]["scope"], "more")
        self.assertTrue(snapshot["entries"][0]["active"])
        self.assertEqual(snapshot["entries"][0]["url"], url)
        self.assertTrue(any("低流量缓存未命中，已重新下载" in message for message in logs))

        route.reset_mock()
        handler(route, request)
        route.fetch.assert_not_called()
        route.fulfill.assert_called_once()

        cleared = browser_session.clear_low_traffic_cache()
        self.assertEqual(cleared["entry_count"], 0)
        self.assertGreaterEqual(cleared["deleted_files"], 2)

        route.reset_mock()
        handler(route, request)
        route.continue_.assert_called_once()
        response_cb(fake_response)
        refilled = browser_session.inspect_low_traffic_cache()
        self.assertEqual(refilled["entry_count"], 1)
        self.assertTrue(refilled["entries"][0]["active"])

    def test_legacy_cache_without_url_is_labeled_on_hit(self):
        url = "https://cdn.grok.com/assets/app.js"
        browser_session._store_cached_response(
            url,
            200,
            {"content-type": "application/javascript"},
            b"cdn",
        )
        meta_path, _body_path = browser_session._low_traffic_cache_paths(url)
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata.pop("url", None)
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
        snapshot = browser_session.inspect_low_traffic_cache()
        self.assertEqual(snapshot["entries"][0]["url"], "")
        self.assertEqual(snapshot["entries"][0]["scope"], "unknown")

        context = mock.Mock()
        browser_session._install_low_traffic_routing(context)
        _matcher, handler = context.route.call_args.args
        route = mock.Mock()
        request = mock.Mock(url=url, resource_type="script", method="GET", headers={})
        handler(route, request)
        route.fetch.assert_not_called()
        labeled = browser_session.inspect_low_traffic_cache()
        self.assertEqual(labeled["entries"][0]["url"], url)
        self.assertEqual(labeled["entries"][0]["scope"], "standard")
        self.assertTrue(labeled["entries"][0]["active"])

    def test_missing_savings_level_defaults_to_standard(self):
        browser_session.configure(is_low_traffic=lambda: True, get_traffic_savings_level=None)
        self.assertEqual(browser_session.traffic_savings_level(), "standard")
        self.assertFalse(
            browser_session.low_traffic_should_cache(
                "https://accounts.x.ai/_next/static/chunks/app-hash.js", "script"
            )
        )
        self.assertTrue(
            browser_session.low_traffic_should_cache(
                "https://cdn.grok.com/assets/app.js", "script"
            )
        )

    def test_more_mode_logs_quality_warning(self):
        context = mock.Mock()
        logs = []
        browser_session._install_low_traffic_routing(context, logs.append)
        self.assertTrue(any("accounts.x.ai" in message for message in logs))
        self.assertTrue(any("高风险 JS" in message for message in logs))

        logs.clear()
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "standard",
        )
        context = mock.Mock()
        browser_session._install_low_traffic_routing(context, logs.append)
        self.assertTrue(any("低流量模式：已启用 grok.com 静态资源缓存" in message for message in logs))
        self.assertFalse(any("降智" in message for message in logs))

    def test_cache_risk_classifier_flags_castle_mixpanel_and_turnstile(self):
        castle = browser_session.classify_low_traffic_cache_risk(
            "https://accounts.x.ai/_next/static/chunks/245w6.js",
            b"window.RTCPeerConnection; fetch('https://m.castle.io/v1/monitor'); navigator.userAgentData.getHighEntropyValues([])",
            "application/javascript",
        )
        self.assertEqual(castle["level"], "high")
        self.assertFalse(castle["replay_safe"])
        self.assertTrue(any("Castle" in reason for reason in castle["reasons"]))

        mixpanel = browser_session.classify_low_traffic_cache_risk(
            "https://accounts.x.ai/_next/static/chunks/mix.js",
            b"https://api-js.mixpanel.com/track",
            "application/javascript",
        )
        self.assertEqual(mixpanel["level"], "high")
        self.assertFalse(mixpanel["replay_safe"])

        css = browser_session.classify_low_traffic_cache_risk(
            "https://accounts.x.ai/_next/static/chunks/app.css",
            b"body{color:#000}",
            "text/css",
        )
        self.assertEqual(css["level"], "low")
        self.assertTrue(css["replay_safe"])

        ads = browser_session.classify_low_traffic_cache_risk(
            "https://accounts.x.ai/_next/static/chunks/ads.js",
            b"https://connect.facebook.net/en_US/fbevents.js",
            "application/javascript",
        )
        self.assertEqual(ads["level"], "medium")
        self.assertTrue(ads["replay_safe"])

    def test_high_risk_script_is_not_replayed_from_cache(self):
        url = "https://accounts.x.ai/_next/static/chunks/castle.js"
        body = b"fetch('https://m.castle.io/v1/monitor'); window.RTCPeerConnection"
        browser_session._store_cached_response(
            url,
            200,
            {"content-type": "application/javascript"},
            body,
        )
        context = mock.Mock()
        logs = []
        browser_session._install_low_traffic_routing(context, logs.append)
        _matcher, handler = context.route.call_args.args
        route = mock.Mock()
        route.fetch.return_value = self._script_response(body)
        request = mock.Mock(url=url, resource_type="script", method="GET", headers={})
        handler(route, request)
        route.fetch.assert_called_once()
        self.assertTrue(any("跳过回放" in message and "Castle" in message for message in logs))
        snapshot = browser_session.inspect_low_traffic_cache()
        entry = snapshot["entries"][0]
        self.assertEqual(entry["risk_level"], "high")
        self.assertFalse(entry["active"])
        self.assertFalse(entry["replay_safe"])
        self.assertGreaterEqual(snapshot["high_risk_count"], 1)

    def test_standard_mode_does_not_treat_accounts_hash_cache_as_active(self):
        browser_session._store_cached_response(
            "https://cdn.grok.com/assets/app.js",
            200,
            {"content-type": "application/javascript"},
            b"cdn",
        )
        browser_session._store_cached_response(
            "https://accounts.x.ai/_next/static/chunks/app-hash.js",
            200,
            {"content-type": "application/javascript"},
            b"accounts",
        )
        browser_session.configure(
            is_low_traffic=lambda: True,
            get_traffic_savings_level=lambda: "standard",
        )
        snapshot = browser_session.inspect_low_traffic_cache()
        by_scope = {item["scope"]: item for item in snapshot["entries"]}
        self.assertTrue(by_scope["standard"]["active"])
        self.assertFalse(by_scope["more"]["active"])
        self.assertEqual(snapshot["active_count"], 1)


if __name__ == "__main__":
    unittest.main()
