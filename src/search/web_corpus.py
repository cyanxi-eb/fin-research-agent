"""网络语料的**独立**落盘与独立索引 —— 供"同一个问题再问一次"直接命中缓存。

## 为什么必须独立（不并入年报索引）

并入年报索引会同时污染三样东西（与法规独立索引**同一个理由**）：
1. Step 3/4 的全部评测数字 —— 年报检索会召回网页内容，`page_hit@k` 直接失真；
2. 「语料外实词」闸门 —— 网页里的词会让"全库零出现"这条判据失效（误放行）；
3. 引用口径 —— 网络来源给的是「URL + 抓取时间」，年报给的是「公司+年份+页码+章节」，
   混进同一个列表后"这个数字出自哪"就再也核不实。

独立的代价只是一个目录 + 一个索引文件，收益是**既有结论全部保持可比**。

## 为什么每次入库都全量重建索引

这批数据量级只有几十条（一次兜底最多入 3 条）。全量重建是**确定性的**且代码只有两行，
而增量删改要自己维护"删哪条 / 改哪条 / 索引里如何同步"，一有 bug 就是索引与 JSONL 不一致
（表现为"文件里有这条、检索却命中不了"，且极难看出）。**少量数据不配有增量逻辑。**

## 幂等的主键：URL 规范化后的哈希

同一篇文章会以多种 URL 形态出现（大小写域名、尾斜杠、锚点、utm 参数）。
不做规范化就重复入库，既浪费索引，又会让同一个来源在"独立域名计数"里被算两次 ——
而**跨域名的独立性判定正是交叉验证的全部意义**。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from src import config
from src.search.provider import SearchResult, domain_of, now_stamp

_JSONL_NAME = "web_corpus.jsonl"
# 追踪参数：它们只影响"从哪个渠道点进来的"，同一篇文章因此被当成两条就太蠢了
_TRACKING_PREFIXES = ("utm_", "spm", "from", "src", "share_")


def _jsonl_path() -> Path:
    return Path(config.WEB_CORPUS_DIR) / _JSONL_NAME


def normalize_url(url: str) -> str:
    """URL 规范化：统一小写 scheme/host、去锚点、去尾斜杠、丢掉纯追踪参数。"""
    p = urlparse((url or "").strip())
    query = "&".join(
        kv for kv in p.query.split("&")
        if kv and not any(kv.lower().startswith(pre) for pre in _TRACKING_PREFIXES))
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def chunk_id_for(url: str) -> str:
    """主键 = 规范化 URL 的 sha1 前 16 位（**不是完整 URL**：日志与前端展示都短得多）。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def network_citation(row: dict) -> str:
    """网络引用口径：`标题｜来源（抓取于 时间）` —— **不给页码与章节**。"""
    title = (row.get("title") or "").strip() or row.get("url") or ""
    source = (row.get("source_name") or domain_of(row.get("url") or "")).strip()
    return f"{title}｜{source}（抓取于 {row.get('fetched_at') or '未知时间'}）"


def load_rows() -> list[dict]:
    """读回全部已入库条目（文件不存在 → 空列表，**不是错误**）。"""
    path = _jsonl_path()
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 单行损坏不该让整个语料区不可用（坏行会在校验脚本里暴露）
    return rows


def _rebuild_index(chunks: list[dict]) -> Path | None:
    """全量重建独立索引（`bm25_web.pkl`）。空语料则不建索引文件。"""
    from src.retrieve.bm25 import BM25Index

    if not chunks:
        return None
    return BM25Index.build(chunks).save(Path(config.WEB_BM25_PATH))


def ingest(results: list[SearchResult], *, status: str, query: str) -> dict:
    """把交叉验证后的来源写入网络语料区（幂等）。

    **调用方必须先过交叉验证**：这里不做"该不该入库"的判断（那份判断在 `crossvalidate`），
    否则会出现"两处各有各的入库标准"，而 `status` 参数是**如实记账**用的。
    """
    rows = load_rows()
    seen = {r.get("chunk_id") for r in rows}
    stamp = now_stamp()
    added: list[dict] = []

    for r in results:
        cid = chunk_id_for(r.url)
        if not cid or cid in seen:
            continue                      # 同一 URL 再来一次：不产生第二条
        seen.add(cid)
        added.append({
            "chunk_id": cid,
            "url": r.url,
            "norm_url": normalize_url(r.url),
            "title": r.title,
            "source_name": r.source_name or domain_of(r.url),
            "fetched_at": r.fetched_at or stamp,
            "ingested_at": stamp,
            "text": r.text or r.snippet or "",
            "query": query,
            "cross_status": status,
            "citation": network_citation({
                "title": r.title, "url": r.url,
                "source_name": r.source_name or domain_of(r.url),
                "fetched_at": r.fetched_at or stamp}),
        })

    if added:
        path = _jsonl_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for row in added:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        _rebuild_index(rows + added)

    return {
        "ok": True,
        "ingested": len(added),
        "rows": len(rows) + len(added),
        "cross_status": status,
        "path": str(_jsonl_path()),
    }


