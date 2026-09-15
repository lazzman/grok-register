# -*- coding: utf-8 -*-
"""浏览器运行时管理。

隔离每个工作线程的浏览器与页面对象，并统一处理启动、重启、进程回收和临时
profile 清理。
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import signal
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

import asyncio
from greenlet import greenlet
from typing import cast as _tcast

from camoufox.sync_api import Camoufox as _Camoufox, NewBrowser
from playwright._impl._connection import Connection as _PwConnection
from playwright._impl._greenlets import MainGreenlet as _PwMainGreenlet
from playwright._impl._object_factory import create_remote_object as _pw_create_remote
from playwright._impl._playwright import Playwright as _PwImpl
from playwright._impl._transport import PipeTransport as _PwPipeTransport
from playwright.sync_api._generated import Playwright as _SyncPlaywright

from backend.automation.page_adapter import BrowserAdapter, PageAdapter
from backend.integrations.proxy import HTTP_PROXY_SCHEMES, parse_http_proxy_url


class IsolatedCamoufox(_Camoufox):
    """Camoufox 子类，绕过 PlaywrightContextManager 的事件循环检查。

    PlaywrightContextManager.__enter__() 调用 asyncio.get_running_loop()，
    如果当前线程有运行中的 asyncio 事件循环（如其他库遗留），
    就会报错 "Sync API inside the asyncio loop"。

    此子类直接创建全新的、非运行状态的事件循环，完全绕过该检查。
    通过重写 __enter__()，跳过 get_running_loop() / is_running() 判断，
    直接用 asyncio.new_event_loop() 创建干净的事件循环。
    """

    def __enter__(self):
        # 强制创建全新的事件循环，跳过 get_running_loop() / is_running() 检查
        self._loop = asyncio.new_event_loop()
        self._own_loop = True

        # 复制 PlaywrightContextManager.__enter__() 的 greenlet 调度逻辑
        def _greenlet_main():
            self._loop.run_until_complete(self._connection.run_as_sync())

        dispatcher_fiber = _PwMainGreenlet(_greenlet_main)

        self._connection = _PwConnection(
            dispatcher_fiber,
            _pw_create_remote,
            _PwPipeTransport(self._loop),
            self._loop,
        )

        g_self = greenlet.getcurrent()

        def _callback_wrapper(channel_owner):
            playwright_impl = _tcast(_PwImpl, channel_owner)
            self._playwright = _SyncPlaywright(playwright_impl)
            g_self.switch()

        self._connection.call_on_object_with_known_name("Playwright", _callback_wrapper)
        dispatcher_fiber.switch()

        playwright = self._playwright
        playwright.stop = self.__exit__

        # Camoufox 特有：启动浏览器
        try:
            self.browser = NewBrowser(self._playwright, **self.launch_options)
        except BaseException as e:
            super().__exit__(type(e), e, e.__traceback__)
            raise
        return self.browser


BROWSER_ENGINE_CAMOUFOX = "camoufox"
BROWSER_ENGINE_CLOAKBROWSER = "cloakbrowser"
SUPPORTED_BROWSER_ENGINES = {
    BROWSER_ENGINE_CAMOUFOX,
    BROWSER_ENGINE_CLOAKBROWSER,
}

# 仅允许删除这些目录树下的临时 profile，防止误删其它路径。
_PROFILE_ROOT_MARKERS = {
    BROWSER_ENGINE_CAMOUFOX: "grok-register-camoufox",
    BROWSER_ENGINE_CLOAKBROWSER: "grok-register-cloakbrowser",
}
# 保留旧常量，兼容已有测试和外部导入。
_PROFILE_ROOT_MARKER = _PROFILE_ROOT_MARKERS[BROWSER_ENGINE_CAMOUFOX]
_CLOAK_PROFILE_ROOT_MARKER = _PROFILE_ROOT_MARKERS[BROWSER_ENGINE_CLOAKBROWSER]

_tls = threading.local()
_get_proxy: Optional[Callable[[], dict]] = None
_is_debug: Optional[Callable[[], bool]] = None
_is_headless: Optional[Callable[[], bool]] = None
_get_locale: Optional[Callable[[], str]] = None
_get_engine: Optional[Callable[[], str]] = None
_is_low_traffic: Optional[Callable[[], bool]] = None
_get_traffic_savings_level: Optional[Callable[[], str]] = None
_extension_path: str = ""
_start_fail_lock = threading.Lock()
_start_fail_streak = 0
_start_fail_threshold = 3
_browser_launch_blocked = threading.Event()
_browser_launch_gate = threading.Lock()
_BROWSER_LAUNCH_TIMEOUT_MS = 60_000
_low_traffic_cache_locks: dict[str, threading.Lock] = {}
_low_traffic_cache_locks_guard = threading.Lock()

_LOW_TRAFFIC_BLOCKED_HOSTS = {
    "cdn.cookielaw.org",
    "js.stripe.com",
    "ublockorigin.pages.dev",
    "ublockorigin.github.io",
}
_LOW_TRAFFIC_MEDIA_HOSTS = {"media.x.ai"}
_LOW_TRAFFIC_VISUAL_HOSTS = {"cdn.grok.com", "grok.com", "www.grok.com"}
# 标准省流只缓存已验证安全的 cdn.grok.com 静态资源。
# 更多节省额外缓存 accounts.x.ai 的 /_next/static/ 哈希资源；页面文档、
# 动态配置、风控状态和 Cloudflare 挑战仍走网络，避免跨 profile 混用。
_LOW_TRAFFIC_CACHE_HOSTS = {"cdn.grok.com"}
_LOW_TRAFFIC_ACCOUNTS_HOSTS = {"accounts.x.ai"}
_LOW_TRAFFIC_ACCOUNTS_STATIC_MARKERS = ("/_next/static/",)
_LOW_TRAFFIC_CACHE_TYPES = {"script", "stylesheet", "font"}
_LOW_TRAFFIC_CACHE_MAX_BYTES = 24 * 1024 * 1024
_LOW_TRAFFIC_CACHE_TOTAL_BYTES = 512 * 1024 * 1024
_LOW_TRAFFIC_CACHE_EXCLUDED_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "set-cookie",
    "cf-ray",
    "age",
    "date",
}
_CACHE_RISK_SCAN_LIMIT = 2 * 1024 * 1024
_CACHE_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
# 命中任一标记即视为高风险：跨账号回放会污染设备指纹、登录遥测或 Turnstile 时序。
_CACHE_RISK_HIGH_MARKERS = (
    ("m.castle.io", "Castle 设备指纹 SDK"),
    ("rtcpeerconnection", "WebRTC ICE 出口探测"),
    ("mozrtcpeerconnection", "WebRTC ICE 出口探测"),
    ("gethighentropyvalues", "UA Client Hints 高熵指纹"),
    ("api-js.mixpanel.com", "Mixpanel 分析 SDK"),
    ("cdn.mxpnl.com", "Mixpanel 分析 SDK"),
    ("challenges.cloudflare.com/turnstile", "Cloudflare Turnstile 加载器"),
    ("reportturnstileerror", "Turnstile 错误上报"),
    ("auth.turnstile.challenge", "Turnstile 挑战追踪"),
    ("highlightturnstile", "Turnstile 登录校验脚本"),
    ("api.axiom.co", "Axiom web-vitals 遥测"),
    ("http.response_transfer_size", "Resource Timing 传输体积"),
    ("device.processor_count", "Sentry 硬件信息采集"),
)
_CACHE_RISK_MEDIUM_MARKERS = (
    ("connect.facebook.net", "广告像素 / 追踪脚本"),
    ("ads-twitter.com", "广告像素 / 追踪脚本"),
    ("analytics.twitter.com", "广告像素 / 追踪脚本"),
    ("pixel-config.reddit.com", "广告像素 / 追踪脚本"),
    ("navigator.webdriver", "自动化特征探测"),
    ("web-vitals", "Web Vitals 性能埋点"),
)
_LOW_TRAFFIC_PASSTHROUGH_TYPES = {
    "document",
    "xhr",
    "fetch",
    "websocket",
    "eventsource",
    "manifest",
}
_LOW_TRAFFIC_BLOCK_EXTENSIONS = {
    ".avif",
    ".eot",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4v",
    ".mov",
    ".mp4",
    ".otf",
    ".png",
    ".svg",
    ".ttf",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
}
_low_traffic_cache_pruned = False
_low_traffic_cache_prune_lock = threading.Lock()


def configure(
    get_proxies=None,
    is_debug=None,
    is_headless=None,
    get_locale=None,
    get_engine=None,
    is_low_traffic=None,
    get_traffic_savings_level=None,
    extension_path="",
):
    global _get_proxy, _is_debug, _is_headless, _get_locale, _get_engine
    global _is_low_traffic, _get_traffic_savings_level, _extension_path
    _get_proxy = get_proxies
    _is_debug = is_debug
    _is_headless = is_headless
    _get_locale = get_locale
    _get_engine = get_engine
    _is_low_traffic = is_low_traffic
    _get_traffic_savings_level = get_traffic_savings_level
    _extension_path = extension_path or ""


def get_start_fail_streak() -> int:
    with _start_fail_lock:
        return _start_fail_streak


def _note_start_success():
    global _start_fail_streak
    with _start_fail_lock:
        _start_fail_streak = 0


def _note_start_failure():
    global _start_fail_streak
    with _start_fail_lock:
        _start_fail_streak += 1
        return _start_fail_streak


def _proxies() -> dict:
    if _get_proxy:
        return _get_proxy() or {}
    return {}


def _browser_locale() -> str:
    value = str(_get_locale() if _get_locale else "en-US").strip()
    return value if value in {"en-US", "zh-CN"} else "en-US"


def normalize_browser_engine(value) -> str:
    engine = str(value or BROWSER_ENGINE_CAMOUFOX).strip().lower()
    if engine not in SUPPORTED_BROWSER_ENGINES:
        return BROWSER_ENGINE_CAMOUFOX
    return engine


def selected_browser_engine() -> str:
    value = _get_engine() if _get_engine else BROWSER_ENGINE_CAMOUFOX
    return normalize_browser_engine(value)


def _debug() -> bool:
    return bool(_is_debug()) if _is_debug else False


def _headless() -> bool:
    return bool(_is_headless()) if _is_headless else False


def low_traffic_enabled() -> bool:
    return bool(_is_low_traffic()) if _is_low_traffic else False


def traffic_savings_level() -> str:
    """standard: grok.com 省流；more: 额外缓存 accounts.x.ai 哈希静态资源。"""
    if not _get_traffic_savings_level:
        return "standard"
    value = str(_get_traffic_savings_level() or "standard").strip().lower()
    if value in {"more", "max"}:
        return "more"
    return "standard"


def low_traffic_should_block(url: str, resource_type: str) -> bool:
    """只拦截不参与注册状态与 Turnstile 验证的高流量资源。"""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    kind = str(resource_type or "").strip().lower()
    if host in _LOW_TRAFFIC_BLOCKED_HOSTS:
        return True
    if kind in {"media", "font"}:
        return host != "challenges.cloudflare.com" and (
            host in _LOW_TRAFFIC_MEDIA_HOSTS or host in _LOW_TRAFFIC_VISUAL_HOSTS
        )
    return kind == "image" and (
        host in _LOW_TRAFFIC_MEDIA_HOSTS or host in _LOW_TRAFFIC_VISUAL_HOSTS
    )


def low_traffic_should_intercept(url: str) -> bool:
    """只拦截需要缓存或丢弃的静态资源，注册文档和 API 走浏览器原生网络。"""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except ValueError:
        return False
    if host in _LOW_TRAFFIC_BLOCKED_HOSTS or host in _LOW_TRAFFIC_CACHE_HOSTS:
        return True
    if host in _LOW_TRAFFIC_MEDIA_HOSTS:
        return True
    if host in _LOW_TRAFFIC_VISUAL_HOSTS:
        return Path(path).suffix in _LOW_TRAFFIC_BLOCK_EXTENSIONS
    if traffic_savings_level() != "more":
        return False
    if host not in _LOW_TRAFFIC_ACCOUNTS_HOSTS or "/cdn-cgi/" in path:
        return False
    return any(marker in path for marker in _LOW_TRAFFIC_ACCOUNTS_STATIC_MARKERS)


def low_traffic_should_cache(url: str, resource_type: str, method: str = "GET") -> bool:
    if str(method or "GET").upper() != "GET":
        return False
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except ValueError:
        return False
    kind = str(resource_type or "").strip().lower()
    if kind not in _LOW_TRAFFIC_CACHE_TYPES:
        return False
    if host in _LOW_TRAFFIC_CACHE_HOSTS:
        return True
    if traffic_savings_level() != "more":
        return False
    if host not in _LOW_TRAFFIC_ACCOUNTS_HOSTS or "/cdn-cgi/" in path:
        return False
    return any(marker in path for marker in _LOW_TRAFFIC_ACCOUNTS_STATIC_MARKERS)


def _low_traffic_cache_root() -> Path:
    configured = str(os.environ.get("GROK_BROWSER_CACHE_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "browser-cache"


def _low_traffic_cache_paths(url: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
    root = _low_traffic_cache_root()
    return root / f"{digest}.json", root / f"{digest}.bin"


def _low_traffic_cache_lock(url: str) -> threading.Lock:
    digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()
    with _low_traffic_cache_locks_guard:
        return _low_traffic_cache_locks.setdefault(digest, threading.Lock())


def _prune_low_traffic_cache_once() -> None:
    global _low_traffic_cache_pruned
    with _low_traffic_cache_prune_lock:
        if _low_traffic_cache_pruned:
            return
        _low_traffic_cache_pruned = True
        root = _low_traffic_cache_root()
        try:
            bodies = [path for path in root.glob("*.bin") if path.is_file()]
            total = sum(path.stat().st_size for path in bodies)
            if total <= _LOW_TRAFFIC_CACHE_TOTAL_BYTES:
                return
            bodies.sort(key=lambda path: path.stat().st_mtime)
            for body_path in bodies:
                if total <= _LOW_TRAFFIC_CACHE_TOTAL_BYTES:
                    break
                try:
                    size = body_path.stat().st_size
                    body_path.unlink(missing_ok=True)
                    body_path.with_suffix(".json").unlink(missing_ok=True)
                    total -= size
                except OSError:
                    continue
        except OSError:
            return


def _cached_response(url: str) -> tuple[int, dict[str, str], bytes] | None:
    meta_path, body_path = _low_traffic_cache_paths(url)
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        body = body_path.read_bytes()
        if len(body) != int(metadata.get("size") or -1):
            return None
        headers = metadata.get("headers")
        if not isinstance(headers, dict):
            return None
        return int(metadata.get("status") or 200), {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() not in _LOW_TRAFFIC_CACHE_EXCLUDED_HEADERS
        }, body
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _store_cached_response(url: str, status: int, headers: dict, body: bytes) -> None:
    if status != 200 or not body or len(body) > _LOW_TRAFFIC_CACHE_MAX_BYTES:
        return
    content_type = str(headers.get("content-type") or "").lower()
    if not any(
        marker in content_type
        for marker in (
            "javascript",
            "text/css",
            "font/",
            "application/font",
            "application/wasm",
        )
    ):
        return
    filtered_headers = {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in _LOW_TRAFFIC_CACHE_EXCLUDED_HEADERS
    }
    meta_path, body_path = _low_traffic_cache_paths(url)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
        temp_meta = meta_path.with_name(meta_path.name + suffix)
        temp_body = body_path.with_name(body_path.name + suffix)
        temp_body.write_bytes(body)
        temp_meta.write_text(
            json.dumps(
                {
                    "status": status,
                    "headers": filtered_headers,
                    "size": len(body),
                    "url": str(url),
                    "cached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(temp_body, body_path)
        os.replace(temp_meta, meta_path)
    except OSError:
        return


def _remember_cached_url(url: str) -> None:
    """旧缓存没有 URL 字段时，命中后补写，方便设置页展示。"""
    meta_path, _body_path = _low_traffic_cache_paths(url)
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if str(metadata.get("url") or ""):
            return
        if not isinstance(metadata, dict):
            return
        metadata["url"] = str(url)
        if not metadata.get("cached_at"):
            metadata["cached_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
        temp_meta = meta_path.with_name(meta_path.name + suffix)
        temp_meta.write_text(
            json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temp_meta, meta_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return


def _short_cache_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
        host = parsed.hostname or ""
        path = parsed.path or ""
        if len(path) > 96:
            path = path[:93] + "..."
        return f"{host}{path}"
    except ValueError:
        return str(url or "")[:96]


def path_looks_like_js(parsed) -> bool:
    path = str(getattr(parsed, "path", "") or "").lower()
    return path.endswith(".js") or "/_next/static/chunks/" in path


def classify_low_traffic_cache_risk(url: str = "", body=b"", content_type: str = "") -> dict:
    """根据缓存文件内容判断回放风险。高风险 JS 不回放，避免跨账号污染指纹。"""
    ctype = str(content_type or "").lower()
    path = ""
    try:
        path = (urlparse(str(url or "")).path or "").lower()
    except ValueError:
        path = str(url or "").lower()
    if (
        "text/css" in ctype
        or "font/" in ctype
        or "application/font" in ctype
        or path.endswith((".css", ".woff", ".woff2", ".ttf", ".otf", ".eot"))
    ):
        return {
            "level": "low",
            "reasons": ["样式或字体，回放风险低"],
            "replay_safe": True,
        }
    raw = body if isinstance(body, (bytes, bytearray)) else str(body or "").encode("utf-8", errors="ignore")
    text_blob = bytes(raw[:_CACHE_RISK_SCAN_LIMIT]).decode("utf-8", errors="ignore")
    blob = f"{url}\n{path}\n{text_blob}".lower()
    reasons: list[str] = []
    for needle, reason in _CACHE_RISK_HIGH_MARKERS:
        if needle in blob and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return {"level": "high", "reasons": reasons, "replay_safe": False}
    for needle, reason in _CACHE_RISK_MEDIUM_MARKERS:
        if needle in blob and reason not in reasons:
            reasons.append(reason)
    if reasons:
        return {"level": "medium", "reasons": reasons, "replay_safe": True}
    return {"level": "low", "reasons": ["未发现指纹或遥测特征"], "replay_safe": True}


def low_traffic_cache_scope(url: str) -> str:
    """standard: cdn.grok.com；more: accounts.x.ai 哈希静态资源。"""
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except ValueError:
        return "unknown"
    if host in _LOW_TRAFFIC_CACHE_HOSTS:
        return "standard"
    if host in _LOW_TRAFFIC_ACCOUNTS_HOSTS and any(
        marker in path for marker in _LOW_TRAFFIC_ACCOUNTS_STATIC_MARKERS
    ):
        return "more"
    return "unknown"


def _low_traffic_cache_entry_active(scope: str, replay_safe: bool = True) -> bool:
    if not low_traffic_enabled() or not replay_safe:
        return False
    if scope == "standard":
        return True
    if scope == "more":
        return traffic_savings_level() == "more"
    return False


def inspect_low_traffic_cache() -> dict:
    """列出本地静态资源缓存，并标记当前省流级别下哪些条目会生效。"""
    root = _low_traffic_cache_root()
    entries: list[dict] = []
    total_bytes = 0
    if root.is_dir():
        try:
            meta_files = list(root.glob("*.json"))
        except OSError:
            meta_files = []
        for meta_path in meta_files:
            body_path = meta_path.with_suffix(".bin")
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                headers = metadata.get("headers") if isinstance(metadata.get("headers"), dict) else {}
                url = str(metadata.get("url") or "")
                scope = low_traffic_cache_scope(url) if url else "unknown"
                body_size = body_path.stat().st_size if body_path.is_file() else 0
                size = int(metadata.get("size") or body_size or 0)
                total_bytes += size
                parsed = urlparse(url) if url else None
                mtime = body_path.stat().st_mtime if body_path.is_file() else meta_path.stat().st_mtime
                content_type = str(headers.get("content-type") or "")
                body = b""
                if body_path.is_file() and (
                    "javascript" in content_type.lower() or path_looks_like_js(parsed)
                ):
                    try:
                        body = body_path.read_bytes()[:_CACHE_RISK_SCAN_LIMIT]
                    except OSError:
                        body = b""
                risk = classify_low_traffic_cache_risk(url, body, content_type)
                entries.append(
                    {
                        "id": meta_path.stem,
                        "url": url,
                        "host": ((parsed.hostname or "") if parsed else ""),
                        "path": ((parsed.path or "") if parsed else ""),
                        "content_type": content_type,
                        "status": int(metadata.get("status") or 200),
                        "size": size,
                        "scope": scope,
                        "risk_level": risk["level"],
                        "risk_reasons": risk["reasons"],
                        "replay_safe": bool(risk["replay_safe"]),
                        "active": _low_traffic_cache_entry_active(
                            scope, bool(risk["replay_safe"])
                        ),
                        "cached_at": str(metadata.get("cached_at") or ""),
                        "mtime": mtime,
                    }
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    entries.sort(
        key=lambda item: (
            _CACHE_RISK_ORDER.get(str(item.get("risk_level") or "unknown"), 9),
            -int(bool(item.get("active"))),
            -int(item.get("size") or 0),
            str(item.get("url") or item.get("id") or ""),
        )
    )
    active_entries = [item for item in entries if item.get("active")]
    high_risk = [item for item in entries if item.get("risk_level") == "high"]
    medium_risk = [item for item in entries if item.get("risk_level") == "medium"]
    return {
        "enabled": low_traffic_enabled(),
        "savings_level": traffic_savings_level() if low_traffic_enabled() else "",
        "root": str(root),
        "total_bytes": total_bytes,
        "max_total_bytes": _LOW_TRAFFIC_CACHE_TOTAL_BYTES,
        "max_entry_bytes": _LOW_TRAFFIC_CACHE_MAX_BYTES,
        "entry_count": len(entries),
        "active_count": len(active_entries),
        "active_bytes": sum(int(item.get("size") or 0) for item in active_entries),
        "high_risk_count": len(high_risk),
        "medium_risk_count": len(medium_risk),
        "high_risk_bytes": sum(int(item.get("size") or 0) for item in high_risk),
        "refills_on_miss": True,
        "entries": entries,
    }


def clear_low_traffic_cache() -> dict:
    """删除本地静态资源缓存。下次打开注册页或重新登录页会按当前省流级别重新下载并写入。"""
    global _low_traffic_cache_pruned
    root = _low_traffic_cache_root()
    deleted = 0
    errors = 0
    if root.is_dir():
        try:
            names = list(root.iterdir())
        except OSError:
            names = []
        for path in names:
            if not path.is_file():
                continue
            if path.suffix not in {".json", ".bin", ".tmp"} and ".tmp" not in path.name:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError:
                errors += 1
    with _low_traffic_cache_prune_lock:
        _low_traffic_cache_pruned = False
    snapshot = inspect_low_traffic_cache()
    snapshot["deleted_files"] = deleted
    snapshot["errors"] = errors
    return snapshot


def _install_low_traffic_routing(browser_context, log_callback=None) -> None:
    if not low_traffic_enabled() or not hasattr(browser_context, "route"):
        return
    _prune_low_traffic_cache_once()

    def handle(route, request):
        url = str(getattr(request, "url", "") or "")
        resource_type = str(getattr(request, "resource_type", "") or "").strip().lower()
        method = str(getattr(request, "method", "GET") or "GET")
        if resource_type in _LOW_TRAFFIC_PASSTHROUGH_TYPES:
            route.continue_()
            return
        if low_traffic_should_block(url, resource_type):
            route.abort()
            return
        request_headers = getattr(request, "headers", {}) or {}
        if not low_traffic_should_cache(url, resource_type, method) or any(
            str(key).lower() == "range" for key in request_headers
        ):
            route.continue_()
            return
        lock = _low_traffic_cache_lock(url)
        with lock:
            cached = _cached_response(url)
        if cached is not None:
            status, headers, body = cached
            risk = classify_low_traffic_cache_risk(
                url, body, headers.get("content-type")
            )
            if risk.get("replay_safe"):
                _remember_cached_url(url)
                route.fulfill(status=status, headers=headers, body=body)
                return
            if log_callback:
                reasons = "、".join(risk.get("reasons") or []) or "高风险脚本"
                log_callback(
                    "[!] 低流量缓存跳过回放: "
                    f"{_short_cache_url(url)}（{reasons}）"
                )
        try:
            response = route.fetch()
            body = response.body()
            headers = dict(response.headers or {})
            status = int(response.status or 0)
            stored = False
            risk = classify_low_traffic_cache_risk(
                url, body, headers.get("content-type")
            )
            with lock:
                if _cached_response(url) is None:
                    _store_cached_response(url, status, headers, body)
                    stored = _cached_response(url) is not None
            if stored and log_callback:
                if risk.get("replay_safe"):
                    log_callback(
                        "[*] 低流量缓存未命中，已重新下载: "
                        f"{_short_cache_url(url)} ({len(body)} bytes)"
                    )
                else:
                    reasons = "、".join(risk.get("reasons") or []) or "高风险脚本"
                    log_callback(
                        "[!] 低流量缓存已保存但不会回放: "
                        f"{_short_cache_url(url)}（{reasons}）"
                    )
            route.fulfill(response=response, body=body)
            return
        except Exception:
            route.continue_()

    browser_context.route(low_traffic_should_intercept, handle)
    if log_callback:
        if traffic_savings_level() == "more":
            log_callback("[*] 低流量模式：已启用 grok.com 与 accounts.x.ai 静态资源缓存与非业务媒体拦截")
            log_callback("[!] 更多节省会跳过 Castle/Mixpanel/Turnstile 等高风险 JS 回放；其余 accounts 哈希资源仍可能影响账号质量，追求质量请用较少节省")
        else:
            log_callback("[*] 低流量模式：已启用 grok.com 静态资源缓存与非业务媒体拦截")


def _install_accounts_resource_diagnostics(browser_context, log_callback=None) -> None:
    """在调试模式下记录 accounts.x.ai 隧道内的真实资源明细。

    代理层只能看到 CONNECT 主机，无法区分隧道中的 JS/CSS/API 请求。
    Playwright 的 requestfinished 事件可以在不读取响应体、不改变请求路径的
    前提下记录资源类型、状态和网络字节数，帮助定位约 1.5 MB 的重复下载。
    该监听器仅在调试模式且存在日志回调时安装，生产运行不会增加额外日志或
    请求。
    """
    if (
        not _debug()
        or not log_callback
        or browser_context is None
        or not hasattr(browser_context, "on")
    ):
        return

    state = {"total": 0, "seen": 0}

    def handle(request):
        try:
            url = str(getattr(request, "url", "") or "")
            host = (urlparse(url).hostname or "").lower()
            if host != "accounts.x.ai":
                return
            resource_type = str(getattr(request, "resource_type", "") or "unknown")
            response = request.response()
            status = int(getattr(response, "status", 0) or 0) if response else 0
            headers = getattr(response, "headers", {}) or {}
            content_length = str(headers.get("content-length") or "")
            try:
                sizes = request.sizes() or {}
                body_size = int(sizes.get("responseBodySize") or 0)
            except Exception:
                body_size = 0
            if body_size <= 0:
                try:
                    body_size = int(content_length or 0)
                except (TypeError, ValueError):
                    body_size = 0
            state["total"] += body_size
            state["seen"] += 1
            parsed = urlparse(url)
            safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            log_callback(
                "[Debug] accounts.x.ai 资源: "
                f"type={resource_type} status={status} bytes={body_size} "
                f"url={safe_url} | cumulative={state['total']} bytes "
                f"({state['seen']} requests)"
            )
        except Exception as exc:
            log_callback(f"[Debug] accounts.x.ai 资源记录失败: {type(exc).__name__}: {exc}")

    try:
        browser_context.on("requestfinished", handle)
    except Exception as exc:
        log_callback(f"[Debug] accounts.x.ai 资源监听安装失败: {type(exc).__name__}: {exc}")


def allow_browser_launches() -> None:
    _browser_launch_blocked.clear()


def block_browser_launches() -> None:
    _browser_launch_blocked.set()


def active_browser():
    return getattr(_tls, "browser", None)


def active_page():
    return getattr(_tls, "page", None)


def set_browser_session(browser_obj=None, page_obj=None):
    _tls.browser = browser_obj
    _tls.page = page_obj


class _SessionProxy:
    __slots__ = ("_key",)

    def __init__(self, key):
        self._key = key

    def _obj(self):
        return getattr(_tls, self._key, None)

    def __bool__(self):
        return self._obj() is not None

    def __eq__(self, other):
        return self._obj() is other

    def __ne__(self, other):
        return self._obj() is not other

    def __getattr__(self, name):
        obj = self._obj()
        if obj is None:
            raise AttributeError(f"{self._key} is not started")
        return getattr(obj, name)


browser = _SessionProxy("browser")
page = _SessionProxy("page")


def _is_managed_profile_dir(path: str) -> bool:
    """是否为本工具创建的临时浏览器资料目录。"""
    if not path:
        return False
    norm = os.path.normpath(path).replace("\\", "/").lower()
    for marker_value in _PROFILE_ROOT_MARKERS.values():
        marker = marker_value.lower()
        if f"/{marker}/" in f"/{norm}/" or norm.rstrip("/").endswith(f"/{marker}"):
            return True
    return False


def _profile_root(engine: str) -> str:
    marker = _PROFILE_ROOT_MARKERS[normalize_browser_engine(engine)]
    return os.path.join(tempfile.gettempdir(), marker)


def _create_profile_dir(engine: str) -> str:
    profile_dir = os.path.join(
        _profile_root(engine),
        f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}",
    )
    os.makedirs(profile_dir, exist_ok=True)
    _tls.profile_dir = profile_dir
    _tls.browser_engine = normalize_browser_engine(engine)
    return profile_dir


def _rmtree_with_retry(path: str, max_retries: int = 3, delay: float = 0.5) -> bool:
    """Windows 上文件锁可能导致 rmtree 失败，带重试。

    返回 True 表示最终删除成功。
    """
    for attempt in range(max_retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            # 最后一次尝试：强制忽略错误
            try:
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.isdir(path)
            except Exception:
                return False
    return not os.path.isdir(path)


def _cleanup_profile_dir(profile_dir=None) -> None:
    """关闭浏览器后删除临时 user-data，避免 TEMP 堆积。"""
    path = profile_dir if profile_dir is not None else getattr(_tls, "profile_dir", None)
    try:
        if getattr(_tls, "profile_dir", None) and (
            profile_dir is None
            or os.path.normpath(str(getattr(_tls, "profile_dir")))
            == os.path.normpath(str(path or ""))
        ):
            _tls.profile_dir = None
            _tls.browser_engine = None
    except Exception:
        pass
    if not path or not _is_managed_profile_dir(str(path)):
        return
    if os.path.isdir(path):
        _rmtree_with_retry(path)


def cleanup_stale_profiles(log_callback=None) -> int:
    """启动时清理上次崩溃 / 强杀残留的临时 profile 目录。

    扫描 Camoufox 与 CloakBrowser 的托管目录，删除所有未被当前进程占用的
    旧目录。
    返回清理的目录数量。
    """
    current_pid = os.getpid()
    cleaned = 0
    for engine in SUPPORTED_BROWSER_ENGINES:
        root = _profile_root(engine)
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                entry_path = os.path.join(root, entry)
                if not os.path.isdir(entry_path):
                    continue
                # 目录名格式: {pid}-{thread_id}-{uuid8}
                # 只跳过当前进程的活跃目录
                if entry.startswith(f"{current_pid}-"):
                    continue
                if _rmtree_with_retry(entry_path):
                    cleaned += 1
        except Exception:
            pass
        try:
            if not os.listdir(root):
                shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass

    if cleaned > 0 and log_callback:
        log_callback(f"[*] 启动清理: 已删除 {cleaned} 个残留浏览器资料目录")
    return cleaned


def _is_camoufox_process(executable: str, command_line: str) -> bool:
    """Match Camoufox browser processes without touching regular Firefox."""
    exe = os.path.normpath(str(executable or "")).replace("\\", "/").lower()
    command = str(command_line or "").replace("\\", "/").lower()
    basename = os.path.basename(exe)
    if basename in {"camoufox", "camoufox-bin", "camoufox.exe"}:
        return True
    return "/camoufox/" in exe or _PROFILE_ROOT_MARKER.lower() in command


def _is_cloakbrowser_process(executable: str, command_line: str) -> bool:
    """Match only CloakBrowser processes launched with this project's profile."""
    command = str(command_line or "").replace("\\", "/").lower()
    return _CLOAK_PROFILE_ROOT_MARKER.lower() in command


