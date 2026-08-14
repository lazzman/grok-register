"""Mail Hub OTP 聚合接码服务提供商。

API：
  - POST   /api/v1/otp                    创建会话，返回服务端分配邮箱
  - GET    /api/v1/otp/{email}            查询会话状态（可 wait_seconds 长轮询）
  - GET    /api/v1/otp/{email}/messages   读取完整邮件列表（本地回退提取）
  - DELETE /api/v1/otp/{email}            释放会话
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

from backend.mailbox.utilities import extract_verification_code, strip_html

HttpDelete = Callable[..., Any]
HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]

API_PREFIX = "/api/v1"

# 默认 xAI 验证码正则：形如 W7J-00I，尽量避开 HTML 属性碎片
DEFAULT_VERIFICATION_PATTERN = (
    r"(?<![A-Za-z0-9-])([A-Za-z0-9]{3}-[A-Za-z0-9]{3})(?![A-Za-z0-9-])"
)


def otp_url(api_base: str, path: str) -> str:
    """拼接 Mail Hub OTP 地址，兼容根地址和已包含 ``/api/v1`` 的地址。"""
    base = str(api_base or "").strip().rstrip("/")
    if not base:
        raise Exception("Mail Hub API Base 未配置")
    normalized_path = path if str(path).startswith("/") else f"/{path}"
    if base.lower().endswith(API_PREFIX):
        return f"{base}{normalized_path}"
    return f"{base}{API_PREFIX}{normalized_path}"


def build_headers(api_key: str, *, content_type: bool = False) -> dict:
    key = str(api_key or "").strip()
    if not key:
        raise Exception("Mail Hub API Key 未配置")
    headers = {"X-API-Key": key}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _response_json(response: Any, action: str) -> dict:
    response.raise_for_status()
    try:
        data = response.json()
    except Exception as exc:
        preview = str(getattr(response, "text", "") or "")[:300]
        raise Exception(f"Mail Hub {action}接口返回非JSON: {preview}") from exc
    if not isinstance(data, dict):
        raise Exception(f"Mail Hub {action}接口返回格式错误: {data}")
    return data


def create_otp_session(
    http_post: HttpPost,
    api_base: str,
    api_key: str,
    *,
    session_ttl_seconds: Any = 300,
    verification_pattern: str = "",
    hint_from_contains: str = "",
    hint_subject_contains: str = "",
) -> str:
    """创建 OTP 会话并返回由服务端分配的邮箱地址。"""
    try:
        ttl = int(session_ttl_seconds or 300)
    except (TypeError, ValueError) as exc:
        raise Exception("Mail Hub 会话 TTL 必须是整数") from exc

    payload: Dict[str, Any] = {"session_ttl_seconds": ttl}
    pattern = str(verification_pattern or "").strip()
    if pattern:
        payload["verification_pattern"] = pattern

    hint: Dict[str, str] = {}
    from_contains = str(hint_from_contains or "").strip()
    subject_contains = str(hint_subject_contains or "").strip()
    if from_contains:
        hint["from_contains"] = from_contains
    if subject_contains:
        hint["subject_contains"] = subject_contains
    if hint:
        payload["hint"] = hint

    response = http_post(
        otp_url(api_base, "/otp"),
        headers=build_headers(api_key, content_type=True),
        json=payload,
        timeout=20,
    )
    data = _response_json(response, "创建邮箱")
    email = str(data.get("email", "") or "").strip()
    if not email:
        raise Exception(f"Mail Hub 创建邮箱接口缺少 email: {data}")
    return email


def get_session_status(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    email: str,
    *,
    wait_seconds: Any = 0,
) -> dict:
    """查询 Mail Hub OTP 会话状态。"""
    address = str(email or "").strip()
    if not address:
        raise ValueError("Mail Hub 邮箱地址不能为空")
    try:
        wait = max(0, int(wait_seconds or 0))
    except (TypeError, ValueError) as exc:
        raise Exception("Mail Hub wait_seconds 必须是整数") from exc

    response = http_get(
        otp_url(api_base, f"/otp/{quote(address, safe='')}"),
        headers=build_headers(api_key),
        params={"wait_seconds": wait},
        timeout=max(20, wait + 5),
    )
    return _response_json(response, "查询邮箱")


def get_messages(
    http_get: HttpGet,
    api_base: str,
    api_key: str,
    email: str,
) -> List[dict]:
    """读取 Mail Hub 当前会话的完整邮件列表。"""
    address = str(email or "").strip()
    if not address:
        raise ValueError("Mail Hub 邮箱地址不能为空")
    response = http_get(
        otp_url(api_base, f"/otp/{quote(address, safe='')}/messages"),
        headers=build_headers(api_key),
        timeout=20,
    )
    data = _response_json(response, "邮件列表")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise Exception(f"Mail Hub 邮件列表接口返回格式错误: {data}")
    return [message for message in messages if isinstance(message, dict)]


def release_session(
    http_delete: HttpDelete,
    api_base: str,
    api_key: str,
    email: str,
) -> None:
    """尽力释放 Mail Hub OTP 会话。"""
    address = str(email or "").strip()
    if not address:
        raise ValueError("Mail Hub 邮箱地址不能为空")
    response = http_delete(
        otp_url(api_base, f"/otp/{quote(address, safe='')}"),
        headers=build_headers(api_key),
        timeout=20,
    )
    response.raise_for_status()


def _normalize_mail_body(message: dict) -> str:
    """将 Mail Hub 邮件字段归一为可供正则匹配的文本。"""
    parts: List[str] = []
    for key in (
        "text_content",
        "raw_content",
        "text",
        "raw",
        "content",
        "intro",
        "body",
        "snippet",
    ):
        value = message.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    for key in ("html_content", "html"):
        value = message.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                parts.append(strip_html(item))
    return "\n".join(parts)


def extract_code_from_messages(
    messages: List[dict],
    verification_pattern: str = "",
) -> Optional[str]:
    """从 Mail Hub 邮件列表本地回退提取验证码。"""
    pattern = str(verification_pattern or "").strip()
    compiled = None
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = None

    for message in messages:
        subject = str(message.get("subject", "") or "")
        content = _normalize_mail_body(message)
        for source in (subject, content):
            if not source:
                continue
            if compiled:
                match = compiled.search(source)
                if match:
                    value = match.group(1) if match.lastindex else match.group(0)
                    value = str(value or "").strip()
                    if value:
                        return value
        code = extract_verification_code(f"{subject}\n{content}", subject)
        if code:
            return code
    return None


def poll_wait_seconds(poll_interval: Any) -> int:
    """确保每次状态查询都请求上游拉取邮件，而非只读本地缓存。"""
    try:
        return max(1, int(float(poll_interval)))
    except (TypeError, ValueError):
        return 1


def create_mailbox(
    http_post: HttpPost,
    api_base: str,
    api_key: str,
    *,
    session_ttl_seconds: Any = 300,
    verification_pattern: str = "",
    hint_from_contains: str = "",
    hint_subject_contains: str = "",
) -> tuple[str, str]:
    """创建会话；token 使用 mailhub:{email} 占位，不把 API Key 当凭证存。"""
    email = create_otp_session(
        http_post,
        api_base,
        api_key,
        session_ttl_seconds=session_ttl_seconds,
        verification_pattern=verification_pattern,
        hint_from_contains=hint_from_contains,
        hint_subject_contains=hint_subject_contains,
    )
    print(f"[*] 已创建 Mail Hub 邮箱: {email}")
    return email, f"mailhub:{email}"


def wait_for_code(
    http_get: HttpGet,
    http_delete: HttpDelete,
    api_base: str,
    api_key: str,
    email: str,
    *,
    verification_pattern: str = "",
    timeout: int = 180,
    poll_interval: int = 3,
    raise_if_cancelled: Callable[[Optional[Callable[[], bool]]], None],
    sleep_with_cancel: Callable[[float, Optional[Callable[[], bool]]], None],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    resend_callback: Optional[Callable[[], None]] = None,
) -> str:
    """轮询 Mail Hub 验证码；结束后无论成功、失败还是取消都释放会话。"""
    deadline = time.time() + timeout
    next_resend_at = time.time() + 35
    next_message_fallback_at = 0.0
    message_fallback_interval = max(10.0, float(poll_interval or 0) * 3)
    status_wait_seconds = poll_wait_seconds(poll_interval)

    try:
        while time.time() < deadline:
            raise_if_cancelled(cancel_callback)

            if resend_callback and time.time() >= next_resend_at:
                try:
                    resend_callback()
                    if log_callback:
                        log_callback("[*] 已触发重新发送验证码")
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] 触发重发验证码失败: {exc}")
                next_resend_at = time.time() + 35

            try:
                status = get_session_status(
                    http_get,
                    api_base,
                    api_key,
                    email,
                    wait_seconds=status_wait_seconds,
                )
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Mail Hub 查询验证码失败: {exc}")
                sleep_with_cancel(max(0.2, float(poll_interval or 1)), cancel_callback)
                continue

            code = str(status.get("code", "") or "").strip()
            if code:
                if log_callback:
                    log_callback(f"[*] Mail Hub 从状态中提取到验证码: {code}")
                return code

            now = time.time()
            if now >= next_message_fallback_at:
                try:
                    messages = get_messages(http_get, api_base, api_key, email)
                    fallback_code = extract_code_from_messages(
                        messages, verification_pattern
                    )
                    if fallback_code:
                        if log_callback:
                            log_callback(
                                f"[*] Mail Hub 从邮件列表回退提取到验证码: {fallback_code}"
                            )
                        return fallback_code
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] Mail Hub 邮件列表回退提取失败: {exc}")
                next_message_fallback_at = now + message_fallback_interval

            session_status = str(status.get("status", "") or "").strip().lower()
            if session_status == "expired":
                raise Exception("Mail Hub 邮箱会话已过期，未收到验证码")
            if session_status == "released":
                raise Exception("Mail Hub 邮箱会话已释放，未收到验证码")

        raise Exception(f"Mail Hub 在 {timeout}s 内未收到验证码邮件")
    finally:
        try:
            release_session(http_delete, api_base, api_key, email)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Mail Hub 释放邮箱会话失败: {exc}")
