"""鉴权契约用例（纯本地，不联网、不调模型）。

这一批守的是**四件不能出错的事**，每件都在真实系统里以不同方式坑过人：

1. **口令哈希**：同一口令两次入库必须得到不同的盐/哈希（否则彩虹表一次命中全库），
   而两者都必须能验过；错口令必须验不过。
2. **令牌的三种失败要分开抛**：过期 / 签名被篡改 / 类型不对。
   HTTP 层都回 401，但**审计原因不同** —— 混成一种异常，
   「有人在拿过期 token 刷接口」和「有人在伪造 token」在日志里就区分不出来了。
3. **登录失败的三种情形一律返回 None**：用户不存在、口令错、已禁用。
   如果分别报错，接口层必然会把它们写成不同消息，于是攻击者可以靠错误文案枚举用户名。
4. **种子账号幂等**：重复启动不报错、不重复建号、**不回退已被改过的口令**
   （否则现象是"昨天改的密码今天重启又变回去了"）。

用例全部跑在 `synth_db` 临时库上（conftest 已把 DB 指向 tmp_path）。
"""
from __future__ import annotations

import pytest

from src import config, db


# ==================== 口令哈希 ====================

def test_hash_password_same_input_gives_different_salt_and_both_verify():
    from src.auth import hash_password, verify_password

    h1, s1 = hash_password("正确的口令abc123")
    h2, s2 = hash_password("正确的口令abc123")

    assert s1 != s2, "同一口令两次必须用不同的盐 —— 盐相同等于全库共用一张彩虹表"
    assert h1 != h2, "盐不同则哈希必不同"
    assert verify_password("正确的口令abc123", h1, s1) is True
    assert verify_password("正确的口令abc123", h2, s2) is True


def test_verify_password_rejects_wrong_password():
    from src.auth import hash_password, verify_password

    h, s = hash_password("正确的口令abc123")
    assert verify_password("错误的口令", h, s) is False
    assert verify_password("", h, s) is False


def test_hash_output_is_hex_and_does_not_contain_plaintext():
    from src.auth import hash_password

    h, s = hash_password("明文口令不要出现在哈希里")
    assert h and s
    # 必须是 hex：落库、比对、日志里都不会出现不可打印字符
    assert bytes.fromhex(h) and bytes.fromhex(s)
    assert "明文口令" not in h


# ==================== JWT 契约 ====================

@pytest.fixture
def jwt_secret(monkeypatch):
    """钉一个测试密钥。

    为什么不复用 config.JWT_SECRET 的真实值：单测不应依赖开发机的本地密钥文件，
    否则换台机器跑就变成"因为没配密钥而失败"，掩盖真正的回归。
    """
    monkeypatch.setattr(config, "JWT_SECRET", "unit-test-secret-not-for-production")
    monkeypatch.setattr(config, "JWT_ACCESS_TTL_MIN", 120)
    monkeypatch.setattr(config, "JWT_REFRESH_TTL_DAYS", 7)
    return config.JWT_SECRET


def test_access_token_roundtrip_carries_sub_role_type(jwt_secret):
    from src.auth import create_access_token, decode_token

    token = create_access_token("u-001", role="admin")
    payload = decode_token(token)
    assert payload["sub"] == "u-001"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert payload["exp"] > payload["iat"]


def test_refresh_token_is_typed_refresh_and_can_be_required(jwt_secret):
    from src.auth import create_refresh_token, decode_token

    token = create_refresh_token("u-001")
    assert decode_token(token, expect_type="refresh")["sub"] == "u-001"


def test_type_mismatch_raises_token_type_error(jwt_secret):
    from src.auth import TokenTypeError, create_access_token, decode_token

    token = create_access_token("u-001")
    with pytest.raises(TokenTypeError):
        decode_token(token, expect_type="refresh")