def _is_managed_browser_process(executable: str, command_line: str) -> bool:
    return _is_camoufox_process(executable, command_line) or _is_cloakbrowser_process(
        executable, command_line
    )


def _is_local_playwright_driver(pid: int, ppid: int, executable: str, command_line: str) -> bool:
    """Match Playwright run-driver processes started by this registrar."""
    if pid == os.getpid():
        return False
    command = str(command_line or "").replace("\\", "/").lower()
    if "playwright/driver" not in command or "run-driver" not in command:
        return False
    if ppid == os.getpid():
        return True
    project = str(Path(__file__).resolve().parents[2]).replace("\\", "/").lower()
    return project in command


def _linux_processes() -> dict[int, tuple[int, str, str]]:
    processes: dict[int, tuple[int, str, str]] = {}
    proc_root = "/proc"
    if not os.path.isdir(proc_root):
        return processes
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(os.path.join(proc_root, entry, "stat"), "r", encoding="utf-8") as handle:
                stat = handle.read()
            closing = stat.rfind(")")
            ppid = int(stat[closing + 2 :].split()[1])
            executable = os.readlink(os.path.join(proc_root, entry, "exe"))
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as handle:
                raw = handle.read()
            command = raw.replace(b"\0", b" ").decode("utf-8", errors="replace")
            processes[pid] = (ppid, executable, command)
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError, ValueError):
            continue
    return processes