def _hit(raw: dict) -> dict:
    """网络检索命中 → 与检索层同形的 hit，但引用字段是**网络口径**。

    **刻意不含 page_no / section**：那两个字段属于年报引用，前端一旦发现它们
    就会把这条当成年报原文渲染，而"这个数字出自第几页"是核不实的。
    """
    c = raw["chunk"]
    return {
        "chunk_id": c.get("chunk_id"),
        "score": raw.get("score"),
        "url": c.get("url"),
        "title": c.get("title"),
        "source_name": c.get("source_name"),
        "fetched_at": c.get("fetched_at"),
        "ingested_at": c.get("ingested_at"),
        "query": c.get("query"),
        "cross_status": c.get("cross_status"),
        "citation": raw.get("citation") or c.get("citation"),
        "text": c.get("text"),
        "chars": len(c.get("text") or ""),
    }


def _lexical_hits(question: str, *, topk: int) -> list[dict]:
    """词面覆盖兜底 —— **专治小语料下 BM25 命中不了**这个真实缺陷。

    为什么必须有：BM25 的 IDF 在"某个词出现在**所有**文档里"时会是**负数**
    （`log(N - df + 0.5) - log(df + 0.5)`，N 小的时候最容易出现），
    而 `BM25Index.search` 会把总分 ≤ 0 的结果直接丢掉。
    网络语料恰好就是"量级只有几条、且同一批入库的条目彼此高度相似"的形态 ——
    也就是说，**在这个缓存最该发挥作用的场景里，BM25 会稳定返回空**。

    判据仍然是确定性的、可解释的：问题里的实词在正文中的**覆盖率**（0~1）排序。
    不复用 BM25 的打分，但复用它的分词与归一化（`content_terms` / `normalize_for_match`），
    所以"什么算一个实词"与全项目其他闸门保持同一口径。
    """
    from src.retrieve.bm25 import content_terms, normalize_for_match

    terms = content_terms(question) or [question.strip()]
    if not terms or not any(terms):
        return []

    out: list[dict] = []
    for row in load_rows():
        body = normalize_for_match(row.get("text") or "")
        if not body:
            continue
        hit = [t for t in terms if normalize_for_match(t) in body]
        if not hit:
            continue
        out.append({
            "chunk": row,
            "score": round(len(hit) / len(terms), 4),
            "signals": {"coverage": round(len(hit) / len(terms), 4), "mode": "lexical_fallback"},
            "citation": row.get("citation") or network_citation(row),
        })
    out.sort(key=lambda h: h["score"], reverse=True)
    return out[:topk]


def query_corpus(question: str, *, topk: int | None = None) -> dict:
    """在**网络语料索引**里检索（不联网、不碰年报索引）。

    索引不存在时返回 `{ok: True, hits: [], note: "网络语料库为空"}`
    —— **空不是错误**：这是"还没人问过这类问题"的正常状态。
    """
    topk = topk or config.WEB_CORPUS_TOP_K
    path = Path(config.WEB_BM25_PATH)
    if not path.exists():
        return {"ok": True, "hits": [], "note": "网络语料库为空", "source": "corpus_cache"}

    from src.retrieve.bm25 import BM25Index

    index = BM25Index.load(path)
    raw_hits = index.search(question, topk=topk)
    if not raw_hits:
        # 小语料下 BM25 的 IDF 可能为负导致"全被过滤"，故补一层词面覆盖兜底（见该函数说明）
        raw_hits = _lexical_hits(question, topk=topk)

    hits = [_hit(h) for h in raw_hits]
    note = "" if hits else "网络语料库里没有命中（该问题可能只完成了联网、未通过交叉验证）"
    return {"ok": True, "hits": hits, "note": note, "source": "corpus_cache"}


def stats() -> dict:
    """给 `/api/health` 用：条数 / 独立域名数 / 最近入库时间。"""
    rows = load_rows()
    domains = {domain_of(r.get("url") or "") for r in rows}
    return {
        "rows": len(rows),
        "domains": len({d for d in domains if d}),
        "last_ingest_at": max((r.get("ingested_at") or "" for r in rows), default=""),
        "path": str(_jsonl_path()),
    }