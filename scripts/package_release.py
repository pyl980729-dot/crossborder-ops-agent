"""打包交付包 —— 排除内部材料与运行时产物，产出可直接发给客户的 zip。

用法：
    python scripts/package_release.py                 # 输出 dist/crossborder-ops-<版本>.zip
    python scripts/package_release.py --output D:/x.zip

排除规则写死在脚本里，避免「手工拷贝时忘了删某个目录」。
"""

from __future__ import annotations

import argparse
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 整个目录跳过（不进入扫描，避免遍历 .venv 浪费时间）
EXCLUDE_DIRS = {
    ".git", ".venv", "__pycache__", ".workbuddy",
    ".idea", ".vscode", "dist", "node_modules", ".pytest_cache",
}

# 按相对路径前缀排除（内部材料、运行时产物）
EXCLUDE_PREFIX = (
    "docs/internal",
    "data/logs",
    "data/backups",
    "data/uploads",
    "tmp_test",
)

EXCLUDE_FILES = {".env", ".env.production", ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".db-journal", ".db-wal", ".db-shm")


def read_version() -> str:
    path = ROOT / "VERSION"
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or "0.0.0"
    return "0.0.0"


def walk_files(skipped_dirs: list[str]):
    """遍历文件，跳过排除目录与排除前缀；被剪枝的目录记进 skipped_dirs。"""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        current = Path(dirpath)
        rel_dir = current.relative_to(ROOT)

        keep = []
        for name in dirnames:
            rel = (rel_dir / name).as_posix()
            if name in EXCLUDE_DIRS:
                skipped_dirs.append(rel)
                continue
            if any(rel == p or rel.startswith(p + "/") for p in EXCLUDE_PREFIX):
                skipped_dirs.append(rel)
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            yield current / name


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 CrossBorder Ops Agent 交付包")
    parser.add_argument("--output", type=Path, default=None, help="输出路径")
    args = parser.parse_args()

    version = read_version()
    top = f"crossborder-ops-{version}"
    target = args.output or (ROOT / "dist" / f"{top}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)

    included = 0
    skipped = 0
    skipped_dirs: list[str] = []

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in walk_files(skipped_dirs):
            rel = path.relative_to(ROOT)
            rel_posix = rel.as_posix()

            if path.name in EXCLUDE_FILES or path.suffix in EXCLUDE_SUFFIX:
                skipped += 1
                continue
            if any(rel_posix == p or rel_posix.startswith(p + "/") for p in EXCLUDE_PREFIX):
                skipped += 1
                continue

            zf.write(path, arcname=f"{top}/{rel_posix}")
            included += 1

    size_mb = target.stat().st_size / 1024 / 1024

    print()
    print("交付包已生成")
    print("=" * 58)
    print(f"  文件      {target}")
    print(f"  版本      {version}")
    print(f"  包含      {included} 个文件")
    print(f"  排除      {skipped} 个文件 / {len(skipped_dirs)} 个目录")
    print(f"  大小      {size_mb:.2f} MB")
    print(f"  顶层目录  {top}/")
    print("=" * 58)
    print()
    print("已排除：.venv / __pycache__ / .workbuddy / docs/internal")
    print("        data/logs / data/backups / data/uploads / .env")
    print()
    print("发给客户后，对方解压、运行 start.bat 或 ./start.sh 即可启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
