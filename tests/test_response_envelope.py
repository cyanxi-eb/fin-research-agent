"""Phase 0-2 响应归一专项测试（FA_RESPONSE_ENVELOPE=1 时）。

这组测试验证开关 ON 时的壳格式、错误码映射、trace_id 透传、skip 规则。
默认 OFF 时这组测试应该跳过（因为 envelope 不会启用）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _envelope_on(monkeypatch):
    """本组测试强制启用 envelope。"""
    monkeypatch.setenv("FA_RESPONSE_ENVELOPE", "1")


@pytest.fixture
def client():
    """轻量 TestClient — envelope 专项不需要 mock 掉所有外部依赖。

    health 端点不涉及外部服务（除了 llm.is_ready，它默认 False）。
    """
    from src.server import app
    with TestClient(app) as c:
        yield c


# --- 成功响应包壳 ---
class TestSuccessEnvelope:

    def test_health_endpoint_is_wrapped(self, client):
        """GET /api/health 200 → {code:0, msg:"ok", data:{...}, trace_id}"""
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()

        assert set(body.keys()) == {"code", "msg", "data", "trace_id"}
        assert body["code"] == 0
        assert body["msg"] == "ok"
        assert body["trace_id"]

        data = body["data"]
        assert "app" in data
        assert "vector" in data

    def test_trace_id_from_custom_header(self, client):
        """X-Request-Id header 透传到信封 trace_id"""
        custom_id = "test-trace-abc123"
        r = client.get("/api/health", headers={"X-Request-Id": custom_id})
        assert r.json()["trace_id"] == custom_id


# --- Skip 规则 ---
class TestSkipRules:

    def test_root_html_not_wrapped(self, client):
        """/ 返回 index.html，静态文件不包壳"""
        r = client.get("/")
        assert r.status_code == 200
        assert "application/json" not in r.headers.get("content-type", "")

    def test_static_not_wrapped(self, client):
        """/favicon.svg 不包壳"""
        r = client.get("/favicon.svg")
        assert r.status_code == 200
        assert "application/json" not in r.headers.get("content-type", "")
