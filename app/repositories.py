"""SQLite-репозиторий пользователей, профиля, сессии и планов."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.schemas import fail, ok, parse_stamp, utc_stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY CHECK (telegram_id > 0),
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    consent_version TEXT
);
CREATE TABLE IF NOT EXISTS profile (
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
    telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
    step TEXT NOT NULL,
    temporary_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan (
    id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    request_key INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'ready', 'failed')),
    delivery_status TEXT NOT NULL CHECK (delivery_status IN ('not_sent', 'sent', 'failed')),
    input_json TEXT NOT NULL,
    content_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_user_created ON plan(telegram_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS plan_one_active ON plan(telegram_id)
    WHERE status IN ('queued', 'running');
"""


class Repository:
    def __init__(self, db_path: Path, backup_dir: Path, busy_timeout_ms: int = 1000):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self.busy_timeout_ms = busy_timeout_ms
        self.lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return conn

    def get_or_create_user(self, telegram_id: int) -> dict:
        now = utc_stamp()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (telegram_id, created_at, last_active_at, consent_version)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(telegram_id) DO UPDATE SET last_active_at=excluded.last_active_at
                """,
                (telegram_id, now, now),
            )
            row = conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return dict(row)

    def set_consent(self, telegram_id: int, version: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET consent_version=?, last_active_at=? WHERE telegram_id=?",
                (version, utc_stamp(), telegram_id),
            )

    def get_profile(self, telegram_id: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM profile WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["data"] = json.loads(payload.pop("data_json"))
        return payload

    def save_profile(self, telegram_id: int, profile: dict) -> dict:
        now = utc_stamp()
        with self.lock, self._connect() as conn:
            current = conn.execute(
                "SELECT revision FROM profile WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            revision = 1 if current is None else current["revision"] + 1
            conn.execute(
                """
                INSERT INTO profile (telegram_id, revision, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    revision=excluded.revision,
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, revision, json.dumps(profile, ensure_ascii=False), now),
            )
            conn.execute("DELETE FROM session WHERE telegram_id=?", (telegram_id,))
        return {"telegram_id": telegram_id, "revision": revision, "data": profile, "updated_at": now}

    def get_session(self, telegram_id: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM session WHERE telegram_id=?", (telegram_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["temporary"] = json.loads(payload.pop("temporary_json"))
        return payload

    def save_session(self, telegram_id: int, step: str, temporary: dict) -> None:
        encoded = json.dumps(temporary, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise ValueError("SESSION")
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session (telegram_id, step, temporary_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    step=excluded.step,
                    temporary_json=excluded.temporary_json,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, step, encoded, utc_stamp()),
            )

    def delete_session(self, telegram_id: int) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM session WHERE telegram_id=?", (telegram_id,))

    def purge_expired_sessions(self, ttl_min: int, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        with self.lock, self._connect() as conn:
            rows = conn.execute("SELECT telegram_id, updated_at FROM session").fetchall()
            expired = [
                row["telegram_id"]
                for row in rows
                if moment - parse_stamp(row["updated_at"]) >= timedelta(minutes=ttl_min)
            ]
            for telegram_id in expired:
                conn.execute("DELETE FROM session WHERE telegram_id=?", (telegram_id,))
        return len(expired)

    def active_plan(self, telegram_id: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM plan
                WHERE telegram_id=? AND status IN ('queued', 'running')
                """,
                (telegram_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_ready(self, telegram_id: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM plan
                WHERE telegram_id=? AND status='ready'
                ORDER BY created_at DESC LIMIT 1
                """,
                (telegram_id,),
            ).fetchone()
        return self._plan_row(row)

    def get_plan(self, telegram_id: int, plan_id: str) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan WHERE telegram_id=? AND id=?",
                (telegram_id, plan_id),
            ).fetchone()
        return self._plan_row(row)

    def plan_by_request(self, request_key: int) -> dict | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM plan WHERE request_key=?", (request_key,)).fetchone()
        return self._plan_row(row)

    def jobs_since(self, telegram_id: int, moment: datetime) -> int:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM plan WHERE telegram_id=? AND created_at>=?",
                (telegram_id, utc_stamp(moment)),
            ).fetchone()
        return int(row["n"])

    def last_plan_time(self, telegram_id: int) -> datetime | None:
        with self.lock, self._connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM plan WHERE telegram_id=? ORDER BY created_at DESC LIMIT 1",
                (telegram_id,),
            ).fetchone()
        return parse_stamp(row["created_at"]) if row else None

    def create_plan(self, telegram_id: int, request_key: int, request: dict) -> dict:
        plan_id = str(uuid.uuid4())
        now = utc_stamp()
        with self.lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO plan (
                        id, telegram_id, request_key, status, delivery_status,
                        input_json, content_json, error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', 'not_sent', ?, NULL, NULL, ?, ?)
                    """,
                    (plan_id, telegram_id, request_key, json.dumps(request, ensure_ascii=False), now, now),
                )
            except sqlite3.IntegrityError as exc:
                return {"error": "BUSY" if "plan_one_active" in str(exc).lower() or "UNIQUE" in str(exc) else "DB_ERROR"}
        return {"id": plan_id}

    def mark_running(self, plan_id: str) -> bool:
        return self._transition(plan_id, "running", None, None)

    def mark_ready(self, plan_id: str, content: dict) -> bool:
        return self._transition(plan_id, "ready", json.dumps(content, ensure_ascii=False), None)

    def mark_failed(self, plan_id: str, code: str) -> bool:
        return self._transition(plan_id, "failed", None, code)

    def mark_delivery(self, plan_id: str, status: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                "UPDATE plan SET delivery_status=?, updated_at=? WHERE id=? AND status='ready'",
                (status, utc_stamp(), plan_id),
            )

    def fail_interrupted(self) -> int:
        with self.lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE plan
                SET status='failed', error_code='RESTARTED', content_json=NULL, updated_at=?
                WHERE status IN ('queued', 'running')
                """,
                (utc_stamp(),),
            )
        return cursor.rowcount

    def purge_old_plans(self, retention_days: int, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        border = utc_stamp(moment - timedelta(days=retention_days))
        with self.lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM plan WHERE created_at<?", (border,))
        return cursor.rowcount

    def user_exists(self, telegram_id: int) -> bool:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        return row is not None

    def delete_user_data(self, telegram_id: int):
        snapshot = self.backup_dir / "_delete_snapshot.sqlite3"
        try:
            self._backup_file(snapshot)
            with self.lock, self._connect() as conn:
                conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
            for old in self.backup_dir.glob("*.sqlite3"):
                if old.name != snapshot.name:
                    old.unlink(missing_ok=True)
            fresh = self.backup_dir / f"{date.today().isoformat()}.sqlite3"
            self._backup_file(fresh)
            snapshot.unlink(missing_ok=True)
        except Exception:
            if snapshot.exists():
                self._restore_file(snapshot)
                snapshot.unlink(missing_ok=True)
            return fail("DB_ERROR", "Не удалось сохранить данные. Повторите действие.", retryable=True)
        return ok({"deleted": telegram_id})

    def maybe_daily_backup(self, retention_days: int = 3) -> None:
        today = date.today()
        destination = self.backup_dir / f"{today.isoformat()}.sqlite3"
        if not destination.exists():
            self._backup_file(destination)
        border = today - timedelta(days=retention_days)
        for path in self.backup_dir.glob("*.sqlite3"):
            try:
                stamped = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if stamped < border:
                path.unlink(missing_ok=True)

    def _transition(self, plan_id: str, status: str, content: str | None, error_code: str | None) -> bool:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT telegram_id, status FROM plan WHERE id=?", (plan_id,)).fetchone()
            if row is None or row["status"] not in ("queued", "running"):
                return False
            user = conn.execute("SELECT 1 FROM users WHERE telegram_id=?", (row["telegram_id"],)).fetchone()
            if user is None:
                return False
            conn.execute(
                """
                UPDATE plan
                SET status=?, content_json=?, error_code=?, updated_at=?
                WHERE id=?
                """,
                (status, content, error_code, utc_stamp(), plan_id),
            )
        return True

    def _plan_row(self, row) -> dict | None:
        if row is None:
            return None
        payload = dict(row)
        payload["input"] = json.loads(payload.pop("input_json"))
        content = payload.pop("content_json")
        payload["content"] = json.loads(content) if content else None
        return payload

    def _backup_file(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.db_path)
        target = sqlite3.connect(destination)
        try:
            with target:
                source.backup(target)
        finally:
            source.close()
            target.close()

    def _restore_file(self, snapshot: Path) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        snapshot.replace(self.db_path)
