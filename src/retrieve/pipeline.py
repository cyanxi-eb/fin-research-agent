"""统一检索入口 —— 全项目**唯一**的"句子 → hits"出口。

三条链路共用这一个函数，由 `mode` 决定走哪条：

| mode | 链路 |
|---|---|
| `bm25`   | 过滤 → BM25 单路 → topk（Step 3 的行为，作为对照基线保留） |
| `hybrid` | 过滤 → BM25 + 向量双路召回 → RRF 融合 → 重排 → topk（Step 4） |

## 为什么从一开始就定这个接口（而不是在节点里直接调 BM25）

1. **能做出"同一批问题 Step3 vs Step4"的对比实验** —— 而"混合检索比纯字面检索好在哪、
   好多少"正是这套系统最有说服力的部分。接口不统一，两次实验的输入就对不齐。
2. **降级路径天然存在**：向量通道/重排不可用时，`hybrid` 会**自动退回 BM25 并在
   `degraded` + `note` 里说明原因** —— 演示环境断网也能跑完整链路，而且"这次降级了"
   是可见的，不会伪装成"混合检索就是这个效果"。
3. **过滤条件必须在打分之后、截断之前应用**（见 `BM25Index.search` 的实现说明），
   否则"限定公司后取 top5"会静默变成"全局 top5 里筛出 2 条"。

## 返回结构的约定

`hits` 是**统一形状**（`normalize_hit` 定义），BM25 路与向量路产出一模一样。
为什么一定要统一：下游（答案合成、引用格式化、Step 5 引用校验）只关心那几个字段，
让它们去分辨"这条是向量来的、那条是 BM25 来的"会把检索实现泄漏到每一层。

`ok=False` **只表示检索本身没法做**（索引缺失、模式非法）。"没召回任何东西"是
`ok=True` + `hits=[]` —— 那是**结论**（没证据），不是故障，上层据此拒答；
把两者混成一个错误码，会让"拒答"看起来像"系统坏了"。
"""
from __future__ import annotations

import time

from src import config
from src.retrieve import fusion, filters, rerank as rerank_mod, vector as vector_mod
from src.retrieve.bm25 import BM25Index, content_terms, normalize_for_match

# 索引单例：加载一次 7MB 的 pickle 要几百毫秒，每个问题都重载太浪费。
# `reset_index()` 供测试与"重建索引后热更新"使用。
_INDEX: BM25Index | None = None
# chunk_id → chunk 的查找表（向量路只存元数据，正文回查这里）
_CHUNK_STORE: dict[str, dict] | None = None
# 全语料拼成的归一化文本（只给"词在全库出现过吗"这类判定用；惰性构建 + 缓存）
_CORPUS_TEXT: str | None = None


def get_index() -> BM25Index:
    """取（惰性加载的）BM25 索引单例。"""
    global _INDEX
    if _INDEX is None:
        _INDEX = BM25Index.load()
    return _INDEX


def chunk_store() -> dict[str, dict]:
    """`chunk_id → chunk`。向量路靠它把元数据补成完整 hit（含正文与引用文案）。

    **正文只有一个来源**（BM25 索引持有的 chunk 列表）。向量库里刻意不存正文：
    正文是本项目演进最快的字段，存两份必然不一致，而不一致的正文会让
    "引用指向 P87、片段内容却是别的页"这种错误出现 —— 那比召不回来严重得多。
    """
    global _CHUNK_STORE
    if _CHUNK_STORE is None:
        _CHUNK_STORE = {c.get("chunk_id"): c for c in get_index().chunks}
    return _CHUNK_STORE


def reset_index() -> None:
    """丢掉缓存（测试 / 重建索引后调用）。"""
    global _INDEX, _CHUNK_STORE, _CORPUS_TEXT
    _INDEX = None
    _CHUNK_STORE = None
    _CORPUS_TEXT = None


def corpus_text() -> str:
    """全部 chunk 正文拼成的**归一化**长串（惰性构建后缓存）。

    用途只有一个：判断"某个词在全语料里出现过吗"。
    归一化用 `normalize_for_match`（去空白 + 转小写），
    与覆盖度判定**同一套语义** —— 两边不一致会出现
    "覆盖度认为没命中、语料检查认为有"这种自相矛盾的结论。

    实测成本：2650 chunk → 约 100 万字符，构建 0.2s，之后 `in` 查询毫秒级。
    """
    global _CORPUS_TEXT
    if _CORPUS_TEXT is None:
        _CORPUS_TEXT = normalize_for_match(
            " ".join(c.get("text") or "" for c in get_index().chunks))
    return _CORPUS_TEXT


