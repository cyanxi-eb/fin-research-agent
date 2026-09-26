"""意图路由用例（纯函数，零成本 —— 不建索引、不联网、不调模型）。

路由是整条链路里**最不该有不确定性**的一环：它一旦判错，后面的检索参数、
模型选择都会被带偏，而错误会伪装成"检索不准/模型不行"。所以这批用例的重点
不是"能判对常见问法"，而是**边界**：什么情况下必须判成 rag（保守兜底），
以及两个条件是否真的"同时成立"才放行。
"""
from __future__ import annotations

from src.graph import router


def test_analysis_requires_both_cue_and_indicator():
    """数值题判据是「疑问形 **且** 库内指标名」—— 两条同时成立才放行。"""
    r = router.route("贵州茅台2024年的营业总收入是多少")
    assert r["intent"] == "analysis"
    assert r["rule"] == "numeric_cue+indicator"
    assert "营业总收入" in r["matched"]["indicators"]


def test_numeric_cue_alone_is_not_analysis():
    """只有疑问形、没有指标名 → **不判**数值题。

    否则「公司食堂的菜谱有什么推荐」里的"什么"会把闲聊送进工具层，
    用户看到的是"查不到这个数"，而正确答案是"这问题不归数值链路管"。
    """
    r = router.route("今年年报一共有多少页")
    assert r["intent"] == "rag"
    assert r["rule"] == "numeric_cue_without_indicator"
    assert r["confidence"] < 0.5, "兜底类判定不该给高置信度"


def test_indicator_alone_is_not_analysis():
    """只有指标名、没有疑问形 → 也不判数值题（可能在问"什么是毛利率"）。"""
    r = router.route("请解释一下毛利率这个指标的含义")
    assert r["intent"] == "rag"


def test_compliance_wins_over_analysis():
    """合规判据优先于数值判据。

    合规问题里常同时出现数字（"第十二条""500 万处罚"）。先判 analysis 会把它
    送进工具层，而工具层回答不了法规问题 —— 用户拿到"没有这个指标"，
    而真正该做的是去法规库查。
    """
    r = router.route("公司2023年未按期披露年报被处罚，这是否违反信息披露规定")
    assert r["intent"] == "compliance"
    assert r["rule"] == "compliance_cue"


def test_generic_words_do_not_trigger_compliance():
    """「是否符合会计准则」**不**该判合规。

    判据里刻意不含"是否""符合"这类通用词 —— 收了它们，所有带"是否"的问题
    都会跑去查法规，而法规库答不了财务口径问题。
    """
    r = router.route("这家公司的会计政策是否符合准则要求")
    assert r["intent"] != "compliance", "通用词不该触发合规路由"


def test_default_is_rag_and_low_confidence():
    """判不出来必须兜底到最保守的一条（检索 + 引用 + 三道闸门）。"""
    r = router.route("公司食堂的菜谱是什么")
    assert r["intent"] == "rag"
    assert r["rule"] == "default"
    assert r["confidence"] == 0.3


def test_empty_question_is_rag_not_crash():
    r = router.route("   ")
    assert r["intent"] == "rag"
    assert r["rule"] == "empty_question"


def test_route_result_shape_is_stable():
    """响应里必须始终带 `rule`：没有它，路由问题只能靠猜。"""
    for q in ("贵州茅台2024年毛利率", "并购重组有什么规定", "随便问问"):
        r = router.route(q)
        assert set(r) == {"intent", "rule", "confidence", "matched", "reason"}
        assert r["intent"] in router.INTENTS
        assert isinstance(r["reason"], str) and r["reason"]


def test_indicator_aliases_come_from_config_and_include_ratios():
    """别名表必须与工具层同源（同取 `config`），且把比率也算进去。

    两份表迟早不一致，后果是"工具层认识这个指标、路由层不认为它在问数值" →
    静默走错子图。
    """
    names = router.indicator_aliases()
    from src import config

    assert set(config.RATIOS) <= set(names)
    assert set(config.INDICATORS) <= set(names)
    assert len(names) == len(set(names)), "别名表不该有重复项"
