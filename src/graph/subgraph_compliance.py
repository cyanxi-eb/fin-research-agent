"""合规核查子图 —— 只查法规库、只给**条文原文**，不作合规结论。

## 为什么这里刻意"不给结论"

"这家公司是否违规"是**法律判断**，需要事实认定 + 条文适用 + 责任主体分析，
而本系统只有年报原文与法规条文，没有事实调查能力。硬给一个"是/否"，
用户拿到的是一个**看起来权威、实际无依据**的结论 —— 在合规场景里，这种错误
比"不回答"严重得多（可能被当作决策依据）。

所以这条链路的输出是："与你的问题相关的条文是这些（附条号与原文），
是否构成违规需由人结合事实判断"。这是**可核验的交付物**，而不是推卸责任 ——
用户真正需要的往往也正是"把条文找出来"。

## 版本必须显式

`prefer_current()` 已把现行版放主位；本模块再把"该年度适用哪一版"提示出来：
2025-07-01 之前适用 182 号，之后适用 226 号。用新法回答旧事（或反之）是合规问答里
最典型的"正确答案型错误" —— 结论方向对，法条依据错。
"""
from __future__ import annotations

from src import config
from src.graph.state import QAState
from src.graph.subgraph_analysis import detect_year
from src.retrieve import regulation

# 226 号的施行日：这一天之前的行为适用 182 号
CURRENT_EFFECTIVE_FROM = "2025-07-01"

COMPLIANCE_DISCLAIMER = (
    "以上仅为相关法规条文原文的检索结果，**不构成合规结论或法律意见**。"
    "是否违规需结合具体事实、适用版本与责任主体由人判断；"
    "条目版本以每条标注的文号与施行日期为准。")


def _version_note(year: int | None, docs: set[str]) -> list[str]:
    notes: list[str] = []
    if year and year < 2025:
        notes.append(f"问题涉及 {year} 年，该年度适用的是**证监会令第182号**"
                     f"（2021-03-18 施行）；226 号自 {CURRENT_EFFECTIVE_FROM} 起施行，"
                     f"不溯及该年度行为。引用时请对号入座。")
    if len(docs) > 1:
        notes.append("本次命中多个版本（" + "、".join(sorted(docs)) +
                     "）。同一条号以现行版（226 号）为准，"
                     "被取代版本仅用于「当时适用哪一版」的追溯。")
    return notes


def compliance_question(state: QAState) -> QAState:
    """合规问答：法规库检索 → 条文级引用 → 显式版本提示。"""
    question = state.get("question", "") or ""
    notes = list(state.get("notes") or [])

    try:
        regulation.index()
    except FileNotFoundError as e:
        return {
            "answer": ("【合规核查】法规库**尚未入库**，无法核对条文。\n\n"
                       f"原因：{e}\n\n"
                       "入库命令：`python -m src.ingest.fetch_regulation`"
                       "（下载证监会/国务院公报原文并建立独立索引）。\n\n"
                       "⚠️ 注意：这与「没查到相关规定」是两件不同的事 —— "
                       "现在的情况是**核对能力本身不可用**，因此不作任何合规判断。"),
            "refused": True, "refusal_reason": "regulation_not_ingested",
            "degraded": False, "confidence": 0.0, "citations": [],
            "evidence": {"kind": "compliance", "ingested": False},
            "notes": notes + ["法规索引缺失 → 拒答（这是环境状态，不是'无相关规定'）。"],
            "disclaimer": COMPLIANCE_DISCLAIMER,
        }

    hits = regulation.search(question, topk=max(config.REGULATION_TOP_K,
                                                config.REGULATION_MAX_ARTICLES * 2))
    if not hits:
        return {
            "answer": ("【合规核查】在**已入库的法规**中未检索到与该问题相关的条文，"
                       "因此不作合规判断。\n\n"
                       "可尝试：① 用条文里的标准说法提问（如「定期报告 披露 期限」）；"
                       "② 确认该事项是否属于已入库法规的管辖范围（当前库见下方证据）。"),
            "refused": True, "refusal_reason": "no_regulation_hit",
            "degraded": False, "confidence": 0.0, "citations": [],
            "evidence": {"kind": "compliance", "ingested": True, "returned": 0,
                         "library": regulation.stats()},
            "notes": notes + ["法规库已入库但本题零命中 → 拒答（这是结论，不是故障）。"],
            "disclaimer": COMPLIANCE_DISCLAIMER,
        }

    primary, superseded = regulation.prefer_current(hits)
    picked = primary[:config.REGULATION_MAX_ARTICLES]
    cites = regulation.citations(picked)
    year = state.get("year") or detect_year(question)

    lines = ["【合规核查】以下是与你问题相关的**法规条文原文**"
             "（只做条文检索与引用，不作合规结论）：", ""]
    for c, h in zip(cites, picked):
        lines.append(f"[{c['index']}] {c['citation']}")
        lines.append(f"    现行状态：{c['status']}"
                     + (f"（自 {c['effective_from']} 施行）" if c.get("effective_from") else ""))
        lines.append(f"    原文：{c['snippet']}" + ("…" if len(h['chunk'].get('text') or "") > 220 else ""))
        if c.get("source_name"):
            lines.append(f"    来源：{c['source_name']}")
        lines.append("")

    if superseded:
        lines.append("**版本差异提示**（同一条号在不同版本中实质不同，以现行版为准）：")
        for h in superseded[:2]:
            ch = h["chunk"]
            lines.append(f"    · {ch.get('doc_no')} 同条：{(ch.get('text') or '')[:160]}…")
        lines.append("")

    version_notes = _version_note(year, {c.get("doc_id") for c in cites})
    if version_notes:
        lines.append("**适用版本提示**")
        lines.extend(f"    · {n}" for n in version_notes)

    answer = "\n".join(lines).rstrip()
    nums = [c["article_no"] for c in cites]
    conf = 0.8 if len(picked) >= 1 else 0.5

    return {
        "answer": answer,
        "refused": False,
        "refusal_reason": None,
        "degraded": False,
        "confidence": conf,
        "citations": cites,
        "regulation_hits": [h["chunk"] | {"score": h["score"]} for h in picked],
        "evidence": {"kind": "compliance", "ingested": True,
                     "returned": len(hits), "picked": len(picked),
                     "articles": nums, "year": year,
                     "versions": sorted({c.get("doc_id") for c in cites}),
                     "superseded_included": len(superseded)},
        "notes": notes + [f"合规链路只引用法规库（独立索引）：命中 {len(hits)} 条，"
                          f"取前 {len(picked)} 条；引用为条文级"
                          f"（文号+条号），不含事实认定。"] + version_notes,
        "disclaimer": COMPLIANCE_DISCLAIMER,
    }