def _cleanup_all_managed_profiles(engine: Optional[str] = None) -> int:
    engines = (
        [normalize_browser_engine(engine)]
        if engine is not None
        else sorted(SUPPORTED_BROWSER_ENGINES)
    )
    cleaned = 0
    for selected in engines:
        root = _profile_root(selected)
        if not os.path.isdir(root):
            continue
        for entry in list(os.listdir(root)):
            path = os.path.join(root, entry)
            if os.path.isdir(path) and _rmtree_with_retry(path):
                cleaned += 1
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    return cleaned


def _kill_browser_processes(
    matcher: Callable[[str, str], bool],
    *,
    profile_engine: Optional[str],
    label: str,
    log_callback=None,
) -> dict:
    if os.name != "posix" or not os.path.isdir("/proc"):
        raise RuntimeError(f"当前系统暂不支持批量终止{label}")

    processes = _linux_processes()
    current_pid = os.getpid()
    targets = {
        pid
        for pid, (_, executable, command) in processes.items()
        if pid != current_pid and matcher(executable, command)
    }

    # Include helper/content descendants even if their executable name differs.
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _, _) in processes.items():
            if pid != current_pid and ppid in targets and pid not in targets:
                targets.add(pid)
                changed = True

    attempted = set()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(targets, reverse=True):
            try:
                os.kill(pid, sig)
                attempted.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise RuntimeError(f"没有权限终止{label}进程 {pid}") from exc
        if sig == signal.SIGTERM and targets:
            time.sleep(0.8)

    cleaned_profiles = _cleanup_all_managed_profiles(profile_engine)
    result = {"killed": len(attempted), "profiles_cleaned": cleaned_profiles}
    if log_callback:
        log_callback(
            f"[!] 已终止 {result['killed']} 个{label}进程，清理 {cleaned_profiles} 个资料目录"
        )
    return result


