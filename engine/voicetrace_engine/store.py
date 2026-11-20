from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,
    size_bytes      INTEGER DEFAULT 0,
    duration_ms     INTEGER DEFAULT 0,
    width           INTEGER DEFAULT 0,
    height          INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'queued',
    stage           TEXT NOT NULL DEFAULT 'queued',
    progress        REAL NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    rtf             REAL,
    elapsed_s       REAL,
    has_transcript  INTEGER NOT NULL DEFAULT 0,
    issues_json     TEXT,
    thumb_path      TEXT,
    transcript_path TEXT,
    auto_correct    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    action          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    transcript_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_job ON history(job_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

JOB_PUBLIC_FIELDS = (
    "id", "name", "path", "size_bytes", "duration_ms", "width", "height",
    "status", "stage", "progress", "error", "created_at", "started_at",
    "finished_at", "rtf", "elapsed_s", "has_transcript", "issues_json",
    "auto_correct",
)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._local = threading.local()
        # Every connection opened through this store, so shutdown can close
        # them all (request threads each keep their own thread-local one).
        self._conns: list[sqlite3.Connection] = []
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            cols = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
            if "auto_correct" not in cols:
                c.execute("ALTER TABLE jobs ADD COLUMN auto_correct INTEGER NOT NULL DEFAULT 1")
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    # ------------------------------------------------------------ jobs
    def add_job(self, path: str, name: str, size_bytes: int, duration_ms: int = 0,
                width: int = 0, height: int = 0, auto_correct: bool = True) -> dict[str, Any]:
        # 默认开启「全部自动校正」：新任务入队即带自动校正，用户不必再逐条勾选。
        job_id = uuid.uuid4().hex[:16]
        created = now_iso()
        with self._lock:
            c = self._conn()
            c.execute(
                """INSERT INTO jobs (id, name, path, size_bytes, duration_ms, width, height,
                   status, stage, progress, created_at, auto_correct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, name, path, size_bytes, duration_ms, width, height,
                 "queued", "queued", 0.0, created, int(auto_correct)),
            )
            c.commit()
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._job_dict(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = set(JOB_PUBLIC_FIELDS) | {"thumb_path", "transcript_path"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        sets = ", ".join(f"{k}=?" for k, _ in items)
        with self._lock:
            c = self._conn()
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", [v for _, v in items] + [job_id])
            c.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            c = self._conn()
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            c.execute("DELETE FROM history WHERE job_id=?", (job_id,))
            c.commit()

    def next_queued(self) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return self._job_dict(row) if row else None

    def pending_count(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','processing')"
        ).fetchone()
        return int(row["n"])

    def reset_stale(self) -> int:
        """Mark jobs left 'processing' by a crash as queued."""
        with self._lock:
            c = self._conn()
            cur = c.execute(
                "UPDATE jobs SET status='queued', stage='queued', progress=0, error=NULL "
                "WHERE status='processing'"
            )
            c.commit()
            return cur.rowcount

    # ------------------------------------------------------- maintenance
    def checkpoint(self) -> tuple:
        """Fold the WAL back into the main db (called on a clean shutdown)."""
        with self._lock:
            row = self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return tuple(row) if row is not None else ()

    def close(self) -> None:
        """Close every connection this store opened; safe to call twice.

        Only called on the shutdown path: other threads' thread-local handles
        are invalid afterwards, which is exactly what engine exit wants.
        """
        with self._lock:
            conns, self._conns = self._conns, []
        self._local.conn = None
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _job_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["has_transcript"] = bool(d.get("has_transcript"))
        d["auto_correct"] = bool(d.get("auto_correct"))
        d["issues_count"] = 0
        try:
            issues = json.loads(d.get("issues_json") or "{}")
            d["issues_count"] = int(issues.get("total", 0))
        except Exception:
            pass
        # path is private-ish but the app needs it for export/open
        return d

    # ------------------------------------------------------------ history
    def push_history(self, job_id: str, transcript_json: str, action: str,
                     keep: int = 40) -> None:
        with self._lock:
            c = self._conn()
            c.execute(
                "INSERT INTO history (job_id, action, created_at, transcript_json) VALUES (?,?,?,?)",
                (job_id, action, now_iso(), transcript_json),
            )
            c.execute(
                """DELETE FROM history WHERE job_id=? AND id NOT IN (
                       SELECT id FROM history WHERE job_id=? ORDER BY id DESC LIMIT ?)""",
                (job_id, job_id, keep),
            )
            c.commit()

    def peek_history(self, job_id: str) -> dict[str, Any] | None:
        """Read the newest history row *without* consuming it.

        Undo needs the snapshot before the transcript is written; consuming it
        first loses the undo when the write fails.
        """
        with self._lock:
            row = self._conn().execute(
                "SELECT id, transcript_json FROM history WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            return {"id": int(row["id"]), "transcript_json": str(row["transcript_json"])}

    def delete_history(self, history_id: int) -> None:
        with self._lock:
            c = self._conn()
            c.execute("DELETE FROM history WHERE id=?", (int(history_id),))
            c.commit()

    def pop_history(self, job_id: str) -> str | None:
        row = self.peek_history(job_id)
        if row is None:
            return None
        self.delete_history(row["id"])
        return row["transcript_json"]

    def history_count(self, job_id: str) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM history WHERE job_id=?", (job_id,)
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            c = self._conn()
            c.execute(
                "INSERT INTO meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            c.commit()