def absent_corpus_terms(terms: list[str]) -> list[str]:
    """哪些实词在**整个语料**里一次都没出现（不是"这一段没命中"）。

    与覆盖度的区别很关键：覆盖度衡量"召回的证据够不够支持回答"，
    这里衡量"问题问的东西在不在我们的资料范围内"。
    前者是**证据不足**，后者是**范围之外** —— 拒答时给用户的解释完全不同。
    """
    if not terms:
        return []
    hay = corpus_text()
    return [t for t in terms if normalize_for_match(t) not in hay]


def available_years(code: str | None = None) -> list[int]:
    """索引里实际存在的年份。

    用途很具体：用户问"2024 年"但库里只有 2025 时，**必须明说"库里没有 2024"**，
    而不是让过滤条件把结果清空 —— 后者会被读成"公司没这项数据"，
    是金融问答里最不能接受的一类误导。
    """
    try:
        years = {c.get("year") for c in get_index().chunks
                 if (code is None or c.get("code") == code) and c.get("year")}
    except FileNotFoundError:
        return []
    return sorted(int(y) for y in years)


def index_stats() -> dict:
    """索引规模（给 /api/health 与冒烟脚本报数用）。缺失索引时返回可读的说明。"""
    try:
        return {"ok": True, **get_index().stats()}
    except FileNotFoundError as e:
        return {"ok": False, "error": "index_missing", "message": str(e)}


def normalize_hit(raw: dict) -> dict:
    """把 BM25 的原始 hit 摊平成检索层的**统一 hit 形状**。

    为什么要摊平而不是把整个 chunk 塞进去：下游（答案合成、引用格式化、
    Step 5 的引用校验）只关心"这几样东西"，把 chunk 原样透出会让每层的字段
    依赖变成隐式的 —— 换个检索实现就得跟着改。**统一形状是让 Step 4 能插进来**的前提。
    """
    c = raw["chunk"]
    return {
        "chunk_id": c.get("chunk_id"),
        "score": raw.get("score"),
        "signals": {k: raw[k] for k in ("bm25", "phrase_hits") if k in raw},
        # 溯源四件套：缺一个都拼不出引用
        "code": c.get("code"),
        "company": c.get("company"),
        "year": c.get("year"),
        "page_no": c.get("page_no"),
        "section": c.get("section"),
        "part": c.get("part"),
        "parts_total": c.get("parts_total"),
        "citation": raw.get("citation"),
        "text": c.get("text"),
        "chars": c.get("chars"),
    }


def page_key(hit: dict) -> tuple:
    """"哪一页"的唯一键。**必须带 code/year** —— 不同公司的第 13 页是两页。"""
    return (hit.get("code"), hit.get("year"), hit.get("page_no"))


def apply_page_quota(hits: list[dict], per_page: int | None = None) -> list[dict]:
    """同页配额：按名次扫描，同一页最多保留 `per_page` 块。`0/None` = 不限。

    放在**截断之前**（`hits[:topk]`），否则配额只会把已经选好的名单缩短，
    起不到"把名额让给其他页"的作用。

    这条变换有个可证明的性质：**top-k 覆盖的页面集合只会变大，不会变小**。
    因为页面的第 1 块永远不被丢弃，而第 j 个被选中的块在原名次上必然 ≥ j，
    所以"第 1 块落在原始 top-k"的页面必然仍在配额后的 top-k 里。
    → 对本项目最关心的 `page_hit@k` 而言，**它只可能持平或上升**。
    （单测 `tests/test_retrieval.py::test_page_quota_*` 守这条性质。）
    """
    q = config.RETRIEVE_MAX_PER_PAGE if per_page is None else per_page
    if not q or q <= 0:
        return list(hits)
    seen: dict[tuple, int] = {}
    out: list[dict] = []
    for h in hits:
        k = page_key(h)
        n = seen.get(k, 0)
        if n >= q:
            continue          # 该页配额已满：跳过它，把名额留给后面的页
        seen[k] = n + 1
        out.append(h)
    return out


