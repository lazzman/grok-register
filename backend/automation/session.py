# -*- coding: utf-8 -*-
"""浏览器运行时管理。

隔离每个工作线程的浏览器与页面对象，并统一处理启动、重启、进程回收和临时
profile 清理。
"""
from __future__ import annotations

import gc
import os
import signal
import shutil
import tempfile
import threading
import time
import uuid
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
from backend.integrations.proxy import (
    HTTP_PROXY_SCHEMES,
    format_proxy_options_for_log,
    parse_http_proxy_url,
)
from backend.integrations.proxy_forwarder import (
    proxy_dict_has_credentials,
    start_local_auth_proxy,
)


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
_extension_path: str = ""
_start_fail_lock = threading.Lock()
_start_fail_streak = 0
_start_fail_threshold = 3
_browser_launch_blocked = threading.Event()


def configure(
    get_proxies=None,
    is_debug=None,
    is_headless=None,
    get_locale=None,
    get_engine=None,
    extension_path="",
):
    global _get_proxy, _is_debug, _is_headless, _get_locale, _get_engine, _extension_path
    _get_proxy = get_proxies
    _is_debug = is_debug
    _is_headless = is_headless
    _get_locale = get_locale
    _get_engine = get_engine
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


def _current_proxy_url() -> str:
    proxies = _proxies()
    return str(proxies.get("https") or proxies.get("http") or "").strip()


def _stop_proxy_forwarder() -> None:
    forwarder = getattr(_tls, "proxy_forwarder", None)
    _tls.proxy_forwarder = None
    _tls.browser_proxy_upstream = None
    _tls.launch_proxy_url = None
    if forwarder is None:
        return
    try:
        forwarder.stop()
    except Exception:
        pass


def browser_proxy_needs_refresh() -> bool:
    """当前线程代理 URL 是否已与浏览器启动时不同（粘性会话更换）。"""
    if active_browser() is None:
        return False
    launched = str(getattr(_tls, "launch_proxy_url", "") or "")
    return launched != _current_proxy_url()


def _attach_camoufox_auth_forwarder(opts: dict, engine: str) -> dict:
    """Camoufox/Firefox 无法稳定发送 HTTP 代理认证，改为本地无认证入口。"""
    proxy = opts.get("proxy") if isinstance(opts.get("proxy"), dict) else {}
    _tls.browser_proxy_upstream = dict(proxy) if proxy else None
    _tls.launch_proxy_url = _current_proxy_url()
    if engine != BROWSER_ENGINE_CAMOUFOX or not proxy_dict_has_credentials(proxy):
        _tls.proxy_forwarder = None
        return opts
    forwarder = start_local_auth_proxy(proxy)
    _tls.proxy_forwarder = forwarder
    patched = dict(opts)
    patched["proxy"] = {"server": forwarder.listen_url}
    return patched


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
    return _kill_browser_processes(
        _is_managed_browser_process,
        profile_engine=None,
        label="浏览器",
        log_callback=log_callback,
    )


def _build_camoufox_proxy(proxy_str: str) -> dict:
    """把代理 URL 转换为两个浏览器后端共用的 Playwright proxy dict。"""
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return {}
    parsed = urlparse(proxy_str)
    if parsed.scheme.lower() in HTTP_PROXY_SCHEMES:
        result = parse_http_proxy_url(proxy_str)
        # Playwright Firefox 在只有 username、没有 password 字段时往往不发认证。
        if "username" in result and "password" not in result:
            result["password"] = ""
        return result
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


def _ensure_default_addons_or_exclude():
    """默认会加载 uBlock。若缓存目录损坏（缺 manifest），自动排除以免启动失败。"""
    try:
        from camoufox.addons import DefaultAddons, get_addon_path
    except Exception:
        return []
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
    exclude_addons = _ensure_default_addons_or_exclude()
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


def start_browser(log_callback=None) -> Tuple[object, object]:
    """启动当前配置的浏览器后端并返回共用页面适配对象。

    ``browser_engine`` 缺省为 Camoufox；切换到 CloakBrowser 时只替换启动
    与生命周期实现，上层注册步骤保持不变。
    """
    engine = selected_browser_engine()
    engine_label = "CloakBrowser" if engine == BROWSER_ENGINE_CLOAKBROWSER else "Camoufox"
    last_exc = None
    for attempt in range(1, 5):
        if _browser_launch_blocked.is_set():
            raise RuntimeError("浏览器启动已被紧急终止操作阻止")
        profile_dir = None
        browser_context = None
        lifecycle = None
        try:
            _stop_proxy_forwarder()
            opts = create_browser_options(unique_profile=True, engine=engine)
            opts = _attach_camoufox_auth_forwarder(opts, engine)
            profile_dir = getattr(_tls, "profile_dir", None)

            if engine == BROWSER_ENGINE_CLOAKBROWSER:
                browser_context, lifecycle = _launch_cloakbrowser_context(opts)
            else:
                browser_context, lifecycle = _launch_camoufox_context(opts)

            if _browser_launch_blocked.is_set():
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
                upstream = getattr(_tls, "browser_proxy_upstream", None)
                proxy_display = format_proxy_options_for_log(
                    upstream if isinstance(upstream, dict) else {}
                )
                forwarder = getattr(_tls, "proxy_forwarder", None)
                if proxy_display and forwarder is not None:
                    log_callback(
                        f"[*] {engine_label} 网络: 本地转发 {forwarder.listen_url} → {proxy_display}"
                    )
                else:
                    log_callback(
                        f"[*] {engine_label} 网络: {'代理 ' + proxy_display if proxy_display else '直连（未配置代理）'}"
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
            _stop_proxy_forwarder()
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
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"{engine_label} 启动失败: {last_exc}")


def stop_browser(force=False):
    if _debug() and not force:
        return
    current = active_browser()
    profile_dir = getattr(_tls, "profile_dir", None)
    set_browser_session(None, None)
    _stop_proxy_forwarder()
    if current is None:
        _cleanup_profile_dir(profile_dir)
        return
    try:
        current.quit(del_data=True)
    except BaseException:
        pass
    _cleanup_profile_dir(profile_dir)


def restart_browser(log_callback=None):
    stop_browser(force=True)
    return start_browser(log_callback=log_callback)


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
