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
    llm_mode    TEXT NOT NULL DEFAULT 'rule'
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);
"""


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
) -> None:
    with _open() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (id, created_at, title, status, stages, input_json, result_json, elapsed_ms, llm_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                title,
                status,
                ",".join(stages),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                elapsed_ms,
                llm_mode,
            ),
        )


def get_run(run_id: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
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
    }


def list_runs(limit: int = 20) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, created_at, title, status, stages, elapsed_ms, llm_mode "
            "FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    with _open() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        ok = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE status = 'ok'").fetchone()["c"]
    return {"total_runs": total, "ok_runs": ok}
