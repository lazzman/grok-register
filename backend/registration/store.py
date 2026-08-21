# -*- coding: utf-8 -*-
"""注册结果仓储。

使用 SQLite WAL 保存任务结果和邮箱停用状态；每次操作建立独立连接以适配后台
线程并发。
"""
from __future__ import annotations

import datetime as _datetime
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


RESULT_COLUMNS = (
    "source_key",
    "batch_id",
    "source",
    "started_at",
    "finished_at",
    "duration_seconds",
    "email",
    "password",
    "status",
    "success",
    "provider",
    "worker_id",
    "cpa_enabled",
    "cpa_status",
    "auth_info",
    "auth_path",
    "cpa_auth_path",
    "grok2api_auth_path",
    "cpa_remote_status",
    "cpa_remote_imported_at",
    "cpa_remote_error",
    "grok2api_remote_status",
    "grok2api_remote_imported_at",
    "grok2api_remote_error",
    "sub2api_remote_status",
    "sub2api_remote_imported_at",
    "sub2api_remote_error",
    "email_account_id",
    "email_disable_status",
    "email_disabled_at",
    "email_disable_error",
    "failure_type",
    "failure_reason",
    "screenshot_path",
    "account_file",
    "sso_saved",
    "nsfw_status",
    "bot_risk",
    "bfs",
    "extra_json",
)

SQLITE_IN_BATCH_SIZE = 900


