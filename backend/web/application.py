# -*- coding: utf-8 -*-
"""管理控制台应用。

本模块负责 HTTP 路由、管理员会话、配置读写和静态资源分发；注册执行由
``backend.registration`` 与 ``backend.web.jobs`` 提供。
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import secrets
import time
import traceback
import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .account_exports import build_account_auth_archive, build_sso_archive, read_sso_token
from .jobs import job_coordinator
from .relogin_jobs import relogin_coordinator
from .sso_check_jobs import sso_check_coordinator
from .update_check import ReleaseUpdateService
from backend.integrations.proxy import validate_http_proxy_url
from backend.integrations import grokiq
from backend.shared.paths import DATA_ROOT, PROJECT_ROOT, STATIC_ROOT
from backend.shared.version import current_version

APP_DIR = PROJECT_ROOT
DATA_DIR = DATA_ROOT
STATIC_DIR = STATIC_ROOT
WEB_SESSION_COOKIE = "grok_register_session"
WEB_SESSION_TTL = 60 * 60 * 24 * 7
WEB_AUTH_FILE = DATA_DIR / "web_auth.json"
LEGACY_WEB_AUTH_FILE = APP_DIR / "web_auth.json"
MAX_BATCH_ACCOUNT_IDS = 10000

CONFIG_PUBLIC_KEYS = (
    "email_provider",
    "duckmail_api_key",
    "duckmail_api_base",
    "defaultDomains",
    "cloudmail_url",
    "cloudmail_admin_email",
    "cloudmail_password",
    "cloudflare_api_base",
    "cloudflare_api_key",
    "cloudflare_auth_mode",
    "cloudflare_custom_auth",
    "cloudflare_path_domains",
    "cloudflare_path_accounts",
    "cloudflare_path_token",
    "cloudflare_path_messages",
    "outlookemail_api_base",
    "outlookemail_api_key",
    "outlookemail_source",
    "outlookemail_group_id",
    "outlookemail_web_password",
    "outlookemail_session_cookie",
    "outlookemail_temp_tag_ids",
    "outlookemail_folder",
    "outlookemail_top",
    "outlookemail_pick_mode",
    "outlookemail_disable_after_cpa_success",
    "proxy",
    "sticky_proxy",
    "enable_nsfw",
    "debug_mode",
    "browser_engine",
    "browser_headless",
    "browser_locale",
    "close_browser_on_stop",
    "log_level",
    "register_count",
    "register_workers",
    "user_agent",
    "cpa_auto_add",
    "sso_detailed_risk_check",
    "cpa_token_mode",
    "cpa_auth_dir",
    "cpa_remote_url",
    "cpa_management_key",
    "grok2api_auth_dir",
    "grok2api_remote_url",
    "grok2api_remote_username",
    "grok2api_remote_password",
    "grok2api_auto_import",
    "grok2api_auto_import_build",
    "grok2api_auto_import_web",
    "grok2api_auto_import_console",
    "cpa_upload_enabled",
    "sub2api_enabled",
    "sub2api_remote_url",
    "sub2api_api_key",
    "sub2api_group_ids",
    "sub2api_proxy_id",
    "sub2api_concurrency",
    "sub2api_priority",
    "sub2api_name_prefix",
    "grokiq_webhook_enabled",
    "grokiq_webhook_url",
    "grokiq_webhook_token",
    "grokiq_webhook_timeout_seconds",
    "mailnest_api_key",
    "mailnest_project_code",
    "mailhub_api_base",
    "mailhub_api_key",
    "mailhub_session_ttl_seconds",
    "mailhub_verification_pattern",
    "mailhub_hint_from_contains",
    "mailhub_hint_subject_contains",
    "yyds_api_key",
    "yyds_jwt",
    "yyds_default_domain",
    "account_interval",
)

SENSITIVE_HINT_KEYS = {
    "duckmail_api_key",
    "cloudmail_password",
    "cloudflare_api_key",
    "cloudflare_custom_auth",
    "outlookemail_api_key",
    "outlookemail_web_password",
    "outlookemail_session_cookie",
    "cpa_management_key",
    "grok2api_remote_password",
    "sub2api_api_key",
    "grokiq_webhook_token",
    "mailnest_api_key",
    "mailhub_api_key",
    "yyds_api_key",
    "yyds_jwt",
    "proxy",
}


class AccountIdsBody(BaseModel):
    ids: List[int] = Field(default_factory=list)


class DeleteAccountsBody(AccountIdsBody):
    delete_files: bool = True


class ConfigUpdateBody(BaseModel):
    config: Optional[Dict[str, Any]] = None

    class Config:
        extra = "allow"


class StartJobBody(BaseModel):
    count: Optional[int] = None
    workers: Optional[int] = None
    config: Optional[Dict[str, Any]] = None


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""
    confirm_password: str = ""


def _batch_account_ids(ids: List[int]) -> List[int]:
    normalized: List[int] = []
    seen = set()
    for account_id in ids or []:
        if account_id <= 0:
            raise HTTPException(status_code=400, detail="账号 ID 必须是正整数")
        if account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)
        if len(normalized) > MAX_BATCH_ACCOUNT_IDS:
            raise HTTPException(
                status_code=400,
                detail=f"单次最多操作 {MAX_BATCH_ACCOUNT_IDS} 个账号",
            )
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择要操作的账号")
    return normalized


def _gr():
    from backend.registration import engine as gr

    return gr


def _load_auth_record() -> Dict[str, str] | None:
    for path in (WEB_AUTH_FILE, LEGACY_WEB_AUTH_FILE):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("username") and data.get("password_hash"):
            return {str(key): str(value) for key, value in data.items()}
    return None


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000).hex()


def _create_auth_record(username: str, password: str) -> Dict[str, str]:
    salt = secrets.token_bytes(16)
    return {
        "username": username,
        "password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "password_hash": _hash_password(password, salt),
        "session_secret": secrets.token_urlsafe(32),
    }


def _save_auth_record(record: Dict[str, str]) -> None:
    WEB_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = WEB_AUTH_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=True, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, WEB_AUTH_FILE)


def _auth_record() -> Dict[str, str] | None:
    return _load_auth_record()


def _web_auth_enabled() -> bool:
    return _auth_record() is not None


def _sign_session(username: str, expires_at: int, secret: str) -> str:
    payload = f"{username}\n{expires_at}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _valid_session(value: str) -> bool:
    record = _auth_record()
    username = str((record or {}).get("username") or "")
    secret = str((record or {}).get("session_secret") or "")
    if not username or not secret or not value or "." not in value:
        return False
    encoded, signature = value.split(".", 1)
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        raw_username, raw_expires = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8").split("\n", 1)
        return hmac.compare_digest(raw_username, username) and int(raw_expires) > int(time.time())
    except (ValueError, UnicodeError, base64.binascii.Error):
        return False


def _auth_required_path(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return path not in {
        "/api/health",
        "/api/auth/login",
        "/api/auth/setup",
        "/api/auth/me",
        "/api/auth/logout",
    }


def _public_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    gr = _gr()
    for key in CONFIG_PUBLIC_KEYS:
        if key in raw:
            out[key] = raw.get(key)
        elif key in gr.DEFAULT_CONFIG:
            out[key] = gr.DEFAULT_CONFIG.get(key)
    out["_sensitive_keys"] = sorted(SENSITIVE_HINT_KEYS)
    return out


def _config_file_snapshot() -> Dict[str, Any]:
    """读取磁盘上的实际 config.json，并返回适合管理端展示的元数据。"""
    gr = _gr()
    path = Path(gr.CONFIG_FILE).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    result: Dict[str, Any] = {
        "path": str(resolved),
        "exists": resolved.is_file(),
        "size": 0,
        "modified_at": "",
        "content": "{}",
        "parse_error": "",
        "sensitive_keys": sorted(SENSITIVE_HINT_KEYS),
    }
    if not resolved.is_file():
        gr.load_config()
        result["content"] = json.dumps(gr.config, ensure_ascii=False, indent=2)
        return result
    try:
        stat = resolved.stat()
        result["size"] = int(stat.st_size)
        result["modified_at"] = datetime.datetime.fromtimestamp(
            stat.st_mtime, tz=datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")
        if stat.st_size > 2 * 1024 * 1024:
            raise ValueError("config.json 超过 2 MiB")
        raw_text = resolved.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
        result["content"] = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        result["parse_error"] = str(exc)
        try:
            result["content"] = resolved.read_text(encoding="utf-8")[: 2 * 1024 * 1024]
        except (OSError, UnicodeError):
            result["content"] = ""
    return result


def _apply_config_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    gr = _gr()
    gr.load_config()
    proxy_update: Optional[str] = None
    if "proxy" in updates:
        proxy_update = str(updates.get("proxy") or "").strip()
        if proxy_update.lower().startswith(("http:", "https:")):
            try:
                proxy_update = validate_http_proxy_url(proxy_update)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"网络代理格式错误: {exc}") from exc
    changed: List[str] = []
    for key in CONFIG_PUBLIC_KEYS:
        if key not in updates:
            continue
        value = updates[key]
        if key in (
            "enable_nsfw",
            "sticky_proxy",
            "debug_mode",
            "browser_headless",
            "close_browser_on_stop",
            "cpa_auto_add",
            "sso_detailed_risk_check",
            "grok2api_auto_import",
            "grok2api_auto_import_build",
            "grok2api_auto_import_web",
            "grok2api_auto_import_console",
            "cpa_upload_enabled",
            "sub2api_enabled",
            "grokiq_webhook_enabled",
            "outlookemail_disable_after_cpa_success",
        ):
            value = bool(value)
        elif key in (
            "register_count",
            "register_workers",
            "outlookemail_top",
            "mailhub_session_ttl_seconds",
            "grokiq_webhook_timeout_seconds",
            "sub2api_proxy_id",
            "sub2api_concurrency",
            "sub2api_priority",
        ):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if key == "register_count":
                value = max(1, min(value, 1000))
            elif key == "register_workers":
                value = max(1, min(value, 8))
            elif key == "outlookemail_top":
                value = max(1, min(value, 50))
            elif key == "mailhub_session_ttl_seconds":
                value = max(1, min(value, 86_400))
            elif key == "grokiq_webhook_timeout_seconds":
                value = max(1, min(value, 60))
            elif key == "sub2api_proxy_id":
                value = max(0, value)
            elif key == "sub2api_concurrency":
                value = max(1, min(value, 32))
            elif key == "sub2api_priority":
                value = max(-100, min(value, 100))
        elif key == "log_level":
            value = str(value or "info").strip().lower() or "info"
        elif key == "browser_locale":
            value = str(value or "en-US").strip()
            if value not in {"en-US", "zh-CN"}:
                value = "en-US"
        elif key == "browser_engine":
            value = str(value or "camoufox").strip().lower()
            if value not in {"camoufox", "cloakbrowser"}:
                value = "camoufox"
        elif key == "email_provider":
            value = str(value or "cloudflare").strip().lower() or "cloudflare"
            if value not in {"cloudflare", "duckmail", "yyds", "mailnest", "mailhub", "outlookemail", "cloudmail"}:
                value = "cloudflare"
        elif key == "outlookemail_source":
            value = str(value or "accounts").strip().lower()
            if value not in {"accounts", "temp"}:
                value = "accounts"
        elif key == "outlookemail_pick_mode":
            value = str(value or "random").strip().lower()
            if value not in {"random", "sequential"}:
                value = "random"
        elif key == "cloudflare_auth_mode":
            value = str(value or "none").strip().lower()
            if value not in {"none", "bearer", "x-api-key", "x-admin-auth", "query-key"}:
                value = "none"
        elif key == "cpa_token_mode":
            mode = str(value or "device_protocol").strip().lower()
            if mode not in ("device_protocol", "device_browser", "auth_code"):
                mode = "device_protocol"
            value = mode
        elif key in (
            "proxy",
            "cpa_remote_url",
            "grok2api_remote_url",
            "sub2api_remote_url",
            "grokiq_webhook_url",
            "outlookemail_api_base",
            "duckmail_api_base",
            "cloudflare_api_base",
            "mailhub_api_base",
        ):
            value = proxy_update if key == "proxy" else str(value or "").strip()
            if key == "sub2api_remote_url":
                value = value.rstrip("/")
                if value and not value.startswith(("http://", "https://")):
                    raise HTTPException(
                        status_code=400,
                        detail="Sub2API 站点地址必须以 http:// 或 https:// 开头",
                    )
            if key == "grokiq_webhook_url" and value:
                try:
                    grokiq.validate_grokiq_config(
                        {
                            **gr.config,
                            **updates,
                            "grokiq_webhook_url": value,
                        }
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif key == "sub2api_group_ids":
            # 按逗号拆分后只保留纯数字项，防止脏数据进入请求体
            raw = str(value or "").strip()
            cleaned: List[str] = []
            if raw:
                for part in raw.split(","):
                    item = part.strip()
                    if item.isdigit():
                        cleaned.append(item)
            value = ",".join(cleaned)
        else:
            if isinstance(value, (dict, list)):
                continue
            value = value if isinstance(value, (int, float, bool)) else str(
                value if value is not None else ""
            )
        gr.config[key] = value
        changed.append(key)
    try:
        grokiq.validate_grokiq_config(gr.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    gr.save_config()
    if any(key.startswith("grokiq_webhook_") for key in changed):
        grokiq.grokiq_notifier.wake()
    return {"changed": changed, "config": _public_config(gr.config)}


def _serialize_record(
    record: Dict[str, Any],
    grokiq_delivery: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    item = dict(record or {})
    if not item.get("cpa_auth_path"):
        item["cpa_auth_path"] = _record_auth_path(item, "cpa")
    if not item.get("grok2api_auth_path"):
        item["grok2api_auth_path"] = _record_auth_path(item, "grok2api")
    item["success"] = bool(item.get("success"))
    item["cpa_enabled"] = bool(item.get("cpa_enabled"))
    item["sso_saved"] = bool(item.get("sso_saved"))
    bfs_text = "" if item.get("bfs") is None else str(item.get("bfs")).strip()
    item["bfs"] = bfs_text
    item["bot_risk"] = bool(item.get("bot_risk")) or bool(bfs_text and bfs_text != "0")
    raw_config = _gr().config
    from backend.integrations.grok2api_client import Grok2APIClient

    item["grok2api_remote_configured"] = Grok2APIClient.is_configured(raw_config)
    for kind in ("cpa", "grok2api"):
        try:
            _find_account_auth_file(item, raw_config, kind)
            item[f"{kind}_auth_available"] = True
        except (FileNotFoundError, OSError, ValueError):
            item[f"{kind}_auth_available"] = False
    item["sso_available"] = _account_has_sso(item)
    item["screenshot_url"] = (
        f"/api/accounts/{item.get('id')}/failure-screenshot"
        if str(item.get("screenshot_path") or "").strip()
        else ""
    )
    extra = item.get("extra_json") or "{}"
    if isinstance(extra, str):
        try:
            item["extra"] = json.loads(extra) if extra.strip() else {}
        except Exception:
            item["extra"] = {"raw": extra}
    else:
        item["extra"] = extra
    extra_data = item["extra"] if isinstance(item["extra"], dict) else {}
    risk_check = extra_data.get("sso_risk_check")
    item["sso_risk_check"] = risk_check if isinstance(risk_check, dict) else None
    item["exception_traceback"] = str(extra_data.get("exception_traceback") or "")
    item["exception_type"] = str(extra_data.get("exception_type") or "")
    item["has_exception_traceback"] = bool(item["exception_traceback"])
    delivery = dict(grokiq_delivery or {})
    item["grokiq_delivery"] = {
        "event_id": str(delivery.get("event_id") or ""),
        "status": str(delivery.get("status") or "not_queued"),
        "attempts": int(delivery.get("attempts") or 0),
        "last_attempt_at": str(delivery.get("last_attempt_at") or ""),
        "delivered_at": str(delivery.get("delivered_at") or ""),
        "last_error": str(delivery.get("last_error") or ""),
    }
    return item


def _auth_directory(raw: Any, fallback: str) -> Path:
    value = str(raw or fallback).strip() or fallback
    path = Path(value).expanduser()
    return path if path.is_absolute() else APP_DIR / path


def _safe_auth_identifier(email: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "._-@" else "_"
        for char in str(email or "").strip()
    )
    return safe or "unknown"


def _path_within(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _record_auth_path(record: Dict[str, Any], kind: str) -> str:
    direct_key = "cpa_auth_path" if kind == "cpa" else "grok2api_auth_path"
    direct = str(record.get(direct_key) or "").strip()
    if direct:
        return direct
    prefix = "CPA 本地:" if kind == "cpa" else "Grok2API:"
    for line in str(record.get("auth_info") or "").splitlines():
        text = line.strip()
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    legacy = str(record.get("auth_path") or "").strip()
    legacy_name = Path(legacy).name.lower() if legacy else ""
    if kind == "cpa" and legacy_name.startswith("xai-"):
        return legacy
    if kind == "grok2api" and legacy_name.startswith("g2a-"):
        return legacy
    return ""


def _find_account_auth_file(record: Dict[str, Any], raw_config: Dict[str, Any], kind: str) -> Path:
    if kind not in {"cpa", "grok2api"}:
        raise ValueError("kind 必须是 cpa 或 grok2api")
    if kind == "cpa":
        roots = [
            _auth_directory(raw_config.get("cpa_auth_dir"), "data/cpa_auth"),
            DATA_DIR / "cpa_auth",
        ]
    else:
        roots = [
            _auth_directory(raw_config.get("grok2api_auth_dir"), "data/grok2api_auth"),
            DATA_DIR / "grok2api_auth",
        ]
    roots = list(dict.fromkeys(path.resolve() for path in roots))

    safe = _safe_auth_identifier(str(record.get("email") or ""))
    if kind == "cpa":
        cpa_name = safe if safe.lower().startswith("xai") else f"xai-{safe}"
        expected_name = f"{cpa_name}.json"
    else:
        expected_name = f"g2a-{safe}.json"

    candidates: List[Path] = []
    recorded = _record_auth_path(record, kind)
    if recorded:
        recorded_path = Path(recorded).expanduser()
        if not recorded_path.is_absolute():
            recorded_path = APP_DIR / recorded_path
        candidates.append(recorded_path)
        candidates.extend(root / recorded_path.name for root in roots)
    candidates.extend(root / expected_name for root in roots)

    seen = set()
    for candidate in candidates:
        try:
            normalized = candidate.resolve()
        except OSError:
            continue
        key = str(normalized)
        if key in seen or not _path_within(normalized, roots):
            continue
        seen.add(key)
        if not normalized.is_file():
            continue
        return normalized
    label = "CPA" if kind == "cpa" else "Grok2API"
    raise FileNotFoundError(f"未找到该账号对应的 {label} JSON")


def _find_account_grok2api_files(
    record: Dict[str, Any], raw_config: Dict[str, Any]
) -> Dict[str, Path]:
    """发现账号对应的 Grok Build、Web、Console 三种导入文件。"""
    roots = [
        _auth_directory(raw_config.get("grok2api_auth_dir"), "data/grok2api_auth"),
        DATA_DIR / "grok2api_auth",
    ]
    roots = list(dict.fromkeys(path.resolve() for path in roots))
    safe = _safe_auth_identifier(str(record.get("email") or ""))
    expected_names = {
        "grok_build": f"g2a-{safe}.json",
        "grok_web": f"grok-web-{safe}.json",
        "grok_console": f"grok-console-{safe}.json",
    }
    recorded_paths: Dict[str, List[Path]] = {name: [] for name in expected_names}
    direct = str(record.get("grok2api_auth_path") or "").strip()
    if direct:
        recorded_paths["grok_build"].append(Path(direct).expanduser())
    prefixes = {f"Grok2API {name}:": name for name in expected_names}
    for line in str(record.get("auth_info") or "").splitlines():
        text = line.strip()
        for prefix, name in prefixes.items():
            if text.startswith(prefix):
                recorded_paths[name].append(Path(text[len(prefix) :].strip()).expanduser())

    found: Dict[str, Path] = {}
    for name, expected in expected_names.items():
        candidates = list(recorded_paths[name])
        candidates.extend(root / expected for root in roots)
        for candidate in candidates:
            path = candidate if candidate.is_absolute() else APP_DIR / candidate
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not _path_within(resolved, roots) or not resolved.is_file():
                continue
            found[name] = resolved
            break
    return found


def _import_account_grok2api(
    store: Any,
    config: Dict[str, Any],
    record: Dict[str, Any],
    client: Any,
) -> Dict[str, Any]:
    """导入一个账号的 Grok2API JSON，并更新远程入库状态。"""
    from backend.integrations.grok2api_client import Grok2APIImportError

    account_id = int(record.get("id") or 0)
    paths = _find_account_grok2api_files(record, config)
    if not paths:
        raise FileNotFoundError("未找到该账号对应的 Grok2API JSON")

    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for format_name, path in paths.items():
        try:
            results[format_name] = client.import_auth_file(
                path, format_name=format_name
            )
        except Grok2APIImportError as exc:
            errors[format_name] = str(exc)

    if not results:
        error_text = "; ".join(
            f"{name}: {error}" for name, error in errors.items()
        )
        store.update_remote_import_status(
            account_id,
            "grok2api",
            status="failed",
            error=error_text,
        )
        raise Grok2APIImportError(error_text or "Grok2API 导入失败")

    sync_failed = sum(
        int(result.get("syncFailed", 0) or 0) for result in results.values()
    )
    import_status = "partial" if errors or sync_failed else "success"
    import_errors = [f"{name}: {error}" for name, error in errors.items()]
    if sync_failed:
        import_errors.append(f"远程同步失败 {sync_failed} 个")
    store.update_remote_import_status(
        account_id,
        "grok2api",
        status=import_status,
        error="; ".join(import_errors),
    )
    refreshed_rows = store.get_results_by_ids([account_id])
    refreshed = refreshed_rows[0] if refreshed_rows else record
    grokiq_notification: Dict[str, Any] = {"queued": False}
    if "grok_build" in results:
        try:
            event = grokiq.enqueue_imported_account(store, refreshed, config)
            grokiq_notification = {
                "queued": bool(event),
                "eventId": str((event or {}).get("event_id") or ""),
            }
        except Exception as exc:
            grokiq_notification = {"queued": False, "error": str(exc)}
    return {
        "result": {
            "formats": results,
            "errors": errors,
            "created": sum(
                int(result.get("created", 0) or 0) for result in results.values()
            ),
            "updated": sum(
                int(result.get("updated", 0) or 0) for result in results.values()
            ),
            "synced": sum(
                int(result.get("synced", 0) or 0) for result in results.values()
            ),
            "syncFailed": sync_failed,
        },
        "grokiqNotification": grokiq_notification,
        "record": refreshed,
    }


def _load_account_auth_json(record: Dict[str, Any], raw_config: Dict[str, Any], kind: str) -> Dict[str, Any]:
    path = _find_account_auth_file(record, raw_config, kind)
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"{path.name} 超过 2 MiB")
        content = path.read_text(encoding="utf-8")
        json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    return {"kind": kind, "path": str(path), "content": content}


def _find_account_sso_file(record: Dict[str, Any]) -> Path:
    email = str(record.get("email") or "").strip()
    direct = str(record.get("account_file") or "").strip()
    candidates = [Path(direct).expanduser()] if direct else []
    if email:
        safe_email = email.replace("/", "_").replace("\\", "_")
        candidates.append(DATA_DIR / "accounts" / f"{safe_email}.txt")
    root = DATA_DIR / "accounts"
    for candidate in candidates:
        path = candidate if candidate.is_absolute() else APP_DIR / candidate
        if _path_within(path, [root]) and path.is_file():
            return path.resolve()
    raise FileNotFoundError("未找到该账号对应的 SSO 文件")


def _account_has_sso(record: Dict[str, Any]) -> bool:
    try:
        read_sso_token(_find_account_sso_file(record))
        return True
    except (FileNotFoundError, OSError, TypeError, ValueError, UnicodeError):
        return False


def _load_account_sso(record: Dict[str, Any]) -> Dict[str, Any]:
    path = _find_account_sso_file(record)
    return {"kind": "sso", "path": str(path), "content": read_sso_token(path)}


def _stream_file(path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
    """按固定块读取文件，让响应在首块就绪后立即进入浏览器下载队列。"""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def _failure_screenshot_file(record: Dict[str, Any]) -> tuple[Path, str]:
    raw_path = str(record.get("screenshot_path") or "").strip()
    if not raw_path:
        raise FileNotFoundError("该记录没有失败截图")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    screenshot_roots = [
        DATA_DIR / "screenshots" / "registration-failures",
        DATA_DIR / "screenshots" / "relogin-failures",
    ]
    if not _path_within(path, screenshot_roots) or not path.is_file():
        raise FileNotFoundError("失败截图文件不存在")
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    media_type = media_types.get(path.suffix.lower())
    if not media_type:
        raise ValueError("失败截图格式不受支持")
    return path.resolve(), media_type


def _relogin_screenshot_file(account_id: int, filename: str) -> tuple[Path, str]:
    """读取带时间戳的重登截图，URL 始终指向该次失败现场。"""
    name = str(filename or "").strip()
    if not name or Path(name).name != name or not name.startswith(f"relogin-{int(account_id)}-"):
        raise FileNotFoundError("重登失败截图不存在")
    path = DATA_DIR / "screenshots" / "relogin-failures" / name
    if not _path_within(path, [DATA_DIR / "screenshots" / "relogin-failures"]) or not path.is_file():
        raise FileNotFoundError("重登失败截图不存在")
    media_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    media_type = media_types.get(path.suffix.lower())
    if not media_type:
        raise ValueError("失败截图格式不受支持")
    return path.resolve(), media_type


def create_app() -> FastAPI:
    app_version = current_version()
    update_service = ReleaseUpdateService(app_version)
    app = FastAPI(
        title="Grok Register Web",
        description="Lightweight console for register / list / manage accounts",
        version=app_version,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.state.update_service = update_service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_web_login(request: Request, call_next):
        if _auth_required_path(request.url.path):
            if not _web_auth_enabled():
                return JSONResponse(
                    status_code=401,
                    content={
                        "ok": False,
                        "error": "请先创建管理员账号",
                        "auth_required": True,
                        "setup_required": True,
                    },
                )
            if not _valid_session(request.cookies.get(WEB_SESSION_COOKIE, "")):
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "请先登录", "auth_required": True},
                )
        return await call_next(request)

    @app.on_event("startup")
    def _startup() -> None:
        gr = _gr()
        gr.load_config()
        gr._wire_runtime_modules()
        try:
            repository = gr.get_registration_repository()
            gr.backfill_access_token_bot_risk()
            gr.backfill_registration_risk_bot_risk()
            grokiq.grokiq_notifier.start(
                repository,
                lambda: dict(gr.config),
            )
        except Exception as exc:
            print(f"[web] 初始化 SQLite 失败: {exc}", flush=True)
        update_service.start()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        update_service.stop()
        grokiq.grokiq_notifier.stop()

    @app.get("/api/health")
    def api_health() -> Dict[str, Any]:
        return {"ok": True, "service": "grok-register-web", "version": app_version}

    @app.get("/api/system/version")
    def api_system_version() -> Dict[str, Any]:
        return {"ok": True, "version": update_service.snapshot()}

    @app.post("/api/system/update/check")
    def api_system_update_check() -> Dict[str, Any]:
        return {"ok": True, "version": update_service.check()}

    @app.get("/api/auth/me")
    def api_auth_me(request: Request) -> Dict[str, Any]:
        record = _auth_record() or {}
        username = str(record.get("username") or "")
        enabled = _web_auth_enabled()
        authenticated = bool(enabled and _valid_session(request.cookies.get(WEB_SESSION_COOKIE, "")))
        return {
            "ok": True,
            "enabled": enabled,
            "setup_required": not enabled,
            "authenticated": authenticated,
            "username": username if authenticated and enabled else "",
        }

    @app.post("/api/auth/setup")
    def api_auth_setup(body: LoginBody) -> JSONResponse:
        if _auth_record() is not None:
            raise HTTPException(status_code=409, detail="管理员账号已创建")
        username = str(body.username or "").strip()
        password = str(body.password or "")
        confirm = str(body.confirm_password or "")
        if len(username) < 3:
            raise HTTPException(status_code=400, detail="账号至少需要 3 个字符")
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="密码至少需要 8 个字符")
        if password != confirm:
            raise HTTPException(status_code=400, detail="两次输入的密码不一致")
        record = _create_auth_record(username, password)
        try:
            _save_auth_record(record)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"保存管理员账号失败: {exc}") from exc
        response = JSONResponse(
            {"ok": True, "enabled": True, "authenticated": True, "username": username}
        )
        expires_at = int(time.time()) + WEB_SESSION_TTL
        response.set_cookie(
            WEB_SESSION_COOKIE,
            _sign_session(username, expires_at, record["session_secret"]),
            max_age=WEB_SESSION_TTL,
            expires=WEB_SESSION_TTL,
            httponly=True,
            secure=str(os.environ.get("GROK_WEB_COOKIE_SECURE", "1")).strip().lower()
            not in {"0", "false", "no", "off"},
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/auth/login")
    def api_auth_login(body: LoginBody) -> JSONResponse:
        record = _auth_record()
        if record is None:
            raise HTTPException(status_code=409, detail="请先创建管理员账号")
        username = record["username"]
        supplied_password = str(body.password or "")
        supplied_user = str(body.username or "")
        try:
            salt = base64.urlsafe_b64decode(record["password_salt"])
        except (ValueError, base64.binascii.Error) as exc:
            raise HTTPException(status_code=500, detail="管理员账号数据损坏") from exc
        valid_password = hmac.compare_digest(
            _hash_password(supplied_password, salt), record["password_hash"]
        )
        if not (hmac.compare_digest(supplied_user, username) and valid_password):
            raise HTTPException(status_code=401, detail="账号或密码错误")
        expires_at = int(time.time()) + WEB_SESSION_TTL
        response = JSONResponse(
            {"ok": True, "enabled": True, "authenticated": True, "username": username}
        )
        response.set_cookie(
            WEB_SESSION_COOKIE,
            _sign_session(username, expires_at, record["session_secret"]),
            max_age=WEB_SESSION_TTL,
            expires=WEB_SESSION_TTL,
            httponly=True,
            secure=str(os.environ.get("GROK_WEB_COOKIE_SECURE", "1")).strip().lower()
            not in {"0", "false", "no", "off"},
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def api_auth_logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(WEB_SESSION_COOKIE, path="/")
        return response

    @app.get("/api/stats")
    def api_stats() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        store = gr.get_registration_repository()
        return {"ok": True, "stats": store.stats(), "job": job_coordinator.status()}

    @app.get("/api/accounts")
    def api_accounts(
        status: str = Query(""),
        email_disable_status: str = Query(""),
        cpa_remote_status: str = Query(""),
        grok2api_remote_status: str = Query(""),
        q: str = Query(""),
        keyword: str = Query(""),
        batch_id: str = Query(""),
        bot_risk: str = Query(""),
        limit: int = Query(20, ge=1, le=10000),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        status_norm = str(status or "").strip().lower()
        keyword_norm = str(q or keyword or "").strip()
        batch_norm = str(batch_id or "").strip()
        bot_risk_norm = str(bot_risk or "").strip().lower()
        cpa_remote_norm = str(cpa_remote_status or "").strip().lower()
        grok2api_remote_norm = str(grok2api_remote_status or "").strip().lower()
        rows = store.list_results(
            status=status_norm,
            email_disable_status=str(email_disable_status or "").strip().lower(),
            cpa_remote_status=cpa_remote_norm,
            grok2api_remote_status=grok2api_remote_norm,
            keyword=keyword_norm,
            batch_id=batch_norm,
            bot_risk=bot_risk_norm,
            limit=limit,
            offset=offset,
        )
        total = store.count_results(
            status=status_norm,
            email_disable_status=str(email_disable_status or "").strip().lower(),
            cpa_remote_status=cpa_remote_norm,
            grok2api_remote_status=grok2api_remote_norm,
            keyword=keyword_norm,
            batch_id=batch_norm,
            bot_risk=bot_risk_norm,
        )
        grokiq_deliveries = store.grokiq_deliveries(
            [row.get("id") for row in rows]
        )
        return {
            "ok": True,
            "total": total,
            "count": len(rows),
            "has_more": offset + len(rows) < total,
            "offset": offset,
            "limit": limit,
            "items": [
                _serialize_record(
                    row,
                    grokiq_deliveries.get(int(row.get("id") or 0)),
                )
                for row in rows
            ],
        }

    @app.get("/api/accounts/relogin/status")
    def api_account_relogin_status() -> Dict[str, Any]:
        return {"ok": True, "relogin": relogin_coordinator.status()}

    @app.get("/api/accounts/select-ids")
    def api_account_select_ids(
        status: str = Query(""),
        email_disable_status: str = Query(""),
        cpa_remote_status: str = Query(""),
        grok2api_remote_status: str = Query(""),
        q: str = Query(""),
        keyword: str = Query(""),
        batch_id: str = Query(""),
        bot_risk: str = Query(""),
    ) -> Dict[str, Any]:
        store = _gr().get_registration_repository()
        ids = store.list_result_ids(
            status=str(status or "").strip().lower(),
            email_disable_status=str(email_disable_status or "").strip().lower(),
            cpa_remote_status=str(cpa_remote_status or "").strip().lower(),
            grok2api_remote_status=str(grok2api_remote_status or "").strip().lower(),
            keyword=str(q or keyword or "").strip(),
            batch_id=str(batch_id or "").strip(),
            bot_risk=str(bot_risk or "").strip().lower(),
        )
        return {"ok": True, "ids": ids, "total": len(ids)}

    @app.get("/api/accounts/actionable-ids")
    def api_account_actionable_ids(
        action: str = Query(...),
        q: str = Query(""),
        bot_risk: str = Query(""),
        kind: str = Query(""),
    ) -> Dict[str, Any]:
        normalized_action = str(action or "").strip().lower()
        store = _gr().get_registration_repository()
        if normalized_action == "auth_export":
            normalized_kind = str(kind or "").strip().lower()
            if normalized_kind not in {"cpa", "grok2api", "sso"}:
                raise HTTPException(status_code=400, detail="kind 必须是 cpa、grok2api 或 sso")
            ids = store.list_result_ids(keyword=str(q or "").strip())
            gr = _gr()
            gr.load_config()
            records_by_id = {
                int(record.get("id") or 0): record
                for record in store.get_results_by_ids(ids)
            }
            available: List[int] = []
            for account_id in ids:
                record = records_by_id.get(account_id)
                if record is None:
                    continue
                try:
                    if normalized_kind == "sso":
                        if not _account_has_sso(record):
                            continue
                    else:
                        _find_account_auth_file(record, gr.config, normalized_kind)
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    continue
                available.append(account_id)
            return {"ok": True, "ids": available, "total": len(available)}
        try:
            ids = store.list_actionable_result_ids(
                normalized_action,
                keyword=str(q or "").strip(),
                bot_risk=str(bot_risk or "").strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if normalized_action == "sso_check":
            records_by_id = {
                int(record.get("id") or 0): record
                for record in store.get_results_by_ids(ids)
            }
            ids = [
                account_id
                for account_id in ids
                if account_id in records_by_id and _account_has_sso(records_by_id[account_id])
            ]
        return {"ok": True, "ids": ids, "total": len(ids)}

    @app.get("/api/accounts/sso-check/status")
    def api_account_sso_check_status() -> Dict[str, Any]:
        return {"ok": True, "sso_check": sso_check_coordinator.status()}

    @app.post("/api/accounts/sso-check/prepare")
    def api_accounts_sso_check_prepare(body: AccountIdsBody) -> Dict[str, Any]:
        ids = _batch_account_ids(body.ids)
        gr = _gr()
        records = gr.get_registration_repository().get_results_by_ids(ids)
        records_by_id = {int(record.get("id") or 0): record for record in records}
        prepared = [
            account_id
            for account_id in ids
            if account_id in records_by_id and _account_has_sso(records_by_id[account_id])
        ]
        return {
            "ok": True,
            "ids": prepared,
            "missing": [account_id for account_id in ids if account_id not in records_by_id],
            "unavailable": [
                account_id
                for account_id in ids
                if account_id in records_by_id and account_id not in prepared
            ],
        }

    @app.post("/api/accounts/sso-check")
    def api_accounts_sso_check(body: AccountIdsBody) -> Dict[str, Any]:
        if job_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="注册任务运行中，请等待任务结束后检查")
        if relogin_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="账号重新登录中，请等待完成后检查")
        try:
            status = sso_check_coordinator.start_many(_batch_account_ids(body.ids))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "sso_check": status}

    @app.post("/api/accounts/relogin")
    def api_accounts_relogin(body: AccountIdsBody) -> Dict[str, Any]:
        if job_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="注册任务运行中，请等待任务结束后重新登录")
        if sso_check_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="SSO 详细检查中，请等待完成后重新登录")
        try:
            status = relogin_coordinator.start_many(_batch_account_ids(body.ids))
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "relogin": status}

    @app.post("/api/accounts/auth-json/{kind}/download")
    def api_accounts_auth_json_download(kind: str, body: AccountIdsBody) -> StreamingResponse:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"cpa", "grok2api", "sso"}:
            raise HTTPException(status_code=400, detail="kind 必须是 cpa、grok2api 或 sso")
        ids = _batch_account_ids(body.ids)
        gr = _gr()
        gr.load_config()
        records = gr.get_registration_repository().get_results_by_ids(ids)
        if not records:
            raise HTTPException(status_code=404, detail="没有匹配的记录")
        if normalized_kind == "sso":
            archive, exported, skipped = build_sso_archive(records, _find_account_sso_file)
        else:
            archive, exported, skipped = build_account_auth_archive(
                records, gr.config, normalized_kind, _find_account_auth_file
            )
        if not exported:
            label = {"cpa": "CPA JSON", "grok2api": "Grok2API JSON", "sso": "SSO"}[normalized_kind]
            raise HTTPException(status_code=404, detail=f"所选账号均没有可导出的 {label}")
        filename = f"{normalized_kind}-auth-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            iter([archive]),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(archive)),
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Exported-Count": str(exported),
                "X-Skipped-Count": str(skipped),
            },
        )

    @app.get("/api/accounts/{account_id}")
    def api_account_detail(account_id: int) -> Dict[str, Any]:
        gr = _gr()
        store = gr.get_registration_repository()
        rows = store.get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        delivery = store.grokiq_deliveries([account_id]).get(account_id)
        return {"ok": True, "item": _serialize_record(rows[0], delivery)}

    @app.post("/api/accounts/{account_id}/relogin")
    def api_account_relogin(account_id: int) -> Dict[str, Any]:
        if job_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="注册任务运行中，请等待任务结束后重新登录")
        if sso_check_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="SSO 详细检查中，请等待完成后重新登录")
        try:
            status = relogin_coordinator.start(account_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "relogin": status}

    @app.post("/api/accounts/grok2api/import")
    def api_accounts_grok2api_import(body: AccountIdsBody) -> Dict[str, Any]:
        """把选中账号已生成的 Grok2API JSON 导入配置的远程服务。"""
        from backend.integrations.grok2api_client import (
            Grok2APIClient,
            Grok2APIImportError,
        )

        ids = _batch_account_ids(body.ids)
        gr = _gr()
        gr.load_config()
        if not Grok2APIClient.is_configured(gr.config):
            raise HTTPException(
                status_code=400,
                detail="请先在系统设置完整配置 Grok2API API 地址、管理员账号和密码",
            )
        store = gr.get_registration_repository()
        records = store.get_results_by_ids(ids)
        records_by_id = {int(record.get("id") or 0): record for record in records}
        imported = 0
        skipped = 0
        failed = 0
        missing = 0
        created = 0
        updated = 0
        synced = 0
        sync_failed = 0
        errors: List[Dict[str, Any]] = []
        try:
            with Grok2APIClient.from_config(gr.config) as client:
                client.login()
                for account_id in ids:
                    record = records_by_id.get(account_id)
                    if record is None:
                        missing += 1
                        continue
                    try:
                        result = _import_account_grok2api(
                            store, gr.config, record, client
                        )
                    except FileNotFoundError:
                        skipped += 1
                        continue
                    except (OSError, ValueError, Grok2APIImportError) as exc:
                        failed += 1
                        errors.append(
                            {
                                "id": account_id,
                                "email": str(record.get("email") or ""),
                                "error": str(exc),
                            }
                        )
                        continue
                    aggregate = result["result"]
                    imported += 1
                    created += int(aggregate.get("created", 0) or 0)
                    updated += int(aggregate.get("updated", 0) or 0)
                    synced += int(aggregate.get("synced", 0) or 0)
                    sync_failed += int(aggregate.get("syncFailed", 0) or 0)
        except Grok2APIImportError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if imported == 0 and failed == 0 and missing == len(ids):
            raise HTTPException(status_code=404, detail="没有匹配的记录")
        if imported == 0 and failed == 0:
            raise HTTPException(
                status_code=404,
                detail="所选账号均没有可导入的 Grok2API JSON",
            )
        return {
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "failed": failed,
            "missing": missing,
            "created": created,
            "updated": updated,
            "synced": synced,
            "syncFailed": sync_failed,
            "errors": errors,
        }

    @app.post("/api/accounts/{account_id}/grok2api/import")
    def api_account_grok2api_import(account_id: int) -> Dict[str, Any]:
        """把已生成的三种 Grok2API JSON 导入配置的远程服务。"""
        from backend.integrations.grok2api_client import (
            Grok2APIClient,
            Grok2APIImportError,
        )

        gr = _gr()
        gr.load_config()
        store = gr.get_registration_repository()
        rows = store.get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        if not Grok2APIClient.is_configured(gr.config):
            raise HTTPException(
                status_code=400,
                detail="请先在系统设置完整配置 Grok2API API 地址、管理员账号和密码",
            )
        try:
            with Grok2APIClient.from_config(gr.config) as client:
                imported = _import_account_grok2api(
                    store, gr.config, rows[0], client
                )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Grok2APIImportError as exc:
            store.update_remote_import_status(
                account_id,
                "grok2api",
                status="failed",
                error=str(exc),
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        delivery = store.grokiq_deliveries([account_id]).get(account_id)
        return {
            "ok": True,
            "result": imported["result"],
            "grokiqNotification": imported["grokiqNotification"],
            "item": _serialize_record(imported["record"], delivery),
        }

    @app.get("/api/accounts/{account_id}/failure-screenshot")
    def api_account_failure_screenshot(account_id: int) -> FileResponse:
        gr = _gr()
        rows = gr.get_registration_repository().get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            path, media_type = _failure_screenshot_file(rows[0])
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")

    @app.get("/api/accounts/{account_id}/relogin-screenshots/{filename}")
    def api_account_relogin_screenshot(account_id: int, filename: str) -> FileResponse:
        try:
            path, media_type = _relogin_screenshot_file(account_id, filename)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")

    @app.get("/api/accounts/{account_id}/auth-json/{kind}")
    def api_account_auth_json(account_id: int, kind: str) -> Dict[str, Any]:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"cpa", "grok2api", "sso"}:
            raise HTTPException(status_code=400, detail="kind 必须是 cpa、grok2api 或 sso")
        gr = _gr()
        gr.load_config()
        rows = gr.get_registration_repository().get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            payload = _load_account_sso(rows[0]) if normalized_kind == "sso" else _load_account_auth_json(rows[0], gr.config, normalized_kind)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **payload}

    @app.get("/api/accounts/{account_id}/auth-json/{kind}/download")
    def api_account_auth_json_download(account_id: int, kind: str) -> StreamingResponse:
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"cpa", "grok2api", "sso"}:
            raise HTTPException(status_code=400, detail="kind 必须是 cpa、grok2api 或 sso")
        gr = _gr()
        gr.load_config()
        rows = gr.get_registration_repository().get_results_by_ids([account_id])
        if not rows:
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            if normalized_kind == "sso":
                payload = _load_account_sso(rows[0])
                token_bytes = f"{payload['content']}\n".encode("utf-8")
                email = str(rows[0].get("email") or account_id)
                safe_email = "".join(char if char.isalnum() or char in ".@_-" else "_" for char in email)
                return StreamingResponse(
                    iter([token_bytes]),
                    media_type="text/plain; charset=utf-8",
                    headers={
                        "Content-Disposition": f'attachment; filename="{safe_email}.sso.txt"',
                        "Content-Length": str(len(token_bytes)),
                        "Cache-Control": "no-cache",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            path = _find_account_auth_file(rows[0], gr.config, normalized_kind)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        file_size = path.stat().st_size
        return StreamingResponse(
            _stream_file(path),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "Content-Length": str(file_size),
                "Cache-Control": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/delete")
    def api_accounts_delete(body: DeleteAccountsBody) -> Dict[str, Any]:
        gr = _gr()
        ids = _batch_account_ids(body.ids)

        from backend.registration.artifacts import (
            cleanup_side_files_for_emails,
            collect_related_file_paths,
            delete_related_files,
        )

        store = gr.get_registration_repository()
        records = store.get_results_by_ids(ids)
        if not records:
            raise HTTPException(status_code=404, detail="没有匹配的记录")

        file_paths: List[str] = []
        seen = set()
        if body.delete_files:
            for record in records:
                for path in collect_related_file_paths(
                    record,
                    accounts_dir=gr.ACCOUNTS_DIR,
                    app_dir=gr.DATA_DIR,
                ):
                    if path in seen:
                        continue
                    seen.add(path)
                    file_paths.append(path)

        deleted_records = store.delete_results([row.get("id") for row in records])
        deleted_files: List[str] = []
        file_errors: List[str] = []
        side_lines = 0
        if body.delete_files:
            deleted_files, file_errors = delete_related_files(file_paths)
            side_cleanup = cleanup_side_files_for_emails(
                gr.ACCOUNTS_DIR,
                [str(item.get("email") or "") for item in deleted_records],
            )
            side_lines = sum(side_cleanup.values())

        return {
            "ok": True,
            "deleted": len(deleted_records),
            "deleted_files": len(deleted_files),
            "side_lines": side_lines,
            "file_errors": file_errors[:20],
        }

    @app.get("/api/config")
    def api_config_get() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        return {"ok": True, "config": _public_config(gr.config)}

    @app.get("/api/config/file")
    def api_config_file_get() -> Dict[str, Any]:
        return {"ok": True, "file": _config_file_snapshot()}

    @app.put("/api/config")
    @app.post("/api/config")
    async def api_config_put(request: Request) -> Dict[str, Any]:
        if job_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="注册任务运行中，暂不可修改配置")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="无效的配置 JSON")
        updates = payload.get("config") if isinstance(payload.get("config"), dict) else payload
        result = _apply_config_updates(updates)
        return {"ok": True, **result}

    @app.get("/api/job")
    def api_job_status() -> Dict[str, Any]:
        return {"ok": True, "job": job_coordinator.status()}

    @app.get("/api/job/logs")
    def api_job_logs(
        after_id: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=2000),
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "logs": job_coordinator.get_logs(after_id=after_id, limit=limit),
            "job": job_coordinator.status(),
        }

    @app.post("/api/job/start")
    def api_job_start(body: StartJobBody) -> Dict[str, Any]:
        if relogin_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="账号重新登录中，请等待完成后再启动注册")
        if sso_check_coordinator.status().get("running"):
            raise HTTPException(status_code=409, detail="SSO 详细检查中，请等待完成后再启动注册")
        gr = _gr()
        gr.load_config()
        if body.config:
            _apply_config_updates(body.config)
            gr.load_config()

        count = body.count if body.count is not None else gr.config.get("register_count", 1)
        workers = body.workers if body.workers is not None else gr.config.get("register_workers", 1)
        try:
            count_i = int(count)
            workers_i = int(workers)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="count / workers 必须是整数")

        try:
            status = job_coordinator.start(count=count_i, workers=workers_i)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"启动失败: {exc}",
            ) from exc
        return {"ok": True, "job": status}

    @app.post("/api/job/stop")
    def api_job_stop() -> Dict[str, Any]:
        try:
            status = job_coordinator.stop()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"停止失败: {exc}") from exc
        return {"ok": True, "job": status}

    @app.post("/api/browser/kill-all")
    def api_browser_kill_all() -> Dict[str, Any]:
        gr = _gr()
        gr._bs.block_browser_launches()
        if job_coordinator.status().get("running"):
            try:
                job_coordinator.request_stop()
            except Exception:
                pass
        try:
            result = gr._bs.kill_all_browser_processes(log_callback=job_coordinator._append_log)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"终止浏览器失败: {exc}") from exc
        return {"ok": True, **result, "job": job_coordinator.status()}

    @app.api_route("/api/connectivity", methods=["GET", "POST"])
    def api_connectivity() -> Dict[str, Any]:
        gr = _gr()
        gr.load_config()
        gr._wire_runtime_modules()
        try:
            checks = gr._conn.run_connectivity_checks(gr.config, gr.http_get, gr.http_post)
            items = [
                {"name": name, "ok": bool(ok), "detail": str(detail)}
                for name, ok, detail in checks
            ]
            blocked = bool(gr._conn.has_blocking_xai_failure(checks))
            return {"ok": True, "items": items, "blocked": blocked}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"连通性检查失败: {exc}") from exc

    # ---- static SPA ----
    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/")
    def spa_index() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503,
                detail="Web UI 未构建。请在 front/ 执行 npm install && npm run build。",
            )
        return FileResponse(index)

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        raise HTTPException(status_code=503, detail="Web UI 未构建")

    return app


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    import uvicorn

    print(f"[web] Grok Register Web UI -> http://{host}:{port}", flush=True)
    print("[web] 复用统一注册、OutlookEmail 与 SSO→auth 执行逻辑", flush=True)
    print(f"[web] API docs -> http://{host}:{port}/api/docs", flush=True)
    uvicorn.run(
        "backend.web.application:create_app",
        factory=True,
        host=host,
        port=int(port),
        log_level="warning",
        access_log=False,
        workers=1,
    )


def main(argv: Optional[List[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Grok Register Web Console (FastAPI)")
    parser.add_argument("--host", default=os.environ.get("GROK_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GROK_WEB_PORT", "8787")))
    args = parser.parse_args(argv)
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
