"""从问题里抽「公司 / 年份」—— 生成**元数据过滤条件**。

## 这一步解决的是什么问题（不是锦上添花）

Step 3 的冒烟实测暴露了一个硬伤：**离线 6/20 条引用落在题面年份之外**。
问"2024 年毛利率"，答案里却挂着 2025 年报的片段 —— 因为 BM25 只看字面，
"毛利率"在两年年报里都高频，谁分高谁进 top5。用户不问"哪一年"时无所谓，
**一旦问句里点名了年份，混进别的年份就是错的**。

过滤条件能整段消除这类错误，而且成本几乎为零（就是几个数组比较）。
所以它是 Step 4 里性价比最高的一项，先做。

## 纪律：**抽不到就不加过滤，绝不猜**

猜错的后果是不对称的：
- 漏抽（该过滤没过）→ 结果里混进别家/别年 → 用户能看出来；
- 抽错（把"比亚迪"认成"中国平安"）→ 结果**全部是错的且看起来很正常** → 灾难。

所以本模块的所有判断都要求**唯一命中**，命中多条一律放弃过滤并把歧义写进 note。
问题里出现多个年份（典型的对比题"2023 和 2024 相比"）同理：不过滤，
因为过滤掉哪一个都是错的。

## 已知不足（写在文档里，不藏着）

**相对年份不做解析**："去年/今年/前年"不转成具体年份 —— 得先知道"今天"是什么时候，
而同一句话在元旦前后含义不同，问答系统的答案却会被缓存/引用很久。
与其猜，不如让用户说清年份（`note` 会提示）。

本模块是**纯函数**：不读索引、不联网，输入只有问题文本与 watchlist，便于单测。
"""
from __future__ import annotations

import re

from src import config

# 4 位年份：前后不能再有数字（否则 12345 里的 2024 会被误判成年份）。
# 不限制前两位（19xx 也认），因为老公司的早期报告确实存在——认出来但库里没有时，
# 上层会给出"库里没有该年份"的明确提示，比"没识别到年份"更有用。
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# 看起来像年份但其实是别的数字：金额/比例常写成 2024 千元、2024 万股这类。
# 出现这些后缀时，前面的 4 位数很可能是**数量**而不是年份。
_NOT_A_YEAR_SUFFIX = ("千元", "万元", "亿元", "万股", "元", "吨", "人", "个", "次", "%")

# 2 字后缀匹配的黑名单：这些词在财经语境里是**行业/组织通用词**，不足以认出公司。
# 例：「中国的银行股表现」不该被认成"中国银行"（后缀"银行"命中），
#     「这个时代」不该被认成"宁德时代"（后缀"时代"命中）。
# 判据：如果这个词单独出现在问题里、用户大概率不是在指某一家特定公司，就该进黑名单。
_GENERIC_SUFFIXES = frozenset({
    # 组织形态
    "集团", "股份", "公司", "控股", "有限", "实业", "国际", "发展", "投资", "资产",
    # 金融
    "银行", "保险", "证券", "基金", "信托", "期货", "租赁",
    # 行业
    "科技", "电子", "通信", "软件", "网络", "电力", "能源", "汽车", "医药", "生物",
    "地产", "建筑", "钢铁", "煤炭", "有色", "化工", "机械", "食品", "饮料", "电器",
    "环保", "农业", "物流", "港口", "航空", "铁路", "传媒", "石油", "石化", "时代",
})

_WATCHLIST_CACHE: list[dict] | None = None


def _watchlist() -> list[dict]:
    global _WATCHLIST_CACHE
    if _WATCHLIST_CACHE is None:
        rows = []
        for c in config.load_watchlist():
            code = str(c.get("code") or "").strip()
            name = str(c.get("name") or "").strip()
            if code or name:
                rows.append({"code": code, "name": name,
                             "industry": c.get("industry"), "years": c.get("years") or []})
        _WATCHLIST_CACHE = rows
    return _WATCHLIST_CACHE


def reset_cache() -> None:
    """测试用：丢掉 watchlist 缓存。"""
    global _WATCHLIST_CACHE
    _WATCHLIST_CACHE = None


