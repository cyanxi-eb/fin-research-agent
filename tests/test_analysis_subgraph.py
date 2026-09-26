"""数值分析子图用例（走合成库，不联网）。

这条链路的价值全在**边界**上：什么时候**不调**工具、什么时候**不给数字**。
给错了数字（张冠李戴、跨期次比较、拿替代口径冒充原名）在金融场景里是事故，
而"没给数字但说清为什么"是可接受的交付。所以用例的重点是"不猜"。
"""
from __future__ import annotations

from src.graph import subgraph_analysis as sa


# ==================== 实体抽取 ====================

def test_detect_companies_accepts_short_name(synth_db):
    """简称（"茅台"）要能命中唯一公司 —— 复用工具层的归一逻辑。"""
    got = sa.detect_companies("茅台2024年的营业总收入是多少")
    assert [c["code"] for c in got] == ["600519"]


def test_detect_companies_refuses_to_guess_on_ambiguity(synth_db):
    """「平安」同时命中中国平安与平安银行 → **一个都不返回**，绝不猜。

    金融查询里选错公司比查不到严重得多：用户会拿着别家的数当自己的用。
    """
    got = sa.detect_companies("平安2024年的归母净利润是多少")
    assert got == [], f"歧义时不该猜，实际返回 {got}"


def test_detect_companies_finds_multiple(synth_db):
    got = sa.detect_companies("五粮液和贵州茅台2024年的毛利率对比")
    assert {c["code"] for c in got} == {"600519", "000858"}


def test_detect_year_takes_explicit_year():
    assert sa.detect_year("贵州茅台2024年的营业总收入") == 2024
    assert sa.detect_year("最近三年的营业总收入") is None


def test_resolve_names_splits_ratio_and_indicator():
    ratios, inds = sa.resolve_names("x", {"indicators": ["毛利率", "营业总收入"]})
    assert ratios == ["毛利率"] and inds == ["营业总收入"]


def test_resolve_names_falls_back_to_the_question_itself():
    """`matched` 为空时必须自己从问句里抽 —— 指标名是问句的属性，不是路由的产物。

    踩过的坑：`force_intent="analysis"`（API 显式指定）时路由没跑、`matched` 为空，
    于是子图报"未能识别指标"—— 而问句里明明写着"营业总收入"。
    """
    ratios, inds = sa.resolve_names("贵州茅台2024年的营业总收入是多少", None)
    assert inds == ["营业总收入"]
    ratios2, _ = sa.resolve_names("贵州茅台2024年的毛利率是多少", {})
    assert ratios2 == ["毛利率"]


# ==================== 调用计划（纯函数）====================

def test_plan_calls_single_company_indicator_uses_series_tool():
    plan = sa.plan_calls([{"name": "贵州茅台"}], [], ["营业总收入"], 2024)
    assert plan == [{"name": sa.T_INDICATOR,
                     "arguments": {"company": "贵州茅台", "indicator": "营业总收入"}}]


def test_plan_calls_multi_company_indicator_uses_compare():
    plan = sa.plan_calls([{"name": "A"}, {"name": "B"}], [], ["营业总收入"], 2024)
    assert len(plan) == 1 and plan[0]["name"] == sa.T_COMPARE
    assert plan[0]["arguments"]["companies"] == ["A", "B"]
    assert plan[0]["arguments"]["period"] == "2024-12-31"


def test_plan_calls_multi_company_ratio_calls_per_company():
    """比率对比工具不做（`compare_companies` 只接受指标），所以逐家调比率工具。

    为什么不让 `compare_companies` 顺手支持比率：比率要的是"公式 + 分子分母分项"，
    逐家调才能把每家的口径摊开；合并成一个排名接口会把口径信息在接口层丢掉。
    """
    plan = sa.plan_calls([{"name": "A"}, {"name": "B"}], ["毛利率"], [], 2024)
    assert len(plan) == 2
    assert all(p["name"] == sa.T_RATIO for p in plan)
    assert [p["arguments"]["company"] for p in plan] == ["A", "B"]


def test_plan_calls_without_company_is_empty():
    assert sa.plan_calls([], ["毛利率"], ["营业总收入"], 2024) == []


# ==================== 端到端（合成库）====================

def _run(question, **kw):
    """直接调子图节点（不用跑整张图，单测要的是"这一层的行为"）。"""
    return sa.analysis_question({"question": question, **kw})


