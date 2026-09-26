"""答案级判定用例（纯函数，不联网、不调真模型、不花 token）。

被测对象是**待建**的 `src/judge.py`。这里刻意只断言**纯函数契约**，
判分调用（`judge_answer`）用**构造的假模型**驱动 —— 真调模型既不可复现、
又会让每次跑测试都花钱，而这两点正是本模块要避免的（见 `src/judge.py` 模块注释）。

覆盖两条**失败方向相反**的规则：
- **必须能剥出 JSON**：模型很爱加 markdown 围栏与前后寒暄，剥不出来就会把
  一次正常的判分记成"未判"，指标被人为压低；
- **必须能容错**：模型返回非法 JSON 时**返回 None 而不是抛异常** ——
  判分失败要能被统计成"未判"，绝不能把整轮评测带塌。
"""
from __future__ import annotations

import pytest

from src import judge, numeric


# ==================== 1. extract_numbers 复用 src/numeric.py ====================

def test_extract_numbers_reuses_numeric_module():
    """必须是 `src/numeric.py` 的同一份实现，而不是另写一套。

    两边各写一套"什么算数字"，就会出现"校验侧认为合法、判分侧认为对不上"
    这种自相矛盾 —— 而这正是 `src/numeric.py` 模块注释里要消除的东西。
    """
    text = "营业总收入 1,741.44 亿元，毛利率 91.93%；见 [3] 与第二十条"
    assert judge.extract_numbers is numeric.extract_numbers
    assert [n["raw"] for n in judge.extract_numbers(text)] == \
        [n["raw"] for n in numeric.extract_numbers(text)]


def test_indicator_hit_matches_display_unit_and_thousands_separator():
    """「1,741.44 亿元」与「174144069958.25 元」是同一个数（容差 max(0.1%, 0.005)）。"""
    gt = {"type": "indicator", "indicator": "营业总收入",
          "value": 174144069958.25, "unit": "元"}
    assert judge.numeric_hit("贵州茅台2024年营业总收入 1,741.44 亿元", gt) is True
    assert judge.numeric_hit("营业总收入为 174144069958.25 元", gt) is True
    # 0.8% 的偏差必须被拦（不能因为容差把错数放行）
    assert judge.numeric_hit("营业总收入为 1,730.00 亿元", gt) is False


def test_indicator_hit_respects_sign():
    """负值必须带符号判定：投资活动现金流净额常为负。

    `numeric.extract_numbers` 只抽数字串、不含符号，若判分侧不补符号，
    「-17.85 亿元」会被误判成对不上 → 正确答案被记成错。这是判分侧的职责。
    """
    gt = {"type": "indicator", "indicator": "投资活动现金流净额",
          "value": -1785202630.71, "unit": "元"}
    assert judge.numeric_hit("投资活动现金流净额 -17.85 亿元", gt) is True
    assert judge.numeric_hit("投资活动现金流净额 17.85 亿元", gt) is False
    # 数字落在**答案开头**时不能被当成负数：此时 `prev` 是空串，
    # 而 `"" in "-−–"` 恒为 True（空串是任意串的子串）——用子串判负号会把首个数字
    # 整体取负，正值答案被判错、负值答案被判对。这两条断言守的就是这个陷阱。
    assert judge.numeric_hit("17.85 亿元", gt) is False
    assert judge.numeric_hit("-17.85 亿元", gt) is True


def test_indicator_hit_percentage_tolerance():
    gt = {"type": "indicator", "indicator": "毛利率", "value": 91.9312166361, "unit": "%"}
    assert judge.numeric_hit("毛利率为 91.93%", gt) is True
    assert judge.numeric_hit("毛利率为 92.5%", gt) is False


# ==================== 2. numeric_hit 对三种 gt 的判定 ====================

