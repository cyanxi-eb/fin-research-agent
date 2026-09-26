"""兜底节点 `websearch` —— 「年报库里查不到」之后才联网。

## 为什么是节点，而不是新意图

"库里没有答案"这件事**只有检索之后才知道**（`answer.synthesize` 的三道闸门给出
`no_evidence / out_of_corpus / low_coverage`）。把它做成意图，等于要求路由器在检索前
就判断出来 —— 做不到；真要硬做，只能把路由表、`_INTENTS`、`/api/health` 的
`intents` 断言、`test_router.py` 全线改一遍，收益为零。
挂在 `verify` 之后的兜底位置，则**一行路由代码都不用动**。

## 顺序是刻意的：先查缓存 → 再联网 → 再交叉验证 → 一致才入库

1. **先查网络语料缓存**：同一问题第二次提问不该再打一次对方接口（省调用、也更快）。
   命中就 `web.source="corpus_cache"` 并且**完全不联网**。
2. **未命中且开关打开**才联网：拿搜索结果、抓**前 N 页正文**（成本控制见 `fetch.py`）。
3. **交叉验证**：≥2 个独立域名且关键数字无冲突才算 `consistent`（判据见 `crossvalidate.py`）。
4. **只有 `consistent` 才入库**：不一致时只并列展示差异，不入库 ——
   否则"两来源说法不同"的那个错数字会被缓存下来，从此每次提问都命中它。

## 异常一律吞掉，但**必须留痕**

联网这件事的失败面很宽：对方改页面、被限流、DNS 不通、超时。
这些都不该让一次**已经算好的本地拒答**变成 500。
所以任何一步异常都写进 `web.note` 并落一条 `web_search` 审计 ——
"这次没能联网"必须看得见（`note` 在前端是可折叠展示的，见 C6）。
"""
from __future__ import annotations

import json
import time

from src import audit as audit_mod
from src import config
from src.search import crossvalidate, web_corpus
from src.search import fetch as fetch_mod
from src.search import provider as provider_mod
from src.search.provider import domain_of, now_stamp


def web_search_enabled(state: dict) -> bool:
    """开关判定：调用方显式指定优先，否则跟随 `config.WEB_SEARCH_ENABLED`。"""
    flag = (state or {}).get("web_search")
    return config.WEB_SEARCH_ENABLED if flag is None else bool(flag)


def should_websearch(state: dict) -> bool:
    """该不该走联网兜底：**拒答** + 拒答原因是"本地资料里没有" + 开关打开。

    三个条件缺一不可：
    - 没拒答（正常回答了）就没必要联网，那只会引入不可核验的第二套说法；
    - `refusal_reason` 不在 `WEB_TRIGGER_REASONS` 里（如 `model_insufficient`）
      说明问题不在"资料里没有"，联网也无助于回答；
    - 开关关闭时（离线部署 / 单测 / 用户主动关）一律不联网。
    """
    if not (state or {}).get("refused"):
        return False
    if state.get("refusal_reason") not in config.WEB_TRIGGER_REASONS:
        return False
    return web_search_enabled(state)


def should_websearch_after_verify(state: dict) -> bool:
    """`verify` 之后的兜底判定 —— **含"人工确认优先"，全项目唯一一份**。

    与 `should_websearch` 只差一条，但很关键：**挂起中的流程不去联网**。
    让人对着一个"还没确认"的结论等网络请求，恢复后结论还会变两次。
    主图的条件边（`builder._after_verify`）与流式路径（手工跑节点）都调这一个函数 ——
    两处各写一遍"该不该联网"，迟早会出现"图里跑了、流式没跑"这种最难查的漂移。
    """
    if ((state or {}).get("hitl") or {}).get("pending"):
        return False
    return should_websearch(state)


# ==================== 每日配额（无 Key 通道被大量调用会被限流/封禁）====================

def quota_snapshot() -> dict:
    """`{date, used, limit, remaining}` —— 供 `/api/health` 与节点内部判断共用。"""
    today = time.strftime("%Y-%m-%d")
    used = 0
    path = config.WEB_SEARCH_QUOTA_PATH
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date") == today:
                used = int(data.get("used") or 0)
    except Exception:              # noqa: BLE001 —— 配额文件损坏不该让问答失败
        used = 0
    return {"date": today, "used": used, "limit": config.WEB_SEARCH_DAILY_QUOTA,
            "remaining": max(0, config.WEB_SEARCH_DAILY_QUOTA - used)}


def _quota_consume() -> None:
    snap = quota_snapshot()
    path = config.WEB_SEARCH_QUOTA_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": snap["date"], "used": snap["used"] + 1}),
                    encoding="utf-8")


# ==================== 组装回复用的字段 ====================

