"""数据库备份 —— 服务运行中也能安全备份（用 SQLite 在线备份接口）。

用法：
    python scripts/backup_db.py              # 备份一次
    python scripts/backup_db.py --keep 14    # 备份后只保留最近 14 份
    python scripts/backup_db.py --list       # 查看已有备份

可以直接挂到系统定时任务里：每天凌晨跑一次，带 --keep 自动滚动清理。
注意：本脚本只备份数据库；data/uploads/ 下的原始数据集请单独备份。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"
BACKUP_DIR = ROOT / "data" / "backups"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("app-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)


def create_backup() -> Path | None:
    if not DB_PATH.exists():
        print(f"数据库不存在：{DB_PATH}")
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"app-{datetime.now():%Y%m%d-%H%M%S}.db"

    source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        with dest:
            source.backup(dest)
    finally:
        source.close()
        dest.close()

    print(f"备份完成 → {target.relative_to(ROOT)}（{human_size(target.stat().st_size)}）")
    return target


def prune(keep: int) -> None:
    backups = list_backups()
    for old in backups[keep:]:
        try:
            old.unlink()
            print(f"清理旧备份 → {old.name}")
        except OSError as exc:
            print(f"无法删除 {old.name}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CrossBorder Ops Agent 数据库备份")
    parser.add_argument("--keep", type=int, default=0, help="只保留最近 N 份备份，0 表示不清理")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    args = parser.parse_args()

    if args.list:
        backups = list_backups()
        if not backups:
            print("暂无备份。")
            return 0
        print(f"{'文件':<34}{'大小':>10}  修改时间")
        print("-" * 64)
        for item in backups:
            stat = item.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{item.name:<34}{human_size(stat.st_size):>10}  {mtime}")
        return 0

    target = create_backup()
    if target is None:
        return 1
    if args.keep > 0:
        prune(args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