def kill_all_camoufox_processes(log_callback=None) -> dict:
    """Terminate every Camoufox process tree and remove Camoufox profiles."""
    block_browser_launches()
    return _kill_browser_processes(
        _is_camoufox_process,
        profile_engine=BROWSER_ENGINE_CAMOUFOX,
        label="Camoufox 浏览器",
        log_callback=log_callback,
    )


def kill_all_cloakbrowser_processes(log_callback=None) -> dict:
    """Terminate every managed CloakBrowser tree and remove its profiles."""
    block_browser_launches()
    return _kill_browser_processes(
        _is_cloakbrowser_process,
        profile_engine=BROWSER_ENGINE_CLOAKBROWSER,
        label="CloakBrowser 浏览器",
        log_callback=log_callback,
    )


def kill_all_browser_processes(log_callback=None) -> dict:
    """Terminate both supported browser backends and remove managed profiles."""
    block_browser_launches()
    result = _kill_browser_processes(
        _is_managed_browser_process,
        profile_engine=None,
        label="浏览器",
        log_callback=log_callback,
    )
    result["killed"] = int(result.get("killed") or 0) + _kill_local_playwright_drivers(
        log_callback=log_callback
    )
    return result


def interrupt_browser_work(log_callback=None) -> dict:
    """Stop in-flight launches so a running job can actually exit."""
    return kill_all_browser_processes(log_callback=log_callback)


