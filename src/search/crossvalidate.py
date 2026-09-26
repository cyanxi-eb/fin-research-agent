"""交叉验证 —— 判定"多个来源是不是在说同一件事"，并给出**可解释**的结论。

## 为什么要"按域名去重"

"多个来源一致"这句话最大的漏洞是**转载**：同一篇通稿被 5 家媒体转载，
在搜索结果里就是 5 条看起来互相印证的证据，实际只有 1 个来源。
所以独立性以**域名**为单位：同一域名的多篇（含同文转载）只算 1 个来源。
这条不守住，交叉验证就成了"给单源结论盖章"的装置。

## 为什么是确定性判据，而不是再叫一次模型

金融问答里"两个来源说法不一致"必须能**指出分歧在哪**（谁给了哪个数字）。
让模型去判，得到的是一句"两来源基本一致"的自然语言，无法核对、不可复现，
而且把一个非确定性的环节塞进了本来就要求"可复现"的验收链路里。
本轮判据因此是确定性的：**问题里的关键实体覆盖 + 正文中关键数字是否一致**。
分四档：`consistent`（≥2 独立域名且关键数字有交集）/ `conflict`（都有数字但无交集）/
`partial`（独立来源凑不到 2 个有证据的）/ `insufficient_sources`（独立域名本身就 < 2）。

## 数字为什么要减掉问题里的数字

问题「贵州茅台 2024 年的营业总收入」里有 `2024`。这个数字在**所有**来源里都会出现，
把它算进一致性判定，等于让"两家来源都提到 2024 年"冒充"两家来源说法一致"。
所以判定用的是"正文里出现、而问题里没有"的数字 —— 那些才是**答案本身**。
"""
from __future__ import annotations

import re
from collections import Counter

from src import config
from src.retrieve.bm25 import content_terms, normalize_for_match
from src.search.fetch import fetch_texts
from src.search.provider import SearchResult, domain_of

# 千分位先去掉，不然 "1,741.44" 会被分成两个数
_THOUSAND_SEP = re.compile(r"(?<=\d),(?=\d)")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

STATUSES = ("consistent", "partial", "conflict", "insufficient_sources")


def _numbers(text: str) -> set[str]:
    """抽数字并归一化（`1700.00` 与 `1700` 视为同一个数；单位不参与比较）。"""
    out: set[str] = set()
    for raw in _NUM_RE.findall(_THOUSAND_SEP.sub("", text or "")):
        norm = raw.rstrip("0").rstrip(".") if "." in raw else raw
        if len(norm) >= 2:            # 单字符数字（列表序号之类）是噪声
            out.add(norm)
    return out


def _source_entry(group: list[SearchResult]) -> dict:
    """一个域名的代表条目（按搜索结果的名次取第一条），并记下它背后有几篇转载。"""
    r = group[0]
    return {"title": r.title, "url": r.url, "source_name": r.source_name,
            "domain": domain_of(r.url), "fetched_at": r.fetched_at,
            "duplicates": len(group) - 1}


def cross_validate(claim: str, results: list[SearchResult], *,
                   fetcher=fetch_texts) -> dict:
    """返回固定六键：`{status, sources, agree, disagree, distinct_domains, note}`。

    `fetcher` 只做一件事：给还没正文的结果回填 `text`。单测与"缓存复用"直接灌好
    正文传进来，因此**这条路径默认不联网**。
    """
    items = list(results or [])
    problems = list(fetcher(items) or []) if fetcher is not None else []

    by_domain: dict[str, list[SearchResult]] = {}
    for r in items:
        key = domain_of(r.url) or (r.source_name or "").strip().lower()
        if key:
            by_domain.setdefault(key, []).append(r)
    domains = list(by_domain)

    claim_terms = content_terms(claim)
    claim_nums = _numbers(claim)

    evidence: dict[str, dict] = {}
    for d, group in by_domain.items():
        body = " ".join((r.text or r.snippet or "") for r in group)
        norm = normalize_for_match(body)
        hits = [t for t in claim_terms if normalize_for_match(t) in norm]
        nums = _numbers(" ".join(r.text for r in group)) - claim_nums
        evidence[d] = {
            "numbers": nums,
            "terms_hit": hits,
            # 「有证据」= 给出了可核对的关键数字，**或**问题里的关键实体全都出现在正文里
            "ok": bool(nums) or bool(claim_terms) and len(hits) == len(claim_terms),
        }

    supporting = [d for d in domains if evidence[d]["ok"]]

    status = "insufficient_sources"
    agreed: set[str] = set()
    if len(domains) >= config.WEB_SEARCH_MIN_DOMAINS:
        if len(supporting) < config.WEB_SEARCH_MIN_DOMAINS:
            status = "partial"
        else:
            with_nums = [d for d in supporting if evidence[d]["numbers"]]
            if len(with_nums) < config.WEB_SEARCH_MIN_DOMAINS:
                # 都没给数字，但都覆盖了问题里的关键实体 → 视为一致（并如实写明依据）
                status = "consistent"
            else:
                counts = Counter(n for d in with_nums for n in evidence[d]["numbers"])
                agreed = {n for n, c in counts.items()
                          if c >= config.WEB_SEARCH_MIN_DOMAINS}
                status = "consistent" if agreed else "conflict"

    if status == "insufficient_sources":
        agree: list = []
        disagree: list = []
    elif status == "conflict":
        agree = [d for d in supporting if evidence[d]["numbers"] & agreed]
        disagree = [{"domain": d, "numbers": sorted(evidence[d]["numbers"])}
                    for d in supporting if not (evidence[d]["numbers"] & agreed)]
    elif status == "partial":
        agree = list(supporting)
        disagree = [{"domain": d, "reason": "未给出可核对的关键数字，也未覆盖问题中的关键实体"}
                    for d in domains if d not in supporting]
    else:
        agree = list(supporting)
        disagree = []

    return {
        "status": status,
        "sources": [_source_entry(by_domain[d]) for d in domains],
        "agree": agree,
        "disagree": disagree,
        "distinct_domains": len(domains),
        "note": _note(status, domains, supporting, agreed, disagree, problems),
    }


def _note(status: str, domains: list[str], supporting: list[str], agreed: set[str],
          disagree: list, problems: list[str]) -> str:
    need = config.WEB_SEARCH_MIN_DOMAINS
    if status == "insufficient_sources":
        text = (f"独立来源不足：仅 {len(domains)} 个域名（要求至少 {need} 个）——"
                f"同一家媒体的转载只算 1 个来源，因此不给出一致性结论。")
    elif status == "partial":
        text = (f"证据不足：{len(supporting)}/{len(domains)} 个独立来源给出了可核对的关键数字"
                f"或覆盖了问题中的关键实体，达不到 {need} 个，因此不视为一致。")
    elif status == "conflict":
        parts = "；".join(
            f"{d['domain']} 给出 {'、'.join(d['numbers']) or '（无数字）'}" for d in disagree)
        text = f"来源之间存在分歧（{len(disagree)} 个来源）：{parts}。"
    else:
        basis = f"一致的关键数字：{'、'.join(sorted(agreed))}" if agreed else "各来源对问题中关键实体的说法一致"
        text = f"{len(supporting)} 个独立来源一致（{basis}）。"
    if problems:
        text += f" 另有 {len(problems)} 个来源正文抓取失败，未参与判定：{problems[0]}"
    return text