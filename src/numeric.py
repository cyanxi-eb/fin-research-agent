"""数值抽取与"能否对上"的比对 —— 零幻觉校验的公共零件。

被两处使用，且**必须是同一份实现**：
- `src/graph/subgraph_analysis.py`：极端保守地只用工具层数值组句；
- `src/graph/verify.py`：把答案里的每个数字回查到"证据集合"（工具返回 / 引用原文）。

如果两边各写一套"什么算数字、怎么算相等"，就会出现
"分析侧认为 1,741.44 亿元合法、校验侧认为它匹配不上"这种自相矛盾，
而两边各自的单测都会通过 —— 这类不一致只能靠共用实现来消除。

## 什么不算"答案里的数字"（必须排除，否则误报淹没有效信号）

1. `[3]` 引用角标 —— 它指向第 3 条证据，不是数据；
2. `第20条` / `第五章` / `第二款` —— 法规与年报的**条文编号**，不是财务数据；
3. 列举序号 `1) 2)` —— 排版产物；
4. URL 内部的数字 —— 来源行会带年报原文链接（`…/finalpage/2025-04-03/1222993920.PDF`），
   链接里的日期与公告 id 不是财务数据，不豁免就会把一条完全正确的答案判成
   "有 4 个无出处数字"并挂起。

排除得干净，`unsupported` 才是"真的有一个数说不清出处"这种**可行动**的信号。
"""
from __future__ import annotations

import json
import re

# 数字：整数/小数/千分位（1,741.44）。中文数字（"一亿"）不在此列 —— 财务正文里
# 关键数值几乎都是阿拉伯数字，扩到中文数字会让"第十二条"这类条文编号大量误入。
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

# URL（含裸 www.）：整段从扫描文本里"挖掉"。替换成**等长空格**而不是直接删除，
# 是为了不挪动其余数字的 start/end 偏移（调用方用偏移回指原文）。
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# 紧邻"第…条/章/款/项/号"的编号。
#
# 为什么把 `号` 也放进来：法规引用里必带文号（`证监会令第182号`），
# 而文号是**标识符不是数据**。不排除的话，每条合规回答都会因为
# "182 / 226 这两个数在证据里找不到"而被判 `citation_unsupported` → 无谓挂起，
# 待确认队列会被这种噪音占满，真正需要人看的反而被埋掉（实测踩过）。
_ARTICLE_TAIL = "条章节款项目号"
# 单位换算：把"亿元/万元"形式的展示值换回原始值（元）
_UNIT_FACTORS = {"亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2}


def clean_number(raw: str) -> float | None:
    """把匹配到的数字串转成 float（去千分位）。转不动返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    # "1,741.44" → "1741.44"；"1.741,44"（欧式）不处理，年报不用该写法
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def extract_numbers(text: str) -> list[dict]:
    """抽出文本里**有意义的**数字，返回 `[{raw, value, start, end}]`。

    排除项见模块 docstring（引用角标 / 条文编号 / 列举序号 / URL 内部数字）。
    """
    out: list[dict] = []
    t = _URL_RE.sub(lambda m: " " * len(m.group(0)), text or "")
    for m in _NUM_RE.finditer(t):
        raw, s, e = m.group(0), m.start(), m.end()
        # 1) 引用角标 [3]
        if s > 0 and t[s - 1] == "[" and e < len(t) and t[e] == "]":
            continue
        # 2) 第20条 / 第五章（含"第20条款"这种连写）
        if s > 0 and t[s - 1] == "第" and e < len(t) and t[e] in _ARTICLE_TAIL:
            continue
        # 3) 列举序号 "1)" "2）"
        if e < len(t) and t[e] in "）)":
            continue
        v = clean_number(raw)
        if v is None:
            continue
        out.append({"raw": raw, "value": v, "start": s, "end": e})
    return out


def text_numbers(text: str) -> list[float]:
    """只取值列表（覆盖度/支持度判断用）。"""
    return [n["value"] for n in extract_numbers(text)]


def number_variants(value: float) -> list[float]:
    """一个数值的**等价写法**集合。

    为什么需要：工具返回的是原始单位（元，如 174144000000.0），
    而答案里写的是展示单位（"1,741.44 亿元"）。两者指的是同一个数，
    不做换算就会把**正确的答案**判成"数字无出处" —— 这种误报比漏报更糟，
    因为它会让人开始怀疑校验本身。
    """
    vs: list[float] = [value]
    for f in _UNIT_FACTORS.values():
        vs.append(value / f)
        vs.append(value * f)
    # 四舍五入到 0~4 位小数（展示层常用 2 位）
    for n in range(0, 5):
        vs.append(round(value, n))
        for f in _UNIT_FACTORS.values():
            vs.append(round(value / f, n))
    return vs


def collect_evidence_numbers(evidence: object) -> list[float]:
    """从任意证据对象（工具返回 / 命中片段列表 / 字典）里收集**所有**数值。

    做法：序列化成 JSON 文本再抽数字。比手写递归更省心，也不会漏掉
    藏在嵌套 `display` / `official` / `terms` 里的展示值 ——
    那些恰恰是答案里真正出现的写法。
    """
    if evidence is None:
        return []
    try:
        blob = json.dumps(evidence, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(evidence)
    return text_numbers(blob)


def is_supported(value: float, allowed: list[float]) -> bool:
    """`value` 能否用 `allowed` 里的某个数解释（含单位换算与展示层舍入）。

    容差取 `max(0.1% 相对, 0.005 绝对)`：既容得下"原始值 174143550000 写成 1,741.44 亿"
    （0.003% 误差），又拦得住"91.93% 被写成 92.5%"（0.6% 误差）这种真实错报。
    """
    for a in allowed:
        for v in number_variants(a):
            tol = max(abs(v) * 0.001, 0.005)
            if abs(value - v) <= tol:
                return True
    return False


def unsupported_numbers(text: str, allowed: list[float]) -> list[dict]:
    """答案里**说不清出处**的数字（去重保序）。空列表 = 零幻觉这一条通过。"""
    seen: dict[str, dict] = {}
    for n in extract_numbers(text):
        if is_supported(n["value"], allowed):
            continue
        seen.setdefault(n["raw"], n)
    return list(seen.values())
