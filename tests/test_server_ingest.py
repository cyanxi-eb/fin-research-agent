"""数据入库向导的 HTTP 面用例（TestClient，不联网）。

三条闸门不能错（设计 D3）：
1. ingest 端点没票 **401**（挂在鉴权上，与其它业务端点同口径）；
2. `commit` 是管理动作：analyst 票 **403**、admin 票 200 —— 403 与 401 是两件事
   （"没登录"和"登录了但不够格"混在一起会误导前端重定向登录页）；
3. `/api/health` **仍然公开**。

wizard 层一律 monkeypatch（不抓数、不调 LLM、不写库）——
wizard 自身的语义由 tests/test_ingest_wizard.py 覆盖，这里只验 HTTP 面。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import config, llm
from src.graph import checkpoint as ckpt


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.server import app

    monkeypatch.setattr(config, "CHECKPOINT_DB_PATH", tmp_path / "ckpt.db")
    ckpt.reset()
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "单测强制离线"))
    with TestClient(app) as c:
        yield c
    ckpt.reset()


@pytest.fixture
def auth_on(monkeypatch):
    """打开鉴权（与 test_server_auth.py 同一套做法：请求期改 config 即生效）。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "JWT_SECRET", "unit-test-secret-not-for-production")


@pytest.fixture
def wizard_stub(monkeypatch):
    """wizard 层假实现：plan/preview/commit 全部替身，HTTP 面只验路由与闸门。"""
    from src.ingest.wizard import IngestPlan

    plan = IngestPlan(companies=[{"code": "600519", "name": "贵州茅台"}],
                      indicators=["营业总收入"], periods=3)
    monkeypatch.setattr("src.ingest.wizard.parse_request",
                        lambda text, llm_model=None, pool=None:
                        {"plan": plan, "note": None})
    monkeypatch.setattr("src.ingest.wizard.preview",
                        lambda plan: {"companies": [
                            {"code": "600519", "name": "贵州茅台", "error": None,
                             "periods": ["2024-12-31"], "rows": []}]})
    monkeypatch.setattr("src.ingest.wizard.commit",
                        lambda plan, selected, actor:
                        {"batch": "fake-batch", "rows": len(selected), "companies": []})


# ==================== 闸门：401 / 403 / 公开端点 ====================

def test_health_still_public_with_auth_on(client, auth_on):
    assert client.get("/api/health").status_code == 200


def test_ingest_endpoints_without_token_are_401(client, auth_on):
    for path, payload in (
            ("/api/ingest/plan", {"text": "把茅台加进来"}),
            ("/api/ingest/preview", {"companies": []}),
            ("/api/ingest/commit", {"plan": {"companies": []}, "selected": []})):
        r = client.post(path, json=payload)
        assert r.status_code == 401, f"{path} 没被保护"


def test_commit_as_analyst_is_403(client, auth_on, synth_db):
    """analyst 登录了但不够格 → 403（不是 401，不该把人踹回登录页）。"""
    from src import auth as auth_mod

    assert client.post("/api/auth/register",
                       json={"username": "analyst1", "password": "pw-12345678"}).status_code == 200
    token = client.post("/api/auth/login",
                        json={"username": "analyst1",
                              "password": "pw-12345678"}).json()["access_token"]

    r = client.post("/api/ingest/commit",
                    json={"plan": {"companies": [], "indicators": [], "ratios": [],
                                   "periods": None, "source": "eastmoney",
                                   "unresolved": []},
                          "selected": []},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert "管理员" in r.json()["detail"]


def test_commit_as_admin_is_200(client, auth_on, synth_db, wizard_stub):
    """admin 票 commit 200；plan/preview 登录即可（读库/抓预览不算管理动作）。"""
    from src import auth as auth_mod

    headers = {"Authorization": "Bearer " + auth_mod.create_access_token(
        "user-1", role="admin", username="alice")}

    r = client.post("/api/ingest/plan", json={"text": "把茅台近3年加进来"}, headers=headers)
    assert r.status_code == 200 and r.json()["plan"]["companies"][0]["code"] == "600519"

    r = client.post("/api/ingest/preview",
                    json={"companies": [{"code": "600519", "name": "贵州茅台"}],
                          "indicators": [], "ratios": [], "periods": None,
                          "source": "eastmoney", "unresolved": []}, headers=headers)
    assert r.status_code == 200 and r.json()["companies"][0]["code"] == "600519"

    r = client.post("/api/ingest/commit",
                    json={"plan": {"companies": [{"code": "600519", "name": "贵州茅台"}],
                                   "indicators": [], "ratios": [], "periods": None,
                                   "source": "eastmoney", "unresolved": []},
                          "selected": [["600519", "2024-12-31", "营业总收入"]]},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 1 and r.json()["batch"]


def test_ingest_endpoints_usable_without_auth(client, wizard_stub):
    """免鉴权形态（单机演示 / Step 8 手测形态）三个端点都可用 ——
    require_admin 在 AUTH_ENABLED=0 时放行，否则向导在免登录形态是死路。"""
    assert client.post("/api/ingest/plan", json={"text": "x"}).status_code == 200
    assert client.post("/api/ingest/preview", json={"companies": []}).status_code == 200
    r = client.post("/api/ingest/commit",
                    json={"plan": {"companies": []}, "selected": [["600519", "2024-12-31", "营业总收入"]]})
    assert r.status_code == 200 and r.json()["rows"] == 1
