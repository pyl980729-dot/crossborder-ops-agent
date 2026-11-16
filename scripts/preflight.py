"""上线前自检 —— 把环境、依赖、配置、数据、端口一次性查完。

交付或上线前跑一遍，能挡掉大部分「部署后才发现」的问题：

    python scripts/preflight.py            # 人工阅读
    python scripts/preflight.py --strict   # 有警告也返回非 0（接 CI 用）
    python scripts/preflight.py --json     # 机器可读

返回码：0 = 通过（可能有警告），1 = 存在必须处理的问题。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_PYTHON = (3, 10)
MIN_DISK_MB = 500
REQUIRED_TABLES = {
    "runs", "users", "stores", "memberships",
    "sessions", "settings", "datasets", "schedules",
}

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "[OK]  ", WARN: "[WARN]", FAIL: "[FAIL]"}

results: list[dict] = []


def check(name: str, level: str, detail: str, hint: str = "") -> None:
    results.append({"name": name, "level": level, "detail": detail, "hint": hint})


def check_python() -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version[:2] >= MIN_PYTHON:
        check("Python 版本", OK, text)
    else:
        check("Python 版本", FAIL, text, f"需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高")


def check_deps() -> None:
    missing = []
    for module in ("fastapi", "uvicorn", "pydantic", "multipart"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        check("依赖", FAIL, "缺少 " + "、".join(missing), "pip install -r requirements.txt")
    else:
        check("依赖", OK, "fastapi / uvicorn / pydantic / python-multipart 均可导入")


def check_dirs() -> None:
    for rel in ("data", "data/logs", "data/uploads"):
        path = ROOT / rel
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            check(f"目录可写 {rel}", FAIL, str(exc), "检查目录权限与属主")
            return
    check("目录可写", OK, "data / logs / uploads 均可写")


def check_disk() -> None:
    free_mb = shutil.disk_usage(ROOT).free // (1024 * 1024)
    if free_mb < MIN_DISK_MB:
        check("磁盘空间", WARN, f"剩余 {free_mb} MB", "建议预留 500 MB 以上，日志与上传文件会持续增长")
    else:
        check("磁盘空间", OK, f"剩余 {free_mb} MB")


def check_database() -> None:
    path = ROOT / "data" / "app.db"
    if not path.exists():
        check("数据库", WARN, "尚未创建", "首次启动会自动建库；若已部署过，请确认数据卷挂载正确")
        return
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    except sqlite3.Error as exc:
        check("数据库", FAIL, f"无法打开: {exc}", "检查文件权限，或确认没有其他进程长期锁定")
        return

    missing = REQUIRED_TABLES - tables
    if missing:
        check("数据库", FAIL, "缺表: " + "、".join(sorted(missing)), "删除 data/app.db 让服务重建，或从备份恢复")
    else:
        size_mb = round(path.stat().st_size / 1024 / 1024, 1)
        check("数据库", OK, f"8 张表齐全，{size_mb} MB")


def check_seed() -> None:
    seed = ROOT / "data" / "seed"
    if (seed / "ads_daily.csv").exists() or (seed / "ads.csv").exists():
        check("演示数据", OK, "种子文件存在")
    else:
        check("演示数据", WARN, "缺少种子文件", "python scripts/gen_seed.py && python scripts/gen_timeline.py")


def check_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        check("配置文件 .env", WARN, "未创建", "仅本机演示可忽略；对外部署请复制 .env.production.example 为 .env")
        return
    check("配置文件 .env", OK, "已存在")


def check_security() -> None:
    require_login = (os.getenv("ECOM_REQUIRE_LOGIN") or "").strip().lower() in ("1", "true", "yes", "on")
    token = (os.getenv("ECOM_API_TOKEN") or "").strip()
    origins = (os.getenv("ECOM_CORS_ORIGINS") or "").strip()

    if require_login:
        check("强制登录", OK, "已开启（未登录访问业务接口返回 401）")
    elif token:
        check("强制登录", OK, "未开启，但已设置服务令牌")
    else:
        check(
            "强制登录", FAIL, "未开启且无服务令牌 = 开放模式",
            "对外部署必须设 ECOM_REQUIRE_LOGIN=1，否则任何能访问端口的人都是负责人",
        )

    if not origins:
        check("跨域来源", WARN, "未限制（允许所有来源）", "生产环境设置 ECOM_CORS_ORIGINS=https://你的域名")
    elif origins == "*":
        check("跨域来源", WARN, "为 *（允许所有来源）", "收紧到具体域名")
    else:
        check("跨域来源", OK, origins)

    if token and len(token) < 24:
        check("服务令牌强度", WARN, f"长度 {len(token)}", "建议用 python -c \"import secrets;print(secrets.token_urlsafe(32))\" 生成")


def check_port(port: int = 8000) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    busy = sock.connect_ex(("127.0.0.1", port)) == 0
    sock.close()
    if busy:
        check(f"端口 {port}", WARN, "已被占用", "服务可能已在运行；若要用新配置重启，请先停掉旧进程")
    else:
        check(f"端口 {port}", OK, "空闲")


def check_runs() -> None:
    path = ROOT / "data" / "app.db"
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
    except sqlite3.Error:
        return

    if runs:
        check("历史运行记录", WARN, f"库里有 {runs} 条运行记录", "交付给客户前可执行 python scripts/reset_data.py --yes 清空演示数据")
    else:
        check("历史运行记录", OK, "空库，客户从零开始")

    if users == 0:
        check("账号", WARN, "尚无账号", "首次打开页面会引导创建负责人；强制登录模式下这是必经步骤")
    else:
        check("账号", OK, f"已有 {users} 个账号")


def main() -> int:
    parser = argparse.ArgumentParser(description="CrossBorder Ops Agent 上线前自检")
    parser.add_argument("--strict", action="store_true", help="警告也视为不通过")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    check_python()
    check_deps()
    check_dirs()
    check_disk()
    check_database()
    check_seed()
    check_env_file()
    check_security()
    check_port(args.port)
    check_runs()

    fails = [r for r in results if r["level"] == FAIL]
    warns = [r for r in results if r["level"] == WARN]

    if args.json:
        print(json.dumps(
            {"results": results, "fail": len(fails), "warn": len(warns),
             "passed": not fails and (not warns or not args.strict)},
            ensure_ascii=False, indent=2,
        ))
    else:
        print()
        print("CrossBorder Ops Agent · 上线前自检")
        print("=" * 62)
        for item in results:
            print(f"{MARK[item['level']]} {item['name']:<16}{item['detail']}")
            if item["hint"] and item["level"] != OK:
                print(f"         → {item['hint']}")
        print("=" * 62)
        print(f"通过 {len([r for r in results if r['level'] == OK])} 项 · 警告 {len(warns)} 项 · 必须处理 {len(fails)} 项")
        print()

    if fails:
        return 1
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