# ---------------- 公司 ----------------

def detect_company(question: str) -> dict:
    """识别问题里的公司。返回 `{code, company, matched, ambiguous, note}`。

    三级匹配（**从最可靠到最宽松**，一旦命中就不再往下走）：
    1. 股票代码（6 位数字）—— 最可靠，用户给代码就是明确的；
    2. 公司全称（watchlist 里的 name）—— 可靠；
    3. 名称的 **2~4 字后缀**（"贵州茅台"→"茅台"、"中国平安"→"平安"）——
       用户口语里常说简称。**必须唯一命中**：命中两家就放弃（"平安"可能同时指
       中国平安与平安银行），并把歧义写进 note；后缀在黑名单里的直接跳过。

    为什么用"后缀"而不是"任意子串"：中文公司名的区分度集中在尾部（品牌词），
    前缀多是地名/行政词（中国、贵州、广东）。用任意子串会让"中国"匹配上一堆公司。
    """
    q = question or ""
    result = {"code": None, "company": None, "matched": None,
              "ambiguous": [], "note": None}
    wl = _watchlist()
    if not q.strip() or not wl:
        return result

    # 1) 代码
    codes = {c["code"] for c in wl if c["code"]}
    for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", q):
        if m.group(1) in codes:
            row = next(c for c in wl if c["code"] == m.group(1))
            return {**result, "code": row["code"], "company": row["name"] or row["code"],
                    "matched": {"kind": "code", "text": m.group(1)},
                    "note": f"按股票代码识别为公司 {row['name'] or row['code']}"
                            f"（{row['code']}），已限定检索范围。"}

    # 2) 全称（**长名优先**：避免"中国平安"先被更短的别名命中）
    for row in sorted(wl, key=lambda c: -len(c["name"] or "")):
        if row["name"] and row["name"] in q:
            return {**result, "code": row["code"], "company": row["name"],
                    "matched": {"kind": "name", "text": row["name"]},
                    "note": f"按公司全称识别为 {row['name']}（{row['code']}），"
                            f"已限定检索范围。"}

    # 3) 后缀简称（4→2 字逐级放宽），要求唯一命中且不在黑名单
    for n in (4, 3, 2):
        hits = []
        for row in wl:
            name = row["name"] or ""
            if len(name) <= n:
                continue
            suffix = name[-n:]
            if suffix in _GENERIC_SUFFIXES:
                continue
            if suffix in q:
                hits.append(row)
        if len(hits) == 1:
            row = hits[0]
            suffix = row["name"][-n:]
            return {**result, "code": row["code"], "company": row["name"],
                    "matched": {"kind": "short_name", "text": suffix},
                    "note": f"问题里的「{suffix}」按简称匹配到 {row['name']}"
                            f"（{row['code']}），已限定检索范围 —— 如理解有误请在问题里写明全称。"}
        if len(hits) > 1:
            names = [f"{r['name']}（{r['code']}）" for r in hits]
            return {**result, "ambiguous": names,
                    "note": f"简称命中多家公司（{ '、'.join(names) }），"
                            f"**未做公司过滤**，请指明是哪一家。"}
    return result


# ---------------- 年份 ----------------