class RegistrationRepository:
    def __init__(self, database_path: os.PathLike[str] | str):
        self.database_path = os.path.abspath(os.fspath(database_path))
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        self._initialize()

    @staticmethod
    def now_text() -> str:
        return _datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=15.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=15000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS registration_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT UNIQUE,
                    batch_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'web',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    email TEXT NOT NULL DEFAULT '',
                    password TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'failure',
                    success INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL DEFAULT '',
                    worker_id INTEGER NOT NULL DEFAULT 0,
                    cpa_enabled INTEGER NOT NULL DEFAULT 0,
                    cpa_status TEXT NOT NULL DEFAULT 'disabled',
                    auth_info TEXT NOT NULL DEFAULT '',
                    auth_path TEXT NOT NULL DEFAULT '',
                    cpa_auth_path TEXT NOT NULL DEFAULT '',
                    grok2api_auth_path TEXT NOT NULL DEFAULT '',
                    cpa_remote_status TEXT NOT NULL DEFAULT 'not_configured',
                    cpa_remote_imported_at TEXT NOT NULL DEFAULT '',
                    cpa_remote_error TEXT NOT NULL DEFAULT '',
                    grok2api_remote_status TEXT NOT NULL DEFAULT 'not_configured',
                    grok2api_remote_imported_at TEXT NOT NULL DEFAULT '',
                    grok2api_remote_error TEXT NOT NULL DEFAULT '',
                    sub2api_remote_status TEXT NOT NULL DEFAULT 'disabled',
                    sub2api_remote_imported_at TEXT NOT NULL DEFAULT '',
                    sub2api_remote_error TEXT NOT NULL DEFAULT '',
                    email_account_id TEXT NOT NULL DEFAULT '',
                    email_disable_status TEXT NOT NULL DEFAULT 'not_attempted',
                    email_disabled_at TEXT NOT NULL DEFAULT '',
                    email_disable_error TEXT NOT NULL DEFAULT '',
                    failure_type TEXT NOT NULL DEFAULT '',
                    failure_reason TEXT NOT NULL DEFAULT '',
                    screenshot_path TEXT NOT NULL DEFAULT '',
                    account_file TEXT NOT NULL DEFAULT '',
                    sso_saved INTEGER NOT NULL DEFAULT 0,
                    nsfw_status TEXT NOT NULL DEFAULT '',
                    bot_risk INTEGER NOT NULL DEFAULT 0,
                    bfs TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_registration_results_finished
                    ON registration_results(finished_at DESC);
                CREATE INDEX IF NOT EXISTS idx_registration_results_email_success
                    ON registration_results(email COLLATE NOCASE, success);
                CREATE INDEX IF NOT EXISTS idx_registration_results_status
                    ON registration_results(status);
                CREATE INDEX IF NOT EXISTS idx_registration_results_batch
                    ON registration_results(batch_id);

                CREATE TABLE IF NOT EXISTS grokiq_outbox (
                    event_id TEXT PRIMARY KEY,
                    registration_id INTEGER NOT NULL UNIQUE,
                    event_type TEXT NOT NULL DEFAULT 'grok2api.account_imported',
                    email TEXT NOT NULL,
                    sso TEXT NOT NULL DEFAULT '',
                    bot_risk INTEGER NOT NULL DEFAULT 0,
                    bfs TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    delivered_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_grokiq_outbox_due
                    ON grokiq_outbox(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_grokiq_outbox_email
                    ON grokiq_outbox(email COLLATE NOCASE);
                """
            )
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(registration_results)").fetchall()
            }
            outbox_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(grokiq_outbox)").fetchall()
            }
            if "sso" not in outbox_columns:
                conn.execute(
                    "ALTER TABLE grokiq_outbox ADD COLUMN sso TEXT NOT NULL DEFAULT ''"
                )
            migrations = {
                "cpa_auth_path": "TEXT NOT NULL DEFAULT ''",
                "grok2api_auth_path": "TEXT NOT NULL DEFAULT ''",
                "email_account_id": "TEXT NOT NULL DEFAULT ''",
                "email_disable_status": "TEXT NOT NULL DEFAULT 'not_attempted'",
                "email_disabled_at": "TEXT NOT NULL DEFAULT ''",
                "email_disable_error": "TEXT NOT NULL DEFAULT ''",
                "screenshot_path": "TEXT NOT NULL DEFAULT ''",
                "cpa_remote_status": "TEXT NOT NULL DEFAULT 'not_configured'",
                "cpa_remote_imported_at": "TEXT NOT NULL DEFAULT ''",
                "cpa_remote_error": "TEXT NOT NULL DEFAULT ''",
                "grok2api_remote_status": "TEXT NOT NULL DEFAULT 'not_configured'",
                "grok2api_remote_imported_at": "TEXT NOT NULL DEFAULT ''",
                "grok2api_remote_error": "TEXT NOT NULL DEFAULT ''",
                "sub2api_remote_status": "TEXT NOT NULL DEFAULT 'disabled'",
                "sub2api_remote_imported_at": "TEXT NOT NULL DEFAULT ''",
                "sub2api_remote_error": "TEXT NOT NULL DEFAULT ''",
                "bot_risk": "INTEGER NOT NULL DEFAULT 0",
                "bfs": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE registration_results ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                """
                UPDATE registration_results
                SET email_disable_status = 'not_applicable'
                WHERE lower(provider) != 'outlookemail'
                  AND email_disable_status = 'not_attempted'
                """
            )
            conn.execute(
                """
                UPDATE registration_results
                SET cpa_remote_status = 'success'
                WHERE cpa_remote_status = 'not_configured'
                  AND auth_info LIKE '%CPA 远程:%'
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_registration_results_email_disable_status
                ON registration_results(email_disable_status)
                """
            )
            conn.execute("PRAGMA user_version = 4")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registration_job_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    batch_id TEXT NOT NULL DEFAULT '',
                    running INTEGER NOT NULL DEFAULT 0,
                    started_at REAL,
                    finished_at REAL,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    workers INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'web',
                    last_error TEXT NOT NULL DEFAULT '',
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    current_stage TEXT NOT NULL DEFAULT '',
                    current_email TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # 单行快照：没有则插入空行
            conn.execute(
                """
                INSERT OR IGNORE INTO registration_job_snapshot (id, updated_at)
                VALUES (1, ?)
                """,
                (self.now_text(),),
            )
            conn.execute("PRAGMA user_version = 7")

    def add_result(self, record: Dict[str, Any]) -> int:
        now = self.now_text()
        status = str(record.get("status") or "failure").strip().lower()
        success = 1 if status == "success" or bool(record.get("success")) else 0
        extra = record.get("extra_json", record.get("extra", {}))
        if isinstance(extra, str):
            extra_json = extra
        else:
            extra_json = json.dumps(extra or {}, ensure_ascii=False, sort_keys=True)

        normalized = {
            "source_key": record.get("source_key") or None,
            "batch_id": str(record.get("batch_id") or ""),
            "source": str(record.get("source") or "web"),
            "started_at": str(record.get("started_at") or now),
            "finished_at": str(record.get("finished_at") or now),
            "duration_seconds": max(float(record.get("duration_seconds") or 0), 0.0),
            "email": str(record.get("email") or "").strip(),
            "password": str(record.get("password") or ""),
            "status": status,
            "success": success,
            "provider": str(record.get("provider") or ""),
            "worker_id": int(record.get("worker_id") or 0),
            "cpa_enabled": 1 if bool(record.get("cpa_enabled")) else 0,
            "cpa_status": str(record.get("cpa_status") or "disabled"),
            "auth_info": str(record.get("auth_info") or ""),
            "auth_path": str(record.get("auth_path") or ""),
            "cpa_auth_path": str(record.get("cpa_auth_path") or ""),
            "grok2api_auth_path": str(record.get("grok2api_auth_path") or ""),
            "cpa_remote_status": str(record.get("cpa_remote_status") or "not_configured"),
            "cpa_remote_imported_at": str(record.get("cpa_remote_imported_at") or ""),
            "cpa_remote_error": str(record.get("cpa_remote_error") or ""),
            "grok2api_remote_status": str(
                record.get("grok2api_remote_status") or "not_configured"
            ),
            "grok2api_remote_imported_at": str(
                record.get("grok2api_remote_imported_at") or ""
            ),
            "grok2api_remote_error": str(record.get("grok2api_remote_error") or ""),
            "sub2api_remote_status": str(
                record.get("sub2api_remote_status") or "disabled"
            ),
            "sub2api_remote_imported_at": str(
                record.get("sub2api_remote_imported_at") or ""
            ),
            "sub2api_remote_error": str(record.get("sub2api_remote_error") or ""),
            "email_account_id": str(record.get("email_account_id") or ""),
            "email_disable_status": str(
                record.get("email_disable_status") or "not_attempted"
            ).strip().lower(),
            "email_disabled_at": str(record.get("email_disabled_at") or ""),
            "email_disable_error": str(record.get("email_disable_error") or ""),
            "failure_type": str(record.get("failure_type") or ""),
            "failure_reason": str(record.get("failure_reason") or ""),
            "screenshot_path": str(record.get("screenshot_path") or ""),
            "account_file": str(record.get("account_file") or ""),
            "sso_saved": 1 if bool(record.get("sso_saved")) else 0,
            "nsfw_status": str(record.get("nsfw_status") or ""),
            "bot_risk": 1 if bool(record.get("bot_risk")) else 0,
            "bfs": "" if record.get("bfs") is None else str(record.get("bfs")),
            "extra_json": extra_json,
        }
        columns = ", ".join(RESULT_COLUMNS)
        placeholders = ", ".join(f":{name}" for name in RESULT_COLUMNS)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO registration_results ({columns}) VALUES ({placeholders})",
                normalized,
            )
            return int(cursor.lastrowid)

    def enqueue_grokiq_event(
        self,
        *,
        registration_id: int,
        email: str,
        bot_risk: bool,
        bfs: Any,
        occurred_at: str,
        sso: str = "",
    ) -> Dict[str, Any]:
        """Create one durable, idempotent account-imported notification."""

        normalized_id = int(registration_id)
        if normalized_id <= 0:
            raise ValueError("registration_id 必须是正整数")
        normalized_email = str(email or "").strip().lower()
        if "@" not in normalized_email:
            raise ValueError("GrokIQ 通知缺少有效邮箱")
        event_id = f"registration:{normalized_id}:grok2api-imported"
        now_text = self.now_text()
        now_epoch = _datetime.datetime.now().timestamp()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO grokiq_outbox (
                    event_id, registration_id, event_type, email, sso, bot_risk, bfs,
                    occurred_at, status, attempts,
                    next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, 'grok2api.account_imported', ?, ?, ?, ?, ?,
                          'pending', 0, ?, ?, ?)
                """,
                (
                    event_id,
                    normalized_id,
                    normalized_email,
                    str(sso or "").strip(),
                    1 if bot_risk else 0,
                    "" if bfs is None else str(bfs),
                    str(occurred_at or ""),
                    now_epoch,
                    now_text,
                    now_text,
                ),
            )
            normalized_sso = str(sso or "").strip()
            if normalized_sso:
                conn.execute(
                    """
                    UPDATE grokiq_outbox
                    SET sso = ?, status = 'pending', next_attempt_at = ?, updated_at = ?
                    WHERE event_id = ? AND sso != ?
                    """,
                    (normalized_sso, now_epoch, now_text, event_id, normalized_sso),
                )
            row = conn.execute(
                "SELECT * FROM grokiq_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def recover_grokiq_deliveries(self) -> int:
        now_text = self.now_text()
        now_epoch = _datetime.datetime.now().timestamp()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE grokiq_outbox
                SET status = 'pending', next_attempt_at = ?, updated_at = ?
                WHERE status = 'delivering'
                """,
                (now_epoch, now_text),
            )
            return int(cursor.rowcount or 0)

    def claim_grokiq_delivery(self) -> Dict[str, Any] | None:
        now_epoch = _datetime.datetime.now().timestamp()
        now_text = self.now_text()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM grokiq_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, created_at ASC
                LIMIT 1
                """,
                (now_epoch,),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE grokiq_outbox
                SET status = 'delivering', attempts = attempts + 1,
                    last_attempt_at = ?, updated_at = ?
                WHERE event_id = ? AND status = 'pending'
                """,
                (now_text, now_text, row["event_id"]),
            )
            if not cursor.rowcount:
                return None
            claimed = conn.execute(
                "SELECT * FROM grokiq_outbox WHERE event_id = ?",
                (row["event_id"],),
            ).fetchone()
        return dict(claimed) if claimed is not None else None

    def retry_grokiq_delivery(
        self,
        event_id: str,
        *,
        error: str,
        delay_seconds: float,
        response_json: str = "",
    ) -> None:
        now_text = self.now_text()
        next_attempt = _datetime.datetime.now().timestamp() + max(
            float(delay_seconds), 1.0
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE grokiq_outbox
                SET status = 'pending', next_attempt_at = ?, last_error = ?,
                    response_json = ?, updated_at = ?
                WHERE event_id = ? AND status != 'delivered'
                """,
                (
                    next_attempt,
                    str(error or "")[:4000],
                    str(response_json or "")[:16000],
                    now_text,
                    str(event_id),
                ),
            )

    def complete_grokiq_delivery(
        self,
        event_id: str,
    ) -> None:
        now_text = self.now_text()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE grokiq_outbox
                SET status = 'delivered', delivered_at = ?, last_error = '',
                    response_json = '', updated_at = ?
                WHERE event_id = ?
                """,
                (
                    now_text,
                    now_text,
                    str(event_id),
                ),
            )

    def grokiq_deliveries(
        self, registration_ids: Iterable[int | str]
    ) -> Dict[int, Dict[str, Any]]:
        ids: List[int] = []
        seen = set()
        for raw in registration_ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            ids.append(value)
        if not ids:
            return {}
        result: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            for start in range(0, len(ids), SQLITE_IN_BATCH_SIZE):
                batch = ids[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT * FROM grokiq_outbox
                    WHERE registration_id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item.pop("sso", None)
                    result[int(row["registration_id"])] = item
        return result

    def has_success(self, email: str) -> bool:
        normalized = str(email or "").strip()
        if not normalized:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM registration_results
                WHERE success = 1 AND email = ? COLLATE NOCASE
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return row is not None

    def has_registered_or_consumed(self, email: str) -> bool:
        """成功、已保存 SSO，或已判定账号已注册 / 注册风控 / SSO 超时的邮箱，都应避免再次取用。"""
        normalized = str(email or "").strip()
        if not normalized:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM registration_results
                WHERE email = ? COLLATE NOCASE
                  AND (
                    success = 1
                    OR sso_saved = 1
                    OR lower(coalesce(failure_type, '')) IN (
                        'already_registered', 'registration_risk', 'sso_timeout'
                    )
                    OR lower(coalesce(email_disable_status, '')) IN ('success', 'failed')
                  )
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _result_filters(
        *,
        status: str = "",
        email_disable_status: str = "",
        cpa_remote_status: str = "",
        grok2api_remote_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        bot_risk: str = "",
    ) -> Tuple[str, List[Any]]:
        clauses = []
        params: List[Any] = []
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            clauses.append("status = ?")
            params.append(normalized_status)
        normalized_disable_status = str(email_disable_status or "").strip().lower()
        if normalized_disable_status:
            clauses.append("email_disable_status = ?")
            params.append(normalized_disable_status)
        normalized_cpa_remote_status = str(cpa_remote_status or "").strip().lower()
        if normalized_cpa_remote_status:
            clauses.append("cpa_remote_status = ?")
            params.append(normalized_cpa_remote_status)
        normalized_grok2api_remote_status = str(grok2api_remote_status or "").strip().lower()
        if normalized_grok2api_remote_status:
            clauses.append("grok2api_remote_status = ?")
            params.append(normalized_grok2api_remote_status)
        normalized_batch_id = str(batch_id or "").strip()
        if normalized_batch_id:
            clauses.append("batch_id = ?")
            params.append(normalized_batch_id)
        normalized_bot_risk = str(bot_risk or "").strip().lower()
        if normalized_bot_risk in {"1", "true", "yes", "risk", "bot", "bot_risk"}:
            clauses.append(
                "(COALESCE(bot_risk, 0) = 1 OR "
                "(trim(COALESCE(bfs, '')) <> '' AND trim(COALESCE(bfs, '')) <> '0'))"
            )
        elif normalized_bot_risk in {"0", "false", "no", "normal", "safe"}:
            clauses.append(
                "COALESCE(bot_risk, 0) = 0 AND ("
                "trim(COALESCE(bfs, '')) = '0' OR "
                "(trim(COALESCE(bfs, '')) = '' AND "
                "json_extract(CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END, "
                "'$.sso_check_status') = 'clean'))"
            )
        elif normalized_bot_risk in {"unknown", "unchecked", "pending"}:
            clauses.append(
                "COALESCE(bot_risk, 0) = 0 AND trim(COALESCE(bfs, '')) = '' AND "
                "COALESCE(json_extract(CASE WHEN json_valid(extra_json) THEN extra_json ELSE '{}' END, "
                "'$.sso_check_status'), '') <> 'clean'"
            )
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            like = f"%{normalized_keyword}%"
            clauses.append(
                "(email LIKE ? OR provider LIKE ? OR failure_reason LIKE ? OR auth_info LIKE ? "
                "OR batch_id LIKE ? OR email_account_id LIKE ? OR email_disable_error LIKE ?)"
            )
            params.extend([like, like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def list_results(
        self,
        *,
        status: str = "",
        email_disable_status: str = "",
        cpa_remote_status: str = "",
        grok2api_remote_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        bot_risk: str = "",
        limit: int = 2000,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where, params = self._result_filters(
            status=status,
            email_disable_status=email_disable_status,
            cpa_remote_status=cpa_remote_status,
            grok2api_remote_status=grok2api_remote_status,
            keyword=keyword,
            batch_id=batch_id,
            bot_risk=bot_risk,
        )
        safe_limit = max(1, min(int(limit or 2000), 10000))
        safe_offset = max(0, int(offset or 0))
        params.extend([safe_limit, safe_offset])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM registration_results
                {where}
                ORDER BY finished_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_results(
        self,
        *,
        status: str = "",
        email_disable_status: str = "",
        cpa_remote_status: str = "",
        grok2api_remote_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        bot_risk: str = "",
    ) -> int:
        """返回与账号列表相同筛选条件下的记录总数。"""
        where, params = self._result_filters(
            status=status,
            email_disable_status=email_disable_status,
            cpa_remote_status=cpa_remote_status,
            grok2api_remote_status=grok2api_remote_status,
            keyword=keyword,
            batch_id=batch_id,
            bot_risk=bot_risk,
        )
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS total FROM registration_results {where}", params
            ).fetchone()
        return int(row["total"] or 0)

    def list_result_ids(
        self,
        *,
        status: str = "",
        email_disable_status: str = "",
        cpa_remote_status: str = "",
        grok2api_remote_status: str = "",
        keyword: str = "",
        batch_id: str = "",
        bot_risk: str = "",
    ) -> List[int]:
        """返回与账号列表相同筛选条件下的全部主键，顺序与列表一致。"""
        where, params = self._result_filters(
            status=status,
            email_disable_status=email_disable_status,
            cpa_remote_status=cpa_remote_status,
            grok2api_remote_status=grok2api_remote_status,
            keyword=keyword,
            batch_id=batch_id,
            bot_risk=bot_risk,
        )
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id
                FROM registration_results
                {where}
                ORDER BY finished_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_actionable_result_ids(
        self,
        action: str,
        *,
        keyword: str = "",
        bot_risk: str = "",
    ) -> List[int]:
        """返回任务页面中符合搜索条件且可执行指定操作的全部账号主键。"""
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"relogin", "sso_check"}:
            raise ValueError("action 必须是 relogin 或 sso_check")
        where, params = self._result_filters(keyword=keyword, bot_risk=bot_risk)
        clauses = []
        if normalized_action == "relogin":
            clauses.extend(["trim(COALESCE(email, '')) <> ''", "trim(COALESCE(password, '')) <> ''"])
        else:
            clauses.extend(["trim(COALESCE(email, '')) <> ''", "COALESCE(sso_saved, 0) = 1"])
        actionable = " AND ".join(clauses)
        where = f"{where} AND {actionable}" if where else f"WHERE {actionable}"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM registration_results {where} ORDER BY finished_at DESC, id DESC",
                params,
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def get_results_by_ids(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        """按主键批量读取记录，保持传入顺序。"""
        normalized: List[int] = []
        seen = set()
        for raw in ids or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        if not normalized:
            return []
        by_id: Dict[int, Dict[str, Any]] = {}
        with self._connect() as conn:
            for start in range(0, len(normalized), SQLITE_IN_BATCH_SIZE):
                batch = normalized[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM registration_results
                    WHERE id IN ({placeholders})
                    """,
                    batch,
                ).fetchall()
                by_id.update({int(row["id"]): dict(row) for row in rows})
        return [by_id[item_id] for item_id in normalized if item_id in by_id]

    def update_relogin_result(
        self,
        account_id: int,
        *,
        account_file: str = "",
        cpa_detail: Optional[Dict[str, Any]] = None,
        status: str = "success",
        error: str = "",
        failure_type: str = "",
        failure_reason: str = "",
        screenshot_path: str = "",
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """记录重新登录结果，并在成功时刷新授权文件路径。"""
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            return False
        if normalized_id <= 0:
            return False
        detail = dict(cpa_detail or {})
        relogin_status = str(status or "failed").strip().lower()
        now = self.now_text()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM registration_results WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                extra = json.loads(str(row["extra_json"] or "{}"))
                if not isinstance(extra, dict):
                    extra = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                extra = {}
            extra.update(
                {
                    "relogin_status": relogin_status,
                    "relogin_at": now,
                    "relogin_error": str(error or ""),
                }
            )
            if diagnostics:
                extra["relogin_diagnostics"] = dict(diagnostics)
            elif relogin_status == "success":
                extra.pop("relogin_diagnostics", None)
            if relogin_status == "success":
                extra.pop("exception_traceback", None)
                extra.pop("exception_type", None)
            values: Dict[str, Any] = {
                "extra_json": json.dumps(extra, ensure_ascii=False, sort_keys=True),
                "id": normalized_id,
            }
            assignments = ["extra_json = :extra_json"]
            if screenshot_path:
                assignments.append("screenshot_path = :screenshot_path")
                values["screenshot_path"] = str(screenshot_path)
            if relogin_status == "success":
                values.update(
                    {
                        "status": "success",
                        "success": 1,
                        "failure_type": "",
                        "failure_reason": "",
                    }
                )
                assignments.extend(
                    [
                        "status = :status",
                        "success = :success",
                        "failure_type = :failure_type",
                        "failure_reason = :failure_reason",
                    ]
                )
            elif str(failure_type or "").strip():
                values.update(
                    {
                        "status": "failure",
                        "success": 0,
                        "failure_type": str(failure_type).strip(),
                        "failure_reason": str(failure_reason or error or "").strip(),
                    }
                )
                assignments.extend(
                    [
                        "status = :status",
                        "success = :success",
                        "failure_type = :failure_type",
                        "failure_reason = :failure_reason",
                    ]
                )
            if relogin_status in {"success", "partial"} and account_file:
                auth_info = detail.get("auth_info", "")
                if isinstance(auth_info, (list, tuple, set)):
                    auth_info = "\n".join(str(item) for item in auth_info if str(item).strip())
                values.update(
                    {
                        "account_file": str(account_file or ""),
                        "sso_saved": 1,
                        "cpa_enabled": 1 if bool(detail.get("enabled")) else 0,
                        "cpa_status": str(detail.get("status") or "not_attempted"),
                        "auth_info": str(auth_info or ""),
                        "auth_path": str(detail.get("auth_path") or ""),
                        "cpa_auth_path": str(detail.get("cpa_auth_path") or ""),
                        "grok2api_auth_path": str(detail.get("grok2api_auth_path") or ""),
                        "cpa_remote_status": str(
                            detail.get("cpa_remote_status") or "not_configured"
                        ),
                        "cpa_remote_imported_at": str(
                            detail.get("cpa_remote_imported_at") or ""
                        ),
                        "cpa_remote_error": str(detail.get("cpa_remote_error") or ""),
                        "grok2api_remote_status": str(
                            detail.get("grok2api_remote_status") or "not_configured"
                        ),
                        "grok2api_remote_imported_at": str(
                            detail.get("grok2api_remote_imported_at") or ""
                        ),
                        "grok2api_remote_error": str(
                            detail.get("grok2api_remote_error") or ""
                        ),
                        "sub2api_remote_status": str(
                            detail.get("sub2api_remote_status") or "disabled"
                        ),
                        "sub2api_remote_imported_at": str(
                            detail.get("sub2api_remote_imported_at") or ""
                        ),
                        "sub2api_remote_error": str(
                            detail.get("sub2api_remote_error") or ""
                        ),
                        "bot_risk": 1 if bool(detail.get("bot_risk")) else 0,
                        "bfs": (
                            ""
                            if detail.get("bfs") is None
                            else str(detail.get("bfs"))
                        ),
                    }
                )
                assignments.extend(
                    [
                        "account_file = :account_file",
                        "sso_saved = :sso_saved",
                        "cpa_enabled = :cpa_enabled",
                        "cpa_status = :cpa_status",
                        "auth_info = :auth_info",
                        "auth_path = :auth_path",
                        "cpa_auth_path = :cpa_auth_path",
                        "grok2api_auth_path = :grok2api_auth_path",
                        "cpa_remote_status = :cpa_remote_status",
                        "cpa_remote_imported_at = :cpa_remote_imported_at",
                        "cpa_remote_error = :cpa_remote_error",
                        "grok2api_remote_status = :grok2api_remote_status",
                        "grok2api_remote_imported_at = :grok2api_remote_imported_at",
                        "grok2api_remote_error = :grok2api_remote_error",
                        "sub2api_remote_status = :sub2api_remote_status",
                        "sub2api_remote_imported_at = :sub2api_remote_imported_at",
                        "sub2api_remote_error = :sub2api_remote_error",
                        "bot_risk = :bot_risk",
                        "bfs = :bfs",
                    ]
                )
                if relogin_status == "success" and not screenshot_path:
                    assignments.append("screenshot_path = ''")
            cursor = conn.execute(
                f"UPDATE registration_results SET {', '.join(assignments)} WHERE id = :id",
                values,
            )
            return bool(cursor.rowcount)

    def update_sso_check_result(
        self,
        account_id: int,
        *,
        risk_state: Dict[str, Any],
        status: str,
    ) -> bool:
        """保存一次 SSO 详细检查结果；仅明确结论同步 bot_risk / bfs。"""
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            return False
        if normalized_id <= 0:
            return False
        state = dict(risk_state or {})
        bot_flag = state.get("bot_flag") if isinstance(state.get("bot_flag"), dict) else {}
        source = state.get("bot_flag_source", bot_flag.get("source"))
        normalized_status = str(status or "unknown").strip().lower() or "unknown"
        now = self.now_text()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT extra_json FROM registration_results WHERE id = ?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                extra = json.loads(str(row["extra_json"] or "{}"))
                if not isinstance(extra, dict):
                    extra = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                extra = {}
            extra.update(
                {
                    "sso_risk_check": state,
                    "sso_check_status": normalized_status,
                    "sso_check_at": now,
                }
            )
            assignments = ["extra_json = :extra_json"]
            values: Dict[str, Any] = {
                "extra_json": json.dumps(extra, ensure_ascii=False, sort_keys=True),
                "id": normalized_id,
            }
            # 未知或请求失败不能证明账号已经恢复正常，保留此前明确的风险结论。
            if normalized_status in {"clean", "flagged"}:
                assignments.append("bot_risk = :bot_risk")
                values.update(
                    {
                        "bot_risk": 1 if normalized_status == "flagged" else 0,
                    }
                )
                if source is not None and str(source).strip() != "":
                    assignments.append("bfs = :bfs")
                    values["bfs"] = str(source)
            cursor = conn.execute(
                f"UPDATE registration_results SET {', '.join(assignments)} WHERE id = :id",
                values,
            )
            return bool(cursor.rowcount)

    def update_bot_risk_by_email(
        self,
        email: str,
        *,
        bot_risk: bool,
        bfs: Any = None,
    ) -> int:
        """按邮箱回填 access_token 上的 bfs / bot_risk 标记。"""
        normalized = str(email or "").strip()
        if not normalized:
            return 0
        bfs_text = "" if bfs is None else str(bfs)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE registration_results
                SET bot_risk = ?, bfs = ?
                WHERE email = ? COLLATE NOCASE
                """,
                (1 if bot_risk else 0, bfs_text, normalized),
            )
            return int(cursor.rowcount or 0)

    def backfill_registration_risk_bot_risk(self) -> int:
        """补齐历史风控标记，并修复重登风控后遗留的 sso_timeout。

        只认 failure_reason 里带 botFlagSource 的行——那是服务端真正下了风控裁决
        的记录。registration_risk 也覆盖"sso 为空"这类前置条件失败，那些不是
        机器人标记，不能一并标上。
        """
        with self._connect() as conn:
            bot_risk_cursor = conn.execute(
                """
                UPDATE registration_results
                SET bot_risk = 1
                WHERE failure_type = 'registration_risk'
                  AND COALESCE(bot_risk, 0) = 0
                  AND failure_reason LIKE '%botFlagSource%'
                """
            )
            relogin_cursor = conn.execute(
                """
                UPDATE registration_results
                SET failure_type = 'registration_risk',
                    failure_reason = CASE
                        WHEN trim(COALESCE(bfs, '')) <> ''
                            THEN '重新登录 SSO 风控异常: botFlagSource=' || trim(bfs)
                        ELSE '重新登录 SSO 风控异常'
                    END
                WHERE status = 'failure'
                  AND failure_type = 'sso_timeout'
                  AND COALESCE(bot_risk, 0) = 1
                  AND json_extract(extra_json, '$.relogin_status') = 'partial'
                  AND json_extract(extra_json, '$.sso_check_status') = 'flagged'
                """
            )
            return int(bot_risk_cursor.rowcount or 0) + int(relogin_cursor.rowcount or 0)

    def update_remote_import_status(
        self,
        account_id: int,
        kind: str,
        *,
        status: str,
        error: str = "",
        imported_at: str = "",
    ) -> bool:
        """更新 CPA 或 Grok2API 的远程入库状态。"""
        normalized_kind = str(kind or "").strip().lower()
        if normalized_kind not in {"cpa", "grok2api"}:
            raise ValueError("kind 必须是 cpa 或 grok2api")
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            return False
        if normalized_id <= 0:
            return False
        normalized_status = str(status or "failed").strip().lower()
        timestamp = str(imported_at or "")
        if normalized_status in {"success", "partial"} and not timestamp:
            timestamp = self.now_text()
        prefix = f"{normalized_kind}_remote"
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE registration_results
                SET {prefix}_status = ?, {prefix}_imported_at = ?, {prefix}_error = ?
                WHERE id = ?
                """,
                (normalized_status, timestamp, str(error or ""), normalized_id),
            )
            return bool(cursor.rowcount)

    def delete_results(self, ids: Iterable[int | str]) -> List[Dict[str, Any]]:
        """删除指定记录，返回实际删除前的记录快照。"""
        records = self.get_results_by_ids(ids)
        if not records:
            return []
        delete_ids = [int(row["id"]) for row in records]
        with self._connect() as conn:
            for start in range(0, len(delete_ids), SQLITE_IN_BATCH_SIZE):
                batch = delete_ids[start : start + SQLITE_IN_BATCH_SIZE]
                placeholders = ", ".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM grokiq_outbox "
                    f"WHERE registration_id IN ({placeholders})",
                    batch,
                )
                conn.execute(
                    f"DELETE FROM registration_results WHERE id IN ({placeholders})",
                    batch,
                )
        return records

    def stats(self) -> Dict[str, Any]:

        today = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN status = 'failure' THEN 1 ELSE 0 END) AS failure,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    SUM(CASE WHEN cpa_status = 'success' THEN 1 ELSE 0 END) AS cpa_success,
                    SUM(CASE WHEN cpa_status = 'failed' THEN 1 ELSE 0 END) AS cpa_failed,
                    SUM(CASE WHEN email_disable_status = 'success' THEN 1 ELSE 0 END) AS email_disabled,
                    SUM(CASE WHEN email_disable_status = 'failed' THEN 1 ELSE 0 END) AS email_disable_failed,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? THEN 1 ELSE 0 END) AS today_total,
                    SUM(CASE WHEN substr(finished_at, 1, 10) = ? AND status = 'success' THEN 1 ELSE 0 END) AS today_success,
                    COUNT(DISTINCT CASE WHEN success = 1 THEN lower(email) END) AS unique_success_emails,
                    AVG(CASE WHEN status = 'success' AND duration_seconds > 0 THEN duration_seconds END) AS avg_success_seconds
                FROM registration_results
                """,
                (today, today),
            ).fetchone()
            providers = conn.execute(
                """
                SELECT provider, COUNT(*) AS total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success
                FROM registration_results
                GROUP BY provider
                ORDER BY total DESC, provider ASC
                """
            ).fetchall()
        result = {key: (row[key] or 0) for key in row.keys()}
        result["providers"] = [dict(item) for item in providers]
        return result

    def import_existing_accounts(self, accounts_dir: os.PathLike[str] | str) -> int:
        """把旧的账号 TXT 补录为成功记录；同邮箱已有成功记录时跳过。"""
        root = Path(accounts_dir)
        if not root.is_dir():
            return 0
        excluded_prefixes = ("mail_", "sso_", "accounts_summary_", "sso_summary_")
        imported = 0
        for path in sorted(root.glob("*.txt")):
            if path.name.startswith(excluded_prefixes):
                continue
            try:
                lines: Iterable[str] = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            except OSError:
                continue
            for raw_line in lines:
                parts = raw_line.strip().split("----", 2)
                if len(parts) != 3:
                    continue
                email, password, sso = (part.strip() for part in parts)
                if "@" not in email or not password or not sso or self.has_success(email):
                    continue
                try:
                    stamp = _datetime.datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=_datetime.datetime.now().astimezone().tzinfo,
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.add_result(
                        {
                            "source_key": f"legacy-success:{email.lower()}",
                            "batch_id": "legacy-import",
                            "source": "legacy_import",
                            "started_at": stamp,
                            "finished_at": stamp,
                            "email": email,
                            "password": password,
                            "status": "success",
                            "success": True,
                            "provider": "历史文件",
                            "cpa_enabled": False,
                            "cpa_status": "unknown",
                            "account_file": str(path.resolve()),
                            "sso_saved": True,
                            "extra": {"imported_from": str(path.resolve())},
                        }
                    )
                    imported += 1
                except (OSError, sqlite3.IntegrityError, ValueError):
                    continue
        return imported

    def get_job_snapshot(self) -> Dict[str, Any]:
        """读取最近一次 Web 注册任务快照（单行）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM registration_job_snapshot WHERE id = 1"
            ).fetchone()
        if not row:
            return {}
        data = dict(row)
        data["running"] = bool(data.get("running"))
        return data

    def save_job_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """持久化最近一次 Web 注册任务快照，供服务重启后恢复批次与进度摘要。"""
        now = self.now_text()
        payload = {
            "batch_id": str(snapshot.get("batch_id") or ""),
            "running": 1 if snapshot.get("running") else 0,
            "started_at": snapshot.get("started_at"),
            "finished_at": snapshot.get("finished_at"),
            "target_count": int(snapshot.get("target_count") or 0),
            "workers": int(snapshot.get("workers") or 1),
            "source": str(snapshot.get("source") or "web"),
            "last_error": str(snapshot.get("last_error") or ""),
            "completed_count": int(snapshot.get("completed_count") or 0),
            "success_count": int(snapshot.get("success_count") or 0),
            "failure_count": int(snapshot.get("failure_count") or 0),
            "current_stage": str(snapshot.get("current_stage") or ""),
            "current_email": str(snapshot.get("current_email") or ""),
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registration_job_snapshot (
                    id, batch_id, running, started_at, finished_at, target_count, workers,
                    source, last_error, completed_count, success_count, failure_count,
                    current_stage, current_email, updated_at
                ) VALUES (
                    1, :batch_id, :running, :started_at, :finished_at, :target_count, :workers,
                    :source, :last_error, :completed_count, :success_count, :failure_count,
                    :current_stage, :current_email, :updated_at
                )
                ON CONFLICT(id) DO UPDATE SET
                    batch_id = excluded.batch_id,
                    running = excluded.running,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    target_count = excluded.target_count,
                    workers = excluded.workers,
                    source = excluded.source,
                    last_error = excluded.last_error,
                    completed_count = excluded.completed_count,
                    success_count = excluded.success_count,
                    failure_count = excluded.failure_count,
                    current_stage = excluded.current_stage,
                    current_email = excluded.current_email,
                    updated_at = excluded.updated_at
                """,
                payload,
            )

    def latest_web_batch_id(self) -> str:
        """回退：从结果表推断最近一个非空 web 批次号。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT batch_id
                FROM registration_results
                WHERE batch_id IS NOT NULL AND trim(batch_id) != ''
                  AND batch_id NOT IN ('legacy-import')
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["batch_id"] if row else "") or ""