def test_article_hit_requires_doc_no_and_article():
    gt = {"type": "article", "doc_no": "中国证监会令第226号",
          "article": "13", "article_label": "第十三条"}
    assert judge.numeric_hit(
        "《上市公司信息披露管理办法》（中国证监会令第226号）第十三条", gt) is True
    # 条号不对 → 不命中
    assert judge.numeric_hit(
        "《上市公司信息披露管理办法》（中国证监会令第226号）第十八条", gt) is False
    # 缺文号（只有条号）→ 不命中：条号在同名法规的不同版本里含义不同
    assert judge.numeric_hit("第十三条", gt) is False


def test_literal_hit_any_marker():
    gt = {"type": "literal", "any": ["标准无保留意见", "无保留意见"]}
    assert judge.numeric_hit("天健会计师事务所出具了标准无保留意见的审计报告", gt) is True
    assert judge.numeric_hit("审计机构为天健会计师事务所", gt) is False


def test_numeric_hit_unknown_or_none_type_is_false():
    """`gt.type == "none"`（应拒答的题）不参与命中判定，返回 False 而不是抛异常。"""
    assert judge.numeric_hit("任意答案", {"type": "none"}) is False
    assert judge.numeric_hit("任意答案", {}) is False
    assert judge.numeric_hit("任意答案", None) is False


# ==================== 3. parse_judge_json：剥 JSON，失败返回 None ====================

def test_parse_judge_json_strips_markdown_fence():
    raw = "```json\n{\"claims\": [{\"claim\": \"营收为1741.44亿\", \"supported\": true}]}\n```"
    parsed = judge.parse_judge_json(raw, metric="faithfulness")
    assert parsed == {"claims": [{"claim": "营收为1741.44亿", "supported": True}]}


def test_parse_judge_json_tolerates_surrounding_chatter():
    raw = "好的，我的评审如下：\n{\"score\": 0.8, \"reason\": \"直接回答了问题\"}\n以上就是结论。"
    parsed = judge.parse_judge_json(raw, metric="answer_relevancy")
    assert parsed["score"] == 0.8


def test_parse_judge_json_coerces_string_boolean_for_faithfulness():
    """模型常把布尔写成字符串，按 metric 归一（这是 metric 参数的用处）。"""
    raw = "{\"claims\": [{\"claim\": \"a\", \"supported\": \"true\"}, " \
          "{\"claim\": \"b\", \"supported\": \"false\"}]}"
    parsed = judge.parse_judge_json(raw, metric="faithfulness")
    assert parsed["claims"][0]["supported"] is True
    assert parsed["claims"][1]["supported"] is False


def test_parse_judge_json_returns_none_on_unparseable():
    """完全解析不出 → 返回 None（不是抛异常）：判分失败要能被计成"未判"。"""
    assert judge.parse_judge_json("完全不是 JSON 的一段话", metric="faithfulness") is None
    assert judge.parse_judge_json("{不是合法 json", metric="faithfulness") is None
    assert judge.parse_judge_json("", metric="faithfulness") is None
    assert judge.parse_judge_json(None, metric="faithfulness") is None
    # 合法 JSON 但不是对象（数组/标量）也视为不可用
    assert judge.parse_judge_json("[1, 2, 3]", metric="faithfulness") is None


# ==================== 4. score_from_judge：夹到 [0,1]，缺字段返回 None ====================

def test_score_from_judge_clamps_to_unit_interval():
    assert judge.score_from_judge({"score": 1.5}, "answer_relevancy") == 1.0
    assert judge.score_from_judge({"score": -0.2}, "answer_relevancy") == 0.0
    assert judge.score_from_judge({"score": 0.75}, "answer_relevancy") == 0.75
    # 字符串分数也要能收
    assert judge.score_from_judge({"score": "0.6"}, "answer_relevancy") == pytest.approx(0.6)


def test_score_from_judge_faithfulness_is_support_ratio():
    parsed = {"claims": [{"supported": True}, {"supported": False}, {"supported": True}]}
    assert judge.score_from_judge(parsed, "faithfulness") == pytest.approx(2 / 3)


