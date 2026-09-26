"""问题→过滤条件 抽取用例（纯函数，不联网）。

这组用例守的是一条**不对称**的纪律：漏抽的代价是"结果里混进别家/别年，用户看得出来"，
抽错的代价是"结果全错但看起来很正常"。所以断言里既要有"该抽出来的要抽出来"，
更要有"**拿不准时必须放弃过滤**并把歧义说出来"。
"""
from __future__ import annotations

import pytest

from src import config
from src.retrieve import filters


@pytest.fixture(autouse=True)
def _watchlist(monkeypatch):
    """固定一份 watchlist，让断言不依赖 config/watchlist.yaml 的真实内容。"""
    rows = [
        {"code": "600519", "name": "贵州茅台", "industry": "食品饮料", "years": [2024, 2025]},
        {"code": "000858", "name": "五粮液", "industry": "食品饮料", "years": [2024]},
        {"code": "300750", "name": "宁德时代", "industry": "电力设备", "years": [2024]},
        {"code": "002594", "name": "比亚迪", "industry": "汽车", "years": [2024]},
        {"code": "601318", "name": "中国平安", "industry": "非银金融", "years": [2024]},
        {"code": "601998", "name": "中国银行", "industry": "银行", "years": [2024]},
    ]
    monkeypatch.setattr(config, "load_watchlist", lambda: rows)
    filters.reset_cache()
    yield
    filters.reset_cache()


# ---------------- 公司 ----------------

def test_full_name_match():
    d = filters.detect_company("贵州茅台2024年的毛利率是多少")
    assert (d["code"], d["company"]) == ("600519", "贵州茅台")
    assert d["matched"]["kind"] == "name"
    assert "已限定检索范围" in d["note"]


def test_code_match_wins_and_needs_six_digits():
    """代码最可靠。但必须**是 6 位独立数字**：长数字串里的前 6 位不能当代码。"""
    assert filters.detect_company("600519 的归母净利润")["code"] == "600519"
    # 8 位数字里嵌着的 600519 不应被当成股票代码
    assert filters.detect_company("编号 160051907 对应的公司")["code"] is None
    # 库外的代码：不认识就是不认识，不许就近猜一个
    assert filters.detect_company("999999 的营收")["code"] is None


def test_short_name_suffix():
    """口语简称（茅台/平安）要认。区分度集中在名称尾部，所以只取后缀。"""
    assert filters.detect_company("茅台2024年的净利率")["code"] == "600519"
    assert filters.detect_company("平安2024年的归母净资产")["code"] == "601318"


def test_ambiguous_short_name_drops_filter(monkeypatch):
    """简称命中多家 → **必须放弃过滤**并列出候选。

    宁可让用户说清是哪一家，也不要随机挑一家 —— 挑错的后果是"答案全错但看起来正常"。
    """
    monkeypatch.setattr(config, "load_watchlist", lambda: [
        {"code": "601989", "name": "中国重工"},
        {"code": "600031", "name": "三一重工"},
    ])
    filters.reset_cache()
    d = filters.detect_company("重工板块2024年的毛利率")
    assert d["code"] is None
    assert len(d["ambiguous"]) == 2
    assert "未做公司过滤" in d["note"]


def test_blacklist_defuses_would_be_ambiguity():
    """黑名单还有一个额外好处：把"看起来歧义"的简称消歧掉。

    「平安」在"中国平安 + 平安银行"并存时**不是**歧义 —— 因为"银行"是通用词，
    "平安银行"根本不该用 2 字后缀被识别。这样用户说「平安」时能稳定得到中国平安。
    """
    rows = [
        {"code": "601318", "name": "中国平安"},
        {"code": "000001", "name": "平安银行"},
    ]
    from src import config as cfg
    orig = cfg.load_watchlist
    cfg.load_watchlist = lambda: rows
    filters.reset_cache()
    try:
        d = filters.detect_company("平安2024年的归母净资产")
        assert d["code"] == "601318"
    finally:
        cfg.load_watchlist = orig
        filters.reset_cache()


def test_generic_suffix_blacklist():
    """通用词后缀不得认出公司：
    「这个时代」→ 宁德时代、「中国的银行股」→ 中国银行，都是典型误伤。
    判据是"这个词单独出现时，用户大概率不是在指某一家公司"。"""
    assert filters.detect_company("这个时代最好的公司是哪家")["code"] is None
    assert filters.detect_company("中国的银行股表现如何")["code"] is None
    assert filters.detect_company("新能源行业的毛利率一般是多少")["code"] is None
    # 反向：非通用词后缀仍然要认（否则简称识别就废了）
    assert filters.detect_company("平安的归母净资产")["code"] == "601318"


def test_no_company_returns_empty_note():
    d = filters.detect_company("公司食堂的菜谱是什么")
    assert d["code"] is None and d["note"] is None


# ---------------- 年份 ----------------

def test_single_year_is_used():
    d = filters.detect_year("贵州茅台2024年的毛利率")
    assert d["year"] == 2024 and d["years"] == [2024]


def test_multiple_years_skip_filter():
    """对比题（2023 和 2024 相比）过滤掉任何一年都是错的 → 不过滤 + 说明。"""
    d = filters.detect_year("2023年和2024年五粮液的营收对比")
    assert d["year"] is None and d["years"] == [2023, 2024]
    assert "未做年份过滤" in d["note"]


def test_year_like_number_with_unit_is_not_a_year():
    """「2024 千元」是数量不是年份。年报里这种写法到处都是（金额单位）。"""
    d = filters.detect_year("研发投入 2024 千元的项目有哪些")
    assert d["year"] is None
    # 反向：正常年份写法要认
    assert filters.detect_year("2024年度的研发投入")["year"] == 2024


def test_relative_year_is_not_guessed():
    """相对年份不解析 —— 同一句话在元旦前后含义不同，而答案会被引用很久。"""
    d = filters.detect_year("茅台去年的归母净利润")
    assert d["year"] is None
    assert "不把相对年份转成具体年份" in d["note"]


def test_embedded_digits_do_not_become_years():
    """长数字串里的 2024 不算年份（如手机号、公告编号）。"""
    assert filters.detect_year("公告编号 120240815 的内容")["year"] is None


# ---------------- 合并 ----------------

def test_detect_combines_and_flags_filtering():
    d = filters.detect("贵州茅台2024年的毛利率是多少")
    assert d["code"] == "600519" and d["year"] == 2024 and d["filtered"] is True
    assert d["evidence"]["company"]["kind"] == "name"
    assert d["evidence"]["year"]["text"] == "2024"


def test_detect_without_anything_does_not_filter():
    d = filters.detect("公司食堂的菜谱是什么")
    assert d["filtered"] is False and d["code"] is None and d["year"] is None
    assert d["notes"] == []


def test_empty_and_whitespace_question():
    for q in ("", "   ", None):
        d = filters.detect(q or "")
        assert d["code"] is None and d["year"] is None
