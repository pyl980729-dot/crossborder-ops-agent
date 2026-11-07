"""重置账号密码 —— 忘了密码、或者想从演示数据切到自己的账号时用。

用法：
    python scripts/reset_password.py                    # 列出所有账号
    python scripts/reset_password.py boss 新密码         # 重置指定账号
    python scripts/reset_password.py --init-admin 密码   # 清空所有用户，重建一个负责人（回到需登录状态）
    python scripts/reset_password.py --open              # 清空所有用户，回到「开放模式」（无需登录）

为什么提供这个脚本：
演示环境里的初始密码不适合当正式凭据。与其把密码写在文档里，
不如给一条「你自己重置」的通道。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.core import auth  # noqa: E402


def list_users() -> None:
    users = db.list_users()
    if not users:
        print("当前没有账号（处于开放模式，打开页面可直接使用）。")
        return
    print(f"{'用户名':<16}{'显示名':<14}{'角色':<10}")
    print("-" * 42)
    for u in users:
        print(f"{u['username']:<16}{u['display_name']:<14}{auth.ROLE_LABELS.get(u['role'], u['role']):<10}")
    print()
    print("重置密码：python scripts/reset_password.py <用户名> <新密码>")


def reset(username: str, password: str) -> None:
    user = db.get_user_by_username(username)
    if not user:
        print(f"账号不存在：{username}")
        raise SystemExit(1)
    if len(password) < 6:
        print("密码至少 6 位")
        raise SystemExit(1)
    db.update_user(user["id"], password_hash=auth.hash_password(password))
    print(f"已重置 {username} 的密码。")


def clear_all() -> None:
    import sqlite3

    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM memberships")
    conn.commit()
    conn.close()


def init_admin(password: str) -> None:
    if len(password) < 6:
        print("密码至少 6 位")
        raise SystemExit(1)
    clear_all()
    uid = db.new_run_id()
    db.create_user(uid, "admin", "管理员", "owner", auth.hash_password(password))
    store_id = db.list_stores()[0]["id"]
    db.add_membership(uid, store_id)
    print(f"已重建负责人账号：admin / 你设置的密码（店铺 {store_id}）")


if __name__ == "__main__":
    db.init_db()
    args = sys.argv[1:]

    if not args:
        list_users()
    elif args[0] == "--open":
        clear_all()
        print("已清空所有账号，系统回到开放模式（打开页面无需登录）。")
    elif args[0] == "--init-admin":
        if len(args) < 2:
            print("用法：python scripts/reset_password.py --init-admin <新密码>")
            raise SystemExit(1)
        init_admin(args[1])
    elif len(args) >= 2:
        reset(args[0], args[1])
    else:
        print("参数不足。运行 python scripts/reset_password.py 查看用法。")
