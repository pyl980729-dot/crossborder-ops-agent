"""持久层 —— sqlite3 标准库，零外部依赖。

选 SQLite 不是为了偷懒：这是一个可以拷走就演示的系统，
评审/面试官 clone 下来 `python run.py` 就能跑，不需要装数据库。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    stages      TEXT NOT NULL,
    input_json  TEXT NOT NULL,
    result_json TEXT NOT NULL,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    llm_mode    TEXT NOT NULL DEFAULT 'rule',
    schedule_id TEXT,
    trigger     TEXT NOT NULL DEFAULT 'manual'
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    mapping_json TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    cron        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    last_run_at TEXT,
    last_run_id TEXT
);
"""

# 老库升级：缺列就补上（SQLite 不支持 IF NOT EXISTS 加列，只能试）
MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN schedule_id TEXT",
    "ALTER TABLE runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'",
]


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _open():
    """带提交与关闭的连接上下文 —— sqlite3 的 with 只提交不关闭。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _open() as conn:
        conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 列已存在


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def save_run(
    run_id: str,
    title: str,
    status: str,
    stages: list[str],
    payload: dict[str, Any],
    result: dict[str, Any],
    elapsed_ms: int,
    llm_mode: str,
    schedule_id: str | None = None,
    trigger: str = "manual",
) -> None:
    with _open() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (id, created_at, title, status, stages, input_json, result_json,
                elapsed_ms, llm_mode, schedule_id, trigger)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                title,
                status,
                ",".join(stages),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                elapsed_ms,
                llm_mode,
                schedule_id,
                trigger,
            ),
        )


def get_run(run_id: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "status": row["status"],
        "stages": row["stages"].split(",") if row["stages"] else [],
        "input": json.loads(row["input_json"]),
        "result": json.loads(row["result_json"]),
        "elapsed_ms": row["elapsed_ms"],
        "llm_mode": row["llm_mode"],
        "schedule_id": row["schedule_id"] if "schedule_id" in keys else None,
        "trigger": row["trigger"] if "trigger" in keys else "manual",
    }


def list_runs(limit: int = 20) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, created_at, title, status, stages, elapsed_ms, llm_mode, trigger "
            "FROM runs ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _open() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ok = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE status = 'ok'").fetchone()["c"]
        scheduled = conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE trigger = 'scheduler'"
        ).fetchone()["c"]
    return {"total_runs": total, "ok_runs": ok, "scheduled_runs": scheduled}


# ---------------------------------------------------------------------------
# 配置项（阈值覆盖）
# ---------------------------------------------------------------------------

def get_settings() -> dict[str, str]:
    with _open() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_settings(values: dict) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _open() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, str(value), now),
            )


def delete_setting(key: str) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------

UPLOAD_DIR = DATA_DIR / "uploads"


def save_dataset(did: str, kind: str, filename: str, rows: list[dict], mapping: dict) -> dict:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{did}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _open() as conn:
        conn.execute(
            "INSERT INTO datasets (id, kind, filename, row_count, mapping_json, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (did, kind, filename, len(rows), json.dumps(mapping, ensure_ascii=False), str(path), created),
        )
    return {"id": did, "kind": kind, "filename": filename, "row_count": len(rows),
            "mapping": mapping, "created_at": created}


def list_datasets(kind: str | None = None) -> list[dict]:
    sql = "SELECT id, kind, filename, row_count, mapping_json, created_at FROM datasets"
    params: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY rowid DESC"
    with _open() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "filename": r["filename"],
            "row_count": r["row_count"],
            "mapping": json.loads(r["mapping_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def load_dataset_rows(did: str) -> list[dict] | None:
    with _open() as conn:
        row = conn.execute("SELECT path FROM datasets WHERE id = ?", (did,)).fetchone()
    if not row:
        return None
    path = Path(row["path"])
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_dataset(did: str) -> bool:
    with _open() as conn:
        row = conn.execute("SELECT path FROM datasets WHERE id = ?", (did,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM datasets WHERE id = ?", (did,))
    try:
        Path(row["path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# 定时任务
# ---------------------------------------------------------------------------

def create_schedule(sid: str, name: str, cron: str, payload: dict) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT INTO schedules (id, name, cron, payload_json, enabled, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                sid,
                name,
                cron,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )


def list_schedules() -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, name, cron, enabled, created_at, last_run_at, last_run_id "
            "FROM schedules ORDER BY created_at"
        ).fetchall()
    return [
        {
            **dict(r),
            "enabled": bool(r["enabled"]),
        }
        for r in rows
    ]


def get_schedule(sid: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "cron": row["cron"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_run_at": row["last_run_at"],
        "last_run_id": row["last_run_id"],
        "payload": json.loads(row["payload_json"]),
    }


def set_schedule_enabled(sid: str, enabled: bool) -> None:
    with _open() as conn:
        conn.execute("UPDATE schedules SET enabled = ? WHERE id = ?", (1 if enabled else 0, sid))


def delete_schedule(sid: str) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM schedules WHERE id = ?", (sid,))


def touch_schedule(sid: str, run_id: str, at: str) -> None:
    with _open() as conn:
        conn.execute(
            "UPDATE schedules SET last_run_at = ?, last_run_id = ? WHERE id = ?",
            (at, run_id, sid),
        )


def previous_run(schedule_id: str | None, exclude_run_id: str) -> dict | None:
    """取上一次运行作为闭环基线：同一任务优先，没有就取全局最近一次。"""
    with _open() as conn:
        if schedule_id:
            row = conn.execute(
                "SELECT * FROM runs WHERE schedule_id = ? AND id != ? "
                "ORDER BY rowid DESC LIMIT 1",
                (schedule_id, exclude_run_id),
            ).fetchone()
            if row:
                return _row_to_run(row)
        row = conn.execute(
            "SELECT * FROM runs WHERE id != ? ORDER BY rowid DESC LIMIT 1",
            (exclude_run_id,),
        ).fetchone()
    return _row_to_run(row) if row else None


def _row_to_run(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "title": row["title"],
        "status": row["status"],
        "input": json.loads(row["input_json"]),
        "result": json.loads(row["result_json"]),
    }
