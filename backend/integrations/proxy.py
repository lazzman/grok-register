"""代理地址解析、校验和脱敏。

HTTP 客户端继续使用配置中的完整代理 URL；Playwright 风格的 proxy dict 会把
认证拆成 ``server``、``username`` 和 ``password``。Camoufox（Firefox）不能
稳定发送代理认证，启动浏览器时会再套一层本地转发。容器运行时还会把回环
地址映射为 Docker Host 别名，同时保留原始认证信息。
"""

from __future__ import annotations

import os
import re
import threading
import uuid
from urllib.parse import unquote, urlsplit, urlunsplit


LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
HTTP_PROXY_SCHEMES = frozenset({"http", "https"})
STICKY_PROXY_PLACEHOLDER = "{id}"
_STICKY_PROXY_ID_LENGTH = 16
_sticky_tls = threading.local()

_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AUTHENTICATED_PROXY_IN_TEXT = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s]+)@"
)


def _proxy_host_port(parsed) -> str:
    """Return a URL netloc containing only host and optional port."""
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    return f"{host}:{port}" if port is not None else host


def _decode_credential(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if _BAD_PERCENT_ESCAPE.search(value):
        raise ValueError(f"{label}包含无效的百分号编码")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}不是有效的 UTF-8 百分号编码") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise ValueError(f"{label}不能包含控制字符")
    return decoded


def validate_http_proxy_url(proxy_url: str) -> str:
    """Validate an HTTP(S) proxy URL and return its trimmed original value.

    The original, still-percent-encoded URL is deliberately returned so HTTP
    libraries can parse authentication themselves. Decoding is only performed
    when building Camoufox/Playwright options.
    """
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    if any(char.isspace() for char in value):
        raise ValueError("代理地址不能包含空白字符")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"代理地址无效: {exc}") from exc
    if scheme not in HTTP_PROXY_SCHEMES:
        raise ValueError("HTTP 认证代理需使用 http:// 或 https://")
    if not host:
        raise ValueError("代理地址缺少主机名")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段；凭据特殊字符请使用百分号编码")
    try:
        parsed.port  # Accessing this property validates the port syntax/range.
    except ValueError as exc:
        raise ValueError(f"代理地址无效: {exc}") from exc
    if "@" in parsed.netloc and parsed.username in (None, ""):
        raise ValueError("代理认证用户名不能为空")
    _decode_credential(parsed.username, "代理用户名")
    _decode_credential(parsed.password, "代理密码")
    return value


def parse_http_proxy_url(proxy_url: str) -> dict[str, str]:
    """Build separated HTTP(S) proxy fields for Camoufox/Playwright."""
    value = validate_http_proxy_url(proxy_url)
    if not value:
        return {}
    parsed = urlsplit(value)
    result = {
        "server": urlunsplit(
            (parsed.scheme.lower(), _proxy_host_port(parsed), "", "", "")
        )
    }
    username = _decode_credential(parsed.username, "代理用户名")
    password = _decode_credential(parsed.password, "代理密码")
    if username is not None:
        result["username"] = username
    if password is not None:
        result["password"] = password
    return result


def _redact_proxy_userinfo(userinfo: str, *, keep_username: bool) -> str:
    if not keep_username:
        return "***:***"
    if ":" not in userinfo:
        return userinfo
    username, _password = userinfo.split(":", 1)
    return f"{username}:***"


def redact_proxy_url(proxy_url: str, *, keep_username: bool = False) -> str:
    """Hide proxy userinfo while retaining a useful scheme/host/port display.

    ``keep_username=True`` 只遮住密码，便于对照粘性会话是否写进了用户名。
    """
    value = str(proxy_url or "").strip()
    if not value or "@" not in value:
        return value
    has_scheme = "://" in value
    try:
        parsed = urlsplit(value if has_scheme else f"http://{value}")
        if "@" not in parsed.netloc or not parsed.hostname:
            raise ValueError("missing proxy host")
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        redacted = urlunsplit(
            (
                parsed.scheme,
                f"{_redact_proxy_userinfo(userinfo, keep_username=keep_username)}@{_proxy_host_port(parsed)}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        return redacted if has_scheme else redacted.split("://", 1)[1]
    except ValueError:
        return _AUTHENTICATED_PROXY_IN_TEXT.sub(r"\1***:***@", value)


def format_proxy_options_for_log(proxy: dict | None) -> str:
    """把 Playwright/Camoufox 的 proxy dict 格式化成可对照会话的脱敏 URL。"""
    if not isinstance(proxy, dict):
        return ""
    server = str(proxy.get("server") or "").strip()
    if not server:
        return ""
    username = proxy.get("username")
    password = proxy.get("password")
    if username is None and password is None:
        return server
    parsed = urlsplit(server)
    host = _proxy_host_port(parsed) if parsed.hostname else server
    scheme = parsed.scheme or "http"
    userinfo = str(username or "")
    if password is not None:
        userinfo = f"{userinfo}:***"
    return urlunsplit((scheme, f"{userinfo}@{host}", "", "", ""))


def redact_proxy_text(value: object) -> str:
    """Redact authenticated proxy URLs embedded in log or exception text."""
    return _AUTHENTICATED_PROXY_IN_TEXT.sub(
        r"\1***:***@", str(value if value is not None else "")
    )


def new_sticky_proxy_id() -> str:
    """生成一轮注册使用的粘性代理会话 id（URL 安全的十六进制）。"""
    return uuid.uuid4().hex[:_STICKY_PROXY_ID_LENGTH]


def apply_sticky_proxy_id(proxy_url: str, session_id: str) -> str:
    """把代理地址中的 ``{id}`` 替换为本次会话值；没有占位符时原样返回。"""
    value = str(proxy_url or "")
    sid = str(session_id or "").strip()
    if not value or not sid or STICKY_PROXY_PLACEHOLDER not in value:
        return value
    return value.replace(STICKY_PROXY_PLACEHOLDER, sid)


def bind_sticky_proxy_session(session_id: str | None = None, *, slot: int | None = None) -> str:
    """绑定当前线程的粘性代理会话，供本轮浏览器与 HTTP 请求共用。"""
    sid = str(session_id or "").strip() or new_sticky_proxy_id()
    _sticky_tls.session_id = sid
    if slot is not None:
        _sticky_tls.slot = int(slot)
    return sid


def current_sticky_proxy_id() -> str:
    return str(getattr(_sticky_tls, "session_id", "") or "")


def current_sticky_proxy_slot() -> int | None:
    slot = getattr(_sticky_tls, "slot", None)
    return None if slot is None else int(slot)


def clear_sticky_proxy_session() -> None:
    _sticky_tls.session_id = ""
    _sticky_tls.slot = None


def resolve_proxy_url(proxy_url: str) -> str:
    """Replace a local proxy host with the Docker host alias when configured."""
    value = str(proxy_url or "").strip()
    docker_host = str(os.environ.get("GROK_DOCKER_PROXY_HOST", "") or "").strip()
    if not value or not docker_host:
        return value

    has_scheme = "://" in value
    try:
        parsed = urlsplit(value if has_scheme else f"http://{value}")
        parsed.port
    except ValueError:
        return value
    if (parsed.hostname or "").lower() not in LOCAL_PROXY_HOSTS:
        return value

    auth = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    resolved = urlunsplit(
        (parsed.scheme, f"{auth}{docker_host}{port}", parsed.path, parsed.query, parsed.fragment)
    )
    return resolved if has_scheme else resolved.split("://", 1)[1]
