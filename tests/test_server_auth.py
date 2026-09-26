"""鉴权在 HTTP 面上的用例（TestClient，不联网）。

为什么单独一个文件而不是塞进 test_server.py：
`tests/conftest.py` 把 `FA_AUTH_ENABLED` 钉成 0，让既有 369 条用例跑**免鉴权**形态；
而鉴权本身的行为必须**开着的时候**才看得见。把"开鉴权"的用例集中在这里，
既有用例一条都不用改，也不会因为某天有人顺手改掉开关而集体静默失效。

三条不能错的事：
1. 业务端点没 token 必须 **401**，且带 `WWW-Authenticate` 头（OAuth2 客户端的重试约定）；
2. `/api/health` 与 `/api/auth/login` **永远公开** —— 否则探活要鉴权、拿 token 要 token，互相死锁；
3. 带合法 token 的业务请求行为与免鉴权形态**完全一致**（鉴权不改业务语义）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config, llm
from src.graph import checkpoint as ckpt
from src.retrieve import pipeline

# 与 test_server.py 同一套离线夹具：不调模型、不联网、不碰真实库
HIT = {"chunk_id": "600519-2024-p32-1", "code": "600519", "company": "贵州茅台",
       "year": 2024, "report_type": "annual", "section": "公司治理", "page_no": 32,
       "part": 1, "parts_total": 1, "text": "本报告期内公司治理结构未发生重大变化。",
       "citation": "贵州茅台2024年年报 P32 公司治理"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.server import app

    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    ckpt.reset()
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "单测强制离线"))
    monkeypatch.setattr(pipeline, "retrieve", lambda *a, **k: {
        "ok": True, "hits": [HIT], "mode": "bm25", "filters": {},
        "stats": {"max_score": 9.0}, "absent_terms": [], "note": None})
    with TestClient(app) as c:
        yield c
    ckpt.reset()


@pytest.fixture
def auth_on(monkeypatch):
    """打开鉴权。

    在**请求期**改 `config.AUTH_ENABLED` 就能生效，前提是 auth 层在调用时读它
    （而不是在导入时快照成常量）—— 这条本身就是个契约，A3 必须照此实现。
    """
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "JWT_SECRET", "unit-test-secret-not-for-production")
    return True


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _register(client, username, password):
    return client.post("/api/auth/register", json={"username": username, "password": password})


# ==================== 401 与公开端点 ====================

def test_business_endpoint_without_token_is_401_with_www_authenticate(client, auth_on):
    r = client.post("/api/ask", json={"question": "贵州茅台2024年的营业总收入是多少"})
    assert r.status_code == 401, "开了鉴权还让无 token 的请求进业务端点 = 白做"
    assert "www-authenticate" in {k.lower() for k in r.headers}, \
        "401 必须带 WWW-Authenticate（OAuth2 客户端靠它决定要不要去换 token）"


def test_protected_read_endpoints_without_token_are_401(client, auth_on):
    for method, path in (("get", "/api/audit"), ("get", "/api/citations"),
                         ("get", "/api/compare?indicator=ROE&codes=600519,000858"),
                         ("get", "/api/hitl/nonexistent")):
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} 没被保护"


def test_health_and_auth_endpoints_stay_public(client, auth_on, synth_db):
    """健康检查与注册/登录必须公开：否则探活失败 + 拿 token 要 token 的死锁。"""
    assert client.get("/api/health").status_code == 200
    assert _register(client, "pubuser", "pw-12345678").status_code == 200
    assert _login(client, "pubuser", "pw-12345678").status_code == 200


# ==================== 注册 / 登录 / 刷新 ====================

def test_login_returns_token_pair_and_user(client, auth_on, synth_db):
    _register(client, "alice", "pw-12345678")
    r = _login(client, "alice", "pw-12345678")
    assert r.status_code == 200
    body = r.json()
    for key in ("access_token", "refresh_token", "token_type", "expires_in", "user"):
        assert key in body, f"登录响应缺 {key}"
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == "alice"
    assert "password" not in str(body).lower(), "响应里不能出现口令相关字段"


def test_login_with_wrong_password_is_401_and_same_message_as_unknown_user(client, auth_on, synth_db):
    _register(client, "alice", "pw-12345678")
    wrong = _login(client, "alice", "not-the-password")
    unknown = _login(client, "no-such-user", "pw-12345678")
    assert wrong.status_code == 401 and unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"], \
        "两种失败的文案必须一致，否则可以靠文案枚举出哪些用户名存在"


def test_register_rejects_short_password_and_duplicate_username(client, auth_on, synth_db):
    assert _register(client, "bob", "123").status_code == 422
    assert _register(client, "bob", "pw-12345678").status_code == 200
    assert _register(client, "bob", "pw-12345678").status_code == 409


def test_refresh_returns_new_access_token(client, auth_on, synth_db):
    _register(client, "carol", "pw-12345678")
    pair = _login(client, "carol", "pw-12345678").json()
    r = client.post("/api/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["access_token"]
    # 拿 access token 当 refresh 用必须被拒（类型闸门）
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": pair["access_token"]}).status_code == 401


# ==================== 带 token 的正常通路 ====================

def test_valid_token_allows_ask_and_actor_comes_from_token(client, auth_on, synth_db):
    _register(client, "dave", "pw-12345678")
    token = _login(client, "dave", "pw-12345678").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.post("/api/ask", json={"question": "贵州茅台2024年的营业总收入是多少",
                                      "actor": "我自称是别人"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["intent"]


def test_me_reports_current_identity_and_logout_is_honest(client, auth_on, synth_db):
    _register(client, "erin", "pw-12345678")
    token = _login(client, "erin", "pw-12345678").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["username"] == "erin"

    out = client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 200
    # 语义必须诚实：JWT 无状态，服务端并没有"吊销"这个 token
    assert "无状态" in out.json()["note"] or "stateless" in out.json()["note"].lower()
    assert client.get("/api/auth/me", headers=headers).status_code == 200, \
        "若这里变成 401，说明代码在谎称已吊销 —— 本轮没有黑名单机制"

    # 坏 token 一律 401（HTTP 头只能是 ASCII，故用一段假 token 而不是中文）
    assert client.get("/api/auth/me",
                      headers={"Authorization": "Bearer not.a.real.token"}).status_code == 401


def test_auth_disabled_keeps_anonymous_access(client, synth_db):
    """免鉴权形态（conftest 的默认）必须继续可用，且 health 如实报告 enabled=false。"""
    assert client.get("/api/health").json()["auth"]["enabled"] is False
    assert client.post("/api/ask", json={"question": "贵州茅台2024年的营业总收入是多少",
                                         "intent": "analysis"}).status_code == 200