def detect_year(question: str) -> dict:
    """识别问题里的年份。返回 `{year, years, matched, note}`。

    规则：
    - 只认 4 位数字年份，且**不能紧跟在**金额/数量单位前（"2024 千元"是数量不是年份）；
    - 出现**恰好一个**年份才作为过滤条件；
    - 出现多个（对比题）→ 不过滤 + note 说明；
    - "去年/今年/前年"**不解析**（见模块头「已知不足」）。
    """
    q = question or ""
    found: list[int] = []
    for m in _YEAR.finditer(q):
        # 取窗口后要**先去空白**再比单位：年报里"2024 千元"中间常有空格/换行，
        # 不 lstrip 就判不出单位，会把数量当成年份（实测踩过）。
        tail = q[m.end(): m.end() + 4].lstrip()
        if any(tail.startswith(u) for u in _NOT_A_YEAR_SUFFIX):
            continue
        y = int(m.group(1))
        if y not in found:
            found.append(y)

    if not found:
        rel = [w for w in ("去年", "今年", "前年", "上年", "本年") if w in q]
        note = ("问题含相对年份表述"
                + "、".join(f"「{w}」" for w in rel)
                + "，本系统**不把相对年份转成具体年份**（同一句话在不同时间含义不同），"
                  "请写明 4 位年份。") if rel else None
        return {"year": None, "years": [], "matched": None, "note": note}

    if len(found) > 1:
        return {"year": None, "years": found, "matched": {"kind": "multi_year"},
                "note": f"问题里出现多个年份（{ '、'.join(str(y) for y in found) }），"
                        f"**未做年份过滤** —— 过滤掉任何一个都可能让对比题答错。"
                        f"如需只查某一年，请单独提问。"}
    return {"year": found[0], "years": found, "matched": {"kind": "year", "text": str(found[0])},
            "note": None}


# ---------------- 合并 ----------------

def detect(question: str) -> dict:
    """一次抽出过滤条件（公司 + 年份），供 `pipeline.retrieve` 直接用。

    `override_*` 由调用方（比如 API 显式传了 code/year）在 pipeline 里覆盖，
    本函数只负责"从文本里读出来"。
    """
    comp = detect_company(question)
    yr = detect_year(question)
    notes = [n for n in (comp.get("note"), yr.get("note")) if n]
    return {
        "code": comp["code"], "company": comp["company"],
        "year": yr["year"],
        "evidence": {"company": comp["matched"], "year": yr["matched"]},
        "years_seen": yr["years"],
        "ambiguous": comp["ambiguous"],
        "notes": notes,
        # `filtered` 让上层一眼看出"这次到底过没过滤"，日志与 debug 都用它
        "filtered": bool(comp["code"] or yr["year"]),
    }


# ---------------- 多轮指代消解（Step 6）----------------
#
# `detect` 只看**单句**。多轮场景里"它的毛利率呢"这种问句抽不到公司，
# 但用户心里指的是上一轮那一家。本组函数负责"从最近一轮里把实体接过来"。
#
# 纪律不变，而且多轮下**更要克制**：
# - 本轮显式实体永远优先（用户改了对象就跟着改，绝不拿历史覆盖）；
# - 上一轮不唯一（对比题多家公司）或为空时**不补**——猜错的对象会让整段答案错得
#   毫无痕迹；宁可让用户再说一遍"是哪家"。
# - 年份**不跨公司沿用**：把 A 公司的年份安到 B 公司头上，是典型的静默口径错误。

def _history_last(history: list[dict] | None) -> dict | None:
    """取最近一轮（`history` 按旧→新排列，末位最新）。非 dict 项一律忽略。"""
    if not history:
        return None
    last = history[-1]
    return last if isinstance(last, dict) else None


def _history_company(round_: dict | None) -> str | None:
    """从一轮记录里取**唯一**的公司代码；为空或对应多家时返回 None（不猜）。

    支持 `code`（单个）与 `codes`（对比题那种多值列表）两种写法：
    `codes` 多于一个即视为"不唯一"，一律放弃沿用。
    """
    if not round_:
        return None
    codes = round_.get("codes")
    if codes is None and isinstance(round_.get("code"), (list, tuple, set)):
        codes = list(round_["code"])
    if codes is not None:
        uniq = [str(c).strip() for c in codes if str(c or "").strip()]
        uniq = list(dict.fromkeys(uniq))
        return uniq[0] if len(uniq) == 1 else None
    code = round_.get("code")
    code = str(code).strip() if code is not None else ""
    return code or None


