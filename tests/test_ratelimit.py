"""Phase 2 速率限制专项测试（FA_RATE_LIMIT_ENABLED=1）。"""
from __future__ import annotations

import os
import time

import pytest


@pytest.fixture(autouse=True)
def _rl_on(monkeypatch):
    monkeypatch.setenv("FA_RATE_LIMIT_ENABLED", "1")
    from src.api import ratelimit
    ratelimit.reset_counters()
    yield
    ratelimit.reset_counters()


@pytest.fixture
def client():
    from src.server import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


class TestRateLimitEnforced:

    def test_auth_login_5_per_minute(self, client):
        """/api/auth/login → 6th request gets 429"""
        path = "/api/auth/login"
        for i in range(5):
            r = client.post(path, json={"username": f"user{i}", "password": "pw"})
            # 可能 200/401（用户不存在），但不能 429
            assert r.status_code != 429, f"第{i+1}次不该 429"
        # 第 6 次
        r = client.post(path, json={"username": "user6", "password": "pw"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        print(f"  429 body: {r.json()}")

    def test_ingest_3_per_minute(self, client):
        """/api/ingest/plan → 4th gets 429"""
        path = "/api/ingest/plan"
        for i in range(3):
            r = client.post(path, json={"text": f"test{i}"})
            assert r.status_code != 429
        r = client.post(path, json={"text": "test4"})
        assert r.status_code == 429

    def test_ask_15_per_minute(self, client):
        """/api/ask 限制较松，前 15 次全过"""
        path = "/api/ask"
        for i in range(15):
            r = client.post(path, json={"question": f"q{i}", "intent": "retrieve"})
            assert r.status_code != 429, f"第{i+1}次不该 429"

    def test_health_not_limited(self, client):
        """/api/health 不在规则表，不限"""
        for i in range(20):
            r = client.get("/api/health")
            assert r.status_code != 429

    def test_v1_path_also_limited(self, client):
        """/api/v1/auth/xxx 也被 rewrite + 限流"""
        for i in range(5):
            r = client.post("/api/v1/auth/login", json={"username": f"v1u{i}", "password": "pw"})
            assert r.status_code != 429
        r = client.post("/api/v1/auth/login", json={"username": "v1u6", "password": "pw"})
        assert r.status_code == 429


class TestRateLimitOffByDefault:

    def test_default_off(self, client):
        """不加 fixture 强制 ON 时，默认 OFF。这里标记 skip，全量 pytest 已覆盖。"""
        pytest.skip("默认 OFF 由全量 pytest 覆盖")
