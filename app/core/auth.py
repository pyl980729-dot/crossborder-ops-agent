"""用户、会话与权限 —— 多租户的底座。

三个角色，权限差异是有业务含义的，不是随便分级：

- **owner（负责人）**：能改核心判断参数、能审批 P0 动作、能管人。
  改 CTR 基准意味着「以后哪些广告算失败」，这会直接改变止血动作，必须负责人拍板。
- **operator（运营）**：能跑诊断、传数据、改文案类参数（标题字符数、语速）。
  这些改了只影响产出形态，不影响「谁被判失败」。
- **viewer（只读）**：能看结果，不能改任何东西。给老板/客户看数据用。

密码用 PBKDF2-HMAC-SHA256，10 万次迭代 + 随机盐。
不用 bcrypt 是为了零依赖 —— 这个项目 clone 下来就要能跑。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------

ROLES = ("owner", "operator", "viewer")

ROLE_LABELS = {
    "owner": "负责人",
    "operator": "运营",
    "viewer": "只读",
}

ROLE_DESC = {
    "owner": "全部权限：改核心阈值、审批 P0 动作、管理成员",
    "operator": "跑诊断、传数据、改内容类参数，不能改核心判断口径",
    "viewer": "只能查看结果，不能做任何修改",
}

# 权限 → 允许的角色
PERMISSIONS: dict[str, set[str]] = {
    "view": {"owner", "operator", "viewer"},
    "run": {"owner", "operator"},
    "upload": {"owner", "operator"},
    "delete_data": {"owner"},
    "config_content": {"owner", "operator"},  # 文案/语速这类，不影响分级判断
    "config_core": {"owner"},                  # CTR 基准、集中度线这类，会改变结论
    "approve_action": {"owner"},
    "manage_members": {"owner"},
    "manage_schedule": {"owner", "operator"},
}

# 阈值分组 → 需要的权限
CONFIG_GROUP_PERMISSION = {
    "广告诊断": "config_core",
    "市场调研": "config_core",
    "选品定价": "config_core",
    "评论归因": "config_content",
    "Listing": "config_content",
    "短视频": "config_content",
}

SESSION_DAYS = 7
PBKDF2_ROUNDS = 100_000


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


def permission_for_config_group(group: str) -> str:
    return CONFIG_GROUP_PERMISSION.get(group, "config_core")


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: str | None = None) -> str:
    """返回 `salt$hash` 格式，盐随密码一起存。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def session_expiry(days: int = SESSION_DAYS) -> str:
    return (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")


def session_valid(expires_at: str) -> bool:
    try:
        return datetime.fromisoformat(expires_at) > datetime.now()
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 当前用户
# ---------------------------------------------------------------------------

@dataclass
class Principal:
    """一次请求的身份。未登录时是匿名，权限最低。"""

    user_id: str = ""
    username: str = "anonymous"
    display_name: str = "未登录"
    role: str = "viewer"
    store_id: str = ""
    store_name: str = ""
    authenticated: bool = False
    via: str = ""  # session | service_token | open

    @property
    def is_admin(self) -> bool:
        return self.role == "owner"

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def can(self, permission: str) -> bool:
        return can(self.role, permission)

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": self.role_label,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "authenticated": self.authenticated,
            "via": self.via,
            "permissions": sorted(p for p in PERMISSIONS if can(self.role, p)),
        }


def anonymous() -> Principal:
    """未登录身份。via 用 anonymous 而不是 open —— open 专指「系统还没初始化」的放行模式。"""
    return Principal(role="viewer", via="anonymous")