def test_score_from_judge_missing_field_returns_none():
    assert judge.score_from_judge(None, "answer_relevancy") is None
    assert judge.score_from_judge({}, "answer_relevancy") is None
    assert judge.score_from_judge({"reason": "没有分数"}, "answer_relevancy") is None
    assert judge.score_from_judge({"score": "高"}, "answer_relevancy") is None
    # faithfulness：没有 claims / claims 为空 → 无法判 → None
    assert judge.score_from_judge({"claims": []}, "faithfulness") is None
    assert judge.score_from_judge({}, "faithfulness") is None


# ==================== 5. aggregate：None 与有效分分别计数 ====================

def test_aggregate_counts_none_separately_and_means_valid_only():
    agg = judge.aggregate([0.5, None, 1.0, None])
    assert agg["n_total"] == 4
    assert agg["n_valid"] == 2
    assert agg["n_missing"] == 2
    assert agg["mean"] == pytest.approx(0.75)


def test_aggregate_all_none_has_no_mean():
    agg = judge.aggregate([None, None])
    assert agg["n_valid"] == 0
    assert agg["n_missing"] == 2
    assert agg["mean"] is None


def test_aggregate_empty_input():
    agg = judge.aggregate([])
    assert agg["n_total"] == 0 and agg["n_valid"] == 0 and agg["mean"] is None


# ==================== 6. prompt 契约（JSON 唯一输出格式） ====================

def test_faithfulness_prompt_is_json_only():
    msgs = judge.faithfulness_prompt("营业总收入是多少", "1,741.44 亿元", ["原文：1741.44 亿元"])
    assert isinstance(msgs, list) and msgs
    joined = " ".join(m["content"] for m in msgs)
    assert "claims" in joined and "supported" in joined
    assert "JSON" in joined
    assert "不要输出任何解释文字" in joined


def test_relevancy_prompt_is_json_only():
    msgs = judge.relevancy_prompt("营业总收入是多少", "1,741.44 亿元")
    assert isinstance(msgs, list) and msgs
    joined = " ".join(m["content"] for m in msgs)
    assert "score" in joined and "JSON" in joined
    assert "不要输出任何解释文字" in joined


def test_faithfulness_helper_returns_ratio():
    assert judge.faithfulness({"claims": [{"supported": True}, {"supported": True}]}) == 1.0
    assert judge.faithfulness({"claims": [{"supported": True}, {"supported": False}]}) == 0.5
    assert judge.faithfulness(None) is None


# ==================== 7. judge_answer：失败只填 None + notes，绝不抛异常 ====================

class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeJudge:
    """按 prompt 里出现的关键字返回不同的合法 JSON（模拟一次成功的判分）。"""

    model_name = "fake-judge"

    def invoke(self, messages):
        text = " ".join(m["content"] if isinstance(m, dict) else getattr(m, "content", "")
                        for m in messages)
        if "claims" in text:
            payload = '{"claims": [{"claim": "营收为1741.44亿", "supported": true}]}'
        else:
            payload = '{"score": 0.9, "reason": "直接回答了问题"}'
        return _Msg(payload)


class _BoomJudge:
    """永远抛异常（模拟网络/额度故障）。"""

    model_name = "boom-judge"

    def invoke(self, messages):
        raise RuntimeError("network down")


def test_judge_answer_success_shape():
    out = judge.judge_answer("营业总收入是多少", "1,741.44 亿元", ["原文：1741.44 亿元"],
                             model=_FakeJudge())
    assert set(out) >= {"faithfulness", "answer_relevancy", "judge_model", "notes"}
    assert out["faithfulness"] == 1.0
    assert out["answer_relevancy"] == pytest.approx(0.9)
    assert out["judge_model"] == "fake-judge"


def test_judge_answer_swallows_failure():
    """判分失败 → 两个分数都是 None + notes 有记录，且**不抛异常**。"""
    out = judge.judge_answer("问题", "答案", ["上下文"], model=_BoomJudge())
    assert out["faithfulness"] is None
    assert out["answer_relevancy"] is None
    assert out["notes"]
    assert isinstance(out["judge_model"], str) and out["judge_model"]
