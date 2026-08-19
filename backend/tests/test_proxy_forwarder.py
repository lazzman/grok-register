import base64
import socket
import socketserver
import threading
import time
import unittest

from backend.integrations.proxy_forwarder import (
    basic_proxy_authorization,
    proxy_dict_has_credentials,
    start_local_auth_proxy,
)


class _UpstreamProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ProxyForwarderTests(unittest.TestCase):
    def test_username_only_is_treated_as_credentials(self):
        self.assertTrue(
            proxy_dict_has_credentials(
                {"server": "http://10.0.160.176:1110", "username": "grok-reg-abc"}
            )
        )
        self.assertFalse(
            proxy_dict_has_credentials({"server": "http://10.0.160.176:1110"})
        )

    def test_username_only_basic_auth_uses_empty_password(self):
        expected = "Basic " + base64.b64encode(b"grok-reg-abc:").decode("ascii")
        self.assertEqual(basic_proxy_authorization("grok-reg-abc", None), expected)
        self.assertEqual(basic_proxy_authorization("grok-reg-abc", ""), expected)

    def test_connect_injects_proxy_authorization(self):
        captured = {}

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                lines = []
                while True:
                    line = self.rfile.readline().decode("iso-8859-1").rstrip("\r\n")
                    if not line:
                        break
                    lines.append(line)
                captured["lines"] = lines
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                try:
                    captured["tunneled"] = self.connection.recv(64)
                except OSError:
                    captured["tunneled"] = b""

        with _UpstreamProxy(("127.0.0.1", 0), Handler) as upstream:
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                port = upstream.server_address[1]
                forwarder = start_local_auth_proxy(
                    {
                        "server": f"http://127.0.0.1:{port}",
                        "username": "grok-reg-sess01",
                    }
                )
                try:
                    with socket.create_connection(
                        ("127.0.0.1", forwarder.listen_port), timeout=5
                    ) as client:
                        client.sendall(
                            b"CONNECT example.com:443 HTTP/1.1\r\n"
                            b"Host: example.com:443\r\n\r\n"
                        )
                        response = client.recv(1024)
                        client.sendall(b"ping-from-browser")
                        time.sleep(0.3)
                finally:
                    forwarder.stop()
            finally:
                upstream.shutdown()
                thread.join(timeout=2)

        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
        expected = "Basic " + base64.b64encode(b"grok-reg-sess01:").decode("ascii")
        self.assertEqual(captured["lines"][0], "CONNECT example.com:443 HTTP/1.1")
        self.assertIn(f"Proxy-Authorization: {expected}", captured["lines"])
        self.assertEqual(captured.get("tunneled"), b"ping-from-browser")

    def test_http_get_injects_proxy_authorization(self):
        captured = {}

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                lines = []
                while True:
                    line = self.rfile.readline().decode("iso-8859-1").rstrip("\r\n")
                    if not line:
                        break
                    lines.append(line)
                captured["lines"] = lines
                body = b"ok"
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\n" + body
                )

        with _UpstreamProxy(("127.0.0.1", 0), Handler) as upstream:
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            try:
                port = upstream.server_address[1]
                forwarder = start_local_auth_proxy(
                    {
                        "server": f"http://127.0.0.1:{port}",
                        "username": "user",
                        "password": "p@ss",
                    }
                )
                try:
                    with socket.create_connection(
                        ("127.0.0.1", forwarder.listen_port), timeout=5
                    ) as client:
                        client.sendall(
                            b"GET http://example.com/ip HTTP/1.1\r\n"
                            b"Host: example.com\r\n\r\n"
                        )
                        response = client.recv(1024)
                finally:
                    forwarder.stop()
            finally:
                upstream.shutdown()
                thread.join(timeout=2)

        self.assertIn(b"200 OK", response)
        self.assertIn(b"ok", response)
        expected = "Basic " + base64.b64encode(b"user:p@ss").decode("ascii")
        self.assertEqual(captured["lines"][0], "GET http://example.com/ip HTTP/1.1")
        self.assertIn(f"Proxy-Authorization: {expected}", captured["lines"])


if __name__ == "__main__":
    unittest.main()
