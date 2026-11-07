"""身份解析与权限校验。

三种身份来源，按顺序尝试：
1. 会话 token（浏览器 Cookie 或 X-Session-Token 头）—— 正常登录用户
2. 服务令牌（ECOM_API_TOKEN）—— 脚本/定时任务用，等同 owner
3. 开放模式 —— 系统里还没有任何用户、也没配服务令牌时，
   匿名访问视为 owner，保证 clone 下来就能用

一旦创建了第一个用户，「开放模式」自动关闭，之后必须登录。
这条规则让「开箱即用」和「不能裸奔」共存。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app import db
from app.core import auth
from app.core.auth import Principal


def _first_store_id() -> str:
    stores = db.list_stores()
    return stores[0]["id"] if stores else db.DEFAULT_STORE


def resolve_principal(request: Request) -> Principal:
    # ---- 1. 会话 ----
    token = request.cookies.get("ecom_session") or request.headers.get("X-Session-Token", "")
    if token:
        sess = db.get_session(token)
        if sess and auth.session_valid(sess["expires_at"]):
            user = db.get_user(sess["user_id"])
            if user:
                store_id = sess["store_id"] or _first_store_id()
                store = db.get_store(store_id)
                return Principal(
                    user_id=user["id"],
                    username=user["username"],
                    display_name=user["display_name"],
                    role=user["role"],
                    store_id=store_id,
                    store_name=(store or {}).get("name", ""),
                    authenticated=True,
                    via="session",
                )

    # ---- 2. 服务令牌 ----
    service_token = (__import__("os").getenv("ECOM_API_TOKEN") or "").strip()
    if service_token:
        provided = request.headers.get("X-API-Token", "")
        if not provided:
            bearer = request.headers.get("Authorization", "")
            if bearer.lower().startswith("bearer "):
                provided = bearer[7:].strip()
        if provided and provided == service_token:
            store_id = _first_store_id()
            store = db.get_store(store_id)
            return Principal(
                user_id="service",
                username="service",
                display_name="服务令牌",
                role="owner",
                store_id=store_id,
                store_name=(store or {}).get("name", ""),
                authenticated=True,
                via="service_token",
            )

    # ---- 3. 开放模式 ----
    if db.count_users() == 0 and not service_token:
        store_id = _first_store_id()
        store = db.get_store(store_id)
        return Principal(
            user_id="local",
            username="local",
            display_name="本机用户",
            role="owner",
            store_id=store_id,
            store_name=(store or {}).get("name", ""),
            authenticated=False,
            via="open",
        )

    return auth.anonymous()


def get_principal(request: Request) -> Principal:
    """FastAPI 依赖：取当前请求的身份（中间件已放好）。"""
    principal = getattr(request.state, "principal", None)
    return principal or resolve_principal(request)


def require(permission: str):
    """生成一个权限依赖。缺权限返回 403 并说明需要什么角色。"""

    def _dep(request: Request) -> Principal:
        p = get_principal(request)
        if p.can(permission):
            return p

        needed = sorted(auth.PERMISSIONS.get(permission, set()))
        needed_labels = "、".join(auth.ROLE_LABELS.get(r, r) for r in needed)

        if not p.authenticated and p.via != "open":
            raise HTTPException(
                status_code=401,
                detail=f"未登录。该操作需要 {needed_labels} 权限。",
            )
        raise HTTPException(
            status_code=403,
            detail=f"权限不足：当前角色「{p.role_label}」，该操作需要 {needed_labels}。",
        )

    return _dep


def require_any(*permissions: str):
    """满足任一权限即可（用于按分组细分的配置修改）。"""

    def _dep(request: Request) -> Principal:
        p = get_principal(request)
        if any(p.can(perm) for perm in permissions):
            return p
        raise HTTPException(
            status_code=403,
            detail=f"权限不足：当前角色「{p.role_label}」，该操作至少需要 "
                   f"{'、'.join(auth.ROLE_LABELS.get(r, r) for r in sorted(set().union(*[auth.PERMISSIONS.get(x, set()) for x in permissions])))}。",
        )

    return _dep
