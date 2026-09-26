"""用户体系与 JWT 鉴权（修不足清单 G-11：接口此前完全没有鉴权）。

## 两条不看代码就看不出所以然的决策

**① 口令哈希为什么不用 passlib / bcrypt，而要自己写 pbkdf2？**
passlib 与 bcrypt 4.x 存在已知的不兼容（passlib 1.7.4 读 bcrypt 的 `__about__` 拿版本号，
新版 bcrypt 已删掉该属性，表现是一堆与真实问题无关的告警甚至报错）。
而「加盐 + 慢哈希 + 恒时比较」这三件事标准库都给全了：
`hashlib.pbkdf2_hmac` + `os.urandom` + `hmac.compare_digest`。
本项目一贯的取舍是「能自写就不引重依赖」（同 config.py 对 akshare 的处理），
这里没有例外。**但 JWT 不自己造** —— 签名校验自己实现几乎必然写错，
所以引 PyJWT，这是"该用库就用库"的一侧。

**② 为什么 JWT_SECRET 为空时必须直接失败，而不是给个默认密钥兜底？**
用默认密钥签名 = 所有 token 任何看过源码的人都能伪造，而系统从响应上
**完全看不出**它处于"看起来有鉴权其实没有"的状态。这类静默失效是本项目最要防的东西，
所以宁可启动即报错（见 `require_jwt_secret`，服务启动自检与每次签发/校验都会走它）。

## 边界（如实记着，不假装已解决）

- **JWT 是无状态的，没有服务端吊销**：登出只做审计留痕，客户端丢掉 token 即可。
  要真吊销得上黑名单/短 TTL + 刷新，本轮不做（`/api/auth/logout` 的文案也照实写）。
- **登录失败的三种情形一律返回 None**，避免用户名枚举；但**响应时间**仍略有可区分度
  （用户不存在时少一次 pbkdf2 往返），故 `authenticate` 在用户不存在时也做一次等价哈希。
- 本模块**不自己拼 SQL**，一律走 `src.db` 的访问函数，保证 SQLite / MySQL 两后端同形。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from src import config, db

# pbkdf2 迭代次数。取 210000 是 OWASP 对 PBKDF2-HMAC-SHA256 的建议量级：
# 单次约几十毫秒（够慢到让爆破不划算），又不会让登录接口慢到影响体验。
PBKDF2_ITERATIONS = 210_000
SALT_BYTES = 16


# ==================== 异常 ====================
#
# 三种失败**分开抛**而不是统一一个 AuthError：HTTP 层虽然都回 401，
# 但审计原因不同 —— 「有人在拿过期 token 刷接口」（可能是前端时钟或刷新逻辑坏了）
# 与「有人在伪造 token」（是攻击）是完全不同的两件事，混成一种就再也分不出来。

class AuthError(Exception):
    """鉴权错误基类。"""


class TokenExpiredError(AuthError):
    """令牌已过期（签名本身是好的）。"""


class TokenInvalidError(AuthError):
    """签名不匹配 / 结构破损 / 算法不符 —— 即"这个 token 不是我们签发的"。"""


class TokenTypeError(AuthError):
    """类型不匹配（如拿 refresh token 当 access token 用）。"""


# ==================== 口令哈希 ====================

def hash_password(raw: str) -> tuple[str, str]:
    """把口令派生成 `(hash_hex, salt_hex)`。

    **每次调用都生成新盐**：同一个口令两次入库得到不同的哈希。
    盐共用的后果是"彩虹表算一次就能同时命中全库"，这是最廉价的密码库事故。
    """
    salt = os.urandom(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", (raw or "").encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return dk.hex(), salt.hex()


def verify_password(raw: str, hash_hex: str, salt_hex: str) -> bool:
    """校验口令。**恒时比较**，失败一律返回 False（不抛异常）。

    为什么用 `hmac.compare_digest` 而不是 `==`：`==` 在首个不同字节处提前返回，
    逐字节的时间差可以被用来一位一位地猜出哈希。这里不是理论洁癖 ——
    修复成本是零（换个函数而已），而写错的成本是整库口令。
    """
    try:
        salt = bytes.fromhex(salt_hex or "")
    except ValueError:
        return False
    if not salt:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", (raw or "").encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex or "")


# ==================== JWT ====================

def require_jwt_secret() -> str:
    """取 JWT 密钥；为空则抛错。

    调用点有两处：**服务启动自检**（配了鉴权却没密钥就该起不来）与**每次签发/校验**。
    两处都走它，才不会出现"启动时校验了、运行期又被改空"的缝隙。
    """
    secret = (config.JWT_SECRET or "").strip()
    if not secret:
        raise RuntimeError(
            "未配置 JWT_SECRET：请设环境变量 FA_JWT_SECRET，"
            "或写入 data/db_keys.local.json 的 \"jwt_secret\" 字段。"
            "（用内置默认密钥签发 token 等于没有鉴权，且从响应上完全看不出来，"
            "因此这里选择启动即失败。）")
    return secret


def _encode(sub: str, token_type: str, ttl: timedelta, *,
            role: str | None = None, username: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": sub,
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
        # jti 的作用不是"防重放"（那要靠黑名单，本轮没有），而是让同一个人
        # 在同一秒内签发的两个 token 互不相同，便于日志里区分是"同一张票被重放"
        # 还是"客户端刷新了两次"。
        "jti": uuid.uuid4().hex,
    }
    if role is not None:
        payload["role"] = role
    if username is not None:
        payload["username"] = username
    return jwt.encode(payload, require_jwt_secret(), algorithm=config.JWT_ALGORITHM)


def create_access_token(sub: str, role: str = "analyst",
                        *, username: str | None = None) -> str:
    """签发访问令牌（短寿命）。"""
    return _encode(sub, "access", timedelta(minutes=config.JWT_ACCESS_TTL_MIN),
                   role=role, username=username)


def create_refresh_token(sub: str, *, username: str | None = None) -> str:
    """签发刷新令牌（长寿命）。**不带 role** —— 角色可能在刷新间隔里被改，
    刷新时应当回数据库重新取，而不是沿用旧 token 里的角色。"""
    return _encode(sub, "refresh", timedelta(days=config.JWT_REFRESH_TTL_DAYS),
                   username=username)


def decode_token(token: str, *, expect_type: str | None = None) -> dict:
    """校验并解出载荷。三种失败分别抛 TokenExpired / TokenInvalid / TokenType。"""
    try:
        payload = jwt.decode(token, require_jwt_secret(),
                             algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError(str(e)) from e
    except jwt.InvalidTokenError as e:
        # InvalidTokenError 是 PyJWT 的族根（签名错/格式破损/算法不符都在其下）。
        # ⚠️ 它同时是 ExpiredSignatureError 的父类，所以上面那条 except 必须写在前面。
        raise TokenInvalidError(str(e)) from e

    if expect_type is not None and payload.get("type") != expect_type:
        raise TokenTypeError(
            f"令牌类型不匹配：需要 {expect_type}，实际 {payload.get('type')}")
    return payload


# ==================== 登录 ====================

def authenticate(username: str, password: str, *, db_path=None) -> dict | None:
    """用户名+口令 → 身份字典；**任何失败情形都返回 None**（不做区分）。

    返回的字典**只含身份字段**（sub / username / role），不含 password_hash / salt ——
    别让口令材料顺着"身份"这条最常见的路径流到日志、响应与审计里。
    """
    user = db.get_user((username or "").strip(), db_path)
    if user is None:
        # 仍然做一次等价开销的派生：否则"用户不存在"会明显更快，
        # 返回文案统一了也挡不住按响应时间枚举用户名。
        hash_password(password or "")
        return None
    if int(user.get("disabled") or 0) == 1:
        return None
    if not verify_password(password or "", user["password_hash"], user["salt"]):
        return None
    return {"sub": user["user_id"],
            "username": user["username"],
            "role": user["role"] or "analyst"}


def create_user(username: str, password: str, *, role: str = "analyst",
                db_path=None) -> dict:
    """注册新用户。用户名已存在或口令过短时由调用方先校验（这里只做落库）。

    返回的身份字典与 `authenticate` 同形，便于注册后直接签发 token。
    """
    h, s = hash_password(password)
    user_id = f"u-{uuid.uuid4().hex[:12]}"
    db.insert_user(user_id, username.strip(), h, s, role=role, db_path=db_path)
    return {"sub": user_id, "username": username.strip(), "role": role}


def ensure_seed_admin(*, db_path=None) -> str:
    """建演示管理员（幂等）。返回 `"created"` / `"exists"` / `"skipped"`。

    口令未配置时返回 `"skipped"` 并**不建号** —— 在源码里写死一个默认口令
    是最危险的一类默认凭据（上线后没人会记得改，而看过仓库的人都能登进来）。
    所以这里选择"不建号 + 启动日志里响亮提示"，由运维显式配 `SEED_ADMIN_PASSWORD`。

    user_id 用 `seed-<username>` 这种**确定性**写法而不是随机 uuid：
    随机 id 会让"第二次启动"变成插入一条新的同名记录（撞 username 唯一约束），
    幂等性就破了。
    """
    username = (config.SEED_ADMIN_USER or "").strip() or "admin"
    password = config.SEED_ADMIN_PASSWORD or ""
    if not password:
        print(f"[auth] 未配置 SEED_ADMIN_PASSWORD，跳过演示账号「{username}」的创建；"
              f"如需登录请设该环境变量（或写入 data/db_keys.local.json 的 "
              f"seed_admin_password）后重启。")
        return "skipped"
    if db.get_user(username, db_path) is not None:
        return "exists"
    h, s = hash_password(password)
    with db.get_conn(db_path) as conn:
        conn.execute(db.upsert_user_sql(),
                     (f"seed-{username}", username, h, s, "admin", 0))
    return "created"


def users_count(*, db_path=None) -> int:
    """用户总数（供 /api/health 展示 —— 能静默降级的东西都要可见）。"""
    try:
        with db.get_conn(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row is not None else 0
    except Exception:                       # noqa: BLE001 —— health 不该因它 500
        return 0


def status() -> dict:
    """鉴权通道状态（`/api/health` 用，与 vector/rerank/checkpointer 的 status() 同一惯例）。

    **`enabled=true` 而 `jwt_secret_configured=false` 必须可见** —— 这是最危险的一种
    "看起来有鉴权"：没有密钥时进程本来就起不来，但若有人把校验去掉，至少这里看得出来。
    """
    present = False
    try:
        present = db.get_user(config.SEED_ADMIN_USER) is not None
    except Exception:                       # noqa: BLE001
        present = False
    return {
        "enabled": config.AUTH_ENABLED,
        "jwt_secret_configured": bool((config.JWT_SECRET or "").strip()),
        "algorithm": config.JWT_ALGORITHM,
        "access_ttl_min": config.JWT_ACCESS_TTL_MIN,
        "refresh_ttl_days": config.JWT_REFRESH_TTL_DAYS,
        "min_password_len": config.AUTH_MIN_PASSWORD_LEN,
        "seed_admin_user": config.SEED_ADMIN_USER,
        "seed_admin_present": present,
        "users_count": users_count(),
        # 这两份表是**给人看的**：前端与文档要能说出"哪些端点要登录"，
        # 而它们必须与端点上的 Depends 保持一致（不一致时以端点为准）。
        "public_paths": sorted(config.PUBLIC_PATHS),
        "protected_prefixes": sorted(config.AUTH_PROTECTED_PREFIXES),
        # JWT 无状态 = 没有服务端吊销名单，这是本轮如实登记的边界
        "revocation": "none（无黑名单，登出靠客户端丢弃 token）",
    }


# ==================== FastAPI 依赖 ====================

# auto_error=False 是**关键**：默认 True 时 FastAPI 会在"没带 token"时直接抛 401，
# 于是 AUTH_ENABLED=0 的免鉴权形态根本走不到我们的代码里（单机演示会被自己的鉴权挡住）。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """401 必须带 WWW-Authenticate：OAuth2 客户端靠这个头决定要不要去换 token。"""
    return HTTPException(status_code=401, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


async def current_user(token: str | None = Depends(oauth2_scheme)) -> dict:
    """把请求解析成身份字典（FastAPI 依赖）。

    - `AUTH_ENABLED=0`（单机免配形态）→ 干净地退化成匿名，而不是"无 token 就 401"；
    - 开了鉴权 → 缺 token / 过期 / 伪造 / 类型不对**一律 401**，但 detail 分得开，
      便于前端把"该重新登录"与"这 token 有问题"分别处理。
    """
    if not config.AUTH_ENABLED:
        return {"sub": "anonymous", "username": "anonymous", "role": "anonymous"}
    if not token:
        raise _unauthorized("缺少访问令牌：请在 Authorization 头里带 Bearer <token>")
    try:
        payload = decode_token(token, expect_type="access")
    except TokenExpiredError:
        raise _unauthorized("登录已过期，请重新登录")
    except TokenTypeError:
        raise _unauthorized("令牌类型不对：业务端点需要 access token")
    except TokenInvalidError:
        raise _unauthorized("访问令牌无效")

    sub = payload.get("sub")
    if not sub:
        raise _unauthorized("访问令牌缺少主体（sub）")
    return {"sub": sub,
            "username": payload.get("username") or sub,
            "role": payload.get("role") or "analyst"}


async def require_admin(user: dict = Depends(current_user)) -> dict:
    """管理端点闸门（FastAPI 依赖）：role != admin 一律 **403**。

    403 与 401 必须分开：401 = "没登录"（前端引导去登录页），
    403 = "登录了但不够格"（前端该提示"需要管理员权限"，跳登录页只会白转一圈）。
    `AUTH_ENABLED=0`（单机免登录形态）→ 放行：单机没有"别人"，
    向导若在这里也被挡，免登录形态下整个入库通道就是死路。
    """
    if config.AUTH_ENABLED and (user.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


if __name__ == "__main__":
    # 自检：python -m src.auth（口令哈希与令牌往返，不写库、不联网）
    _h, _s = hash_password("自检口令")
    print(f"hash ok: verify(correct)={verify_password('自检口令', _h, _s)} "
          f"verify(wrong)={verify_password('错的', _h, _s)}")
    if config.JWT_SECRET:
        _t = create_access_token("self-check", role="admin", username="self")
        _p = decode_token(_t, expect_type="access")
        print(f"jwt  ok: sub={_p['sub']} role={_p['role']} type={_p['type']}")
    else:
        print("jwt  skip: 未配置 FA_JWT_SECRET（令牌往返需密钥）")
    print(f"auth enabled = {config.AUTH_ENABLED}；users = {users_count()}")