def _kill_local_playwright_drivers(log_callback=None) -> int:
    processes = _linux_processes()
    current_pid = os.getpid()
    targets = {
        pid
        for pid, (ppid, executable, command) in processes.items()
        if _is_local_playwright_driver(pid, ppid, executable, command)
    }
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _, _) in processes.items():
            if pid != current_pid and ppid in targets and pid not in targets:
                targets.add(pid)
                changed = True
    if not targets:
        return 0
    attempted = set()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in sorted(targets, reverse=True):
            try:
                os.kill(pid, sig)
                attempted.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
        if sig == signal.SIGTERM and targets:
            time.sleep(0.3)
    if attempted and log_callback:
        log_callback(f"[!] 已终止 {len(attempted)} 个卡住的 Playwright 驱动")
    return len(attempted)


def _build_camoufox_proxy(proxy_str: str) -> dict:
    """把代理 URL 转换为两个浏览器后端共用的 Playwright proxy dict。"""
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return {}
    parsed = urlparse(proxy_str)
    if parsed.scheme.lower() in HTTP_PROXY_SCHEMES:
        return parse_http_proxy_url(proxy_str)
    if parsed.scheme and parsed.hostname:
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
    else:
        server = proxy_str
    result: dict = {"server": server}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _detect_camoufox_exe() -> str:
    """检测 Camoufox 可执行文件路径，支持新旧两种安装格式。

    新格式（multiversion）：browsers/{repo}/{version}/camoufox.exe + .0.5_FLAG
    旧格式（legacy）：直接在 INSTALL_DIR 下，无 .0.5_FLAG

    旧格式下 installed_verstr() 会报错 "official/stable is not installed"，
    需要传 executable_path + ff_version 绕过该检查。
    """
    import json as _json
    from camoufox.pkgman import INSTALL_DIR, LAUNCH_FILE, OS_NAME

    exe_name = LAUNCH_FILE.get(OS_NAME, "camoufox.exe")
    compat_flag = INSTALL_DIR / ".0.5_FLAG"

    # 新格式：COMPAT_FLAG 存在，让 launch_options() 自行处理
    if compat_flag.exists():
        return ""

    # 旧格式：直接在 INSTALL_DIR 下查找
    legacy_exe = INSTALL_DIR / exe_name
    if legacy_exe.exists():
        return str(legacy_exe)

    return ""


