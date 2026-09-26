"""联网端点（Phase 0-2 逐路由组迁移第三批）。

**纯 verbatim copy**：从 server.py 复制过来，不改任何逻辑。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src import audit as audit_mod
from src import auth as auth_mod
from src.graph.websearch_node import search_now
from src.search import web_corpus

router = APIRouter()


# --- 辅助函数（verbatim from server.py） ---

def _row_brief(row: dict) -> dict:
    """列表里的条目只给正文**前 400 字**：正文动辄数万字符，全量返回会让响应体涨几倍。"""
    out = dict(row)
    text = out.get("text") or ""
    out["text"] = text[:400]
    out["chars"] = len(text)
    out["truncated"] = len(text) > 400
    return out


# --- 端点（verbatim from server.py，@app → @router） ---

@router.get("/api/web/search")
def api_web_search(q: str = Query(..., min_length=1, description="搜索词"),
                   limit: int | None = Query(None, ge=1, le=20,
                                             description="覆盖本次最大结果数"),
                   user: dict = Depends(auth_mod.current_user)) -> dict:
    """手动触发一次联网检索 —— **与自动兜底走同一条路径**
    （先查缓存 → 配额 → 联网 → 抓正文 → 交叉验证 → 只有一致才入库）。

    刻意不另写一套"手动版"：人工验证通过的行为必须与自动兜底逐字相同。
    总开关 `FA_WEB_SEARCH_ENABLED=0` 时 `ok=false` 且**不发起任何请求**。
    """
    web = search_now(q, limit=limit)
    audit_mod.log("web_search", target=q.strip(), actor=user.get("sub"), detail={
        "manual": True, "source": web.get("source"), "provider": web.get("provider"),
        "distinct_domains": (web.get("cross_validation") or {}).get("distinct_domains"),
        "status": (web.get("cross_validation") or {}).get("status"),
        "ingested": web.get("ingested"), "note": web.get("note"),
        "duration_ms": web.get("duration_ms"),
    })
    return {
        "ok": web.get("ok"),
        "enabled": web.get("enabled"),
        "provider": web.get("provider"),
        "source": web.get("source"),
        "results": web.get("results") or [],
        "cross_validation": web.get("cross_validation"),
        "ingested": bool(web.get("ingested")),
        "corpus_cache": web.get("source") == "corpus_cache",
        "fetched_pages": web.get("fetched_pages"),
        "duration_ms": web.get("duration_ms"),
        "note": web.get("note"),
    }


@router.get("/api/web/corpus")
def api_web_corpus(limit: int = Query(20, ge=1, le=200),
                   user: dict = Depends(auth_mod.current_user)) -> dict:
    """已入库的网络语料条目（**独立语料区**，与年报 / 法规索引互不干扰）。"""
    rows = web_corpus.load_rows()
    return {
        "ok": True,
        "stats": web_corpus.stats(),
        "total": len(rows),
        "count": len(rows[:limit]),
        "rows": [_row_brief(r) for r in rows[:limit]],
    }
