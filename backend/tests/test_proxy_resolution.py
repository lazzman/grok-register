import unittest
from unittest import mock

from backend.integrations.proxy import (
    apply_sticky_proxy_id,
    bind_sticky_proxy_session,
    clear_sticky_proxy_session,
    current_sticky_proxy_id,
    current_sticky_proxy_slot,
    format_proxy_options_for_log,
    parse_http_proxy_url,
    redact_proxy_text,
    redact_proxy_url,
    resolve_proxy_url,
    validate_http_proxy_url,
)


class DockerProxyResolutionTests(unittest.TestCase):
    def test_localhost_proxy_maps_to_docker_host(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://127.0.0.1:7897"),
                "http://host.docker.internal:7897",
            )

    def test_credentials_are_preserved(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("socks5://user:pass@localhost:7897"),
                "socks5://user:pass@host.docker.internal:7897",
            )

    def test_encoded_http_credentials_are_preserved_during_host_rewrite(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://user%40mail:p%40ss%3Aword@localhost:7897"),
                "http://user%40mail:p%40ss%3Aword@host.docker.internal:7897",
            )

    def test_regular_proxy_is_unchanged(self):
        with mock.patch.dict(
            "os.environ", {"GROK_DOCKER_PROXY_HOST": "host.docker.internal"}, clear=False
        ):
            self.assertEqual(
                resolve_proxy_url("http://proxy.example.com:7897"),
                "http://proxy.example.com:7897",
            )


class HttpProxyParsingTests(unittest.TestCase):
    def test_authenticated_http_proxy_is_split_for_camoufox(self):
        self.assertEqual(
            parse_http_proxy_url("http://user:password@proxy.example.com:8080"),
            {
                "server": "http://proxy.example.com:8080",
                "username": "user",
                "password": "password",
            },
        )

    def test_percent_encoded_credentials_are_decoded(self):
        self.assertEqual(
            parse_http_proxy_url(
                "https://user%40mail.example:p%40ss%3Aword@proxy.example.com:8443"
            ),
            {
                "server": "https://proxy.example.com:8443",
                "username": "user@mail.example",
                "password": "p@ss:word",
            },
        )

    def test_original_encoded_url_is_retained_for_http_clients(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(validate_http_proxy_url(proxy), proxy)

    def test_invalid_percent_encoding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad%ZZ@proxy.example.com:8080")

    def test_unencoded_path_character_in_credentials_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "百分号编码"):
            validate_http_proxy_url("http://user:bad/word@proxy.example.com:8080")

    def test_proxy_credentials_are_redacted_for_display_and_log_text(self):
        proxy = "http://user%40mail:p%40ss@proxy.example.com:8080"
        self.assertEqual(
            redact_proxy_url(proxy),
            "http://***:***@proxy.example.com:8080",
        )
        message = redact_proxy_text(f"request failed via {proxy}")
        self.assertNotIn("user%40mail", message)
        self.assertNotIn("p%40ss", message)
        self.assertIn("http://***:***@proxy.example.com:8080", message)

        malformed = redact_proxy_text(
            "failed via http://user:raw/secret@proxy.example.com:8080"
        )
        self.assertNotIn("raw/secret", malformed)

    def test_keep_username_masks_only_password(self):
        proxy = "http://user-sid-abc123:p%40ss@proxy.example.com:8080"
        self.assertEqual(
            redact_proxy_url(proxy, keep_username=True),
            "http://user-sid-abc123:***@proxy.example.com:8080",
        )
        self.assertEqual(
            redact_proxy_url("http://{id}@127.0.0.1:1080", keep_username=True),
            "http://{id}@127.0.0.1:1080",
        )

    def test_camoufox_proxy_options_log_keeps_username(self):
        self.assertEqual(
            format_proxy_options_for_log(
                {
                    "server": "http://10.0.160.176:1110",
                    "username": "user-sid-ad1b1e9a32a640aa",
                    "password": "secret",
                }
            ),
            "http://user-sid-ad1b1e9a32a640aa:***@10.0.160.176:1110",
        )
        self.assertEqual(
            format_proxy_options_for_log({"server": "http://10.0.160.176:1110"}),
            "http://10.0.160.176:1110",
        )
        self.assertEqual(format_proxy_options_for_log({}), "")


class StickyProxyTests(unittest.TestCase):
    def tearDown(self):
        clear_sticky_proxy_session()

    def test_placeholder_is_replaced_in_username(self):
        self.assertEqual(
            apply_sticky_proxy_id("http://{id}@127.0.0.1:1080", "abc123def456"),
            "http://abc123def456@127.0.0.1:1080",
        )
        self.assertEqual(
            apply_sticky_proxy_id(
                "http://user-{id}:p%40ss@proxy.example.com:8080", "sess01"
            ),
            "http://user-sess01:p%40ss@proxy.example.com:8080",
        )

    def test_missing_placeholder_or_session_keeps_original_url(self):
        proxy = "http://user:pass@127.0.0.1:1080"
        self.assertEqual(apply_sticky_proxy_id(proxy, "abc123"), proxy)
        self.assertEqual(
            apply_sticky_proxy_id("http://{id}@127.0.0.1:1080", ""),
            "http://{id}@127.0.0.1:1080",
        )

    def test_template_proxy_url_is_valid_before_substitution(self):
        proxy = "http://{id}@127.0.0.1:1080"
        self.assertEqual(validate_http_proxy_url(proxy), proxy)
        parsed = parse_http_proxy_url(proxy)
        self.assertEqual(parsed["server"], "http://127.0.0.1:1080")
        self.assertEqual(parsed["username"], "{id}")

    def test_bind_session_is_thread_local_and_can_track_slot(self):
        first = bind_sticky_proxy_session("alpha", slot=0)
        self.assertEqual(first, "alpha")
        self.assertEqual(current_sticky_proxy_id(), "alpha")
        self.assertEqual(current_sticky_proxy_slot(), 0)
        clear_sticky_proxy_session()
        self.assertEqual(current_sticky_proxy_id(), "")
        self.assertIsNone(current_sticky_proxy_slot())


if __name__ == "__main__":
    unittest.main()