def test_expired_token_raises_token_expired_error(jwt_secret, monkeypatch):
    from src.auth import TokenExpiredError, create_access_token, decode_token

    # 把 TTL 压成负数即得一个"签发即过期"的 token。
    # 这样能测过期分支，而不依赖 sleep —— sleep 会让用例变慢且仍可能不稳。
    monkeypatch.setattr(config, "JWT_ACCESS_TTL_MIN", -1)
    token = create_access_token("u-001")
    with pytest.raises(TokenExpiredError):
        decode_token(token)


def test_tampered_signature_raises_token_invalid_error(jwt_secret):
    from src.auth import TokenInvalidError, create_access_token, decode_token

    token = create_access_token("u-001")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(TokenInvalidError):
        decode_token(tampered)


def test_garbage_token_raises_token_invalid_error(jwt_secret):
    from src.auth import TokenInvalidError, decode_token

    with pytest.raises(TokenInvalidError):
        decode_token("显然不是token")


# ==================== 登录（authenticate）====================

def _seed_user(username="analyst1", password="pw-12345678", *, disabled=0):
    from src.auth import hash_password
    from src import db as db_mod

    h, s = hash_password(password)
    db_mod.insert_user(f"u-{username}", username, h, s, role="analyst", disabled=disabled)
    return username, password


def test_authenticate_returns_user_on_correct_password(synth_db):
    from src.auth import authenticate

    username, password = _seed_user()
    user = authenticate(username, password)
    assert user is not None
    assert user["username"] == username
    assert user["role"] == "analyst"
    assert "password_hash" not in user, "登录返回的身份里绝不能带上口令哈希"
    assert "salt" not in user


def test_authenticate_returns_none_for_missing_wrong_or_disabled(synth_db):
    """三种失败必须**无法区分**，否则接口层会用不同文案把用户名枚举出去。"""
    from src.auth import authenticate

    _seed_user(username="analyst1", password="pw-12345678")
    _seed_user(username="blocked1", password="pw-12345678", disabled=1)

    assert authenticate("analyst1", "wrong-password") is None   # 口令错
    assert authenticate("nobody-here", "pw-12345678") is None   # 用户不存在
    assert authenticate("blocked1", "pw-12345678") is None      # 已禁用


# ==================== 种子账号 ====================

def test_ensure_seed_admin_is_idempotent_and_keeps_existing_password(synth_db, monkeypatch):
    from src.auth import ensure_seed_admin, verify_password

    monkeypatch.setattr(config, "SEED_ADMIN_USER", "seedadmin")
    monkeypatch.setattr(config, "SEED_ADMIN_PASSWORD", "seed-pw-12345678")

    assert ensure_seed_admin() != "skipped"
    assert ensure_seed_admin() != "skipped"   # 第二次调用不得报错（幂等）

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, password_hash, salt FROM users WHERE username = ?",
            ("seedadmin",)).fetchall()
    assert len(rows) == 1, f"重复调用建出了 {len(rows)} 个同名账号 —— 幂等性破了"
    assert verify_password("seed-pw-12345678", rows[0]["password_hash"], rows[0]["salt"])

    # 模拟"运维改过口令"，再启动一次：新口令不得被配置里的旧口令顶回去
    from src.auth import hash_password

    new_hash, new_salt = hash_password("运维后来改的口令")
    with db.get_conn() as conn:
        conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
                     (new_hash, new_salt, "seedadmin"))
    ensure_seed_admin()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT password_hash, salt FROM users WHERE username = ?",
            ("seedadmin",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["password_hash"] == new_hash, "启动流程把已改过的口令覆盖回去了"


def test_ensure_seed_admin_skips_when_password_not_configured(synth_db, monkeypatch):
    """口令没配就**不建号**，而不是建一个默认口令的账号。

    在源码里写死 admin/admin123 这类默认凭据是最危险的一类漏洞：
    上线后没人记得改，而任何看过仓库的人都能直接登进来。
    """
    from src.auth import ensure_seed_admin

    monkeypatch.setattr(config, "SEED_ADMIN_USER", "seedadmin")
    monkeypatch.setattr(config, "SEED_ADMIN_PASSWORD", "")

    assert ensure_seed_admin() == "skipped"
    assert db.get_user("seedadmin") is None