def _materialize(vector_hits: list[dict]) -> tuple[list[dict], list[str]]:
    """向量候选（只有元数据）→ 统一 hit 形状。返回 `(hits, 丢失的 chunk_id)`。

    丢失是**真会发生**的情况：向量库建好之后又重建了 BM25/chunk（换了年份、补了公司），
    两边就不同源了。这时不能把没有正文的候选塞进 hits —— 没有正文就无法拼引用，
    引用不到出处的内容在本项目里没有价值。丢弃 + 记录 + 在 note 里说出来。
    """
    from src import citation as citation_mod

    store = chunk_store()
    hits: list[dict] = []
    orphans: list[str] = []
    for vh in vector_hits:
        cid = vh.get("chunk_id")
        c = store.get(cid)
        if c is None:
            orphans.append(str(cid))
            continue
        hits.append({
            "chunk_id": cid,
            # 这里的 score 是**该路自己的分**，融合阶段会被替换成 RRF 分
            "score": vh.get("cosine"),
            "signals": {"cosine": vh.get("cosine"), "vector_rank": vh.get("rank")},
            "code": c.get("code"), "company": c.get("company"), "year": c.get("year"),
            "page_no": c.get("page_no"), "section": c.get("section"),
            "part": c.get("part"), "parts_total": c.get("parts_total"),
            "citation": citation_mod.format_citation(c),
            "text": c.get("text"), "chars": c.get("chars"),
        })
    return hits, orphans


