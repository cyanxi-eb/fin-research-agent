"""答案级评测（Step 6 / C6）—— 产出 `eval/report_answer.json`。

## 与 `scripts/eval_retrieval.py` 的分工

`eval_retrieval.py` 测的是**检索**（top-5 里有没有落在那几页），
本脚本测的是**答案**（最终交付给用户的那个字符串对不对）。
两者必须分开：检索命中不等于答案正确（检索到了那页、但模型抄错了数），
答案正确也不必然来自检索（数值题走的是结构化库，压根不看原文）。

## 本步只做**四项确定性指标**（默认不调任何模型、零 token）

| 指标 | 分母 | 判据 |
|---|---|---|
| 数值准确率 | `gt.type=="indicator"` | `judge.numeric_hit(answer, gt)`（含单位换算、千分位、符号） |
| 引用命中率 | `expected_pages` 非空 | 引用里的 `(code, year, page_no)` 是否命中期望页 |
| 拒答正确率 | `expect_refuse` 非空 | 响应的 `refused` 是否等于 `expect_refuse` |
| 路由命中率 | `article/indicator/literal` 三类 | 法规题→`compliance`、数值题→`analysis`、引用题→`rag` |

判分用的 LLM 指标（faithfulness / answer_relevancy）走 `--judge`（**默认关**）：
关着的时候全流程零 token、可免费重跑（评测一旦默认带 LLM 就既花钱又带"模型今天心情如何"的噪声）；
打开后只对 `answer_expect.judge=true` 且未拒答的题逐题判分，判分失败按"未判"单列、**不进分母**。

## 两个容易误读的点（都在输出里如实记录）

1. **"挂起"不是错误**：离线降级下部分题会触发 `hitl.pending`（如 `citation_unsupported`），
   那是"待人工确认"这个设计本身在工作，计为一条独立状态而不是失败。
2. **数值题没有页码引用**：数值题走工具层，`citations` 为空（出处是 `表.字段` 而不是页码），
   所以引用命中率的分母里数值题结构上不可能命中 —— 报告里把分母与构成一并打印，便于解读。

用法：
    python scripts/eval_rag.py --no-write --limit 8          # 冒烟（默认不调模型）
    python scripts/eval_rag.py                                # 全量，写 eval/report_answer.json
    python scripts/eval_rag.py --judge                        # 全量 + LLM 判分，写 eval/report_answer.md
    python scripts/eval_rag.py --mode hybrid --quota 1 --json # 换检索配置，只打印 JSON
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

# ⚠️ 在**导入图之前**把 Checkpointer 落到临时目录：`run_agent` 会把每轮的挂起/定稿态
# 写进 Checkpointer，默认路径是开发库 `data/db/checkpoints.db`。评测跑几百题会往开发库里
# 灌一堆 `fa-xxxx` 会话，既污染手工排查、又让"重跑"不再幂等。
# `src/config.py` 没有为这个路径提供环境变量（只有后端选择），所以这里直接改配置对象 ——
# `src/graph/checkpoint.py` 是在**调用时**读 `config.CHECKPOINT_DB_PATH`，运行期改得动。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="eval_rag_ckpt_"))
config.CHECKPOINT_DB_PATH = _TMP_DIR / "checkpoints.db"

from src import judge, llm  # noqa: E402
from src.graph import builder  # noqa: E402
from src.retrieve import pipeline  # noqa: E402

GOLDEN_PATH: Path = config.ROOT_DIR / "eval" / "golden_qa.jsonl"
REPORT_JSON: Path = config.ROOT_DIR / "eval" / "report_answer.json"
REPORT_MD: Path = config.ROOT_DIR / "eval" / "report_answer.md"

# 期望意图：金标准类型 → 应该走哪条子图
EXPECTED_INTENT: dict[str, str] = {
    "article": "compliance",
    "indicator": "analysis",
    "literal": "rag",
}


def load_golden() -> list[dict]:
    rows = []
    for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def citation_hit(row: dict, citations: list[dict]) -> bool:
    """引用里是否有 `(code, year, page_no)` 命中该题的 `expected_pages`。

    必须**连公司/年份一起比**：页码在每家公司里都从 1 开始编，
    只比页码会把"别家报告的第 N 页"算成命中（`eval_retrieval.py` 踩过同一个坑）。
    """
    gold = set(row.get("expected_pages") or [])
    if not gold:
        return False
    for c in citations:
        if c.get("page_no") not in gold:
            continue
        if row.get("code") and c.get("code") != row["code"]:
            continue
        if row.get("year") and c.get("year") != row["year"]:
            continue
        return True
    return False


def evidence_in_retained(row: dict, *, mode: str) -> bool | None:
    """「证据完整性」：数值题的答案数值能否在**被保留的那一块**引用片段原文里找到。

    为什么数值题要**另跑一次检索**：数值题走工具层（`subgraph_analysis`），响应里的
    `citations` 恒为空 —— 它压根不检索，"被保留的片段"在响应里根本不存在。而 G-13 要问的
    正是"同页只保留一块时，那个数还在不在被保留的片段里"，这是个**反事实**问题：
    假设这题走 RAG 路，配额会不会把含数字的那一块挤掉。所以这里用同一套
    `pipeline.retrieve`（同 mode、同 `RETRIEVE_MAX_PER_PAGE` 配额）探一次，
    判据复用 `judge.numeric_hit`（与校验侧同一份"什么算数字"的实现）。

    非数值题 / 无期望页的题返回 `None`（不进这个分母）。
    """
    gt = row.get("gt") or {}
    if gt.get("type") != "indicator" or not row.get("expected_pages"):
        return None
    res = pipeline.retrieve(row["question"], mode=mode,
                            max_per_page=config.RETRIEVE_MAX_PER_PAGE)
    return any(judge.numeric_hit(h.get("text") or "", gt) for h in (res.get("hits") or []))


def run_one(row: dict, *, mode: str, force_intent: str | None,
            judge_enabled: bool = False) -> dict:
    """跑单题，返回观测值（不含题目元信息，便于聚合）。"""
    resp = builder.run_agent(row["question"], mode=mode, use_llm=False, audit=False,
                             force_intent=force_intent)
    answer = resp.get("answer") or ""
    citations = resp.get("citations") or []
    gt = row.get("gt") or {}
    obs = {
        "ok": bool(resp.get("ok")),
        "intent": resp.get("intent"),
        "route_intent": (resp.get("route") or {}).get("intent"),
        "refused": bool(resp.get("refused")),
        "degraded": bool(resp.get("degraded")),
        # 挂起是"待人工确认"这个设计在工作，**不是失败** —— 单独记一个状态
        "hitl_pending": bool((resp.get("hitl") or {}).get("pending")),
        "hitl_reason": (resp.get("hitl") or {}).get("reason"),
        "n_citations": len(citations),
    }
    if gt.get("type") == "indicator":
        obs["numeric_hit"] = judge.numeric_hit(answer, gt)
        obs["evidence_in_snippet"] = evidence_in_retained(row, mode=mode)
    if row.get("expected_pages"):
        obs["cite_hit"] = citation_hit(row, citations)
    # ---- LLM 判分（默认关）：只对 `judge=true` 且未拒答的题调 ----
    if judge_enabled and (row.get("answer_expect") or {}).get("judge") and not obs["refused"]:
        # `contexts` 取响应里 citations 的 `snippet`：它就是**这条答案被允许引用**的那批证据
        # （与答案正文的 `[n]` 编号同源）。为什么不复用 `pipeline.render_context`：
        # 它需要原始 `hits`，而 `run_agent` 的响应只导出 `citations`（不带 hits），
        # 在这一层拿不到 hits；且数值题 citations 为空 —— 见 `evidence_in_retained` 的说明。
        contexts = [c.get("snippet") or "" for c in citations]
        contexts = [c for c in contexts if c]
        verdict = judge.judge_answer(row["question"], answer, contexts)
        obs["judged"] = True
        obs["faithfulness"] = verdict["faithfulness"]
        obs["answer_relevancy"] = verdict["answer_relevancy"]
        obs["judge_model"] = verdict["judge_model"]
        obs["judge_notes"] = verdict["notes"]
    return obs


def aggregate(rows: list[dict], obs: dict[str, dict]) -> dict:
    """把逐题观测聚合成四项指标。**分母写清楚**（哪些题参与了哪个指标）。"""
    def mean(vals: list[float]) -> float | None:
        return round(statistics.fmean(vals), 4) if vals else None

    def rate(ids: list[str], key: str) -> tuple[float | None, int]:
        vals = [1.0 if obs[i].get(key) else 0.0 for i in ids if key in obs[i]]
        return mean(vals), len(vals)

    ind_ids = [r["qa_id"] for r in rows if (r.get("gt") or {}).get("type") == "indicator"]
    cite_ids = [r["qa_id"] for r in rows if r.get("expected_pages")]
    ref_ids = [r["qa_id"] for r in rows if r.get("expect_refuse")]
    route_ids = [r["qa_id"] for r in rows
                 if (r.get("gt") or {}).get("type") in EXPECTED_INTENT]
    # 证据完整性：数值题里"有期望页"的那些（没期望页就无从谈"保留的片段里有没有这个数"）
    ev_ids = [r["qa_id"] for r in rows
              if (r.get("gt") or {}).get("type") == "indicator" and r.get("expected_pages")]

    numeric, numeric_n = rate(ind_ids, "numeric_hit")
    cite, cite_n = rate(cite_ids, "cite_hit")
    evidence, evidence_n = rate(ev_ids, "evidence_in_snippet")
    # 拒答正确率：refused 与 expect_refuse **相等**才算对（该拒没拒、不该拒却拒都算错）
    ref_vals = [1.0 if obs[i]["refused"] == bool(_row(rows, i).get("expect_refuse")) else 0.0
                for i in ref_ids]
    refuse, refuse_n = mean(ref_vals), len(ref_vals)

    # ---- LLM 判分：均值只按**有效分**算；判分失败的题计为"未判"、单列计数、不进分母 ----
    judged_ids = [r["qa_id"] for r in rows if obs.get(r["qa_id"], {}).get("judged")]
    faith = judge.aggregate([obs[i].get("faithfulness") for i in judged_ids])
    relev = judge.aggregate([obs[i].get("answer_relevancy") for i in judged_ids])
    for agg in (faith, relev):
        agg["value"] = round(agg["mean"], 4) if agg["mean"] is not None else None

    route_vals = []
    route_by_type: dict[str, dict] = {}
    for r in rows:
        t = (r.get("gt") or {}).get("type")
        if t not in EXPECTED_INTENT:
            continue
        want = EXPECTED_INTENT[t]
        got = obs[r["qa_id"]].get("intent")
        hit = got == want
        route_vals.append(1.0 if hit else 0.0)
        bucket = route_by_type.setdefault(t, {"want": want, "hit": 0, "n": 0})
        bucket["hit"] += int(hit)
        bucket["n"] += 1
    for b in route_by_type.values():
        b["value"] = round(b["hit"] / b["n"], 4) if b["n"] else None

    return {
        "numeric_accuracy": {"value": numeric, "n": numeric_n},
        "citation_hit": {"value": cite, "n": cite_n},
        "refusal_accuracy": {"value": refuse, "n": refuse_n},
        "evidence_completeness": {"value": evidence, "n": evidence_n},
        "faithfulness": faith,
        "answer_relevancy": relev,
        "route_hit": {"value": mean(route_vals), "n": len(route_vals),
                      "by_type": route_by_type},
    }


def _row(rows: list[dict], qa_id: str) -> dict:
    for r in rows:
        if r["qa_id"] == qa_id:
            return r
    return {}


def _fmt(v: float | None, n: int) -> str:
    if v is None or n == 0:
        return "—（n=0）"
    return f"{v * 100:.1f}%（{n} 题）"


def _hit_cell(o: dict) -> str:
    if o.get("numeric_hit") is not None:
        return "数✓" if o["numeric_hit"] else "数✗"
    if o.get("cite_hit") is not None:
        return "引✓" if o["cite_hit"] else "引✗"
    return "—"


def _score_cell(v: float | None) -> str:
    return "未判" if v is None else f"{v:.2f}"


def write_report_md(rows: list[dict], metrics: dict, per_q: dict[str, dict],
                    meta: dict, elapsed: float) -> None:
    """写人读报告 `eval/report_answer.md`：**四个数字 + 逐题明细 + 已知不足**。

    只在 `--judge` 时写：不带判分时 faithfulness / answer_relevancy 无值，
    写一份"没有判分"的报告会被人误读成"这轮判了、结果就是这样"。
    """
    by_type = meta["by_type"]
    n_eval = meta["evaluated"]
    n_ind, n_lit, n_art = (by_type.get("indicator", 0), by_type.get("literal", 0),
                           by_type.get("article", 0))
    n_ref = sum(1 for r in rows if r.get("expect_refuse"))
    n_judge_true = sum(1 for r in rows if (r.get("answer_expect") or {}).get("judge"))

    m = metrics
    faith, relev = m["faithfulness"], m["answer_relevancy"]
    num, cite = m["numeric_accuracy"], m["citation_hit"]

    L: list[str] = []
    L.append("# 答案级评测报告（Step 6 / C7）")
    L.append("")
    L.append(f"- 生成时间：{meta['built_at']}（本次评测耗时 {elapsed:.0f}s）")
    L.append(f"- **判分模型：{meta.get('judge_model') or '未启用'}**"
             f"（faithfulness / answer_relevancy 由该模型判；判分失败计为「未判」）")
    L.append(f"- 评测集：`eval/golden_qa.jsonl` 共 {meta['golden_total']} 题；"
             f"**本次实际评测 {n_eval} 题**（mode={meta['mode']}，每页配额={meta['quota']}，"
             f"intent={meta['intent']}）")
    L.append(f"- 题数与分类计数：数值题（`indicator`）{n_ind}、引用题（`literal`）{n_lit}、"
             f"法规题（`article`）{n_art}、拒答题（`expect_refuse`）{n_ref}；"
             f"其中 `answer_expect.judge=true` **{n_judge_true} 题**"
             f"（`judge=false` {n_eval - n_judge_true} 题，均为拒答题、无答案可判）")
    L.append("")
    # 样本量提醒**按数据算**（金标准会扩容，写死会立刻过期）
    remind = []
    if num["n"]:
        remind.append(f"数值题 {num['n']} 题 → 1 题 = {100.0 / num['n']:.1f}pp")
    if faith["n_valid"]:
        remind.append(f"faithfulness 有效分 {faith['n_valid']} 题 → 1 题 = "
                      f"{100.0 / faith['n_valid']:.1f}pp")
    if relev["n_valid"]:
        remind.append(f"answer_relevancy 有效分 {relev['n_valid']} 题 → 1 题 = "
                      f"{100.0 / relev['n_valid']:.1f}pp")
    L.append(f"> ⚠️ **样本量提醒**：{('；'.join(remind) or '—')}。"
             "所以 5pp 量级的差异只能算「方向」，不能算「结论」。")
    # LLM 判分**不是逐位可复现**的：同模型、同题、同 temperature=0，多轮全量跑下来的
    # faithfulness / answer_relevancy 仍有明显抖动（实测同一 55 题三轮见下）。
    # 这不是缺陷，而是"判分本身带模型噪声"这个事实。
    # 所以报告必须把**判分模型名 + 生成时间**一并写出，读数字时按下面的量级去读；
    # 换模型或重入库后这两个数不可直接与历史值比（沿用 G-15 的思路）。
    L.append("> ℹ️ **可复现性**：确定性四项（数值/引用/拒答/路由）逐位可复现；"
             "faithfulness / answer_relevancy 由 LLM 判分，**同模型重跑会抖动**"
             "（实测同一 55 题三轮：23.7%/82.3% → 25.1%/79.8% → 20.3%/83.6%，"
             "faithfulness 极差已近 5pp）。因此这两个数必须与「判分模型名 + 生成时间」一起读，"
             "**单轮之间的差异若小于上面的量级，不能当作改进或退步**；换模型或重入库后不可直接比。")
    L.append("")
    L.append("## 1. 四个数字")
    L.append("")
    L.append("| 指标 | 值 | 分母（口径） |")
    L.append("|---|---|---|")
    L.append(f"| **faithfulness**（忠实度） | {_fmt(faith['value'], faith['n_valid'])} | "
             f"有效分 {faith['n_valid']} / 判分 {faith['n_total']} 题；未判 {faith['n_missing']} 题 |")
    L.append(f"| **answer_relevancy**（答案相关性） | {_fmt(relev['value'], relev['n_valid'])} | "
             f"有效分 {relev['n_valid']} / 判分 {relev['n_total']} 题；未判 {relev['n_missing']} 题 |")
    L.append(f"| **数值准确率** | {_fmt(num['value'], num['n'])} | "
             f"`gt.type==indicator` 的题（含单位换算/千分位/符号） |")
    L.append(f"| **引用命中率** | {_fmt(cite['value'], cite['n'])} | "
             f"`expected_pages` 非空的题（**构成见 §3**） |")
    L.append("")
    L.append(f"辅助：证据完整性（数值题答案数值能在被保留的检索片段里找到）"
             f"{_fmt(m['evidence_completeness']['value'], m['evidence_completeness']['n'])}；"
             f"路由命中率 {_fmt(m['route_hit']['value'], m['route_hit']['n'])}。")
    L.append("")
    # 分桶：按"有没有检索上下文"把判分结果拆开 —— 数值题走工具层、citations 为空，
    # 拿空【资料】判 faithfulness 只能得到"无支撑"，那是**结构性**的，不是质量差。
    # 头条数字会因此被拉低，所以必须把这个分桶摆出来，否则会被读成"答案在编造"。
    judged_ids = [r["qa_id"] for r in rows if per_q.get(r["qa_id"], {}).get("judged")]
    ctx_ids = [i for i in judged_ids
               if (_row(rows, i).get("gt") or {}).get("type") != "indicator"]
    noctx_ids = [i for i in judged_ids
                 if (_row(rows, i).get("gt") or {}).get("type") == "indicator"]
    if ctx_ids or noctx_ids:
        L.append("### 1.1 按「有无检索上下文」分桶（暴露上述结构性偏差）")
        L.append("")
        L.append("| 分桶 | 题数 | faithfulness | answer_relevancy |")
        L.append("|---|---|---|---|")
        for label, ids in (("有检索上下文（literal / article）", ctx_ids),
                           ("无检索上下文（indicator，走工具层）", noctx_ids)):
            if not ids:
                continue
            fb = judge.aggregate([per_q[i].get("faithfulness") for i in ids])
            rb = judge.aggregate([per_q[i].get("answer_relevancy") for i in ids])
            L.append(f"| {label} | {len(ids)} | {_fmt(fb['mean'], fb['n_valid'])} "
                     f"| {_fmt(rb['mean'], rb['n_valid'])} |")
        L.append("")
        L.append("> 读法：**faithfulness 的头条数字主要反映「有多少题没有可判的上下文」，"
                 "而不是「答案编造了多少」**。数值题的正确性应看「数值准确率」；"
                 "引用题/法规题的忠实度才是有信息量的那一列。")
        L.append("")
    L.append("> 口径说明：`faithfulness` / `answer_relevancy` 的均值**只按有效分算**，"
             "判分失败的题计为「未判」并**单列计数、不进分母** —— 把未判当 0 分会凭空压低指标，"
             "直接丢掉又不留痕（看不出这轮有多少题没判成）。这与 `eval_retrieval.py` 里"
             "「拒答正确率」用分桶的思路一致。")
    L.append("")
    L.append("## 2. 逐题明细")
    L.append("")
    L.append("`命中` 列：数✓/数✗ = 数值题是否命中金标准数值；引✓/引✗ = 引用题是否命中期望页。")
    L.append("")
    L.append("| 题目 | 类型 | 路由意图 | 拒答 | 命中 | faithfulness | relevancy | 备注 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        o = per_q.get(r["qa_id"], {})
        t = (r.get("gt") or {}).get("type") or "—"
        judge_want = (r.get("answer_expect") or {}).get("judge")
        if o.get("judged"):
            f_cell, r_cell = _score_cell(o.get("faithfulness")), _score_cell(o.get("answer_relevancy"))
        elif judge_want and o.get("refused"):
            f_cell = r_cell = "未判(拒答)"
        elif judge_want:
            f_cell = r_cell = "未判(无上下文)"
        else:
            f_cell = r_cell = "—"
        note = ""
        if o.get("hitl_pending"):
            note = f"⏸ 待人工确认（{o.get('hitl_reason')}）"
        if o.get("judge_notes"):
            note = (note + "；" if note else "") + "；".join(o["judge_notes"])
        if o.get("error"):
            note = (note + "；" if note else "") + f"错误 {o['error']}"
        L.append(f"| `{r['qa_id']}`<br>{r['question'][:26]} | {t} | {o.get('intent') or '—'} "
                 f"| {'是' if o.get('refused') else '否'} | {_hit_cell(o)} "
                 f"| {f_cell} | {r_cell} | {note} |")
    L.append("")

    # ---- 已知不足：**从数据里算出来**，不靠人挑 ----
    L.append("## 3. 已知不足（自动列出）")
    L.append("")
    cite_ind = sum(1 for r in rows
                   if r.get("expected_pages") and (r.get("gt") or {}).get("type") == "indicator")
    cite_lit = sum(1 for r in rows
                   if r.get("expected_pages") and (r.get("gt") or {}).get("type") == "literal")
    L.append(f"- **引用命中率的分母含 {cite_ind} 道数值题（构成问题，未收窄分母）**："
             f"分母 {cite['n']} 题里数值题 {cite_ind} 道、引用题（literal）{cite_lit} 道。"
             f"数值题走工具层（`src/tools/`），`citations` 恒为空、出处是「表.字段」而不是页码，"
             f"**结构上不可能命中页码**。所以 {_fmt(cite['value'], cite['n'])} 这个数字"
             f"不是「引用质量」的干净读数 —— 真要看引用质量应只看 literal 题。"
             f"（未把数值题移出分母：那会让分母随题型构成漂移，报告与历史口径不可比。）")
    mis = [r["qa_id"] for r in rows if (r.get("gt") or {}).get("type") == "article"
           and per_q.get(r["qa_id"], {}).get("intent") != "compliance"]
    if mis:
        L.append(f"- **{len(mis)} 道法规题被路由成 `rag`（未修 `src/graph/router.py`）**："
                 f"{', '.join(mis)}。根因是 `_COMPLIANCE_CUES` 未覆盖这些问法，"
                 f"属真实缺口；本步不改 `src/graph/*`，仅如实标注。"
                 f"连带影响：法规题因此走年报检索，拿不到条文级引用。")
    if faith["n_missing"] or relev["n_missing"]:
        L.append(f"- **判分失败 / 未判的题数**：faithfulness 未判 {faith['n_missing']} 题、"
                 f"answer_relevancy 未判 {relev['n_missing']} 题"
                 f"（有效分 {faith['n_valid']} / {relev['n_valid']}，均不进分母）。"
                 f"逐题原因见 §2 的「备注」列。")
    else:
        L.append(f"- **判分失败 / 未判的题数**：faithfulness 与 answer_relevancy 均 "
                 f"{faith['n_valid']} 题全部判成，无未判。")
    # 数值题无检索上下文 —— 忠实度对它们结构上不可判（不是质量差）
    no_ctx = sum(1 for r in rows
                 if (r.get("answer_expect") or {}).get("judge")
                 and per_q.get(r["qa_id"], {}).get("judged")
                 and (r.get("gt") or {}).get("type") == "indicator")
    if no_ctx:
        L.append(f"- **数值题的 faithfulness 是「无上下文判定」（结构性，不是质量差）**："
                 f"判分的 {no_ctx} 道数值题走工具层、`citations` 为空，"
                 f"送进判分 prompt 的【资料】是空的 —— 模型只能判「无资料支撑」。"
                 f"所以 faithfulness 的头条数字被这批题拉低，**不能读成「答案在编造」**；"
                 f"数值题的正确性看「数值准确率」更合适。")
    if meta["hitl_pending"]:
        L.append(f"- **待人工确认（`hitl.pending`，不是错误）**：{len(meta['hitl_pending'])} 题 "
                 f"{meta['hitl_pending']} —— 这是「离线降级下等人工确认」这个设计在工作。")
    L.append("")
    L.append("## 4. 复现")
    L.append("")
    L.append("```bash")
    L.append("python scripts/eval_rag.py --judge          # 全量 + LLM 判分，写本报告与 report_answer.json")
    L.append("python scripts/eval_rag.py --judge --limit 6 # 冒烟（前 6 题，均为数值题）")
    L.append("python scripts/eval_rag.py --no-write --limit 8  # 不带判分：零 token、行为不变")
    L.append("```")
    REPORT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="答案级评测（确定性四项，默认不调模型）")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 题（冒烟用）")
    ap.add_argument("--mode", choices=sorted(config.RETRIEVE_MODES),
                    default=config.RETRIEVE_MODE, help="检索模式")
    ap.add_argument("--quota", type=int, choices=[1, 2], default=None,
                    help="覆盖每页配额（不改环境变量，便于消融对比）")
    ap.add_argument("--intent", choices=["auto", "rag", "analysis", "compliance"],
                    default="auto", help="强制意图（auto = 交给路由器判）")
    ap.add_argument("--json", action="store_true", help="只打印机器可读 JSON")
    ap.add_argument("--no-write", action="store_true", help="不写 eval/report_answer.json")
    ap.add_argument("--judge", action="store_true",
                    help="接上 LLM 判分（faithfulness / answer_relevancy）—— 默认关，关时零 token")
    args = ap.parse_args()

    if args.quota is not None:
        # `run_agent` 不暴露配额参数，而 `pipeline.retrieve` 在**调用时**读 config，
        # 所以运行期改配置即可生效（不改环境变量：中途改环境变量会让两次跑的不是同一件事）。
        config.RETRIEVE_MAX_PER_PAGE = args.quota
    force_intent = None if args.intent == "auto" else args.intent

    # 判分模型名从**激活的供应商配置**里取（不打印 key，只取 model 名），
    # 保证报告头部写的是"这一轮真正用的模型"，而不是硬编码。
    judge_model = llm.get_active_llm().get("model") if args.judge else None

    rows = load_golden()
    if args.limit is not None:
        rows = rows[:args.limit]

    if not args.json:
        print(f"评测集 {GOLDEN_PATH.name} 共 {len(load_golden())} 题；"
              f"本次评测 {len(rows)} 题；mode={args.mode}；"
              f"每页配额={config.RETRIEVE_MAX_PER_PAGE}；intent={args.intent}；"
              f"判分模型={judge_model or '未启用（确定性四项，零 token）'}")

    t0 = time.monotonic()
    per_q: dict[str, dict] = {}
    for r in rows:
        try:
            per_q[r["qa_id"]] = run_one(r, mode=args.mode, force_intent=force_intent,
                                        judge_enabled=args.judge)
        except Exception as e:  # noqa: BLE001 —— 单题炸掉不该让整轮评测没结果
            per_q[r["qa_id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not args.json:
                print(f"  ✗ {r['qa_id']}: {type(e).__name__}: {e}")
    elapsed = time.monotonic() - t0

    metrics = aggregate(rows, per_q)
    by_type: dict[str, int] = {}
    for r in rows:
        t = (r.get("gt") or {}).get("type") or "unknown"
        by_type[t] = by_type.get(t, 0) + 1
    pending = [qid for qid, o in per_q.items() if o.get("hitl_pending")]

    meta = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "golden": str(GOLDEN_PATH.name),
        "golden_total": len(load_golden()),
        "evaluated": len(rows),
        "by_type": by_type,
        "mode": args.mode,
        "quota": config.RETRIEVE_MAX_PER_PAGE,
        "intent": args.intent,
        "judge_enabled": bool(args.judge),
        "judge_model": judge_model,
        "elapsed_sec": round(elapsed, 1),
        "hitl_pending": pending,
    }
    report = {"meta": meta, "metrics": metrics, "per_q": per_q}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n=== 确定性四项指标（耗时 {elapsed:.0f}s）===")
        print(f"  数值准确率 : {_fmt(metrics['numeric_accuracy']['value'], metrics['numeric_accuracy']['n'])}")
        print(f"  引用命中率 : {_fmt(metrics['citation_hit']['value'], metrics['citation_hit']['n'])}")
        print(f"  拒答正确率 : {_fmt(metrics['refusal_accuracy']['value'], metrics['refusal_accuracy']['n'])}")
        print(f"  路由命中率 : {_fmt(metrics['route_hit']['value'], metrics['route_hit']['n'])}")
        print(f"  证据完整性 : "
              f"{_fmt(metrics['evidence_completeness']['value'], metrics['evidence_completeness']['n'])}"
              f"（数值题答案数值能在被保留的检索片段里找到）")
        for t, b in sorted(metrics["route_hit"]["by_type"].items()):
            print(f"      · {t:<10} → 期望 {b['want']:<10} "
                  f"{b['hit']}/{b['n']}（{_fmt(b['value'], b['n'])}）")
        if args.judge:
            print(f"\n=== LLM 判分（{judge_model}；均值只按有效分算，未判不进分母）===")
            for key, label in (("faithfulness", "faithfulness "), ("answer_relevancy", "relevancy    ")):
                a = metrics[key]
                print(f"  {label}: {_fmt(a['value'], a['n_valid'])}"
                      f"（判分 {a['n_total']} 题 / 有效 {a['n_valid']} / 未判 {a['n_missing']}）")
        if pending:
            # 如实记录：挂起是"待人工确认"，不是失败
            print(f"  ⏸ 待人工确认（hitl.pending）：{len(pending)} 题 {pending}")
        # 样本量提醒**按数据算**，不硬编码（金标准会扩容）
        n_ind = metrics["numeric_accuracy"]["n"]
        if n_ind:
            print(f"  ⚠️ 样本量提醒：数值题 {n_ind} 题，1 题 = {100.0 / n_ind:.1f}pp"
                  f"（5pp 量级的差异只能算方向，不能算结论）")

    if not args.no_write:
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        if not args.json:
            print(f"\n报告已写入 {REPORT_JSON}")
    # 人读报告**只在 --judge 时写**：不带判分时 faithfulness / answer_relevancy 无值，
    # 写一份"没有判分"的 md 会覆盖掉上一轮的判分报告、把人误导成"这轮判了"。
    if args.judge and not args.no_write:
        write_report_md(rows, metrics, per_q, meta, elapsed)
        if not args.json:
            print(f"答案级报告已写入 {REPORT_MD}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        # 清掉临时 Checkpointer（尽力而为：Windows 上连接未释放时删不掉，忽略即可 ——
        # 它在系统临时目录里，不在仓库内，不污染开发库）
        shutil.rmtree(_TMP_DIR, ignore_errors=True)