def _citation_of(url: str, title: str, source_name: str, fetched_at: str) -> dict:
    """**网络引用口径**：title / url / source_name / fetched_at（+ 可读的 citation 串）。

    刻意不含 `page_no` / `section`：那是年报引用口径，混进来前端就会把网络来源
    当年报原文渲染，而"这个数字出自哪一页"是核不实的。
    """
    return {
        "title": title,
        "url": url,
        "source_name": source_name or domain_of(url),
        "fetched_at": fetched_at,
        "citation": web_corpus.network_citation({
            "title": title, "url": url,
            "source_name": source_name or domain_of(url), "fetched_at": fetched_at}),
    }


def _brief(result) -> dict:
    """搜索结果摘要（给前端展示用）。**不含正文** —— 正文会让响应体膨胀几倍。"""
    return {"title": result.title, "url": result.url, "snippet": result.snippet,
            "source_name": result.source_name, "fetched_at": result.fetched_at}


def _citations_from_results(results: list) -> list[dict]:
    """每个独立域名一条（同一家媒体的多篇不重复列，与交叉验证的独立性口径一致）。"""
    seen: dict[str, dict] = {}
    for r in results:
        d = domain_of(r.url) or r.source_name
        if d in seen:
            continue
        seen[d] = _citation_of(r.url, r.title, r.source_name, r.fetched_at)
    return list(seen.values())


def _citations_from_hits(hits: list[dict]) -> list[dict]:
    """缓存命中时的引用：直接用入库时记下的网络引用口径。"""
    out: list[dict] = []
    for h in hits:
        out.append(_citation_of(h.get("url") or "", h.get("title") or "",
                                h.get("source_name") or "", h.get("fetched_at") or ""))
    return out


def _blank(question: str, reason: str | None) -> dict:
    return {
        "attempted": True, "enabled": True, "source": None, "provider": None,
        "query": question, "results": [], "cross_validation": None,
        "ingested": False, "citations": [], "note": "",
        "fetched_pages": 0, "duration_ms": 0,
        "refusal_reason": reason,
    }


def websearch_node(state: dict) -> dict:
    """兜底节点：产出 `state["web"]`（**唯一的产出**，不覆盖 answer / refused）。"""
    question = (state.get("question") or "").strip()
    reason = state.get("refusal_reason")
    web = _blank(question, reason)
    started = time.monotonic()

    if not web_search_enabled(state):
        web.update(attempted=False, enabled=False,
                   note="联网搜索已关闭（FA_WEB_SEARCH_ENABLED=0 或本次请求显式关闭）")
    else:
        try:
            _run(web, question)
        except Exception as e:                   # noqa: BLE001 —— 见模块 docstring
            web["source"] = web.get("source") or "live"
            web["note"] = (f"联网搜索失败（{type(e).__name__}: {e}）："
                           f"本次仍按本地结论拒答，不做任何推测。")

    _finish(web, started)
    # 每次跑过都留一条痕：验收要看"第二次提问命中缓存、没有再次联网"，
    # 靠的就是审计里 `web_search` 记录的 source 字段（live / corpus_cache）。
    audit_mod.log("web_search", target=question, actor=None, detail={
        "source": web.get("source"), "provider": web.get("provider"),
        "distinct_domains": (web.get("cross_validation") or {}).get("distinct_domains"),
        "status": (web.get("cross_validation") or {}).get("status"),
        "ingested": web.get("ingested"), "note": web.get("note"),
        "refusal_reason": reason, "duration_ms": web.get("duration_ms"),
    })
    return {"web": web}


def _finish(web: dict, started: float) -> dict:
    web["duration_ms"] = int((time.monotonic() - started) * 1000)
    return web


