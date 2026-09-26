"""重排（rerank）—— 用交叉编码器给「问题 × 候选片段」逐对打分，重排融合后的候选。

## 为什么要这一步（而不是"融合完直接取 top5"）

BM25 和向量都是**双塔**结构：问题和文档各自编码，靠向量/词频间接比较。
它们擅长的是**召回**（快、便宜、宁滥勿缺），但对"这段到底答不答得上这个问题"
判断得比较粗 —— 典型表现是召回了同一页里主题相近但结论不同的段落。

交叉编码器把问题和文档**拼在一起**过一遍模型，精度明显更高，代价是**不能预计算**：
候选有多少条就要跑多少次前向。所以标准用法就是"粗排召回 20 条 → 精排取 5 条"。

## 三条硬约束（都是本项目的立场，不是通用做法）

1. **必须可降级**：重排是外部依赖（API 或本地模型），它挂了绝不能让整条问答链路挂。
   任何异常 → **原序直通 + 在 note 里写清为什么**。静默直通不可接受：
   "这次没重排"和"重排了但结果一样"对用户是两件事。
2. **不做绝对分数阈值**：不同 rerank 模型的分数刻度不同（有的 0~1、有的 logits 无界），
   拿一个写死的阈值卡"低于它就丢"，换个模型就会全丢或全留。排序本身才是信号。
3. **降级链要能在 health 里看见**：`status()` 说明当前是哪条通道、为什么不可用。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from src import config

# 本地 CrossEncoder 缓存（首次加载数秒）
_LOCAL_MODEL = None


def _spec(backend: str | None = None) -> dict:
    """解析某条重排通道的调用参数。`backend=None` 时取配置值。

    **为什么必须支持显式传入**：`status()` 原先只看 `config.RERANK_BACKEND`，
    于是调用方把 `backend="api"` 传进来时，可用性判定仍按配置（默认 `passthrough`）
    得出"不可用"→ 直接降级直通。结果是**评测里"开重排"那一档根本没跑到重排**，
    报告上两档数字一模一样，看起来像"重排没用"，实际是它压根没被调用。
    """
    backend = (backend or config.RERANK_BACKEND or "").lower()
    if backend == "api":
        profile = config.LLM_PROVIDERS.get(config.RERANK_PROVIDER) or {}
        return {"backend": "api", "model": config.RERANK_API_MODEL,
                "api_key": profile.get("api_key", ""), "url": config.DASHSCOPE_RERANK_URL}
    if backend == "local":
        return {"backend": "local", "model": config.RERANKER_MODEL}
    return {"backend": "passthrough", "model": None}


def status(backend: str | None = None) -> dict:
    """重排通道状态（给 `/api/health`，或指定通道时问"那条能用吗"）。

    **不可用也不算错** —— 直通是合法状态。
    `backend` 显式传入时按该通道判定（与 `_spec` 同一套优先级），
    这样 `/api/health` 报的是"当前配置的通道"，而调用方也能问"我能不能用 api 通道"。
    """
    backend = (backend or config.RERANK_BACKEND or "").lower()
    if backend not in config.RERANK_BACKENDS:
        return {"available": False, "backend": backend,
                "reason": f"未知的 RERANK_BACKEND「{backend}」，可选 {sorted(config.RERANK_BACKENDS)}"}
    if backend == "passthrough":
        return {"available": False, "backend": backend,
                "reason": "未启用（RERANK_BACKEND=passthrough，候选按融合序直通）"}
    if backend == "api":
        spec = _spec(backend)
        if not str(spec["api_key"] or "").strip():
            return {"available": False, "backend": "api", "model": spec["model"],
                    "reason": f"重排通道 {config.RERANK_PROVIDER} 未配置 API Key"}
        return {"available": True, "backend": "api", "model": spec["model"],
                "reason": "ok", "doc_char_limit": config.RERANK_MAX_DOC_CHARS}
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return {"available": False, "backend": "local", "model": config.RERANKER_MODEL,
                "reason": "本地重排需要 sentence-transformers"}
    return {"available": True, "backend": "local", "model": config.RERANKER_MODEL,
            "reason": "ok"}


# ---------------- API 通道 ----------------

def _api_rerank(query: str, docs: list[str], spec: dict) -> list[dict]:
    """调 DashScope 原生 text-rerank，返回 `[{"index", "relevance_score"}]`。"""
    payload = json.dumps({
        "model": spec["model"],
        "input": {"query": query, "documents": docs},
        # 不传 top_n：传了就等于让服务端替我们截断，而截断规则不受我们控制
        "parameters": {"return_documents": False},
    }).encode("utf-8")
    req = urllib.request.Request(
        spec["url"], data=payload,
        headers={"Authorization": f"Bearer {spec['api_key']}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=config.RERANK_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    results = ((body.get("output") or {}).get("results")) or []
    return [{"index": int(r.get("index", -1)),
             "score": float(r.get("relevance_score", 0.0))} for r in results]


def _local_rerank(query: str, docs: list[str]) -> list[dict]:
    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        from sentence_transformers import CrossEncoder
        _LOCAL_MODEL = CrossEncoder(config.RERANKER_MODEL)
    scores = _LOCAL_MODEL.predict([[query, d] for d in docs])
    return [{"index": i, "score": float(s)} for i, s in enumerate(scores)]


# ---------------- 对外入口 ----------------

def rerank(query: str, hits: list[dict], *, topk: int | None = None,
           backend: str | None = None) -> dict:
    """对 hits 重排。返回 `{"hits", "applied", "backend", "note", "scores"}`。

    **永不抛异常**：任何失败都退化成"原序直通 + note 说明"。
    `hits` 必须带 `text`（交叉编码器要读正文）；没有正文的候选**跳过重排但保留在结果里**，
    并且这一跳过必须在 note 里体现 —— 悄悄少几条候选是最难查的一类 bug。
    """
    backend = (backend or config.RERANK_BACKEND or "passthrough").lower()
    plain = {"hits": list(hits), "applied": False, "backend": backend, "note": None,
             "scores": []}

    if backend == "passthrough" or not hits:
        plain["note"] = ("未启用重排（RERANK_BACKEND=passthrough），"
                         "候选按融合顺序直通。") if hits else None
        return plain
    if backend not in config.RERANK_BACKENDS:
        plain["note"] = f"未知的重排通道「{backend}」，已直通。"
        return plain

    st = status(backend)          # **问的是"这条通道能用吗"**，不是"当前配置了什么"
    if not st.get("available"):
        plain["note"] = f"重排不可用（{st.get('reason')}），候选按融合顺序直通。"
        return plain

    # 只重排前 N 条（重排是逐对计算，候选越多越慢；深层候选本来也进不了 topk）
    head = hits[: config.RERANK_TOP_N]
    tail = hits[config.RERANK_TOP_N:]
    docs = [str(h.get("text") or "")[: config.RERANK_MAX_DOC_CHARS] for h in head]
    missing = [i for i, d in enumerate(docs) if not d]

    spec = _spec(backend)
    t0 = time.monotonic()
    try:
        if spec["backend"] == "api":
            rows = _api_rerank(query, docs, spec)
        else:
            rows = _local_rerank(query, docs)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        plain["note"] = f"重排调用失败（HTTP {e.code} {detail}），候选按融合顺序直通。"
        return plain
    except Exception as e:   # noqa: BLE001 —— 重排失败一律降级，绝不因它挂掉问答
        plain["note"] = f"重排调用失败（{type(e).__name__}: {e}），候选按融合顺序直通。"
        return plain

    # 服务端可能只返回部分结果（或顺序打乱）→ **按 index 回映射**，不假设它按序返回
    by_index = {r["index"]: r["score"] for r in rows if 0 <= r["index"] < len(head)}
    if not by_index:
        plain["note"] = "重排返回空结果，候选按融合顺序直通。"
        return plain

    # 有分数的按分数降序；**没返回分数的保持原相对顺序排在后面**（而不是丢掉）
    scored = sorted(by_index, key=lambda i: (-by_index[i], i))
    unscored = [i for i in range(len(head)) if i not in by_index]
    ordered = [head[i] for i in scored] + [head[i] for i in unscored]
    out = []
    for i in scored:
        item = dict(head[i])
        item["rerank_score"] = round(by_index[i], 6)
        item["signals"] = {**item.get("signals", {}), "rerank": round(by_index[i], 6)}
        out.append(item)
    for i in unscored:
        out.append(dict(head[i]))
    out.extend(dict(h) for h in tail)

    note = (f"已用 {spec['backend']} 重排 {len(scored)}/{len(head)} 条"
            f"（{config.RERANK_API_MODEL if spec['backend'] == 'api' else config.RERANKER_MODEL}，"
            f"{time.monotonic() - t0:.2f}s）")
    if missing:
        note += f"；其中 {len(missing)} 条候选无正文，未参与重排但保留在结果末尾"
    if unscored:
        note += f"；{len(unscored)} 条未被服务端打分，按原相对顺序保留"
    return {"hits": out[:topk] if topk else out, "applied": True, "backend": spec["backend"],
            "note": note, "scores": [round(by_index[i], 6) for i in scored]}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    print("status:", json.dumps(status(), ensure_ascii=False))
    demo = [
        {"chunk_id": "A", "text": "本年度公司毛利率为 91.93%，较上年提升 0.1 个百分点。"},
        {"chunk_id": "B", "text": "公司召开第八届董事会第十次会议，审议通过年度报告。"},
        {"chunk_id": "C", "text": "研发费用同比增加 12%，主要系新产品开发投入加大。"},
    ]
    res = rerank("贵州茅台2024年的毛利率是多少", demo)
    print("applied:", res["applied"], "| backend:", res["backend"])
    print("note:", res["note"])
    for i, h in enumerate(res["hits"], start=1):
        print(f"  {i}. {h['chunk_id']} rerank={h.get('rerank_score')} {h['text'][:40]}")
