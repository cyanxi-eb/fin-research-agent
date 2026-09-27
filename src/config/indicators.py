from __future__ import annotations
import json
import os
from pathlib import Path

from src.config.core import WATCHLIST_PATH

# ---- 财务指标口径表（唯一事实来源）----
# 为什么收口在这里：LLM 选指标时会用「营业收入 / 营业总收入 / 总营收」等不同说法，
# 别名不收口就会查不到数据、或把两个不同口径当成一个指标两套值。
#
# `sources` 是**按优先级排列的 (source_key, field) 列表**，代码取第一个非空值。
# 全部为实测确认值（2026-09-22，见 scripts/probe_eastmoney_finance.py 与 CHANGELOG 的对账记录），
# 不凭记忆写。实测对账样本：贵州茅台 2024 年报 营业总收入 1741.44 亿 / 归母净利润 862.28 亿 /
# 加权 ROE 36.02% / 毛利率 91.93% / 资产负债率 19.04%，与年报原文一致，金额单位为**元**。
#
# ⚠️ 两组**刻意拆开**的口径（不拆就会给错数）：
#   - 营业总收入 ≠ 营业收入：前者含利息收入/已赚保费/手续费及佣金。**实测茅台 2024 年报
#     营业总收入 1741.44 亿 vs 营业收入 1708.99 亿，差 32.45 亿（1.9%）** ——
#     连非金融公司都有差异，换成中国平安这类综合金融集团就是量级差异。
#   - 归母净利润 ≠ 净利润：后者含少数股东损益。
# 工具层返回结果时会带上实际命中的 source 与口径，使"用的是哪个口径"始终可见、可核对。
INDICATORS: dict[str, dict] = {
    # ---------- 利润表 ----------
    "营业总收入": {
        "aliases": ["营业总收入", "总营收", "营收"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "TOTAL_OPERATE_INCOME"),
                    ("dc_income", "TOTAL_OPERATE_INCOME")],
    },
    "营业收入": {
        "aliases": ["营业收入"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "OPERATE_INCOME"), ("dc_income", "OPERATE_INCOME")],
    },
    "归母净利润": {
        "aliases": ["归母净利润", "归属于上市公司股东的净利润",
                    "归属母公司股东的净利润", "归母净利"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "PARENT_NETPROFIT"), ("dc_income", "PARENT_NETPROFIT")],
    },
    "净利润": {
        "aliases": ["净利润"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "NETPROFIT")],
    },
    "扣非净利润": {
        "aliases": ["扣非净利润", "扣除非经常性损益后的净利润"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "DEDUCT_PARENT_NETPROFIT"),
                    ("dc_income", "DEDUCT_PARENT_NETPROFIT")],
    },
    "营业成本": {
        # 严格口径：报表现成的「营业成本」科目。制造业正常有值；**保险/金融业没有这一行**
        # （平安 2024 年报合并利润表的科目是「营业收入 / 营业支出 / 营业利润」，
        #   见 data/parsed/601318/2024.json 的财务报告页），所以对保险仍会报缺。
        # 保险的成本要看下面的「营业支出」，**不要把两者合并**：
        # 制造业的「营业总成本」还含销售/管理/研发/财务费用，与「营业成本」不是一个口径。
        #
        # `counterpart` 是给工具层用的**口径提示**：用户/LLM 问保险公司的「营业成本」时，
        # 不能只回"没有"，要说清"该科目在保险业不存在，对应的是营业支出，值是多少"。
        # 这比直接回一个数更负责——直接回数会让用户以为拿到了「营业成本」。
        "aliases": ["营业成本"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "counterpart": {"indicator": "营业支出",
                        "why": "保险/金融业利润表无「营业成本」科目，其成本行为「营业支出」"
                               "（报表科目名不同、适用行业不同，不能当同一个口径互换）"},
        "sources": [("income", "OPERATE_COST"),
                    ("dc_income", "OPERATE_COST"),
                    ("sina_income", "营业成本")],
    },
    "营业总成本": {
        "aliases": ["营业总成本"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表",
        "sources": [("income", "TOTAL_OPERATE_COST"),
                    ("dc_income", "TOTAL_OPERATE_COST"),
                    ("sina_income", "营业总成本")],
    },
    "营业支出": {
        # 金融/保险口径的成本行。**单独立项而不是并进「营业成本」**，因为：
        # 保险公司利润表没有「营业成本」科目，它的成本行是「营业支出」
        # （东财把它标准化成 TOTAL_OPERATE_COST，新浪叫「营业支出」）。
        # 两者数值对 601318 一致（8,572.76 亿），但**口径名称不同、适用行业不同** ——
        # 合进「营业成本」会让"制造业营业成本"和"保险营业支出"混成一条序列，
        # 那是最典型的静默口径错误。券商 App 上平安的"营业成本"通常是按本口径显示的。
        "aliases": ["营业支出", "营业总支出", "保险营业支出"],
        "unit": "元", "display_unit": "亿元", "statement": "利润表（金融/保险口径）",
        "sources": [("income", "TOTAL_OPERATE_COST"),
                    ("dc_income", "OPERATE_EXPENSE"),
                    ("sina_income", "营业支出")],
    },
    # ---------- 资产负债表 ----------
    # 后两个回退源是给保险公司准备的：实测中国平安在 F10 资产负债表下**整表为空**。
    # 主要指标表的 TOTAL_ASSETS_PK / LIABILITY / TOTAL_EQUITY_PK 是会计恒等式字段，
    # 对保险股与消费股都返回，且与三大报表交叉验证一致
    # （平安：129578.27 亿 − 116531.15 亿 = 13047.12 亿 = 权益；茅台三者与 F10 完全一致）。
    "总资产": {
        "aliases": ["总资产", "资产总计", "资产总额"],
        "unit": "元", "display_unit": "亿元", "statement": "资产负债表",
        "sources": [("balance", "TOTAL_ASSETS"),
                    ("dc_balance", "TOTAL_ASSETS"),
                    ("main", "TOTAL_ASSETS_PK")],
    },
    "总负债": {
        "aliases": ["总负债", "负债合计", "负债总额"],
        "unit": "元", "display_unit": "亿元", "statement": "资产负债表",
        "sources": [("balance", "TOTAL_LIABILITIES"),
                    ("dc_balance", "TOTAL_LIABILITIES"),
                    ("main", "LIABILITY")],
    },
    "归母净资产": {
        # ⚠️ 这一项是**"多源不只是为了容错，而是为了补缺"**的最典型例子：
        # 东财**全系**都不提供保险股的归属于母公司股东权益 ——
        #   F10 资产负债表对 601318 整表为空；
        #   数据中心资产负债简表没有 PARENT_EQUITY 列（它是金融业业务口径简表）；
        #   主要指标只有 TOTAL_EQUITY_PK（**含少数股东**，是另一个口径，不能顶替）。
        # 而券商 App 上中国平安有这一项，说明数据客观存在 → 引入新浪财经的
        # 「归属于母公司的股东权益合计」。实测 601318：归母 9,286.00 亿，
        # 且 9,286.00 + 少数股东 3,761.12 = 13,047.12 亿（= 东财 TOTAL_EQUITY_PK），恒等式零误差。
        "aliases": ["净资产", "归母净资产", "归属于母公司股东的权益",
                    "归属于母公司股东权益", "归属母公司股东权益"],
        "unit": "元", "display_unit": "亿元", "statement": "资产负债表",
        "sources": [("balance", "TOTAL_PARENT_EQUITY"),
                    ("sina_balance", "归属于母公司的股东权益合计")],
    },
    "所有者权益合计": {
        "aliases": ["所有者权益合计", "股东权益合计", "所有者权益"],
        "unit": "元", "display_unit": "亿元", "statement": "资产负债表",
        "sources": [("balance", "TOTAL_EQUITY"),
                    ("dc_balance", "TOTAL_EQUITY"),
                    ("main", "TOTAL_EQUITY_PK"),
                    ("sina_balance", "所有者权益合计")],
    },
    # ---------- 现金流量表 ----------
    "经营活动现金流净额": {
        "aliases": ["经营活动产生的现金流量净额", "经营活动现金流净额", "经营现金流"],
        "unit": "元", "display_unit": "亿元", "statement": "现金流量表",
        "sources": [("cashflow", "NETCASH_OPERATE"),
                    ("dc_cashflow", "NETCASH_OPERATE")],
    },
    "投资活动现金流净额": {
        "aliases": ["投资活动产生的现金流量净额", "投资活动现金流净额"],
        "unit": "元", "display_unit": "亿元", "statement": "现金流量表",
        "sources": [("cashflow", "NETCASH_INVEST"),
                    ("dc_cashflow", "NETCASH_INVEST")],
    },
    "筹资活动现金流净额": {
        "aliases": ["筹资活动产生的现金流量净额", "筹资活动现金流净额"],
        "unit": "元", "display_unit": "亿元", "statement": "现金流量表",
        "sources": [("cashflow", "NETCASH_FINANCE"),
                    ("dc_cashflow", "NETCASH_FINANCE")],
    },
    # ---------- 官方口径比率（主要指标接口）----------
    # 这几个比率东财**直接给出官方口径**。字段名是拼音缩写而非英文，实测确认：
    # ROEJQ=净资产收益率(加权) / ROEKCJQ=扣非加权 / XSMLL=销售毛利率 / XSJLL=销售净利率 /
    # ZCFZL=资产负债率 / EPSJB=基本每股收益 / BPS=每股净资产。
    # 我们仍然实现派生计算（src/tools/ratios.py），但以官方值为准，派生值用于**交叉对账**：
    # 两者差异大往往说明口径不同（如 ROE 加权平均 vs 简单平均），这种差异要暴露而不是掩盖。
    # 注意：毛利率/净利率对保险公司天然不存在（XSMLL 返回 null），缺失是正确行为。
    "毛利率": {
        "aliases": ["毛利率", "销售毛利率"],
        "unit": "%", "display_unit": "%", "statement": "主要指标",
        # 保险股 XSMLL 为 null（该科目在保险业不存在）。此时不要直接说"没有"，
        # 而是指向下面的同义口径「毛利率(保险口径)」——App 上看到的那个数是它算的。
        "counterpart": {"ratio": "毛利率(保险口径)",
                        "why": "保险业利润表无「营业成本」，多数券商 App 展示的「毛利率」"
                               "是用「营业支出」替代营业成本算出来的，属另一套口径"},
        "sources": [("main", "XSMLL")],
    },
    "净利率": {
        "aliases": ["净利率", "销售净利率"],
        "unit": "%", "display_unit": "%", "statement": "主要指标",
        "sources": [("main", "XSJLL")],
    },
    "资产负债率": {
        "aliases": ["资产负债率"],
        "unit": "%", "display_unit": "%", "statement": "主要指标",
        "sources": [("main", "ZCFZL")],
    },
    "ROE": {
        "aliases": ["ROE", "净资产收益率", "加权平均净资产收益率", "ROE加权"],
        "unit": "%", "display_unit": "%", "statement": "主要指标",
        "sources": [("main", "ROEJQ")],
    },
    "扣非ROE": {
        "aliases": ["扣非ROE", "扣非净资产收益率", "扣除非经常性损益后的净资产收益率"],
        "unit": "%", "display_unit": "%", "statement": "主要指标",
        "sources": [("main", "ROEKCJQ")],
    },
    "每股收益": {
        "aliases": ["每股收益", "基本每股收益", "EPS"],
        "unit": "元", "display_unit": "元", "statement": "主要指标",
        "sources": [("main", "EPSJB")],
    },
    "每股净资产": {
        "aliases": ["每股净资产", "BPS"],
        "unit": "元", "display_unit": "元", "statement": "主要指标",
        "sources": [("main", "BPS")],
    },
}

# 指标 → 中文口语别名里**有口径歧义**的那些。工具层认不出来时要能解释"为什么认不出来"，
# 而不是干巴巴说"不认识"—— 对金融问答来说，"你说的口径不明确"本身就是要交付的信息。
AMBIGUOUS_ALIASES: dict[str, str] = {
    "利润": "净利润与归母净利润是两个口径，请指明",
    "收益": "请指明是营业总收入、净利润还是每股收益",
    "负债": "请指明是总负债、资产负债率还是某项具体负债",
}


# ---- 派生比率口径表 ----
# 为什么比率也要收口在 config：比率是**最容易算错**的一类指标 —— 同一句「毛利率」，
# 分母用「营业收入」还是「营业总收入」结果不同；ROE 用期末净资产还是加权平均净资产
# 能差好几个点。口径写在代码里等于把争议埋进实现，写在这里才能被工具层、评估集、
# 前端文案共同引用。
#
# 结构：
#   numerator / denominator : [(指标名, 符号)] 列表，值 = Σ(符号 × 该指标值)
#   official_indicator      : 东财官方口径指标名（来自主要指标接口），用于**交叉对账**；无则不填
#   note                    : 必须随结果一起返回，说清这个比率的口径边界
#
# 重要：`official_indicator` 不是"更准的答案"，而是**另一套口径**。两者差异本身就是
# 分析结论（例如 ROE：期末简化口径 vs 加权平均口径），要暴露而不是取其一掩盖。
RATIOS: dict[str, dict] = {
    "毛利率": {
        "aliases": ["毛利率", "销售毛利率", "gross margin", "GM"],
        "formula": "(营业收入 − 营业成本) / 营业收入 × 100%",
        "unit": "%",
        # ⚠️ 分母刻意用「营业收入」而不是「营业总收入」：东财官方 XSMLL 实测就是用
        # 营业收入做分母（茅台 2024：(1708.99−137.89)/1708.99 = 91.93% 与官方一致；
        # 若改用营业总收入 1741.44 亿，算得 92.08%，与官方值对不上）。
        "numerator": [("营业收入", 1), ("营业成本", -1)],
        "denominator": [("营业收入", 1)],
        "official_indicator": "毛利率",
        "note": "分母为营业收入（非营业总收入），与东财官方销售毛利率同口径。"
                "保险等无「营业成本」科目的行业不适用——那类公司请用「毛利率(保险口径)」。",
        # 缺项时的口径替代建议（工具层会顺手把这个替代口径的值也算出来一起返回）。
        # 为什么这么做：用户问"中国平安毛利率"，若只回"保险业没有毛利率"，
        # 用户会觉得我们在推脱；而直接拿营业支出口径的数顶替「毛利率」，又是在说谎。
        # 正确做法是**给出替代口径并标明它叫什么、怎么算的**，让用户自己判断要不要用。
        "alternatives": ["毛利率(保险口径)"],
    },
    "净利率": {
        "aliases": ["净利率", "销售净利率", "net margin"],
        "formula": "净利润 / 营业总收入 × 100%",
        "unit": "%",
        "numerator": [("净利润", 1)],
        "denominator": [("营业总收入", 1)],
        "official_indicator": "净利率",
        "note": "净利润含少数股东损益；若要看归属母公司的盈利能力，请用归母净利润/营业总收入。",
    },
    "资产负债率": {
        "aliases": ["资产负债率", "负债率", "debt ratio", "lev"],
        "formula": "总负债 / 总资产 × 100%",
        "unit": "%",
        "numerator": [("总负债", 1)],
        "denominator": [("总资产", 1)],
        "official_indicator": "资产负债率",
        "note": "金融/保险业杠杆天然高于制造业，横向比较需限定同行业。",
    },
    "ROE": {
        "aliases": ["ROE", "净资产收益率", "roe期末", "期末ROE"],
        "formula": "归母净利润 / 期末归母净资产 × 100%（简化口径，未加权平均）",
        "unit": "%",
        "numerator": [("归母净利润", 1)],
        "denominator": [("归母净资产", 1)],
        "official_indicator": "ROE",
        "note": "本值为**期末简化口径**；东财官方 ROEJQ 是加权平均净资产收益率。"
                "两者差值通常在 1~5 个百分点，差异来自期中净资产变动（分红/增发/回购），"
                "不是计算错误。做趋势比较时应统一用同一口径。",
    },
    "经营现金流净利润比": {
        "aliases": ["经营现金流净利润比", "现金流净利润比", "盈利质量", "现金含量"],
        "formula": "经营活动现金流净额 / 归母净利润",
        "unit": "倍",
        "numerator": [("经营活动现金流净额", 1)],
        "denominator": [("归母净利润", 1)],
        "official_indicator": None,   # 无官方口径，纯派生
        "note": "单位为「倍」。经验判读：持续 <1 说明利润未转化为现金（应收账款/存货积压"
                "或激进确认收入），需结合成长阶段看；金融业现金流波动大，不宜机械套用。",
    },
    # 保险/金融业的「毛利率」同义口径。
    #
    # 为什么单独立项、不加进「毛利率」的 alias 列表：两者**分母公式相同、分子科目不同**，
    # 数值也不相等（制造业用营业成本，保险用营业支出），混成一个名字就等于把两套口径
    # 悄悄合并成一条时间序列 —— 那正是本项目最要防的静默错误。
    # 但用户和券商 App 都会把它叫"毛利率"，所以必须**能被问出来**，
    # 于是列为独立比率 + 在「毛利率」上挂 alternatives 做引导。
    "毛利率(保险口径)": {
        "aliases": ["毛利率(保险口径)", "保险毛利率", "毛利率保险口径", "保险业毛利率"],
        "formula": "(营业收入 − 营业支出) / 营业收入 × 100%",
        "unit": "%",
        # 分母仍用「营业收入」（与制造业毛利率维持同一条规则）；
        # 保险利润表首行就是这个科目，东财同时映射到 OPERATE_INCOME 与 TOTAL_OPERATE_INCOME，
        # 故两者对本口径等价（实测 601318 2024 均为 10,289.25 亿）。
        "numerator": [("营业收入", 1), ("营业支出", -1)],
        "denominator": [("营业收入", 1)],
        "official_indicator": None,   # 无官方口径：东财 XSMLL 与同花顺主要指标都没有这一项
        "note": "**保险/金融业专用口径**，用「营业支出」替代制造业的「营业成本」。"
                "东财与同花顺都不提供官方值，券商 App 上显示的「毛利率」多由此计算，"
                "故本项目自行派生并公开算式。与制造业毛利率**不可横向比较**："
                "制造业分母口径虽同为营业收入，但成本科目含义完全不同。",
    },
}


def resolve_ratio(name: str) -> str | None:
    """把用户/LLM 给的说法归一为标准比率名；认不出来返回 None（不做模糊猜测）。"""
    if not name:
        return None
    key = name.strip().replace(" ", "")
    if key in RATIOS:
        return key
    for std, meta in RATIOS.items():
        if key in meta["aliases"]:
            return std
    lower = key.lower()
    for std, meta in RATIOS.items():
        if lower in [a.lower() for a in meta["aliases"]]:
            return std
    return None


def ratio_meta(name: str) -> dict | None:
    """取标准比率的口径定义（含分子分母项与 note）。"""
    std = resolve_ratio(name)
    if not std:
        return None
    meta = dict(RATIOS[std], name=std)
    meta["needed_indicators"] = sorted(
        {ind for ind, _ in (meta["numerator"] + meta["denominator"])})
    return meta


def resolve_indicator(name: str) -> str | None:
    """把用户/LLM 给的说法归一到标准指标名；认不出来返回 None（调用方应报「不认识该指标」）。

    不做模糊猜测：金融场景把「营业收入」错认成「营业总收入」是要出事故的。
    会计口径的别名一律要求精确匹配，不接受子串匹配。
    """
    if not name:
        return None
    key = name.strip().replace(" ", "")
    if key in INDICATORS:
        return key
    for std, meta in INDICATORS.items():
        if key in meta["aliases"]:
            return std
    return None


def indicator_meta(name: str) -> dict | None:
    """取标准指标的口径定义（含 sources / unit）。

    工具层要把口径一起返回给用户，所以必须给全 —— 只返回名字的话，
    「用的是营业总收入还是营业收入」「这个值取自哪张表哪个字段」在下游就都看不见了。
    """
    std = resolve_indicator(name)
    if not std:
        return None
    meta = dict(INDICATORS[std], name=std)
    # 便捷字段：首选源（展示与期望用）；**实际命中的源由数据层逐行记录**
    meta["source_table"], meta["source_field"] = meta["sources"][0]
    return meta




def load_watchlist() -> list[dict]:
    """读 config/watchlist.yaml。

    设计成「文件不存在也能跑」：返回空列表而不是抛异常，
    免得一个配置缺失让 probe/单测全挂掉。
    """
    try:
        import yaml  # 延迟导入：不读 watchlist 的场景不必依赖 pyyaml
    except ImportError:
        return []
    if not WATCHLIST_PATH.exists():
        return []
    data = yaml.safe_load(WATCHLIST_PATH.read_text(encoding="utf-8")) or {}
    return list(data.get("companies") or [])


def get_llm_config() -> dict:
    """取当前激活的 LLM 通道完整配置。

    实现委托给 src.llm（含界面切换后的持久化状态），此处保持向后兼容。
    """
    from src.llm import get_active_llm  # 延迟导入避免循环依赖

    return get_active_llm()


if __name__ == "__main__":
    # 自检：python src/config.py（直接运行时 src 包不在 sys.path，需补上项目根）
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    print(f"root       : {ROOT_DIR}")
    print(f"data dir   : {DATA_DIR}")
    print(f"watchlist  : {WATCHLIST_PATH} (exists={WATCHLIST_PATH.exists()})")
    wl = load_watchlist()
    print(f"companies  : {len(wl)} -> {[c.get('code') for c in wl]}")
    print(f"retrieve   : mode={RETRIEVE_MODE} topk={RETRIEVE_TOPK} "
          f"chunk={CHUNK_SIZE}/{CHUNK_OVERLAP} rerank={RERANK_ENABLED}")
    print(f"db backend : {DB_BACKEND}  path={DB_PATH}")
    print(f"mysql      : {MYSQL['user']}@{MYSQL['host']}:{MYSQL['port']}/{MYSQL['database']}")
    print(f"indicators : {len(INDICATORS)} 个；别名解析自检 "
          f"「营收」-> {resolve_indicator('营收')}，"
          f"「营业收入」-> {resolve_indicator('营业收入')}，"
          f"「净资产收益率」-> {resolve_indicator('净资产收益率')}，"
          f"「瞎写的指标」-> {resolve_indicator('瞎写的指标')}")
    print(f"             口径拆开自检（这两个不能混）："
          f"营业总收入 {INDICATORS['营业总收入']['sources'][0]} ≠ "
          f"营业收入 {INDICATORS['营业收入']['sources'][0]}；归母净利润≠净利润")
    _apis = {}
    for _v in DATA_SOURCES.values():
        _apis[_v["api"]] = _apis.get(_v["api"], 0) + 1
    print(f"数据源      : {len(DATA_SOURCES)} 个（{_apis}）→ "
          f"{DATA_SOURCES['main']['report_name']} 等（每公司 {EM_PERIODS} 期 {EM_REPORT_TYPE_ANNUAL}）")
    for p in ("deepseek", "qwen"):
        cfg = LLM_PROVIDERS[p]
        print(f"llm[{p}]    : key_configured={bool(cfg['api_key'])} models={cfg['models']}")