def _run(web: dict, question: str, *, limit: int | None = None) -> None:
    """缓存 → 联网 → 抓正文 → 交叉验证 → 入库（顺序见模块 docstring）。"""
    cached = web_corpus.query_corpus(question, topk=config.WEB_CORPUS_TOP_K)
    if cached.get("hits"):
        web.update(source="corpus_cache", provider=None,
                   citations=_citations_from_hits(cached["hits"]),
                   note="命中网络语料缓存：本地已存有与本题相关的网络来源，本次未再联网。")
        return

    snap = quota_snapshot()
    if snap["remaining"] <= 0:
        web["note"] = (f"今日联网配额已用尽（{snap['used']}/{snap['limit']}），"
                       f"本次未联网；本地拒答结论不变。")
        return

    provider = provider_mod.get_provider()
    if provider is None:
        web.update(enabled=False,
                   note="联网搜索通道已禁用（FA_WEB_SEARCH_BACKEND=none），本次未联网。")
        return

    web["provider"] = provider.name
    web["source"] = "live"
    results = provider.search(question, limit=limit or config.WEB_SEARCH_MAX_RESULTS)
    _quota_consume()
    web["results"] = [_brief(r) for r in results]
    if not results:
        web["note"] = "联网搜索没有返回任何结果。"
        return

    problems = fetch_mod.fetch_texts(results, max_pages=config.WEB_SEARCH_MAX_PAGES)
    web["fetched_pages"] = sum(1 for r in results if r.text)
    web["citations"] = _citations_from_results(results)

    # 正文已由 fetch_texts 灌好，故 fetcher=None：交叉验证这一步**不再发起请求**
    verdict = crossvalidate.cross_validate(question, results, fetcher=None)
    web["cross_validation"] = verdict
    if problems:
        verdict["note"] = f"{verdict['note']}（{len(problems)} 个来源正文抓取失败）"

    if verdict["status"] == "consistent":
        ing = web_corpus.ingest(results, status=verdict["status"], query=question)
        web["ingested"] = bool(ing.get("ingested"))
        web["note"] = (f"交叉验证一致（{verdict['distinct_domains']} 个独立域名）："
                       f"已入库 {ing.get('ingested', 0)} 条，下次提问可直接命中缓存。"
                       if web["ingested"] else
                       f"交叉验证一致（{verdict['distinct_domains']} 个独立域名），"
                       f"但来源此前已入库，本次未新增条目。")
    else:
        web["ingested"] = False
        # 不一致 / 证据不足：**只并列展示**、不入库，并在 note 里写明分歧点
        web["note"] = (f"交叉验证未通过（{verdict['status']}）：{verdict['note']} "
                       f"本次只并列展示来源，不入库。")

    # 入库/未入库都记一下"抓取时间"，便于与年报引用区分（网络引用的时间口径）
    web["checked_at"] = now_stamp()


# ==================== 手动触发与健康状态（/api/web/search 与 /api/health 用）====================

def _provider_available() -> tuple[bool, str | None]:
    """通道是否可用：**只做构造与配置检查，不发起任何请求**。

    这个字段存在的理由就是"静默降级"：`enabled=true` 但 `provider_available=false`
    意味着每一次兜底都会失败，而响应里只会留一句 note。健康检查里必须看得见。
    """
    try:
        p = provider_mod.get_provider()
    except Exception as e:                     # noqa: BLE001 —— 缺 Key / 通道名写错都算不可用
        return False, f"{type(e).__name__}: {e}"
    if p is None:
        return False, "通道已禁用（FA_WEB_SEARCH_BACKEND=none）"
    return True, None


def web_status() -> dict:
    """`/api/health` 的 `web` 段：开关 / 通道 / 语料规模 / 配额。"""
    available, why = _provider_available()
    st = web_corpus.stats()
    return {
        "enabled": config.WEB_SEARCH_ENABLED,
        "backend": config.WEB_SEARCH_BACKEND,
        "provider_available": available,
        "provider_reason": why,
        "corpus_rows": st.get("rows"),
        "corpus_domains": st.get("domains"),
        "last_ingest_at": st.get("last_ingest_at"),
        "quota": quota_snapshot(),
        "min_domains": config.WEB_SEARCH_MIN_DOMAINS,
        "trigger_reasons": sorted(config.WEB_TRIGGER_REASONS),
    }


def search_now(question: str, *, limit: int | None = None) -> dict:
    """手动跑一次联网检索（`GET /api/web/search`），**复用节点同一条路径**。

    刻意不另写一份"手动版"：缓存优先、配额、抓正文、交叉验证、一致才入库 ——
    这些行为必须与自动兜底**逐字一致**，否则人工验证通过的东西上线后行为不同。

    **总开关仍然生效**：一个能被 GET 绕过的开关不算开关。开关关闭时
    `enabled=false` 且不发起任何请求（通道级禁用走 `FA_WEB_SEARCH_BACKEND=none`）。
    """
    q = (question or "").strip()
    started = time.monotonic()
    web = _blank(q, None)
    failed = False

    if not config.WEB_SEARCH_ENABLED:
        web.update(attempted=False, enabled=False,
                   note="联网搜索总开关已关闭（FA_WEB_SEARCH_ENABLED=0）：本次未联网。")
    else:
        try:
            _run(web, q, limit=limit)
        except Exception as e:                 # noqa: BLE001 —— 与节点同口径
            failed = True
            web["source"] = web.get("source") or "live"
            web["note"] = f"联网搜索失败（{type(e).__name__}: {e}）：本次不做任何推测。"

    _finish(web, started)
    # `ok` = "这一次确实跑起来了"，而不是"拿到结果"：没结果也是正常业务（见 note）
    web["ok"] = (not failed) and bool(web.get("enabled"))
    return web