def _detect_ff_version() -> str:
    """从 version.json 读取 Firefox 主版本号（如 "152"）。

    旧格式安装的 version.json 格式：{"version": "152.0.4", "release": "beta.28"}
    新格式还包含 "build" 字段。
    """
    import json as _json
    from camoufox.pkgman import INSTALL_DIR

    version_file = INSTALL_DIR / "version.json"
    if not version_file.exists():
        # 检查 browsers/ 子目录
        browsers_dir = INSTALL_DIR / "browsers"
        if browsers_dir.exists():
            for vf in browsers_dir.rglob("version.json"):
                version_file = vf
                break
    if not version_file.exists():
        return ""

    try:
        data = _json.loads(version_file.read_bytes())
        version_str = str(data.get("version", ""))
        major = version_str.split(".")[0]
        return major if major.isdigit() else ""
    except Exception:
        return ""



def _is_valid_firefox_addon(path: str) -> bool:
    """Camoufox 要求 addon 为已解压目录，且根目录含 manifest.json。"""
    root = str(path or "").strip()
    if not root or not os.path.isdir(root):
        return False
    return os.path.isfile(os.path.join(root, "manifest.json"))


def _ensure_default_addons_or_exclude(disable_defaults: bool = False):
    """默认会加载 uBlock。若缓存目录损坏（缺 manifest），自动排除以免启动失败。"""
    try:
        from camoufox.addons import DefaultAddons, get_addon_path
    except Exception:
        return []
    if disable_defaults:
        return list(DefaultAddons)
    exclude = []
    for addon in DefaultAddons:
        addon_path = get_addon_path(addon.name)
        if os.path.exists(addon_path) and not _is_valid_firefox_addon(addon_path):
            # 半下载/损坏目录：删掉，让 camoufox 下次可重下；本次先 exclude
            try:
                shutil.rmtree(addon_path, ignore_errors=True)
            except Exception:
                pass
            exclude.append(addon)
    return exclude