def _hybrid(question: str, *, topk: int, code: str | None, year: int | None,
            section: str | None, rerank_backend: str | None,
            max_per_page: int | None = None) -> dict:
    """混合检索：双路召回 → RRF → 重排。**任何一步降级都不抛异常，只记进 note。**"""
    debug: dict = {"routes": {}, "degraded": False, "notes": [], "timings": {}}

    def note(msg: str) -> None:
        if msg and msg not in debug["notes"]:
            debug["notes"].append(msg)

    # ---- 路 1：BM25（永不缺席 —— 没有它连正文都取不到）----
    t0 = time.monotonic()
    raw_bm25 = get_index().search(question, topk=config.RECALL_TOPN,
                                  code=code, year=year, section=section)
    bm25_hits = [normalize_hit(h) for h in raw_bm25]
    debug["timings"]["bm25_ms"] = round((time.monotonic() - t0) * 1000, 1)
    debug["routes"]["bm25"] = len(bm25_hits)

    # ---- 路 2：向量（可降级）----
    vector_hits: list[dict] = []
    vstatus = vector_mod.status()
    if not vstatus.get("available"):
        debug["degraded"] = True
        note(f"向量通道不可用（{vstatus.get('reason')}）→ 已降级为纯 BM25 检索。")
    else:
        t1 = time.monotonic()
        try:
            vector_hits = vector_mod.get_index().search(
                question, topk=config.RECALL_TOPN, code=code, year=year, section=section)
        except Exception as e:   # noqa: BLE001 —— 召回失败一律降级，绝不因它挂掉问答
            debug["degraded"] = True
            note(f"向量召回失败（{type(e).__name__}: {e}）→ 已降级为纯 BM25 检索。")
            vector_hits = []
        debug["timings"]["vector_ms"] = round((time.monotonic() - t1) * 1000, 1)
        debug["routes"]["vector"] = len(vector_hits)

    # ---- 融合：只用"两路都能引用"的候选 ----
    # 先把向量候选补成完整 hit（缺正文的丢掉），再融合 —— 顺序很关键：
    # 若先融合再丢弃，被丢掉的那些仍然占据了 RRF 名次，会让后面本该进 topk 的
    # 片段被挤出去（融合结果里有一堆"不可用"的名额）。
    mat_vec, orphans = _materialize(vector_hits)
    if orphans:
        debug["orphans"] = len(orphans)
        note(f"向量库有 {len(orphans)} 条候选在 chunk 存储里找不到对应正文"
             f"（向量库与 chunk 库不同源）→ 已丢弃这些候选，建议重建向量库："
             f"python scripts/index_vector.py --force")
    if not vector_hits:
        # "向量路空手而归"与"通道不可用"是两件事，必须分开记：
        # 前者是**正常的检索结论**（语义上确实不相似），后者是故障。
        # 混为一谈的话，排查"为什么召回变少了"时根本分不清方向。
        debug["notes"].append("向量路未召回任何候选。")

    # ---- 融合：**只融合两路都能引用到的候选** ----
    # 顺序很关键：先把向量候选补成完整 hit（缺正文的丢掉）、再融合。
    # 若反过来（先融合再丢弃），被丢掉的候选仍然占着 RRF 名次，
    # 会把本该进 topk 的片段挤出去 —— 融合结果里留下一堆"不可用"的名额。
    fuse_in = {"bm25": [{"chunk_id": h["chunk_id"]} for h in bm25_hits],
               "vector": [{"chunk_id": h["chunk_id"]} for h in mat_vec]}
    fused = fusion.fuse(fuse_in)
    by_bm: dict[str, dict] = {h["chunk_id"]: h for h in bm25_hits}
    by_vec: dict[str, dict] = {h["chunk_id"]: h for h in mat_vec}
    # 融合后统一用 **RRF 分**做 score（两路原始分不同量纲，直接混着暴露会误导），
    # 各路原始分留在 signals 里供排查。
    hits = []
    for item in fused:
        cid = item["chunk_id"]
        base = dict(by_bm.get(cid) or by_vec[cid])
        # **两路的信号要合并、不能覆盖**：一条被两路都召回的片段，若只留后写入的那份，
        # "它是共识项"这件事就丢了 —— 而共识正是 RRF 排序的依据，排查时最需要看到它。
        signals: dict = {}
        for src in (by_vec.get(cid), by_bm.get(cid)):
            if src:
                signals.update(src.get("signals") or {})
        signals.update({"rrf": item["rrf_score"], "ranks": item["ranks"]})
        base["score"] = item["rrf_score"]
        base["signals"] = signals
        hits.append(base)
    debug["fused"] = len(fused)
    debug["explain"] = fusion.explain(fused, fuse_in, limit=5)

    # ---- 重排（可降级）----
    t2 = time.monotonic()
    rr = rerank_mod.rerank(question, hits, topk=None, backend=rerank_backend)
    hits = rr["hits"]
    debug["timings"]["rerank_ms"] = round((time.monotonic() - t2) * 1000, 1)
    debug["rerank"] = {"applied": rr["applied"], "backend": rr["backend"]}
    if rr.get("note"):
        note(rr["note"])

    # 截断放在重排之后：重排需要**看得到足够多的候选**才有意义
    kept = apply_page_quota(hits, max_per_page)
    debug["page_quota"] = {"per_page": (config.RETRIEVE_MAX_PER_PAGE
                                       if max_per_page is None else max_per_page),
                           "dropped": len(hits) - len(kept)}
    hits = kept[:topk]
    return {"hits": hits, "debug": debug}


def _apply_auto_filter(question: str, code: str | None, year: int | None,
                       notes: list[str], debug: dict,
                       history: list[dict] | None = None) -> tuple[str | None, int | None]:
    """从问题里补过滤条件（显式传参优先，只补 None 的那些）。

    `history` 交给 `filters.resolve_entities` —— 多轮场景下"它 / 该公司"这类问句
    在本轮抽不到公司时，可从最近一轮沿用（规则见 `filters.resolve_entities`）。
    """
    if code and year:
        return code, year
    det = filters.resolve_entities(question, history)
    for n in det.get("notes") or []:
        if n not in notes:
            notes.append(n)
    if debug is not None:
        debug["auto_filter"] = {k: det[k] for k in
                                ("code", "company", "year", "years_seen", "filtered") if k in det}
    if not code and det.get("code"):
        # 用户显式传了 code 就不覆盖：显式参数的优先级高于文本推断
        code = det["code"]
    if not year and det.get("year"):
        year = det["year"]
    return code, year


