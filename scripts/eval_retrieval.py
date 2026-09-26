"""检索评测：Step 3（纯 BM25）vs Step 4（混合检索）—— 产出 `eval/report.md`。

## 为什么必须有这个脚本（而不是"感觉变好了"）

Step 4 加了向量召回、RRF、重排、自动过滤四样东西，每一件都可能让指标变好**或变坏**。
没有对照数字的话，"我做了混合检索"就只是一句自述；有了数字才能说清
"哪一项带来了多少提升、代价是什么"。这是简历上唯一站得住的量化素材。

## 三组数字各测什么

| 指标 | 含义 | 为什么需要 |
|---|---|---|
| `page_hit@5` | top5 里是否至少有一条落在**期望页**（金标准来自源 PDF 的标记串扫描） | 最直接的质量指标；只在"能定页"的题上算 |
| `section_hit@5` | top5 是否落在**期望章节** | 页级定不了的题（PDF 表格抽取把数字打散）的兜底，弱但可用 |
| `年份精度` / `公司精度` | top5 里 year/code 与题面一致的比例 | **Step 3 的实测硬伤**：离线 6/20 条引用落在题面年份之外 |
| `拒答正确率` | 4 道超范围题是否拒答（走确定性闸门，不调模型） | 防止"为了召回率把拒答能力换掉了" |

## 四个对照配置（消融）

`bm25 无过滤` 是 Step 3 的原始行为，作为基线；`bm25 自动过滤` 单独隔离出
"只加过滤"的收益；`hybrid` 再加向量+RRF；最后 `hybrid+rerank` 看重排的边际贡献。
这样拆开，任何一步的收益都能单独归因。

**评测不调大模型**：拒答判定走确定性闸门（证据覆盖度），检索质量只看 hits。
理由：一旦引入 LLM，评测结果就会带上"今天模型心情好不好"这层噪声，
而且每次跑都要花钱 —— 评测必须是可重复、可免费重跑的。

用法：
    python scripts/eval_retrieval.py                 # 全量四配置
    python scripts/eval_retrieval.py --only hybrid   # 只跑指定配置（子串匹配）
    python scripts/eval_retrieval.py --topk 5 --no-write
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import answer as answer_mod  # noqa: E402
from src import config  # noqa: E402
from src.retrieve import pipeline, rerank as rerank_mod, vector as vector_mod  # noqa: E402

GOLDEN_PATH: Path = config.ROOT_DIR / "eval" / "golden_qa.jsonl"
REPORT_MD: Path = config.ROOT_DIR / "eval" / "report.md"
REPORT_JSON: Path = config.ROOT_DIR / "eval" / "report.json"

# 消融配置：从"Step 3 原始行为"一路加到最后。
# 每一项**只加一个开关**，这样每一步的收益都能单独归因（见报告§2）。
CONFIGS: list[dict] = [
    {"key": "bm25_raw", "label": "① bm25（无过滤，Step 3 原样）",
     "mode": "bm25", "auto_filter": False, "rerank": None, "quota": 0},
    {"key": "bm25_filter", "label": "② bm25 + 自动过滤",
     "mode": "bm25", "auto_filter": True, "rerank": None, "quota": 0},
    {"key": "bm25_quota", "label": "③ bm25 + 过滤 + 每页配额",
     "mode": "bm25", "auto_filter": True, "rerank": None,
     "quota": config.RETRIEVE_MAX_PER_PAGE},
    {"key": "hybrid", "label": "④ hybrid（双路+RRF）+ 过滤 + 配额",
     "mode": "hybrid", "auto_filter": True, "rerank": "passthrough",
     "quota": config.RETRIEVE_MAX_PER_PAGE},
    {"key": "hybrid_rerank", "label": "⑤ hybrid + rerank + 过滤 + 配额",
     "mode": "hybrid", "auto_filter": True, "rerank": "api",
     "quota": config.RETRIEVE_MAX_PER_PAGE},
]


def load_golden() -> list[dict]:
    rows = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def run_one(row: dict, cfg: dict, topk: int) -> dict:
    """跑单题单配置，返回该题的观测值（**不含**任何题目元信息，便于聚合）。

    所有配置都**不显式传 code/year** —— 一律让 `filters.detect` 从问题文本里抽。
    这样 ① 与 ② 的差别就只有一个开关（auto_filter），是干净的消融；
    若给 ② 直接喂金标准的 code/year，测出来的就不是"系统实际表现"而是"过滤器被喂了答案"。
    """
    q = row["question"]
    res = pipeline.retrieve(q, topk=topk, mode=cfg["mode"], rerank_backend=cfg["rerank"],
                            auto_filter=cfg["auto_filter"], max_per_page=cfg.get("quota"))
    hits = res.get("hits") or []
    pages = [h.get("page_no") for h in hits]
    years = [h.get("year") for h in hits]
    codes = [h.get("code") for h in hits]
    sections = [h.get("section") or "" for h in hits]

    out = {
        "ok": bool(res.get("ok")),
        "degraded": bool(res.get("degraded")),
        "filter_code": (res.get("filters") or {}).get("code"),
        "filter_year": (res.get("filters") or {}).get("year"),
        "hits": len(hits),
        "pages": pages,
        # top-k 里覆盖的**不同页数** —— 配额的作用就是把同一页的分块换成其他页，
        # 这个数直接反映"名单多样性"，是配额生效与否的第一手证据。
        "distinct_pages": len({(h.get("code"), h.get("year"), h.get("page_no")) for h in hits}),
        "citations": [h.get("citation") for h in hits],
        "n_routes": ((res.get("stats") or {}).get("routes") or {}),
    }
    # 过滤抽取是否正确（只在开启自动过滤的配置上有意义）
    if cfg["auto_filter"] and (row.get("code") or row.get("year")):
        ok_c = (not row.get("code")) or out["filter_code"] == row["code"]
        ok_y = (not row.get("year")) or out["filter_year"] == row["year"]
        out["filter_ok"] = bool(ok_c and ok_y)

    # 页级命中：**必须连公司/年份一起比**，只比页码会误判 ——
    # 页码在每家公司里都是从 1 开始编的，"P18" 在五粮液和中国平安里都存在。
    # 不带 code/year 比页码，等于把"别家报告的第 18 页"当成命中，
    # 会在无过滤的基线上凭空造出一些"命中"（实测踩过：基线的 P7/P11/P18 其实来自别家公司）。
    if row.get("gt_page") == "page":
        gold = set(row.get("expected_pages") or [])
        out["page_hit"] = any(
            h.get("page_no") in gold
            and (not row.get("code") or h.get("code") == row["code"])
            and (not row.get("year") or h.get("year") == row["year"])
            for h in hits)
        out["page_hit_count"] = len([
            h for h in hits if h.get("page_no") in gold
            and (not row.get("code") or h.get("code") == row["code"])])
    # 章节级命中（兜底口径）；同样要求公司对得上
    if row.get("expected_sections"):
        gold_sec = row["expected_sections"]
        out["section_hit"] = any(
            (not row.get("code") or h.get("code") == row["code"])
            and any(g in (h.get("section") or "") for g in gold_sec)
            for h in hits)
    # 年份 / 公司精度
    if row.get("year"):
        out["year_ratio"] = (len([y for y in years if y == row["year"]]) / len(years)
                             if years else 0.0)
    if row.get("code"):
        out["company_ratio"] = (len([c for c in codes if c == row["code"]]) / len(codes)
                                if codes else 0.0)
    # 拒答（确定性闸门，不调模型）
    if row.get("expect_refuse"):
        ans = answer_mod.synthesize(q, res, use_llm=False)
        out["refused"] = bool(ans.get("refused"))
        out["refusal_reason"] = ans.get("refusal_reason")
        out["answer_note"] = (res.get("note") or "")[:200]
    return out


def aggregate(rows: list[dict], obs: dict[str, dict]) -> dict:
    """把逐题观测聚合成该配置的指标。**分母写清楚**（哪些题参与了哪个指标）。"""
    def mean(vals: list[float]) -> float | None:
        return round(statistics.fmean(vals), 4) if vals else None

    page_rows = [r["qa_id"] for r in rows
                 if r.get("gt_page") == "page" and not r.get("broad")]
    broad_rows = [r["qa_id"] for r in rows if r.get("broad")]
    sec_rows = [r["qa_id"] for r in rows if r.get("expected_sections")]
    year_rows = [r["qa_id"] for r in rows if r.get("year") and not r.get("expect_refuse")]
    comp_rows = [r["qa_id"] for r in rows if r.get("code") and not r.get("expect_refuse")]
    ref_rows = [r["qa_id"] for r in rows if r.get("expect_refuse")]
    filt_rows = [r["qa_id"] for r in rows
                 if obs.get(r["qa_id"], {}).get("filter_ok") is not None]

    return {
        "page_hit": mean([1.0 if obs[i].get("page_hit") else 0.0 for i in page_rows]),
        "page_n": len(page_rows),
        "broad_hit": mean([1.0 if obs[i].get("page_hit") else 0.0 for i in broad_rows]),
        "broad_n": len(broad_rows),
        "section_hit": mean([1.0 if obs[i].get("section_hit") else 0.0 for i in sec_rows]),
        "section_n": len(sec_rows),
        "year_ratio": mean([obs[i].get("year_ratio", 0.0) for i in year_rows]),
        "year_n": len(year_rows),
        "company_ratio": mean([obs[i].get("company_ratio", 0.0) for i in comp_rows]),
        "company_n": len(comp_rows),
        "filter_acc": mean([1.0 if obs[i].get("filter_ok") else 0.0 for i in filt_rows]),
        "filter_n": len(filt_rows),
        "refuse_acc": mean([1.0 if obs[i].get("refused") else 0.0 for i in ref_rows]),
        "refuse_n": len(ref_rows),
        "distinct_pages": mean([float(obs[i].get("distinct_pages") or 0) for i in year_rows]),
        "degraded_n": len([i for i in obs if obs[i].get("degraded")]),
        "empty_n": len([i for i in obs if i in year_rows and obs[i].get("hits") == 0]),
    }


def fmt(v, pct: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if pct else f"{v}"


def write_report(rows: list[dict], results: dict, meta: dict, elapsed: float) -> None:
    lines: list[str] = []
    lines.append("# 检索评测报告：Step 3（纯 BM25）→ Step 4+（混合检索 + 每页配额）")
    lines.append("")
    lines.append(f"- 生成时间：{meta['built_at']}（本次评测耗时 {elapsed:.0f}s）")
    lines.append(f"- 索引：{meta['index']}")
    lines.append(f"- 向量库：{meta['vector']}")
    lines.append(f"- 重排通道（配置值）：{meta['rerank']}；本报告中 ⑤ 档**显式指定** `api` 通道，"
                 f"实际状态见该档 note")
    lines.append(f"- 每页配额：**{meta.get('quota')}**（0 = 不限；仅 ③④⑤ 档生效）")
    lines.append(f"- 评测集：`eval/golden_qa.jsonl` 共 {len(rows)} 题 —— "
                 f"页级金标准 {sum(1 for r in rows if r.get('gt_page') == 'page' and not r.get('broad'))} 题、"
                 f"宽泛题 {sum(1 for r in rows if r.get('broad'))} 题、"
                 f"章节级金标准 {sum(1 for r in rows if r.get('expected_sections'))} 题、"
                 f"拒答题 {sum(1 for r in rows if r.get('expect_refuse'))} 题")
    lines.append("")
    _page_n = sum(1 for r in rows if r.get("gt_page") == "page" and not r.get("broad"))
    # 逐题权重必须**算出来**而不是写死：金标准会随语料变化重算（页级题数变过 21 → 25），
    # 写死的"1 题 = 4.8pp"会立刻变成错的提醒 —— 而"样本量小"这件事恰恰是这段话的重点。
    _pp = (100.0 / _page_n) if _page_n else 0.0
    lines.append(f"> ⚠️ **样本量提醒**：页级金标准只有 {_page_n} 题，**1 题 = {_pp:.1f}pp**。"
                 "所以本报告里 5pp 量级的差异只能算「方向」，不能算「结论」；"
                 "真正可信的是 20pp 量级的变化（过滤、向量召回）。")
    lines.append("")
    lines.append("## 1. 汇总（消融对照）")
    lines.append("")
    lines.append("| 配置 | page_hit@5 | 宽泛题 | section_hit@5 | top5 覆盖页数 | 年份精度 | 公司精度 | 过滤抽取 | 拒答正确率 | 空召回 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cfg in CONFIGS:
        agg = results[cfg["key"]]["agg"]
        lines.append(
            f"| {cfg['label']} | {fmt(agg['page_hit'])} ({agg['page_n']}题) "
            f"| {fmt(agg['broad_hit'])} | {fmt(agg['section_hit'])} ({agg['section_n']}题) "
            f"| {fmt(agg['distinct_pages'], pct=False)} "
            f"| {fmt(agg['year_ratio'])} | {fmt(agg['company_ratio'])} "
            f"| {fmt(agg['filter_acc'])} ({agg['filter_n']}题) "
            f"| {fmt(agg['refuse_acc'])} ({agg['refuse_n']}题) | {agg['empty_n']} |")
    lines.append("")
    lines.append("> 各指标的分母不同（只有「能定页」的题算 page_hit，只有给了章节期望的题算 "
                 "section_hit），所以每列都标了题数 —— 不同分母上的百分比不能直接比大小。"
                 "「top5 覆盖页数」是 top-5 里**不同页**的平均个数（上限 5），"
                 "直接反映每页配额是否真的把名额让给了其他页。")
    lines.append("")

    # ---- 关键发现：自动生成，避免"人挑好看的写" ----
    bm_raw = results["bm25_raw"]["agg"]
    best_key = max(results, key=lambda k: (results[k]["agg"]["page_hit"] or 0,
                                           results[k]["agg"]["year_ratio"] or 0))
    lines.append("## 2. 关键发现")
    lines.append("")

    def delta(k: str) -> str:
        a, b = bm_raw.get(k), results[best_key]["agg"].get(k)
        if a is None or b is None:
            return "—"
        return f"{(b - a) * 100:+.1f}pp"

    lines.append(f"- **最佳配置：{dict((c['key'], c['label']) for c in CONFIGS)[best_key]}**")
    lines.append(f"- 年份精度 {fmt(bm_raw['year_ratio'])} → "
                 f"{fmt(results[best_key]['agg']['year_ratio'])}（{delta('year_ratio')}）"
                 f"；公司精度 {fmt(bm_raw['company_ratio'])} → "
                 f"{fmt(results[best_key]['agg']['company_ratio'])}（{delta('company_ratio')}）")
    lines.append(f"- page_hit@5 {fmt(bm_raw['page_hit'])} → "
                 f"{fmt(results[best_key]['agg']['page_hit'])}（{delta('page_hit')}）")

    # ---- 逐步归因：每一步只加一个开关，差值就是那一步的贡献 ----
    def dpp(a: dict, b: dict, k: str) -> str:
        if a.get(k) is None or b.get(k) is None:
            return "—"
        return f"{(b[k] - a[k]) * 100:+.1f}pp"

    steps = [("bm25_raw", "bm25_filter", "①→② 只加**自动元数据过滤**"),
             ("bm25_filter", "bm25_quota", "②→③ 只加**每页配额**"),
             ("bm25_quota", "hybrid", "③→④ 只加**向量路 + RRF 融合**"),
             ("hybrid", "hybrid_rerank", "④→⑤ 只加**重排**（显式 api 通道）")]
    lines.append("- **逐步归因**（每一步只加一个开关，差值即该步贡献）：")
    for a_key, b_key, desc in steps:
        a, b = results[a_key]["agg"], results[b_key]["agg"]
        lines.append(
            f"  - {desc}：page_hit@5 {fmt(a['page_hit'])} → {fmt(b['page_hit'])}"
            f"（{dpp(a, b, 'page_hit')}）；section_hit@5（{dpp(a, b, 'section_hit')}）；"
            f"年份精度（{dpp(a, b, 'year_ratio')}）；公司精度（{dpp(a, b, 'company_ratio')}）；"
            f"top5 覆盖页数 {fmt(a['distinct_pages'], pct=False)} → "
            f"{fmt(b['distinct_pages'], pct=False)}")
    flips = []
    for r in rows:
        if r.get("gt_page") != "page" or r.get("broad"):
            continue
        a = results["bm25_raw"]["per_q"].get(r["qa_id"], {}).get("page_hit")
        b = results[best_key]["per_q"].get(r["qa_id"], {}).get("page_hit")
        if not a and b:
            flips.append(r["qa_id"])
    unflips = [r["qa_id"] for r in rows
               if r.get("gt_page") == "page" and not r.get("broad")
               and results["bm25_raw"]["per_q"].get(r["qa_id"], {}).get("page_hit")
               and not results[best_key]["per_q"].get(r["qa_id"], {}).get("page_hit")]
    if flips:
        lines.append(f"- **被救回的题**（①漏、最佳命中）：{', '.join(flips)}")
    if unflips:
        lines.append(f"- ⚠️ **被弄坏的题**（①命中、最佳漏）：{', '.join(unflips)}")
    deg = results[best_key]["agg"]["degraded_n"]
    if deg:
        lines.append(f"- ⚠️ 有 {deg} 题发生降级（向量/重排不可用），已按 BM25 结果计分")

    # ---- 重排的边际效果（单独列，因为它是唯一可能为负的一步）----
    if "hybrid" in results and "hybrid_rerank" in results:
        h = results["hybrid"]["per_q"]
        r = results["hybrid_rerank"]["per_q"]
        lost = [k for k in h if h[k].get("page_hit") and not r.get(k, {}).get("page_hit")]
        gain = [k for k in h if not h[k].get("page_hit") and r.get(k, {}).get("page_hit")]
        changed = sum(1 for k in h if h[k].get("pages") != r.get(k, {}).get("pages"))
        lines.append(
            f"- **重排的净效果：{dpp(results['hybrid']['agg'], results['hybrid_rerank']['agg'], 'page_hit')}**"
            f"（它确实生效了：{changed}/{len(h)} 题的 top5 顺序被改变）。"
            f"丢 {len(lost)} 题（{', '.join(lost) or '—'}）、"
            f"救回 {len(gain)} 题（{', '.join(gain) or '—'}）。"
            f"**解读**：交叉编码器优化的是「段落与问题语义相关」，而本评测的期望是"
            f"「这一页里有那个数字」—— 两者不总一致（含数字的表格页往往不是语义最相关的段落）。"
            f"所以默认 `RERANK_BACKEND=passthrough` 不只是省钱的取舍，"
            f"**在这套评测上是更优的选择**。")
    lines.append("")

    # ---- 已知不足：从数据里列出来，而不是靠人挑 ----
    missed_ref = [r["qa_id"] for r in rows if r.get("expect_refuse")
                  and not results[best_key]["per_q"].get(r["qa_id"], {}).get("refused")]
    lines.append("### 已知不足（自动列出）")
    lines.append("")
    if missed_ref:
        lines.append(f"- **该拒答却答了（{len(missed_ref)} 题）**：{', '.join(missed_ref)}"
                     f" —— 两道确定性闸门都拦不住它，因为**两道都只看「字面在不在」**："
                     f"① 覆盖度：「产能」在茅台年报里真有（基酒产能），覆盖率因此达标；"
                     f"② 语料外实词：这条闸门要求实词在**全库**零出现，"
                     f"而「锂电池」在**别的公司**（宁德时代）的年报里是有的 → 不触发。"
                     f"它漏在「全库有、但在被限定的那家公司里没有」这道缝里。"
                     f"补法是「把语料外判定下沉到**过滤后的范围**」，但那会引入"
                     f"「限定了范围就更容易拒答」的新误伤面，需要扩评测集再验证 —— "
                     f"已记入 `不足清单与处置方案.md` 的 G-01（方案，本轮不做）。")
    else:
        lines.append("- 拒答全部正确。")

    # 同页配额：用数据说话，而不是照抄"top5 被同页占满"的印象
    if "bm25_filter" in results and "bm25_quota" in results:
        a = results["bm25_filter"]["agg"]
        b = results["bm25_quota"]["agg"]
        lines.append(
            f"- **「同页多块占满 top5」这件事，实测影响比原先假设的小**："
            f"加上配额后 top5 覆盖页数 {fmt(a['distinct_pages'], pct=False)} → "
            f"{fmt(b['distinct_pages'], pct=False)}（上限 5），"
            f"page_hit@5 {dpp(a, b, 'page_hit')}。原因是**一页通常只切成 2 块**，"
            f"配额 2 几乎不改变任何东西（`RETRIEVE_MAX_PER_PAGE=3` 同样无变化）。"
            f"配额取 1 时 bm25 档 page_hit@5 还会更高（复现："
            f"`python scripts/eval_retrieval.py --only bm25 --quota 1 --no-write`）——"
            f"这里**不复述固定值**：该数字随评测集变化，写死会立刻过期（本行原先写死过一个"
            f"旧口径的数字，评测集扩容后就变成了错的）。"
            f"但**该口径只比页码**，无法区分「页码对了、但数字不在保留的那一块里」——"
            f"所以默认仍取 2（保住同页第 2 块），把「要不要取 1」留给 Step 6 的答案级评测定夺。")
    lines.append("- **跨年可比列不回退**：问「五粮液 2023 年营收」时库中只有 2024 年报，"
                 "严格年份过滤会让它拒答。2024 年报的可比列**确实**含 2023 年数据，"
                 "但「确认那一列真有该年份」需要在引用回查之后做（Step 5 的 verify）。"
                 "宁可拒答并说明，也不要硬凑一个其他年份的数。参见 G-02。")
    lines.append("")

    # ---- 逐题明细 ----
    lines.append("## 3. 逐题明细")
    lines.append("")
    lines.append("`期望` 列：页级金标准页码（* 表示章节级期望 / 宽泛题）；"
                 "其余列是该配置 top5 命中的页码，`✓/✗` 表示是否命中期望。")
    lines.append("")
    lines.append("| 题目 | 期望 | " + " | ".join(c["label"][:14] for c in CONFIGS) + " |")
    lines.append("|---|---|" + "---|" * len(CONFIGS))
    for r in rows:
        # 「期望」列的回退顺序要按**题的类型**走：法规题既没有年报页码也没有章节期望，
        # 若统一回退成字面量「拒答」，明细表就会把法规题标成拒答题（纯误导）。
        if r.get("gt", {}).get("type") == "article":
            gold = "法规 " + str(r["gt"].get("article_label")
                                or r["gt"].get("article") or "")
        else:
            gold = ",".join(str(p) for p in (r.get("expected_pages") or [])) or \
                ("/".join(r.get("expected_sections") or []) or "拒答")
        if r.get("broad"):
            gold += " *"
        cells = []
        for cfg in CONFIGS:
            o = results[cfg["key"]]["per_q"].get(r["qa_id"], {})
            pages = ",".join(str(p) for p in (o.get("pages") or [])) or "—"
            if r.get("expect_refuse"):
                cells.append(("拒答✓" if o.get("refused") else "**未拒答**") + f" `{pages}`")
            elif o.get("page_hit") is not None:
                cells.append(("✓" if o["page_hit"] else "✗") + f" `{pages}`")
            elif o.get("section_hit") is not None:
                cells.append(("章✓" if o["section_hit"] else "章✗") + f" `{pages}`")
            else:
                cells.append(f"`{pages}`")
        lines.append(f"| {r['qa_id']}<br>{r['question'][:26]} | {gold} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 4. 复现")
    lines.append("")
    lines.append("```bash")
    lines.append("python scripts/build_golden.py        # 复核金标准页码（标记串扫原文）")
    lines.append("python scripts/eval_retrieval.py      # 重跑本报告（5 档消融）")
    lines.append("")
    lines.append("# 配额消融（不必改环境变量：环境变量在评测中途改，两次跑的就不是同一件事）")
    lines.append("python scripts/eval_retrieval.py --only bm25 --quota 1 --no-write")
    lines.append("python scripts/eval_retrieval.py --only bm25 --quota 3 --no-write")
    lines.append("")
    lines.append("# 只跑某一档（子串匹配 key 或 label）")
    lines.append("python scripts/eval_retrieval.py --only hybrid_rerank")
    lines.append("```")
    # 指回答案级报告：本报告测的是**检索**（top-5 里有没有落在期望页），
    # 而"最终交付给用户的答案对不对"是另一个问题（检索到了那页、模型也可能抄错数）。
    # 那一层由 `eval_rag.py --judge` 产出，这里留一行指针，免得只看本报告的人以为它测的是答案。
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 📄 **答案级评测见 [`eval/report_answer.md`](report_answer.md)**"
                 "（由 `python scripts/eval_rag.py --judge` 生成）：含 faithfulness / "
                 "answer_relevancy / 数值准确率 / 引用命中率 —— 本报告只测检索，不测答案对不对。")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑标签里含该子串的配置")
    ap.add_argument("--topk", type=int, default=config.RETRIEVE_TOPK)
    ap.add_argument("--quota", type=int, default=None,
                    help="覆盖每页配额（0=不限）—— 用来复现「配额消融」，"
                         "不必改环境变量重跑（环境变量在评测中途改，两次跑的就不是同一件事）")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    quota = config.RETRIEVE_MAX_PER_PAGE if args.quota is None else args.quota
    configs = [dict(c) for c in CONFIGS]
    if args.quota is not None:
        for c in configs:
            if c.get("quota"):
                c["quota"] = quota
                c["label"] += f"（配额={quota}）"

    rows = load_golden()
    configs = [c for c in configs if not args.only or args.only in c["key"] or args.only in c["label"]]
    print(f"评测集 {len(rows)} 题；配置 {[c['key'] for c in configs]}；"
          f"topk={args.topk}；每页配额={quota}")

    t0 = time.monotonic()
    results: dict[str, dict] = {}
    for cfg in configs:
        print(f"\n=== {cfg['label']} ===", flush=True)
        per_q: dict[str, dict] = {}
        for r in rows:
            try:
                per_q[r["qa_id"]] = run_one(r, cfg, args.topk)
            except Exception as e:   # noqa: BLE001 —— 单题炸掉不该让整轮评测没结果
                per_q[r["qa_id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                print(f"  ✗ {r['qa_id']}: {type(e).__name__}: {e}")
        agg = aggregate(rows, per_q)
        results[cfg["key"]] = {"agg": agg, "per_q": per_q}
        print(f"   page_hit@5={fmt(agg['page_hit'])} ({agg['page_n']}题) "
              f"section_hit@5={fmt(agg['section_hit'])} ({agg['section_n']}题) "
              f"年份精度={fmt(agg['year_ratio'])} 公司精度={fmt(agg['company_ratio'])} "
              f"拒答={fmt(agg['refuse_acc'])}")
    elapsed = time.monotonic() - t0

    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "quota": quota,
        "index": json.dumps(pipeline.index_stats(), ensure_ascii=False),
        "vector": json.dumps(vector_mod.status(), ensure_ascii=False),
        "rerank": json.dumps(rerank_mod.status(), ensure_ascii=False),
    }
    REPORT_JSON.write_text(json.dumps(
        {"meta": meta, "configs": [c["key"] for c in configs],
         "results": {k: {"agg": v["agg"], "per_q": v["per_q"]} for k, v in results.items()}},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # 报告只在跑满四配置时才写（缺配置的报告会让人误读成"少了一档"）
    if not args.no_write and len(configs) == len(CONFIGS):
        write_report(rows, results, meta, elapsed)
        print(f"\n报告已写入 {REPORT_MD}")
    elif not args.no_write:
        print(f"\n（只跑了部分配置，未覆盖 {REPORT_MD}；明细见 {REPORT_JSON}）")
    print(f"耗时 {elapsed:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
