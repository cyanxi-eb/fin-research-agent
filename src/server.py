"""FastAPI 服务入口（Phase 0-2：路由已分层到 src/api/routes/）。

启动：
```
uvicorn src.server:app --reload --port 8000
```

路由文件：
  src/api/routes/{auth,ask,web,compare,ingest,audit}.py — 各导出 `router`
  src/server.py — 只负责 app 生命周期 + 根路由 + health + middleware + router 装配

**设计纪律**：会阻塞的端点用同步 `def`（FastAPI 自动丢线程池）；
只有 SSE `/api/ask/stream` 是 async（留在事件循环里逐段写）。
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import audit as audit_mod
from src import auth as auth_mod
from src import config, db, llm
from src.api.routes import ask as ask_route
from src.api.routes import audit as audit_route
from src.api.routes import auth as auth_route
from src.api.routes import compare as compare_route
from src.api.routes import ingest as ingest_route
from src.api.routes import web as web_route
from src.graph import checkpoint as ckpt
from src.graph import router as router_mod
from src.graph.websearch_node import web_status
from src.retrieve import pipeline, regulation, rerank as rerank_mod, vector as vector_mod

APP_VERSION = "0.9.0"

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
FRONTEND_INDEX = WEB_DIR / "index.html"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if config.AUTH_ENABLED:
        auth_mod.require_jwt_secret()
        db.init_schema()
        print(f"[auth] 演示账号：{auth_mod.ensure_seed_admin()}"
              f"（用户 {config.SEED_ADMIN_USER}）")
    else:
        print("[auth] FA_AUTH_ENABLED=0：业务端点不校验令牌（单机/单测形态）。")
    yield


app = FastAPI(
    title="Fin Research Agent",
    version=APP_VERSION,
    description="年报问答与分析 Agent（可核验溯源）",
    lifespan=lifespan,
)


# ==================== RequestId middleware ====================
# 每个请求分配或透传一个 trace_id（HTTP header X-Request-Id），写入 response header。
# 零破坏性：测试不因 header 存在而失败；后续可在 route 里通过 request.state.request_id
# 取到它，用于审计日志的统一关联键。
@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request_id = (
        request.headers.get("x-request-id")
        or request.headers.get("x-trace-id")
        or uuid.uuid4().hex
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


# --- Phase 1 React 前端静态资源 + SPA fallback ---
# 旧单文件 index.html 已由 Vite 产物替换（web/assets/index-*.js + index.html）
# /api/* 路由组在上方已显式注册，优先匹配；这里只处理前端静态 + SPA fallback

# === Phase 0-2: 迁移到 src/api/routes/ 的路由组 ===
app.include_router(auth_route.router)
app.include_router(ask_route.router)
app.include_router(web_route.router)
app.include_router(compare_route.router)
app.include_router(ingest_route.router)
app.include_router(audit_route.router)

# === Phase 2: 新增会话/书签/入库批次路由组 ===
from src.api.routes import sessions as sessions_route
from src.api.routes import bookmarks as bookmarks_route
from src.api.routes import ingest_batches as ingest_batches_route
app.include_router(sessions_route.router)
app.include_router(bookmarks_route.router)
app.include_router(ingest_batches_route.router)

@app.get("/assets/{fname}")
def serve_asset(fname: str) -> FileResponse:
    fpath = WEB_DIR / "assets" / fname
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail=f"静态资源不存在：{fname}")
    return FileResponse(str(fpath))


@app.get("/favicon.svg")
def serve_favicon() -> FileResponse:
    return FileResponse(str(WEB_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/")
def api_index() -> FileResponse:
    """前端根 — 返回 Vite 产物 index.html。"""
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=404, detail="前端未构建：web/index.html 不存在")
    return FileResponse(str(FRONTEND_INDEX), media_type="text/html")


# ==================== health endpoint ====================
@app.get("/api/health")
def api_health() -> dict:
    """服务自检：进程、索引、法规库、检索两路通道、大模型通道、Checkpointer、审计、鉴权。

    为什么要报这么多通道状态：这些组件**都能在不报错的情况下静默降级**
    （向量库没建 → hybrid 退回纯 BM25；重排 Key 没配 → 直通；Checkpointer 换了后端 →
    挂起流程读不回）。降级本身是设计好的行为，但如果健康检查里看不到，
    降级就会长期静默 —— 表现为"效果一般/有时对有时不对"，而没人知道原因。
    """
    ready, why = llm.is_ready()
    state = llm.get_state()
    return {
        "ok": True,
        "app": app.title,
        "version": app.version,
        "pid": os.getpid(),
        "index": pipeline.index_stats(),
        "regulation": regulation.stats(),
        "llm": {
            "provider": state["provider"],
            "model": state["model"],
            "ready": ready,
            "reason": None if ready else why,
        },
        "vector": vector_mod.status(),
        "rerank": rerank_mod.status(),
        "checkpointer": ckpt.status(),
        "hitl": {"enabled": config.HITL_ENABLED,
                 "min_confidence": config.HITL_MIN_CONFIDENCE,
                 "triggers": ["citation_unsupported", "low_confidence", "numeric_mismatch"]},
        "audit": audit_mod.stats(),
        "auth": auth_mod.status(),
        "web": web_status(),
        "frontend": {"available": FRONTEND_INDEX.exists(), "path": str(FRONTEND_INDEX)},
        "retrieve_modes": sorted(config.RETRIEVE_MODES),
        "retrieve_mode_default": config.RETRIEVE_MODE,
        "intents": sorted(router_mod.INTENTS),
    }


# ==================== SPA fallback（必须放最后，所有具体路由先匹配） ====================
@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> FileResponse:
    """SPA fallback：任何不是 /api/* /assets/* /favicon.svg 的路径都回 index.html。

    这样浏览器直接访问 http://host/compare 时，前端 Router 能正常接管；
    /api/* 已被上方的 include_router 先匹配，不会走到这里。
    """
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(status_code=404, detail=f"API 端点不存在：/{full_path}")
    if not FRONTEND_INDEX.exists():
        raise HTTPException(status_code=404, detail="前端未构建：web/index.html 不存在")
    return FileResponse(str(FRONTEND_INDEX), media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