def create_camoufox_options(unique_profile=True) -> dict:
    """构建 Camoufox 启动参数 dict。

    返回可直接传给 Camoufox(**opts) 的参数字典。

    浏览器策略：
    - headless：由 Web 设置控制，默认使用有头模式
    - humanize=True：人类化鼠标移动 + 点击轨迹
    - geoip=True：基于代理 IP 匹配时区 / 经纬度
    - locale：固定为英文或简体中文，避免代理出口改变页面语言
    - block_webrtc=True：WebRTC IP 泄漏防护（避免真实 IP 通过 STUN 暴露）
    - 指纹由 BrowserForge 自动生成（匹配 Firefox/Camoufox 引擎）
    """
    headless = _headless()
    opts: dict = {
        "headless": headless,
        "humanize": True,       # 人类化鼠标移动 + 贝塞尔轨迹
        "geoip": True,          # 基于 IP 匹配时区 / 经纬度；语言由 locale 单独固定
        "locale": _browser_locale(),  # 覆盖 GeoIP 语言，保持页面元素文本稳定
        "block_webrtc": True,   # 防止 WebRTC 泄漏真实 IP（即使使用代理）
        "i_know_what_im_doing": True,  # 抑制 Firefox 版本伪装警告（Camoufox 引擎层伪装是预期行为）
        "timeout": _BROWSER_LAUNCH_TIMEOUT_MS,
    }

    # 旧格式安装兼容：传 executable_path 绕过 installed_verstr() 检查
    # 注意：不传 ff_version，让 Camoufox 自动检测版本号
    # 传 ff_version 会导致指纹中版本号与引擎不匹配，Turnstile 检测到不一致会拒绝通过
    # 前提：config.json 中已设置 active_version="." 让 installed_verstr() 正常工作
    exe_path = _detect_camoufox_exe()
    if exe_path:
        opts["executable_path"] = exe_path

    # 代理
    proxies = _proxies()
    proxy = str(proxies.get("https") or proxies.get("http") or "").strip()
    if proxy:
        opts["proxy"] = _build_camoufox_proxy(proxy)

    # 扩展（Camoufox 使用 addons 参数，加载已解压的 Firefox 扩展目录）
    # 默认会附带 uBlock；若缓存损坏则自动 exclude，避免 manifest.json missing。
    exclude_addons = _ensure_default_addons_or_exclude(
        disable_defaults=low_traffic_enabled()
    )
    if exclude_addons:
        opts["exclude_addons"] = exclude_addons
    if _extension_path:
        if _is_valid_firefox_addon(_extension_path):
            opts["addons"] = [_extension_path]
        # 无效路径直接忽略，不阻断浏览器启动

    # Profile 隔离
    if unique_profile:
        profile_dir = _create_profile_dir(BROWSER_ENGINE_CAMOUFOX)
        opts["persistent_context"] = True
        opts["user_data_dir"] = profile_dir

    return opts


def create_cloakbrowser_options(unique_profile=True) -> dict:
    """构建 CloakBrowser ``launch_persistent_context`` 启动参数。"""
    opts: dict = {
        "headless": _headless(),
        "humanize": True,
        "geoip": True,
        # 明确固定页面语言；geoip 仍用于时区与 WebRTC 出口匹配。
        "locale": _browser_locale(),
        "timeout": _BROWSER_LAUNCH_TIMEOUT_MS,
    }

    proxies = _proxies()
    proxy = str(proxies.get("https") or proxies.get("http") or "").strip()
    if proxy:
        opts["proxy"] = _build_camoufox_proxy(proxy)

    # CloakBrowser 使用 Chromium 扩展目录；manifest.json 校验与 Firefox
    # 解压扩展的最低要求相同。
    if _extension_path and _is_valid_firefox_addon(_extension_path):
        opts["extension_paths"] = [_extension_path]

    if unique_profile:
        opts["user_data_dir"] = _create_profile_dir(BROWSER_ENGINE_CLOAKBROWSER)
    return opts


def create_browser_options(unique_profile=True, engine: Optional[str] = None) -> dict:
    """按所选后端构建启动参数；默认继续使用 Camoufox。"""
    selected = normalize_browser_engine(engine or selected_browser_engine())
    if selected == BROWSER_ENGINE_CLOAKBROWSER:
        return create_cloakbrowser_options(unique_profile=unique_profile)
    return create_camoufox_options(unique_profile=unique_profile)


class BrowserBackendUnavailable(RuntimeError):
    pass


def _launch_camoufox_context(opts: dict):
    # IsolatedCamoufox.__enter__() 直接创建全新事件循环，完全绕过
    # PlaywrightContextManager 的 get_running_loop() 检查。
    camoufox = IsolatedCamoufox(**opts)
    return camoufox.__enter__(), camoufox


def _launch_cloakbrowser_context(opts: dict):
    try:
        from cloakbrowser import launch_persistent_context
    except ImportError as exc:
        raise BrowserBackendUnavailable(
            "CloakBrowser 后端依赖未安装，请重新安装 requirements.txt"
        ) from exc

    launch_options = dict(opts)
    user_data_dir = str(launch_options.pop("user_data_dir", "") or "").strip()
    if not user_data_dir:
        user_data_dir = _create_profile_dir(BROWSER_ENGINE_CLOAKBROWSER)
    context = launch_persistent_context(user_data_dir=user_data_dir, **launch_options)
    return context, None


def _close_unwrapped_context(browser_context=None, lifecycle=None) -> None:
    try:
        if browser_context is not None:
            browser_context.close()
    except BaseException:
        pass
    try:
        if lifecycle is not None:
            lifecycle.__exit__(None, None, None)
    except BaseException:
        pass