def _history_year(round_: dict | None) -> int | None:
    """从一轮记录里取**唯一**的年份；为空或出现多个（对比题）时返回 None。"""
    if not round_:
        return None
    years = round_.get("years")
    if years is None and isinstance(round_.get("year"), (list, tuple, set)):
        years = list(round_["year"])
    if years is not None:
        vals: list[int] = []
        for y in years:
            try:
                vals.append(int(y))
            except (TypeError, ValueError):
                continue
        vals = list(dict.fromkeys(vals))
        return vals[0] if len(vals) == 1 else None
    y = round_.get("year")
    try:
        return int(y) if y is not None else None
    except (TypeError, ValueError):
        return None


def _company_name_of(code: str) -> str | None:
    """按代码在 watchlist 里查公司名（查不到就回代码本身）。"""
    for row in _watchlist():
        if row["code"] == code:
            return row["name"] or code
    return code


def trim_history(history: list[dict] | None, max_rounds: int | None = None) -> list[dict]:
    """只保留最近 N 轮（N = `config.HISTORY_MAX`，默认 5）。

    裁剪不是省内存，而是**降低误沿用风险** —— 指代消解只看最近一轮，
    更早的轮次留着只会让"上一轮指哪家"更含糊。返回新列表（不改调用方入参）。
    """
    items = [h for h in (history or []) if isinstance(h, dict)]
    cap = config.HISTORY_MAX if max_rounds is None else max_rounds
    if cap is None or cap < 0:
        return items
    return items[-cap:] if cap > 0 else []


def resolve_entities(question: str, history: list[dict] | None = None) -> dict:
    """抽出过滤条件，并在问句缺公司/年份时**从最近一轮沿用**（多轮指代消解）。

    规则：
    - **本轮显式实体优先**：问句里认出的公司/年份，绝不用 history 覆盖；
    - 公司：本轮认不出、且最近一轮有**唯一** code 时沿用，并在 note 写明来源；
      最近一轮为空或对应多家公司（`codes` 多值）时**不补**；
    - 年份：本轮认不出时可沿用最近一轮，但**不跨公司** —— 只有当最终确定的公司
      与最近一轮的公司相同时才沿用（否则就是把上一家的年份安到这一家头上）。

    返回值与 `detect` 兼容（多出 `sources` / `from_history` / `note` 三个键），
    这样 `pipeline._apply_auto_filter` 可以无缝替换原来的 `detect` 调用。
    """
    det = detect(question)
    last = _history_last(history)
    h_code = _history_company(last)
    h_year = _history_year(last)

    notes = list(det.get("notes") or [])
    sources = {"company": "current" if det.get("code") else None,
               "year": "current" if det.get("year") else None}

    if not det.get("code") and h_code:
        det["code"] = h_code
        det["company"] = _company_name_of(h_code)
        sources["company"] = "history"
        notes.append(f"沿用上一轮的公司：{det['company'] or h_code}")

    # 年份沿用要求"公司一致"：公司来自上一轮，或本轮显式公司正好与之相同。
    # 只要最终公司不等于上一轮的公司，就不沿用 —— 这是"不跨公司"的落地。
    if not det.get("year") and h_year and h_code and det.get("code") == h_code:
        det["year"] = h_year
        sources["year"] = "history"
        notes.append(f"沿用上一轮的年份：{h_year}（与上一轮同一家公司，未跨公司沿用）")

    det["notes"] = notes
    det["note"] = "\n".join(notes) if notes else None
    det["sources"] = sources
    det["from_history"] = any(v == "history" for v in sources.values())
    det["filtered"] = bool(det.get("code") or det.get("year"))
    return det


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    samples = [
        "贵州茅台2024年的毛利率是多少",
        "茅台去年的归母净利润",
        "600519 近三年营业收入趋势",
        "中国平安的归母净资产是多少",
        "2023年和2024年五粮液的营收对比",
        "宁德时代的研发投入",
        "这个时代最好的公司是哪家",
        "公司食堂菜谱",
        "比亚迪 2024 年营业成本中 2024 千元的项目",
    ]
    for s in samples:
        d = detect(s)
        print(f"\nQ: {s}")
        print("   ->", json.dumps({k: d[k] for k in
                                  ("code", "company", "year", "years_seen", "filtered")},
                                 ensure_ascii=False))
        for n in d["notes"]:
            print("   note:", n)
