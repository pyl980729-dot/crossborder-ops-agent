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

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'operator',
    password_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT 'shopee_br',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id    TEXT NOT NULL,
    store_id   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, store_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    store_id   TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

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
    # 多租户隔离列
    "ALTER TABLE runs ADD COLUMN store_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE datasets ADD COLUMN store_id TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE schedules ADD COLUMN store_id TEXT NOT NULL DEFAULT 'default'",
]

DEFAULT_STORE = "default"


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
        # 兜底默认店铺：单店场景不需要显式创建
        conn.execute(
            "INSERT OR IGNORE INTO stores (id, name, platform, created_at) VALUES (?, ?, ?, ?)",
            (DEFAULT_STORE, "默认店铺", "shopee_br", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


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
    store_id: str = DEFAULT_STORE,
) -> None:
    with _open() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (id, created_at, title, status, stages, input_json, result_json,
                elapsed_ms, llm_mode, schedule_id, trigger, store_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                store_id,
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


def list_runs(limit: int = 20, store_id: str = DEFAULT_STORE) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, created_at, title, status, stages, elapsed_ms, llm_mode, trigger "
            "FROM runs WHERE store_id = ? ORDER BY rowid DESC LIMIT ?",
            (store_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def stats(store_id: str = DEFAULT_STORE) -> dict:
    with _open() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE store_id = ?", (store_id,)).fetchone()["c"]
        ok = conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'ok' AND store_id = ?", (store_id,)
        ).fetchone()["c"]
        scheduled = conn.execute(
            "SELECT COUNT(*) AS c FROM runs WHERE trigger = 'scheduler' AND store_id = ?",
            (store_id,),
        ).fetchone()["c"]
    return {"total_runs": total, "ok_runs": ok, "scheduled_runs": scheduled}


# ---------------------------------------------------------------------------
# 配置项（阈值覆盖）
# ---------------------------------------------------------------------------

def _setting_key(store_id: str, key: str) -> str:
    """settings 用复合键：`店铺::字段`。老数据没有前缀，迁移时归到默认店铺。"""
    return f"{store_id or DEFAULT_STORE}::{key}"


def get_settings(store_id: str = DEFAULT_STORE) -> dict[str, str]:
    prefix = _setting_key(store_id, "")
    with _open() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
    return {r["key"][len(prefix):]: r["value"] for r in rows}


def save_settings(values: dict, store_id: str = DEFAULT_STORE) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _open() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (_setting_key(store_id, key), str(value), now),
            )


def delete_setting(key: str, store_id: str = DEFAULT_STORE) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (_setting_key(store_id, key),))


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------

UPLOAD_DIR = DATA_DIR / "uploads"


def save_dataset(
    did: str, kind: str, filename: str, rows: list[dict], mapping: dict,
    store_id: str = DEFAULT_STORE,
) -> dict:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{did}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _open() as conn:
        conn.execute(
            "INSERT INTO datasets (id, kind, filename, row_count, mapping_json, path, created_at, store_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (did, kind, filename, len(rows), json.dumps(mapping, ensure_ascii=False), str(path), created, store_id),
        )
    return {"id": did, "kind": kind, "filename": filename, "row_count": len(rows),
            "mapping": mapping, "created_at": created}


def list_datasets(kind: str | None = None, store_id: str = DEFAULT_STORE) -> list[dict]:
    sql = "SELECT id, kind, filename, row_count, mapping_json, created_at FROM datasets WHERE store_id = ?"
    params: tuple = (store_id,)
    if kind:
        sql += " AND kind = ?"
        params = params + (kind,)
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


def load_dataset_rows(did: str, store_id: str | None = None) -> list[dict] | None:
    """带 store 校验：不能通过猜 id 读到别的店铺的数据。"""
    with _open() as conn:
        if store_id:
            row = conn.execute(
                "SELECT path FROM datasets WHERE id = ? AND store_id = ?", (did, store_id)
            ).fetchone()
        else:
            row = conn.execute("SELECT path FROM datasets WHERE id = ?", (did,)).fetchone()
    if not row:
        return None
    path = Path(row["path"])
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def delete_dataset(did: str, store_id: str | None = None) -> bool:
    with _open() as conn:
        if store_id:
            row = conn.execute(
                "SELECT path FROM datasets WHERE id = ? AND store_id = ?", (did, store_id)
            ).fetchone()
        else:
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

def create_schedule(sid: str, name: str, cron: str, payload: dict, store_id: str = DEFAULT_STORE) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT INTO schedules (id, name, cron, payload_json, enabled, created_at, store_id) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                sid,
                name,
                cron,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                store_id,
            ),
        )


