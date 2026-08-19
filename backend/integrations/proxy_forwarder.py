"""本地 HTTP 代理转发器。

Firefox / Camoufox 不能稳定发送 HTTP 代理认证（``Proxy-Authorization``），
带用户名的粘性代理会在 ``Page.goto`` 上一直等到超时。Python HTTP 客户端没有
这个问题，所以连通性检查能过、浏览器却连不上。

做法：在 ``127.0.0.1`` 起一个无需认证的本地入口，由转发器在连上游时注入
Basic 认证。浏览器只看到 ``http://127.0.0.1:port``。
"""

from __future__ import annotations

import base64
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


_HOP_BY_HOP = {
    b"proxy-authorization",
    b"proxy-connection",
    b"connection",
    b"keep-alive",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


def proxy_dict_has_credentials(proxy: dict | None) -> bool:
    """Playwright 风格 proxy dict 是否带用户名或密码。"""
    if not isinstance(proxy, dict):
        return False
    return proxy.get("username") is not None or proxy.get("password") is not None


def basic_proxy_authorization(username: str | None, password: str | None) -> str:
    """生成 ``Proxy-Authorization`` 的 Basic 值，允许空密码。"""
    token = base64.b64encode(
        f"{username or ''}:{password or ''}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {token}"


def _recv_http_head(sock: socket.socket, limit: int = 65536) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise ValueError("HTTP 头部过长")
    header, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        raise ValueError("未读到完整 HTTP 头部")
    return header + b"\r\n\r\n", rest


def _rewrite_proxy_request(raw_head: bytes, authorization: str) -> bytes:
    lines = raw_head.split(b"\r\n")
    if not lines or not lines[0]:
        raise ValueError("空的 HTTP 请求")
    rewritten = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in _HOP_BY_HOP:
            continue
        rewritten.append(line)
    rewritten.append(f"Proxy-Authorization: {authorization}".encode("latin-1"))
    rewritten.append(b"Proxy-Connection: keep-alive")
    rewritten.append(b"Connection: keep-alive")
    return b"\r\n".join(rewritten) + b"\r\n\r\n"


def _pipe(left: socket.socket, right: socket.socket, idle_timeout: float | None = None) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, idle_timeout)
            if errored:
                break
            if not readable:
                break
            for src in readable:
                dst = right if src is left else left
                try:
                    data = src.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _connect_upstream(server: str, timeout: float = 20) -> socket.socket:
    parsed = urlsplit(server)
    host = parsed.hostname
    if not host:
        raise ValueError("上游代理缺少主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((host, port), timeout=timeout)
    if parsed.scheme == "https":
        import ssl

        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    sock.settimeout(timeout)
    return sock


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client: socket.socket = self.request
        client.settimeout(30)
        upstream = None
        try:
            raw_head, leftover = _recv_http_head(client)
            request_line = raw_head.split(b"\r\n", 1)[0].decode("iso-8859-1")
            parts = request_line.split(" ")
            if len(parts) < 2:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            method = parts[0].upper()
            rewritten = _rewrite_proxy_request(raw_head, self.server.authorization)
            upstream = _connect_upstream(self.server.upstream_server)
            upstream.sendall(rewritten)

            if method == "CONNECT":
                resp_head, resp_rest = _recv_http_head(upstream)
                client.sendall(resp_head)
                if resp_rest:
                    client.sendall(resp_rest)
                status_parts = resp_head.split(b"\r\n", 1)[0].split()
                if len(status_parts) < 2 or status_parts[1] != b"200":
                    return
                if leftover:
                    upstream.sendall(leftover)
                client.settimeout(None)
                upstream.settimeout(None)
                _pipe(client, upstream)
                return

            if leftover:
                upstream.sendall(leftover)
            client.settimeout(None)
            upstream.settimeout(None)
            _pipe(upstream, client, idle_timeout=60)
        except Exception:
            try:
                client.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass


class _ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, upstream_server: str, authorization: str):
        super().__init__(("127.0.0.1", 0), _ForwardHandler)
        self.upstream_server = upstream_server
        self.authorization = authorization


class LocalAuthProxy:
    """把浏览器无认证请求转发到带认证的上游 HTTP 代理。"""

    def __init__(self, proxy: dict):
        server = str(proxy.get("server") or "").strip()
        if not server:
            raise ValueError("本地转发缺少上游代理 server")
        self.upstream_server = server
        self.username = proxy.get("username")
        self.password = proxy.get("password")
        self.authorization = basic_proxy_authorization(self.username, self.password)
        self._httpd: _ForwardServer | None = None
        self._thread: threading.Thread | None = None
        self.listen_host = "127.0.0.1"
        self.listen_port = 0

    @property
    def listen_url(self) -> str:
        return f"http://{self.listen_host}:{self.listen_port}"

    def start(self) -> str:
        if self._httpd is not None:
            return self.listen_url
        httpd = _ForwardServer(self.upstream_server, self.authorization)
        self.listen_host, self.listen_port = httpd.server_address[:2]
        thread = threading.Thread(
            target=httpd.serve_forever,
            name=f"proxy-forwarder-{self.listen_port}",
            daemon=True,
        )
        httpd.timeout = 0.5
        self._httpd = httpd
        self._thread = thread
        thread.start()
        return self.listen_url

    def stop(self) -> None:
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)


def start_local_auth_proxy(proxy: dict) -> LocalAuthProxy:
    forwarder = LocalAuthProxy(proxy)
    forwarder.start()
    return forwarder