def retrieve(question: str, *, topk: int | None = None,
             code: str | None = None, year: int | None = None,
             section: str | None = None,
             mode: str | None = None,
             auto_filter: bool = True,
             rerank_backend: str | None = None,
             max_per_page: int | None = None,
             history: list[dict] | None = None) -> dict:
    """检索入口。返回：

    ```
    {"ok", "error", "mode", "question", "filters", "hits", "stats", "note",
     "degraded", "debug"}
    ```

    `auto_filter=True` 时会从问题里抽公司/年份补成过滤条件 —— 这是对 Step 3
    「30% 引用落在题面年份之外」的直接修复（见 `filters.py` 的说明）。
    显式传入的 `code/year` 优先级更高，不会被文本推断覆盖。

    `max_per_page` 覆盖同页配额（`None` = 用 `config.RETRIEVE_MAX_PER_PAGE`；
    **`0` = 关闭配额**）。留这个参数是为了让评测能把"只加配额"单独隔离出来 ——
    一个开关若能靠环境变量在评测中间改，那两次跑测的就不是同一件事了。
    """
    mode = (mode or config.RETRIEVE_MODE or "bm25").lower()
    notes: list[str] = []
    debug: dict = {"notes": [], "degraded": False}
    filters_used = {"code": code, "year": year, "section": section, "topk": topk,
                    "max_per_page": (config.RETRIEVE_MAX_PER_PAGE
                                     if max_per_page is None else max_per_page)}

    if mode not in config.RETRIEVE_MODES:
        return {"ok": False, "error": "unknown_mode", "mode": mode,
                "message": f"未知检索模式「{mode}」", "known_modes": sorted(config.RETRIEVE_MODES),
                "question": question, "filters": filters_used, "hits": [],
                "absent_terms": [], "stats": {},
                "degraded": False, "debug": debug}

    if auto_filter:
        code, year = _apply_auto_filter(question, code, year, notes, debug, history)
        filters_used.update(code=code, year=year)

    try:
        index = get_index()
    except FileNotFoundError as e:
        return {"ok": False, "error": "index_missing", "mode": mode,
                "message": str(e), "question": question, "filters": filters_used,
                "hits": [], "absent_terms": [], "stats": {},
                "degraded": False, "debug": debug}

    # 年份在库里不存在时**必须说出来**：否则过滤后的空结果会被读成「公司没有这项数据」
    if year and code:
        years = available_years(code)
        if years and int(year) not in years:
            notes.append(
                f"库中 {code} 只有 { '、'.join(str(y) for y in years) } 年的报告，"
                f"**没有 {year} 年** —— 本次检索限定在 {year}，因此没有结果。"
                f"（这不是「公司没有该数据」，而是我们没入库该年份）")

    # ---- 语料外实词：问题问的东西在不在资料范围内 ----
    # 与覆盖度是**两件事**：覆盖度说"证据不够"，这里说"范围之外"。
    # 拒答时给用户的解释完全不同，所以要分开算、分开记。
    absent: list[str] = []
    try:
        absent = absent_corpus_terms(content_terms(question))
    except Exception as e:   # noqa: BLE001 —— 这只是"多一条线索"，不该影响检索本身
        debug["absent_error"] = f"{type(e).__name__}: {e}"
    if absent:
        debug["absent_terms"] = absent
        notes.append(f"问题里的「{ '、'.join(absent) }」在全部已入库年报中**从未出现**"
                     f" —— 通常意味着所问内容不在披露范围内，或用了口语化说法。")

    if mode == "bm25":
        want = topk or config.RETRIEVE_TOPK
        quota = config.RETRIEVE_MAX_PER_PAGE if max_per_page is None else max_per_page
        # 有配额时多取候选，否则配额无处施展 —— 它的作用正是"把名额让给其他页"，
        # 只取 want 条的话同页分块已经把名单占满了，没有可让的对象。
        fetch = max(want, config.RECALL_TOPN) if (quota or 0) > 0 else want
        raw_hits = index.search(question, topk=fetch, code=code, year=year, section=section)
        all_hits = [normalize_hit(h) for h in raw_hits]
        kept = apply_page_quota(all_hits, quota)
        debug["routes"] = {"bm25": len(all_hits)}
        debug["page_quota"] = {"per_page": quota, "dropped": len(all_hits) - len(kept)}
        hits = kept[:want]
        debug["notes"] = notes + debug["notes"]
    else:
        res = _hybrid(question, topk=topk or config.RETRIEVE_TOPK, code=code, year=year,
                      section=section, rerank_backend=rerank_backend,
                      max_per_page=max_per_page)
        hits = res["hits"]
        debug = {**res["debug"], "auto_filter": debug.get("auto_filter")}
        debug["degraded"] = bool(debug.get("degraded"))
        notes = notes + [n for n in (debug.get("notes") or []) if n not in notes]

    if not hits and not any("没有结果" in n for n in notes):
        notes.append("未召回任何片段。可能原因：① 问题提到的公司/指标不在库内；"
                     "② 过滤条件过窄（指定了 code/year/section）；"
                     "③ 该说法在年报原文里没有对应表述。"
                     "**这不代表'公司没有这项数据'** —— 只在原文里找不到表述。")

    debug["notes"] = notes + [n for n in (debug.get("notes") or []) if n not in notes]
    return {"ok": True, "error": None, "mode": mode, "question": question,
            "filters": filters_used, "hits": hits,
            # 语料外实词：给上层的"范围之外"拒答闸门用（见 answer.synthesize 闸门 0）
            "absent_terms": absent,
            "stats": {"returned": len(hits),
                      "max_score": hits[0]["score"] if hits else None,
                      "routes": debug.get("routes") or {},
                      "rerank": debug.get("rerank")},
            "note": "\n".join(notes) if notes else None,
            "degraded": bool(debug.get("degraded")),
            "debug": debug}