def test_analysis_answer_carries_value_and_source(synth_db, monkeypatch):
    """答案形态（2026-09-25 用户定稿）：来源要**人话可读**，不是内部字段名。

    - 数值来源写「年报·合并利润表」这样的报表名，`income.TOTAL_OPERATE_INCOME`
      这类 表.字段 串只留在 tool_calls.audit 里供程序核对；
    - 前端按**纯文本**渲染答案，`**` 加粗星号会原样露出 —— 组句里不许有 markdown 记号。
    """
    monkeypatch.setattr(sa, "_report_url", lambda code, year: None)
    st = _run("贵州茅台2024年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert st["refused"] is False
    assert "1,020.00 亿元" in st["answer"], "答案里必须出现库里那一期的值"
    assert "合并利润表" in st["answer"], "来源要写成报表名，不是内部字段名"
    assert "income.TOTAL_OPERATE_INCOME" not in st["answer"], "字段名不该直接暴露给用户"
    assert "**" not in st["answer"], "前端按纯文本渲染，markdown 星号会露出来"
    assert st["tool_calls"] and st["tool_calls"][0]["ok"] is True
    assert st["tool_calls"][0]["sources"], "溯源串（表.字段）要留在 tool_calls 里供审计"
    assert st["tool_results"], "原始返回要留在状态里供 verify 做数字回归"


def test_analysis_appends_report_url_when_manifest_has_it(synth_db, monkeypatch):
    """年报原文 URL（巨潮 manifest）存在时，答案要给出「原文：URL」这行可点的凭据。"""
    monkeypatch.setattr(
        "src.ingest.fetch_cninfo.load_manifest",
        lambda code: {"2024": {"url": "http://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF"}})
    st = _run("贵州茅台2024年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert ("原文：http://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF"
            in st["answer"]), "manifest 里有年报 URL 就要展示，别让用户自己去找"


def test_analysis_without_manifest_still_answers_without_url(synth_db, monkeypatch):
    """manifest 缺失（未下载/手工放/容器精简）→ 不给 URL 也不崩，答案照常。"""
    monkeypatch.setattr("src.ingest.fetch_cninfo.load_manifest", lambda code: {})
    st = _run("贵州茅台2024年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert "1,020.00 亿元" in st["answer"], "URL 只是加分项，不能挡住数值本身"
    assert "原文：" not in st["answer"], "没有凭据就不给 URL —— 绝不编一个链接"


def test_analysis_without_company_gives_no_number_and_lists_library(synth_db):
    """认不出公司 → **不调工具、不给数字**，而是说明并列出可查范围。

    这条是"零幻觉"的另一种形态：与其猜一家公司，不如把"能查什么"告诉用户。
    """
    st = _run("未上市公司某某2024年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert st["tool_calls"] == [], "认不出公司时不该调用任何取数工具"
    assert "贵州茅台" in st["answer"], "要列出库内可查公司，让用户能自我纠正"
    assert "没有调用取数工具" in st["answer"]


def test_analysis_without_indicator_gives_no_number(synth_db):
    st = _run("贵州茅台2024年的情况怎么样", route={"matched": {"indicators": []}})
    assert st["tool_calls"] == []
    assert "未能识别出库内指标名" in st["answer"]


def test_analysis_uses_explicit_year_not_latest(synth_db):
    """问题里点了 2024，答案就必须是 2024 那一期（库里 2025 期也存在）。"""
    st = _run("贵州茅台2024年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert "2024-12-31" in st["answer"]
    assert "1,020.00 亿元" in st["answer"]


def test_analysis_reports_missing_year_without_silently_substituting(synth_db):
    """要 2022 年但库里没有 → 给可得期次，并**显式说明没有那一年**。

    静默换成最近一期是最典型的静默错误：数字看着正常，年份不对。
    """
    st = _run("贵州茅台2022年的营业总收入是多少",
              route={"matched": {"indicators": ["营业总收入"]}})
    assert "没有 2022 年" in st["answer"]


def test_analysis_composes_ratio_with_formula_and_cross_check(synth_db):
    st = _run("贵州茅台2024年的毛利率是多少",
              route={"matched": {"indicators": ["毛利率"]}})
    a = st["answer"]
    assert "60.00%" in a, "合成库里毛利率 = (1.0e11-4.0e10)/1.0e11 = 60%"
    assert "公式" in a and "分子" in a and "分母" in a
    assert "官方口径对账" in a, "比率必须做官方口径交叉对账"


def test_analysis_all_numbers_traceable_to_tools(synth_db):
    """零幻觉的机制验证：把所有工具返回拼起来当证据集合，答案里的数字必须全部对上。

    这条是"分析链路自己保证不编数"的机器可验证明 —— 与 `verify` 用的是同一套判定，
    所以这里失败等价于线上会被挂起。
    """
    from src.numeric import unsupported_numbers
    from src.numeric import collect_evidence_numbers

    for q, ind in (("贵州茅台2024年的营业总收入是多少", "营业总收入"),
                   ("贵州茅台2024年的毛利率是多少", "毛利率"),
                   ("五粮液和贵州茅台2024年的营业总收入对比", "营业总收入")):
        st = _run(q, route={"matched": {"indicators": [ind]}})
        allowed = collect_evidence_numbers(st["tool_results"])
        bad = unsupported_numbers(st["answer"], allowed)
        assert bad == [], f"{q} 出现了无出处的数字：{bad}"
        assert "**" not in st["answer"], f"{q} 的答案里露出了 markdown 星号"
