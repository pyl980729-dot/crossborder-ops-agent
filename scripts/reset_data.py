"""清空业务数据 —— 交付前把演示痕迹去掉，让客户从空白开始。

用法：
    python scripts/reset_data.py                # 只预览，不删任何东西
    python scripts/reset_data.py --yes          # 清业务数据，保留账号 / 店铺 / 阈值
    python scripts/reset_data.py --yes --all    # 连账号一起清（回到未初始化状态）

删除前会自动备份一份数据库到 data/backups/，删错了可以恢复。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

UPLOAD_DIR = ROOT / "data" / "uploads"
BACKUP_DIR = ROOT / "data" / "backups"


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("runs", "datasets", "schedules", "users", "sessions", "memberships", "settings"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = 0
    return out


def backup(conn: sqlite3.Connection) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"app-before-reset-{datetime.now():%Y%m%d-%H%M%S}.db"
    dest = sqlite3.connect(target)
    with dest:
        conn.backup(dest)
    dest.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="清空 CrossBorder Ops Agent 的业务数据")
    parser.add_argument("--yes", action="store_true", help="确认执行（不加只预览）")
    parser.add_argument("--all", action="store_true", help="连账号与会话一起清空，回到未初始化状态")
    parser.add_argument("--reset-settings", action="store_true", help="同时清空阈值覆盖，恢复默认")
    args = parser.parse_args()

    if not db.DB_PATH.exists():
        print(f"数据库不存在：{db.DB_PATH}")
        return 1

    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    before = table_counts(conn)

    print()
    print("将要清理的内容")
    print("-" * 52)
    print(f"  运行记录 runs        {before['runs']:>4} 条")
    print(f"  数据集 datasets      {before['datasets']:>4} 条")
    print(f"  定时任务 schedules   {before['schedules']:>4} 条")
    uploads = list(UPLOAD_DIR.glob("*.json")) if UPLOAD_DIR.exists() else []
    print(f"  上传文件             {len(uploads):>4} 个")
    if args.reset_settings:
        print(f"  阈值覆盖 settings    {before['settings']:>4} 条")
    if args.all:
        print(f"  账号 users           {before['users']:>4} 个")
        print(f"  会话 sessions        {before['sessions']:>4} 条")
        print(f"  成员关系 memberships {before['memberships']:>4} 条")
    print("-" * 52)
    print("保留：店铺、平台配置" + ("（不含账号）" if args.all else "、账号、阈值覆盖"))

    if not args.yes:
        print()
        print("这是预览。确认执行请加 --yes")
        conn.close()
        return 0

    saved = backup(conn)
    print()
    print(f"已备份数据库 → {saved.relative_to(ROOT)}")

    with conn:
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM datasets")
        conn.execute("DELETE FROM schedules")
        if args.reset_settings:
            conn.execute("DELETE FROM settings")
        if args.all:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM memberships")
    conn.close()

    removed = 0
    for item in uploads:
        try:
            item.unlink()
            removed += 1
        except OSError:
            pass

    print(f"已删除 {removed} 个上传文件")
    print()
    print("清理完成。" + ("系统已回到未初始化状态，打开页面会引导创建负责人。" if args.all else "账号与阈值配置已保留。"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
