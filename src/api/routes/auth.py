"""鉴权端点（Phase 0-2 逐路由组迁移第五批）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src import audit as audit_mod
from src import auth as auth_mod
from src import config, db

router = APIRouter()


# --- Pydantic 模型（verbatim from server.py） ---

class RegisterRequest(BaseModel):
    """注册入参。口令下限走 config，避免与代码里的校验两处不一致。

    **刻意不接受 `role`**：允许自助注册时自选角色 = 任何人注册一个 admin 就能提权。
    角色只能由种子账号（运维显式配置）或后续的管理端点决定，不能由注册者自报。
    """

    username: str = Field(..., min_length=1, max_length=64, description="登录名，唯一")
    password: str = Field(..., min_length=config.AUTH_MIN_PASSWORD_LEN,
                          description=f"口令，至少 {config.AUTH_MIN_PASSWORD_LEN} 位")


class LoginRequest(BaseModel):
    """登录入参。**用 JSON 而不是 OAuth2PasswordRequestForm** —— 后者要求表单解析，
    会连带引入 `python-multipart` 这个纯为格式服务的依赖，收益为零。"""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class RefreshRequest(BaseModel):
    """刷新入参。"""

    refresh_token: str = Field(..., min_length=1)


# --- 辅助函数 + 常量（verbatim from server.py） ---

_LOGIN_FAILED_MSG = "用户名或密码不正确"


def _is_username_conflict(exc: Exception) -> bool:
    """判断是不是"用户名已被占用"的唯一约束冲突。"""
    text = str(exc).lower()
    return "unique" in text or "duplicate entry" in text


def _token_pair(user: dict) -> dict:
    """签发一对令牌并组装登录响应（注册/登录/刷新三条路共用同一份结构）。"""
    return {
        "access_token": auth_mod.create_access_token(
            user["sub"], role=user["role"], username=user["username"]),
        "refresh_token": auth_mod.create_refresh_token(
            user["sub"], username=user["username"]),
        "token_type": "bearer",
        "expires_in": config.JWT_ACCESS_TTL_MIN * 60,
    }


# --- 端点（verbatim from server.py，@app → @router） ---

@router.post("/api/auth/register")
def api_auth_register(req: RegisterRequest) -> dict:
    """注册新用户（角色固定 analyst）。

    用户名冲突回 **409** 而不是 400：参数本身合法，冲突的是"资源已存在"这一状态 ——
    这两件事在前端的处理完全不同（400 要改输入格式，409 要换个名字）。
    """
    username = req.username.strip()
    try:
        user = auth_mod.create_user(username, req.password)
    except Exception as e:                      # noqa: BLE001
        if _is_username_conflict(e):
            audit_mod.log("auth_register", target=username, actor="anonymous",
                          detail={"ok": False, "reason": "username_taken"})
            raise HTTPException(status_code=409, detail=f"用户名「{username}」已被占用")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    audit_mod.log("auth_register", target=username, actor=user["sub"],
                  detail={"ok": True, "role": user["role"]})
    return {"ok": True, "user": user}


@router.post("/api/auth/login")
def api_auth_login(req: LoginRequest) -> dict:
    """登录：换 access + refresh 令牌。"""
    user = auth_mod.authenticate(req.username, req.password)
    if user is None:
        audit_mod.log("auth_login", target=req.username.strip(), actor="anonymous",
                      detail={"ok": False, "reason": "bad_credentials"})
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED_MSG,
                            headers={"WWW-Authenticate": "Bearer"})
    audit_mod.log("auth_login", target=user["username"], actor=user["sub"],
                  detail={"ok": True, "role": user["role"]})
    return {"ok": True, "user": user, **_token_pair(user)}


@router.post("/api/auth/refresh")
def api_auth_refresh(req: RefreshRequest) -> dict:
    """用 refresh 换一张新的 access。"""
    try:
        payload = auth_mod.decode_token(req.refresh_token, expect_type="refresh")
    except auth_mod.TokenExpiredError:
        raise HTTPException(status_code=401, detail="刷新令牌已过期，请重新登录",
                            headers={"WWW-Authenticate": "Bearer"})
    except (auth_mod.TokenTypeError, auth_mod.TokenInvalidError):
        raise HTTPException(status_code=401, detail="刷新令牌无效",
                            headers={"WWW-Authenticate": "Bearer"})

    row = db.get_user(payload.get("username") or "")
    if row is None or int(row.get("disabled") or 0) == 1:
        raise HTTPException(status_code=401, detail="账号不可用，请重新登录",
                            headers={"WWW-Authenticate": "Bearer"})
    user = {"sub": row["user_id"], "username": row["username"],
            "role": row["role"] or "analyst"}
    audit_mod.log("auth_refresh", target=user["username"], actor=user["sub"],
                  detail={"ok": True})
    return {"ok": True, **_token_pair(user)}


@router.get("/api/auth/me")
def api_auth_me(user: dict = Depends(auth_mod.current_user)) -> dict:
    """回显当前身份（前端用它验活本地存的 token 是否还有效）。"""
    return {"ok": True, "auth_enabled": config.AUTH_ENABLED, **user}


@router.post("/api/auth/logout")
def api_auth_logout(user: dict = Depends(auth_mod.current_user)) -> dict:
    """登出：**只留痕，不吊销**。"""
    audit_mod.log("auth_logout", target=user.get("username"), actor=user.get("sub"),
                  detail={"ok": True, "revoked": False})
    return {"ok": True, "user": user,
            "revocation": "none",
            "note": "登出已记录。JWT 是无状态的：服务端未吊销这张令牌，"
                    "登出由客户端丢弃 token 完成；要真正吊销需引入黑名单，本轮不做。"}