def list_schedules(store_id: str = DEFAULT_STORE) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, name, cron, enabled, created_at, last_run_at, last_run_id, store_id "
            "FROM schedules WHERE store_id = ? ORDER BY created_at",
            (store_id,),
        ).fetchall()
    return [
        {
            **dict(r),
            "enabled": bool(r["enabled"]),
        }
        for r in rows
    ]


def list_schedules_all() -> list[dict]:
    """调度器专用：跨所有店铺取任务，每个任务自带 store_id。"""
    with _open() as conn:
        rows = conn.execute(
            "SELECT id, name, cron, enabled, created_at, last_run_at, last_run_id, store_id "
            "FROM schedules ORDER BY created_at"
        ).fetchall()
    return [{**dict(r), "enabled": bool(r["enabled"])} for r in rows]


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


def previous_run(
    schedule_id: str | None,
    exclude_run_id: str,
    store_id: str = DEFAULT_STORE,
) -> dict | None:
    """取上一次运行作为闭环基线：同一任务优先，没有就取本店铺最近一次。

    注意必须带 store_id —— 否则 A 店的运行会变成 B 店的对比基线，
    闭环结论直接串店。
    """
    with _open() as conn:
        if schedule_id:
            row = conn.execute(
                "SELECT * FROM runs WHERE schedule_id = ? AND id != ? AND store_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (schedule_id, exclude_run_id, store_id),
            ).fetchone()
            if row:
                return _row_to_run(row)
        row = conn.execute(
            "SELECT * FROM runs WHERE id != ? AND store_id = ? ORDER BY rowid DESC LIMIT 1",
            (exclude_run_id, store_id),
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


# ---------------------------------------------------------------------------
# 用户 / 店铺 / 会话
# ---------------------------------------------------------------------------

def count_users() -> int:
    with _open() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create_user(uid: str, username: str, display_name: str, role: str, password_hash: str) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, username, display_name, role, password_hash,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _user_row(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "created_at": row["created_at"],
    }


def get_user_by_username(username: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    return {**_user_row(row), "password_hash": row["password_hash"]}


def get_user(uid: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return _user_row(row) if row else None


def list_users() -> list[dict]:
    with _open() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [_user_row(r) for r in rows]


def update_user(uid: str, role: str | None = None, display_name: str | None = None,
                password_hash: str | None = None) -> None:
    sets, params = [], []
    if role:
        sets.append("role = ?")
        params.append(role)
    if display_name:
        sets.append("display_name = ?")
        params.append(display_name)
    if password_hash:
        sets.append("password_hash = ?")
        params.append(password_hash)
    if not sets:
        return
    params.append(uid)
    with _open() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)


def delete_user(uid: str) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM memberships WHERE user_id = ?", (uid,))


# ---- 会话 ----

def create_session(token: str, user_id: str, store_id: str | None, expires_at: str) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, store_id, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, user_id, store_id, expires_at,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def get_session(token: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(now: str) -> None:
    with _open() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))


# ---- 店铺 ----

def list_stores() -> list[dict]:
    with _open() as conn:
        rows = conn.execute("SELECT * FROM stores ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def get_store(store_id: str) -> dict | None:
    with _open() as conn:
        row = conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
    return dict(row) if row else None


def create_store(sid: str, name: str, platform: str) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT INTO stores (id, name, platform, created_at) VALUES (?, ?, ?, ?)",
            (sid, name, platform, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def delete_store(sid: str) -> None:
    if sid == DEFAULT_STORE:
        return  # 默认店铺不允许删
    with _open() as conn:
        conn.execute("DELETE FROM stores WHERE id = ?", (sid,))
        conn.execute("DELETE FROM memberships WHERE store_id = ?", (sid,))


# ---- 成员关系 ----

def add_membership(user_id: str, store_id: str) -> None:
    with _open() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO memberships (user_id, store_id, created_at) VALUES (?, ?, ?)",
            (user_id, store_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def list_user_stores(user_id: str) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT s.id, s.name, s.platform FROM memberships m "
            "JOIN stores s ON s.id = m.store_id WHERE m.user_id = ? ORDER BY s.created_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_store_members(store_id: str) -> list[dict]:
    with _open() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.display_name, u.role, m.created_at "
            "FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.store_id = ? ORDER BY m.created_at",
            (store_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_memberships() -> list[dict]:
    with _open() as conn:
        rows = conn.execute("SELECT * FROM memberships").fetchall()
    return [dict(r) for r in rows]