def _context_blocks(hits: list[dict], max_chars: int) -> list[str]:
    """按预算拼上下文块（`[i] 引用\\n正文`），**截断按整块丢，不切半句**（半句会误导模型）。"""
    blocks: list[str] = []
    used = 0
    for i, h in enumerate(hits, start=1):
        block = f"[{i}] {h['citation']}\n{h['text']}"
        if used + len(block) > max_chars and blocks:
            break
        blocks.append(block)
        used += len(block)
    return blocks


def render_context(hits: list[dict], max_chars: int | None = None) -> str:
    """把 hits 拼成给 LLM 的上下文，**每段前面带引用编号**。

    编号必须与最终答案里的 `[1] [2]` 对齐 —— 让模型"引用编号"而不是"自己写页码"，
    是防它编造出处的关键：模型只能选我们给它的编号，选不出不存在的页码。
    """
    return "\n\n".join(_context_blocks(hits, max_chars or config.ANSWER_CONTEXT_CHARS))


def rendered_count(hits: list[dict], max_chars: int | None = None) -> int:
    """**实际交给模型**的片段数，也就是模型被允许引用的最大编号。

    为什么必须单独有这个名字：引用校验若拿 `len(hits)` 当上界，模型报一个
    超出上下文的编号（如 `[8]`）就会被当成合法引用 —— 而我们根本没把第 8 段给它看。
    那不是模型编造出处，是**我们替模型伪造了出处**，性质更坏。
    所以校验的上界必须来自"送进去了几段"，而不是"召回了几个"。
    """
    return len(_context_blocks(hits, max_chars or config.ANSWER_CONTEXT_CHARS))


if __name__ == "__main__":
    # 自检：python -m src.retrieve.pipeline "毛利率" [--code 600519] [--topk 3] [--mode hybrid]
    import json
    import sys

    args = sys.argv[1:]
    q = args[0] if args and not args[0].startswith("--") else "贵州茅台2024年的毛利率是多少"
    c = args[args.index("--code") + 1] if "--code" in args else None
    k = int(args[args.index("--topk") + 1]) if "--topk" in args else 5
    m = args[args.index("--mode") + 1] if "--mode" in args else None

    print("index stats:", json.dumps(index_stats(), ensure_ascii=False))
    print("vector     :", json.dumps(vector_mod.status(), ensure_ascii=False))
    print("rerank     :", json.dumps(rerank_mod.status(), ensure_ascii=False))

    for mode in ([m] if m else ["bm25", "hybrid"]):
        res = retrieve(q, topk=k, code=c, mode=mode)
        print(f"\n===== mode={mode} ok={res['ok']} hits={len(res['hits'])} "
              f"degraded={res['degraded']} =====")
        print("filters:", json.dumps(res["filters"], ensure_ascii=False))
        for i, h in enumerate(res["hits"], start=1):
            sig = h.get("signals") or {}
            print(f"  [{i}] score={h['score']} cos={sig.get('cosine')} {h['citation']}")
        if res.get("note"):
            print("note:", res["note"])
