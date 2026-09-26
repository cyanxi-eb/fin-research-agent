"""Step 3 端到端冒烟：**跑图**问 6 个代表性问题，把引用页码打出来供人工核对。

用法：
```
python scripts/smoke_qa.py              # 走真实大模型（会花 token）
python scripts/smoke_qa.py --no-llm     # 不调模型，只验检索+引用+拒答（离线可跑）
python scripts/smoke_qa.py --json       # 附完整 JSON（落 data/smoke_qa_result.json）
python scripts/smoke_qa.py --only 分红   # 只跑 id 含该子串的用例
```

**与鉴权无关（说清楚，免得被误当成"鉴权也测过了"）**：本脚本是**进程内**调用
（`run_qa` / `run_agent`），**不发任何 HTTP 请求**，因此没有请求头可带、也不经过
`current_user` 这个依赖。鉴权契约由 `tests/test_server_auth.py` 与
`scripts/smoke_step6.py`（带 token 的形态）覆盖。

**验收口径**（对齐实施方案 Step 3）：
> 5 条问题中 ≥4 条引用页码正确（人工核对）；问「公司食堂菜谱」这类
> 超范围问题能拒答而不是编。

脚本把口径拆成三类结论，**分开统计，不混成一个通过率**：

| 桶 | 判据 | 退出码 |
|---|---|---|
| ✅ 硬校验通过 | 有引用、每条都能拼出 `P<页码>`、公司没串、`dangling` 为空、该拒答的拒了 | 0 |
| 📉 基线（Step 4/5 待改进） | **完整性没问题，只是没作答**：该答的题被拒答/降级，且该用例标了 `gap` 归因 | 0 |
| ❌ 失败 | 引用缺页码、串了别家公司、正文角标是死链、该拒答却答了、`--no-llm` 之外莫名降级 | 1 |

**两条判定路径（Step 5 起）**：用例的 `expect` 里**声明了 `via`** 的，走**完整主图**
（`run_agent`：路由 → 子图 → verify → HITL），断言"路由到了哪个意图、答案里有没有期望值、
verify 是否通过、有没有意外挂起"；其余用例仍走 RAG 子图，按**引用口径**判。
为什么必须分开：数值题/合规题**本来就不该走 RAG**（它们的答案来自工具层/法规库、不生成引用），
拿"必须有引用、片段里要有某个词"去判它们，测的是系统**故意不走**的那条路。

**为什么要有"基线"这一桶**：Step 3 刻意只用 BM25（零依赖、当天能跑通），
它的召回精度本来就有限，而 Step 4 的验收标准明确要求"留下 Step 3 vs Step 4 的指标对比"。
把"被召回精度卡住没作答"和"引用是假的/串了公司"混成同一个失败，
要么高估了 Step 3（把缺陷当能力边界放过去），要么低估了它（把基线当缺陷）。
所以卡住就**如实记成基线 + 写清归因**，Step 4 拿它当起点。

**机器校验代替不了人眼，但能把人眼的活缩小到"只看语义"**：
脚本会把每条引用片段**重新拿到源 PDF 的那一页原文里找一遍**（`--no-pdf-check` 可关）。
这条链是独立的 —— 片段是从我们自己的 chunk 里取的，用它反查自己的页码等于自证；
重新开 PDF、只按 `page_no` 取页、再找文字，才能发现"切分/元数据把页码绑错了"这类错，
而其它检查全都发现不了。剩下要人判断的只有一件事：**这一页的文字是否真的支持那个结论**。
那部分刻意不做成断言 —— 断言会把"没核"伪装成"核过了"。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, llm  # noqa: E402
from src.graph.builder import run_agent, run_qa  # noqa: E402
from src.retrieve import pipeline  # noqa: E402
from src.retrieve.bm25 import normalize_for_match  # noqa: E402

_PAGE = re.compile(r"P\d+")
_YEAR = re.compile(r"(?:19|20)\d{2}")

# 每条：问题 + 校验意图。`cite_has` 给的是**题面指标的口径名**，
# 用来确认"引到的这一页真的在讲这件事"，而不是碰巧被 BM25 排到前面。
# `gap` = "这题的正确答案在库里，但按 Step 3 的召回精度拿不到"的**归因**；
# 写了 gap 的用例若被拒答/降级，记入基线桶而不是失败（见模块文档）。
CASES: list[dict] = [
    {
        "id": "数值型 · 制造业毛利率",
        "question": "贵州茅台2024年的毛利率是多少",
        "expect": {"must_cite": True, "cite_code": "600519", "cite_has": ["毛利率"]},
    },
    {
        "id": "数值型 · 营业收入",
        "question": "五粮液2024年的营业收入是多少",
        "expect": {"must_cite": True, "cite_code": "000858", "cite_has": ["营业收入", "营业总收入"]},
    },
    {
        "id": "引用型 · 分红方案",
        "question": "贵州茅台2024年的现金分红方案是什么",
        "expect": {
            "must_cite": True, "cite_code": "600519", "cite_has": ["分红", "派"],
            "gap": "答案在分红预案表里（表头写作「每10 股派息数」这种带空格形态），"
                   "BM25 对该表的排序进不了 top8；库内**有**这一页，属召回精度问题 → Step 4。",
        },
    },
    {
        "id": "数值型 · 保险股归母净资产（走 Step 5 路由 → 工具层）",
        "question": "中国平安的归母净资产是多少",
        # ⚠️ 这条用例的**判定路径**在 Step 5 变了，用例也跟着变：
        # Step 3/4 时期它按"翻年报原文"期望（cite_has=['净资产']），而实测 BM25 会把
        # 「内含价值」类页面排到前面 —— P66 里的「净资产」是内含价值口径的
        # 「调整后资产净值/股东净资产值」，与「归母净资产」不是一回事，
        # 于是形成一次**误引**（引用页的片段里根本没有那个指标）。
        # 而这个问题本来就该走结构化库（9,286.00 亿来自新浪源补的
        # `sina_balance.归属于母公司的股东权益合计`），Step 5 的路由正是做这件事。
        # 所以这里改成断言**设计路径**：route→analysis、答案来自工具层、verify 通过。
        # 不再用引用口径判（它压根不生成引用，走的是零幻觉的工具层通道）。
        "expect": {
            "via": "analysis",
            "answer_has": ["9,286.00"],
            "gap": "Step 5 已解决：路由把数值题导到工具层，答案 9,286.00 亿元"
                   "（来源 sina_balance.归属于母公司的股东权益合计），6 期全部带来源字段。",
        },
    },
    {
        "id": "跨公司 · 两家一起问",
        "question": "五粮液和贵州茅台2024年的营业收入分别是多少",
        "expect": {
            "must_cite": True,
            "gap": "检索层没有「按公司分桶」的逻辑，一家的问题词会被另一家的高频段落挤掉名额；"
                   "跨公司对比属于 Step 5 的多跳/对比路径（也可能直接走工具层的 compare_companies）。",
        },
    },
    {
        "id": "超范围 · 必须拒答",
        "question": "公司食堂菜谱有什么推荐",
        "expect": {"must_refuse": True},
    },
]


def _q(question: str, code: str | None, use_llm: bool, mode: str | None = None) -> dict:
    """跑一题。mode 由命令行决定（默认走 config.RETRIEVE_MODE）——
    Step 4 之后同一批问题**在两条检索链路上各跑一遍**才能看出差别。"""
    return run_qa(question, code=code, use_llm=use_llm, mode=mode)


def _q_routed(question: str, code: str | None, use_llm: bool, mode: str | None = None) -> dict:
    """跑一题**完整主图**（路由 → 三子图 → verify → HITL → finalize）。

    Step 5 起，有些问题（数值题 / 合规题）**本来就不该走 RAG**：
    它们的正确路径是被路由导到工具层或法规库。对这些题，用「引用口径」判分是**测错了路径** ——
    所以单独有这个入口，断言设计路径本身（intent / 答案值 / verify 结论）。
    """
    return run_agent(question, code=code, use_llm=use_llm, mode=mode, audit=False)


# ==================== 页码核验（对源 PDF，而不是对我们自己的中间产物）====================

def _pdf_page_loader():
    """惰性打开 PDF 并按页返回原文（缓存）。找不到文件返回 None。"""
    cache: dict[tuple[str, int], list[str] | None] = {}

    def get(code: str, year: int) -> list[str] | None:
        key = (code, year)
        if key not in cache:
            from src.ingest.fetch_cninfo import load_manifest

            entry = (load_manifest(code) or {}).get(str(year)) or {}
            pdf = Path(entry.get("local") or "")
            if not pdf.exists():
                cache[key] = None
            else:
                import pymupdf

                doc = pymupdf.open(pdf)
                try:
                    cache[key] = [doc[i].get_text("text") or "" for i in range(doc.page_count)]
                finally:
                    doc.close()
        return cache[key]

    return get


def verify_on_pdf(c: dict, get_pages) -> bool | None:
    """把引用片段拿到**源 PDF 的第 page_no 页原文**里找一遍。`None` = 无法核验。

    为什么值得单独做一遍：片段是从我们自己的 chunk 里取出来的，
    用它反查自己的页码等于自证。重新打开 PDF、只按 `page_no` 取那一页、
    再去里面找片段，才是一条**独立**的证据链 —— 若切分/元数据把页码绑错了，
    这一步会直接暴露，而其它检查全都发现不了。

    比对前两边都做去空白+小写（PDF 原文与 `_normalize` 后的换行/空格位置不同），
    并且**用滑动窗口**找：页面正文里有一小段被当作跨页页眉清掉了，
    整段精确匹配会误报失败。窗口命中即算该页确有这段文字。
    """
    pages = get_pages(c.get("code") or "", c.get("year") or 0)
    pno = c.get("page_no")
    if not pages or not pno or not (1 <= pno <= len(pages)):
        return None
    hay = normalize_for_match(pages[pno - 1])
    snip = normalize_for_match(c.get("snippet") or "")
    if not snip:
        return None
    win = 30
    if len(snip) <= win:
        return snip in hay
    for start in range(0, len(snip) - win + 1, 12):
        if snip[start:start + win] in hay:
            return True
    return False


def _year_mix(question: str, out: dict) -> tuple[list[str], list[str]]:
    """题面年份 vs 引用年份：返回 `(题面年份, 与题面不符的引用)`。

    这是**观察项，不是断言**。BM25 只看字面相似度，不知道"问的是 2024 年"，
    所以「贵州茅台2024年的毛利率」很容易先召回 2025 年报里措辞几乎相同的段落。
    引用本身是**真实页码**（不算编造），但用另一年的数答这一年的题是**静默错误**，
    必须被看见。把"题面年份外混入几条"量出来，正好作为 Step 4（混合检索+重排）
    和 Step 5（路由把年份抽成过滤条件）的对照基线 —— 那一步的收益要靠这个数字说话。
    """
    asked = set(_YEAR.findall(question or ""))
    if not asked:
        return [], []
    odd = []
    for c in out.get("citations") or []:
        cited = set(_YEAR.findall(c.get("citation") or ""))
        if cited and not (cited & asked):
            odd.append(f"[{c['index']}] {c['citation']}")
    return sorted(asked), odd


def _check(case: dict, out: dict, *, llm_on: bool) -> tuple[list[str], list[str]]:
    """返回 `(硬失败, 基线归因)`。两类都是非空列表里的说明字符串。

    `llm_on=False` 时降级是**预期行为**，不进任何一类。
    """
    exp = case["expect"]
    gap = exp.get("gap")
    hard: list[str] = []
    base: list[str] = []

    # 1) 必须拒答的：拒不拒、有没有偷偷给出处
    if exp.get("must_refuse"):
        if not out["refused"]:
            hard.append(f"应拒答但没有（refusal_reason={out['refusal_reason']}）")
        if out["citations"]:
            hard.append(f"拒答却给出了 {len(out['citations'])} 条引用 —— 给了出处就不能算拒答")
        return hard, base

    # 2) 不该拒答却被拒答的：完整性没问题，纯粹是"没拿到能答的材料"
    if out["refused"]:
        why = f"被拒答（{out['refusal_reason']}）：{(out['notes'] or [''])[0][:90]}"
        (base if gap else hard).append(why)
        return hard, base

    # 3) 引用完整性 —— 这几项**不看 gap**，任何情况下都是硬失败
    if exp.get("must_cite") and not out["citations"]:
        hard.append("没有引用（答案不可核验）")

    for c in out["citations"]:
        if not c.get("citation"):
            hard.append("引用缺少 citation 文本")
        elif not _PAGE.search(c["citation"]):
            hard.append(f"引用里没有页码：{c['citation']}")
        if exp.get("cite_code") and c.get("code") != exp["cite_code"]:
            hard.append(f"题目限定了 {exp['cite_code']}，却引用了 {c.get('code')} {c.get('citation')}")

    if exp.get("cite_has"):
        blob = " ".join((c.get("snippet") or "") for c in out["citations"])
        if not any(k in blob for k in exp["cite_has"]):
            hard.append(f"被引用片段的原文里找不到任何关键词 {exp['cite_has']}（可能引错了页）")

    dangling = (out.get("cite_check") or {}).get("dangling") or []
    if dangling:
        hard.append(f"答案正文的编号 {dangling} 在引用列表里没有落点（死链）")

    # 4) 降级：--no-llm 下是设计好的结果；其它情况按 gap 归类
    if llm_on and out["degraded"]:
        msg = "走了降级路径（没生成综合答案）：" + (out["notes"] or ["?"])[0][:90]
        (base if gap else hard).append(msg)
    return hard, base


def _check_routed(case: dict, out: dict) -> tuple[list[str], list[str]]:
    """判"设计路径"型的用例：**不问引用，问路由与结论**。

    为什么必须换一套判据：数值题的正确答案来自**工具层**（零幻觉通道），
    它**不生成引用**（引用那是 RAG 的口径）。拿"必须有引用""片段里要有某个词"去判它，
    测的是系统**故意不走**的那条路 —— 那样得到的失败既不是缺陷，也掩盖了真实缺陷。
    这里断言四件事：① 路由到了期望意图；② 答案里出现了期望值；
    ③ verify 判定 supported；④ 没有意外挂起。
    """
    exp = case["expect"]
    hard: list[str] = []
    base: list[str] = []
    route = out.get("route") or {}
    intent = route.get("intent")

    if intent != exp["via"]:
        hard.append(f"路由到 {intent}，期望 {exp['via']}（rule={route.get('rule')}）")
    if (out.get("hitl") or {}).get("pending"):
        hard.append(f"意外挂起：{(out.get('hitl') or {}).get('reason')}")
    if out.get("refused"):
        hard.append(f"被拒答（{out.get('refusal_reason')}）")

    ans = out.get("answer") or ""
    missing = [v for v in exp.get("answer_has") or [] if v not in ans]
    if missing:
        hard.append(f"答案里没有期望值 {missing}")

    v = out.get("verify") or {}
    if not v.get("checked"):
        base.append("没有 verify 结论（可能走的是不校验的路径）")
    elif not v.get("supported"):
        hard.append(f"verify 判不通过：unsupported={v.get('unsupported')}")

    tools = out.get("tool_calls") or []
    if exp["via"] == "analysis" and not tools:
        hard.append("数值题声称走工具层，但 tool_calls 为空 —— 数字来源不明")
    return hard, base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="不调大模型（只验检索/引用/拒答）")
    ap.add_argument("--json", action="store_true", help="附完整 JSON")
    ap.add_argument("--only", help="只跑 id 里含该子串的用例")
    ap.add_argument("--no-pdf-check", action="store_true",
                    help="跳过「引用片段是否真在源 PDF 该页」的核验")
    ap.add_argument("--mode", choices=sorted(config.RETRIEVE_MODES),
                    help="检索模式（默认 config.RETRIEVE_MODE）")
    args = ap.parse_args()

    get_pages = None if args.no_pdf_check else _pdf_page_loader()

    ready, why = llm.is_ready()
    st = pipeline.index_stats()
    print("=" * 78)
    print("端到端冒烟（Step 3 链路 + Step 4 混合检索 + Step 5 路由/校验）")
    print("=" * 78)
    print(f"索引     : {st}")
    print(f"大模型   : provider={llm.get_state()['provider']} model={llm.get_state()['model']} "
          f"ready={ready}" + ("" if ready else f"（{why}）"))
    print(f"本次模式 : {'不调大模型（降级为原文摘录）' if args.no_llm else '调用大模型'}"
          f" / 检索 {args.mode or config.RETRIEVE_MODE}")
    print("鉴权     : 本脚本是进程内跑图（不发 HTTP 请求），不经过 Bearer 鉴权层 —— "
          "本结果只对问答链路成立，不代表鉴权路径已被验证"
          "（后者见 tests/test_server_auth.py 与 scripts/smoke_step6.py）")
    if not args.no_llm and not ready:
        print("⚠️ 当前通道没配 Key，实际会走降级路径 —— 想看真实合成答案请先配 Key。")
    print()

    cases = [c for c in CASES if not args.only or args.only in c["id"]]
    failed: list[str] = []
    baseline: list[str] = []
    results: list[dict] = []
    year_stat = {"asked": 0, "cites": 0, "odd": 0}
    pdf_stat = {"ok": 0, "fail": 0, "skip": 0}

    for i, case in enumerate(cases, start=1):
        print("-" * 78)
        print(f"[{i}/{len(cases)}] {case['id']}")
        print(f"Q: {case['question']}")
        exp = case["expect"]
        # 两条判定路径：默认走 RAG 子图（Step 3 口径，看引用）；
        # 声明了 `via` 的走完整主图（Step 5 口径，看路由/结论/verify）。
        if exp.get("via"):
            out = _q_routed(case["question"], exp.get("cite_code"), not args.no_llm,
                            mode=args.mode)
            hard, base = _check_routed(case, out)
            print(f"   路由: intent={(out.get('route') or {}).get('intent')} "
                  f"rule={(out.get('route') or {}).get('rule')} "
                  f"conf={(out.get('route') or {}).get('confidence')}")
            for t in (out.get("tool_calls") or []):
                print(f"   工具: {t.get('name')} → {str(t.get('summary'))[:110]}")
            v = out.get("verify") or {}
            print(f"   verify: checked={v.get('checked')} supported={v.get('supported')} "
                  f"unsupported={v.get('unsupported')}")
        else:
            out = _q(case["question"], exp.get("cite_code"), not args.no_llm,
                     mode=args.mode)
            hard, base = _check(case, out, llm_on=not args.no_llm)

        flag = "❌" if hard else ("📉" if base else "✅")
        print(f"{flag} refused={out['refused']} degraded={out['degraded']} "
              f"confidence={out['confidence']} citations={len(out['citations'])}")
        if out["refused"]:
            print(f"   拒答原因: {out['refusal_reason']}")
        print(f"   证据覆盖度: {(out.get('evidence') or {}).get('ratio')} "
              f"(拼全部召回 {(out.get('evidence') or {}).get('ratio_union')})")
        ans = (out.get("answer") or "").replace("\n", " ")
        print(f"   答案: {ans[:240]}{'…' if len(ans) > 240 else ''}")

        asked_years, odd = _year_mix(case["question"], out)
        if asked_years:
            year_stat["asked"] += 1
            year_stat["cites"] += len(out["citations"])
            year_stat["odd"] += len(odd)
            print(f"   年份核对: 题面 {asked_years} → "
                  f"{'年份一致' if not odd else f'⚠️ 年份不符 {len(odd)} 条'}")
            for o in odd:
                print(f"      ⚠️ {o}")

        if out["citations"]:
            print("   —— 引用（逐条核：页码真的对得上吗）——")
            for c in out["citations"]:
                mark = ""
                if get_pages is not None:
                    ok = verify_on_pdf(c, get_pages)
                    pdf_stat["ok" if ok else ("skip" if ok is None else "fail")] += 1
                    mark = {True: " [PDF核验 ✅ 该页确有此文字]",
                            False: " [PDF核验 ❌ 该页找不到此文字 → 页码可疑]",
                            None: " [PDF核验 — 无法核验（PDF 缺失或页码越界）]"}[ok]
                    if ok is False:
                        hard.append(f"引用 [{c['index']}] {c['citation']} 的片段在源 PDF 该页找不到")
                print(f"   [{c['index']}] {c['citation']}{mark}")
                print(f"       {(c.get('snippet') or '')[:100]}")
        else:
            print("   （没有引用）")
        for n in (out.get("notes") or []):
            print(f"   note: {n[:110]}")

        for e in hard:
            print(f"   ❌ {e}")
        for b in base:
            print(f"   📉 {b}")
            print(f"      归因：{case['expect'].get('gap')}")
        failed.extend(f"{case['id']}: {e}" for e in hard)
        baseline.extend(f"{case['id']}: {b}" for b in base)
        if args.json:
            results.append({"case": case["id"], "hard_errors": hard, "baseline": base,
                            "year_mismatch": odd, "response": out})
        print()

    # 限定 code 的过滤校验：独立跑一遍，避免和上面的语义校验搅在一起
    print("-" * 78)
    print("[附加] 元数据过滤：限定 code=600519 时不许混入别家公司")
    filt = _q("2024年的营业收入是多少", "600519", not args.no_llm)
    codes = {c.get("code") for c in filt["citations"]}
    if codes and codes != {"600519"}:
        failed.append(f"code 过滤: 引用了 {codes}，应只有 600519")
        print(f"   ❌ 引用了 {codes}")
    else:
        print(f"   ✅ 引用公司集合 = {codes or '（无引用）'}")

    print()
    print("=" * 78)
    print(f"结论：{len(cases)} 条用例 → 通过 {len(cases) - len(failed) - len(baseline)} / "
          f"基线 {len(baseline)} / 失败 {len(failed)}")
    if year_stat["asked"]:
        rate = year_stat["odd"] / year_stat["cites"] if year_stat["cites"] else 0
        mode_now = (args.mode or config.RETRIEVE_MODE)
        print(f"跨年份混入：{year_stat['asked']} 条题面含年份的问题共 "
              f"{year_stat['cites']} 条引用，其中 {year_stat['odd']} 条落在题面年份之外"
              f"（{rate:.0%}）。")
        if mode_now == "bm25":
            print("  当前是 bm25 模式（无元数据过滤）：BM25 只看字面相似、不认识"
                  "「问的是哪一年」，同一条指标换一年的措辞会被先召回 —— "
                  "引用页码是真的，但用别年的数答这一年的题属于静默错误。"
                  "换 `--mode hybrid` 会通过自动过滤消除这类混入。")
        else:
            print("  当前是 hybrid 模式：公司/年份由问题文本自动推断成元数据过滤条件，"
                  "所以引用理论上不该跨公司跨年份 —— 这里出现非 0 就是过滤失效的信号"
                  "（重点检查 filters.detect 有没有漏抽）。")
    if get_pages is not None:
        tot = pdf_stat["ok"] + pdf_stat["fail"] + pdf_stat["skip"]
        print(f"页码核验（对源 PDF）：{tot} 条引用 → 命中 {pdf_stat['ok']} / "
              f"对不上 {pdf_stat['fail']} / 无法核验 {pdf_stat['skip']}。"
              f"这一步是拿引用片段回源 PDF 那一页找原文，绕开了我们自己的切分产物，"
              f"能发现「页码绑错」这类别的检查看不见的错。")
    if failed:
        print(f"❌ 失败 {len(failed)} 项（引用完整性/拒答策略，属真缺陷）：")
        for f in failed:
            print(f"   - {f}")
    if baseline:
        print(f"📉 基线 {len(baseline)} 项（完整性没问题，是召回精度卡住 → Step 4/5）：")
        for b in baseline:
            print(f"   - {b}")
    if not failed:
        print("✅ 没有完整性缺陷：没有假页码、没有串公司、没有死链、该拒答的拒了。")
    print("👉 剩下的只有人眼能判：上面每一页的文字，是否真的支持那个结论。")
    print("=" * 78)

    if args.json:
        outp = config.DATA_DIR / "smoke_qa_result.json"
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(
            {"version": "0.4.0", "llm_on": not args.no_llm,
             "summary": {"passed": len(cases) - len(failed) - len(baseline),
                         "baseline": len(baseline), "failed": len(failed)},
             "year_mix": year_stat, "cases": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"完整 JSON 已写入 {outp}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