def start_browser(log_callback=None, cancel_callback=None) -> Tuple[object, object]:
    """启动当前配置的浏览器后端并返回共用页面适配对象。

    ``browser_engine`` 缺省为 Camoufox；切换到 CloakBrowser 时只替换启动
    与生命周期实现，上层注册步骤保持不变。
    """
    engine = selected_browser_engine()
    engine_label = "CloakBrowser" if engine == BROWSER_ENGINE_CLOAKBROWSER else "Camoufox"
    last_exc = None

    def _launch_interrupted() -> bool:
        if _browser_launch_blocked.is_set():
            return True
        return bool(cancel_callback()) if cancel_callback else False

    for attempt in range(1, 5):
        if _launch_interrupted():
            raise RuntimeError("浏览器启动已被紧急终止操作阻止")
        while not _browser_launch_gate.acquire(timeout=0.4):
            if _launch_interrupted():
                raise RuntimeError("浏览器启动已被紧急终止操作阻止")
        profile_dir = None
        browser_context = None
        lifecycle = None
        retry_wait = 0
        try:
            opts = create_browser_options(unique_profile=True, engine=engine)
            profile_dir = getattr(_tls, "profile_dir", None)

            if engine == BROWSER_ENGINE_CLOAKBROWSER:
                browser_context, lifecycle = _launch_cloakbrowser_context(opts)
            else:
                browser_context, lifecycle = _launch_camoufox_context(opts)

            if _launch_interrupted():
                raise RuntimeError("浏览器启动已被紧急终止操作阻止")

            _install_low_traffic_routing(browser_context, log_callback=log_callback)
            _install_accounts_resource_diagnostics(
                browser_context, log_callback=log_callback
            )

            if _launch_interrupted():
                raise RuntimeError("浏览器启动已被紧急终止操作阻止")

            # 获取或创建页面
            raw_pages = (
                browser_context.pages
                if hasattr(browser_context, "pages")
                else []
            )
            if raw_pages:
                raw_page = raw_pages[0]
            else:
                raw_page = browser_context.new_page()

            page_obj = PageAdapter(raw_page, browser_context)
            browser_obj = BrowserAdapter(
                browser=None,
                context=browser_context,
                camoufox_instance=lifecycle,
                engine_name=engine,
            )
            browser_obj.user_data_path = profile_dir or ""

            set_browser_session(browser_obj, page_obj)
            browser_context = None
            lifecycle = None
            _note_start_success()

            if log_callback:
                log_callback(f"[*] 浏览器后端: {engine_label}")
                log_callback(f"[*] 浏览器模式: {'无头' if opts['headless'] else '有头'}")
                log_callback(f"[*] 浏览器语言: {opts['locale']}")
                proxy_options = opts.get("proxy") if isinstance(opts.get("proxy"), dict) else {}
                proxy_server = str(proxy_options.get("server") or "").strip()
                log_callback(
                    f"[*] {engine_label} 网络: {'代理 ' + proxy_server if proxy_server else '直连（未配置代理）'}"
                )
            if log_callback and profile_dir:
                log_callback(f"[Debug] 当前浏览器资料目录: {profile_dir}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser_obj, page_obj
        except Exception as exc:
            last_exc = exc
            streak = _note_start_failure()
            if log_callback:
                log_callback(
                    f"[Debug] {engine_label} 启动失败(第{attempt}/4次, 连续失败{streak}): {exc}"
                )
            _close_unwrapped_context(browser_context, lifecycle)
            profile_dir = profile_dir or getattr(_tls, "profile_dir", None)
            try:
                cur = active_browser()
                if cur is not None:
                    cur.quit(del_data=True)
            except Exception:
                pass
            set_browser_session(None, None)
            _cleanup_profile_dir(profile_dir)
            if isinstance(exc, BrowserBackendUnavailable):
                break
            if _launch_interrupted():
                raise RuntimeError("浏览器启动已被紧急终止操作阻止") from exc
            retry_wait = min(1.5 * attempt, 4)
        finally:
            try:
                _browser_launch_gate.release()
            except RuntimeError:
                pass
        if retry_wait:
            deadline = time.time() + retry_wait
            while time.time() < deadline:
                if _launch_interrupted():
                    raise RuntimeError("浏览器启动已被紧急终止操作阻止")
                time.sleep(min(0.2, max(0.0, deadline - time.time())))
    raise Exception(f"{engine_label} 启动失败: {last_exc}")


def stop_browser(force=False):
    if _debug() and not force:
        return
    current = active_browser()
    profile_dir = getattr(_tls, "profile_dir", None)
    set_browser_session(None, None)
    if current is None:
        _cleanup_profile_dir(profile_dir)
        return
    try:
        current.quit(del_data=True)
    except BaseException:
        pass
    _cleanup_profile_dir(profile_dir)


def restart_browser(log_callback=None, cancel_callback=None):
    stop_browser(force=True)
    return start_browser(log_callback=log_callback, cancel_callback=cancel_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    try:
        if _debug():
            if log_callback:
                log_callback(f"[*] 调试模式：保留浏览器（{reason}）")
            collected = gc.collect()
            if log_callback:
                log_callback(f"[*] Python GC 已回收对象数: {collected}")
            return
        if log_callback:
            log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
        stop_browser(force=True)
        collected = gc.collect()
        if log_callback:
            log_callback(f"[*] Python GC 已回收对象数: {collected}")
    except BaseException:
        try:
            if not _debug():
                stop_browser(force=True)
        except BaseException:
            pass


def refresh_active_page():
    if active_browser() is None:
        restart_browser()
    try:
        browser_obj = active_browser()
        tabs = browser_obj.get_tabs()
        page_obj = tabs[-1] if tabs else browser_obj.new_tab()
        set_browser_session(browser_obj, page_obj)
    except Exception:
        restart_browser()
    return page


def extract_cf_clearance_and_ua(log_callback=None, ensure_grok=True):
    """提取 grok.com 域 cf_clearance + UA。"""
    cf_clearance = ""
    user_agent = ""
    try:
        active = refresh_active_page()
        if active is None:
            return "", ""

        def _read_cf_and_ua(page_obj, grok_only=False):
            clearance = ""
            ua_text = ""
            cookies = page_obj.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                    domain = str(item.get("domain", "")).strip().lower()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()
                    domain = str(getattr(item, "domain", "")).strip().lower()
                if name != "cf_clearance" or not value:
                    continue
                if grok_only and "grok.com" not in domain:
                    continue
                if "grok.com" in domain:
                    clearance = value
                    break
                if not clearance and not grok_only:
                    clearance = value
            try:
                ua = page_obj.run_js("return navigator.userAgent;")
                if ua:
                    ua_text = str(ua).strip()
            except Exception:
                pass
            return clearance, ua_text

        def _page_passed_cf(page_obj):
            try:
                title = str(
                    page_obj.run_js("return document.title || '';") or ""
                ).lower()
                body = str(
                    page_obj.run_js(
                        "return (document.body && (document.body.innerText||'')) || '';"
                    )
                    or ""
                ).lower()
                if "just a moment" in title or "just a moment" in body[:200]:
                    return False
                if "checking your browser" in body[:300]:
                    return False
                return True
            except Exception:
                return False

        cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
        if ensure_grok and not cf_clearance:
            if log_callback:
                log_callback("[*] 未找到 grok.com 的 cf_clearance，打开 grok.com 过盾...")
            try:
                active.get("https://grok.com/")
                try:
                    active.wait.doc_loaded()
                except Exception:
                    pass
                time.sleep(2)
                for _ in range(20):
                    if _page_passed_cf(active):
                        cf_clearance, user_agent = _read_cf_and_ua(
                            active, grok_only=True
                        )
                        if cf_clearance:
                            break
                    time.sleep(1.0)
                if log_callback:
                    if cf_clearance:
                        log_callback("[*] 已取得 grok.com 的 cf_clearance")
                    else:
                        log_callback(
                            "[!] 打开 grok.com 后仍无有效 cf_clearance"
                            "（页面可能仍卡在 Just a moment）"
                        )
            except Exception as nav_exc:
                if log_callback:
                    log_callback(f"[Debug] 打开 grok.com 取 cf_clearance 失败: {nav_exc}")
                cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 提取 cf_clearance 失败: {exc}")
    return cf_clearance, user_agent
