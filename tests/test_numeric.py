"""数值抽取与比对用例（纯函数）。

这是"零幻觉校验"的公共零件：判断"答案里的这个数字能不能被证据解释"。
它的失败方向有两个，**误报比漏报更危险**：

- 误报（把对的判成错的）→ 正确回答被无谓挂起，很快会让人开始怀疑校验本身，
  最后把闸门关掉 —— 那才是真损失；
- 漏报（把编的判成对的）→ 闸门形同虚设。

所以用例对称地覆盖两侧：**该放行的必须放行**（单位换算、千分位、展示舍入），
**该拦的必须拦**（凭空多出来的百分比）。
"""
from __future__ import annotations

from src import numeric


def test_skips_citation_markers_and_article_numbers():
    """引用角标与条文编号不是数据，必须排除。

    不排除的话，每条合规回答都会因为 "182 / 226 / 20" 这些标识符被判
    `citation_unsupported` → 无谓挂起，待确认队列变垃圾场（实测踩过）。
    """
    text = "见 [3] 与第二十条、第五章；证监会令第182号第二十条规定…"
    txt = text
    assert numeric.text_numbers(txt) == [], f"不该抽出数字，实际 {numeric.text_numbers(txt)}"


def test_extracts_plain_and_thousand_separated_numbers():
    nums = numeric.text_numbers("营业总收入 1,741.44 亿元，毛利率 91.93%")
    assert 1741.44 in nums and 91.93 in nums


def test_skips_enumerations():
    assert numeric.text_numbers("1) 第一项 2）第二项") == []


def test_skips_numbers_inside_urls():
    """URL 里的数字不是"答案里的数字"。

    答案的来源行会带年报原文链接（巨潮 PDF，形如
    `…/finalpage/2025-04-03/1222993920.PDF`），里面全是数字 ——
    不豁免的话，verify 会把一条完全正确的答案判成"有 4 个无出处数字"
    并挂起。这是 2026-09-25 组句加入「原文：URL」后必踩的联动坑。
    """
    text = ("贵州茅台「营业总收入」= 1,741.44 亿元\n"
            "原文：http://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF")
    vals = numeric.text_numbers(text)
    for leak in (2025.0, 403.0, 1222993920.0):
        assert leak not in vals, f"URL 里的 {leak} 被当成答案数字抽出：{vals}"
    assert 1741.44 in vals, "正文数字必须照常抽出（豁免只针对 URL 内部）"
    # 校验口径上同样要过：allowed 只含主值的原始单位 → 不因 URL 误报
    assert numeric.unsupported_numbers(text, [174144000000.0]) == []


def test_unit_variants_match_raw_value():
    """工具给的是原始单位（元），答案写的是展示单位（亿元/万元）—— 必须能对上。

    对不上就会出现"正确答案被自己的校验判成幻觉"，比不校验更糟。
    """
    raw = 1.7414355e11
    assert numeric.is_supported(1741.44, [raw]), "1741.44 亿元 应能由 1.7414355e11 元解释"
    # 万元写法：1.7414355e11 元 = 17,414,355 万元
    assert numeric.is_supported(17414355.0, [raw]), "万元写法也要能对上"


def test_unsupported_when_value_nowhere_to_be_found():
    """凭空多出来的百分比必须被拦（这是真实的编造形态）。"""
    bad = numeric.unsupported_numbers("毛利率为 92.5%", [91.93])
    assert [b["raw"] for b in bad] == ["92.5"]
    assert numeric.unsupported_numbers("毛利率为 91.93%", [91.93]) == []


def test_rounding_tolerance_is_tight_but_realistic():
    """容差取 0.1% 相对：要容得下"原始值写成两位小数"，但要拦得住 0.6% 的偏差。"""
    raw = 1.7414355e11
    assert numeric.is_supported(1741.43, [raw])       # 0.0006% 误差 → 放行
    assert not numeric.is_supported(1730.0, [raw])    # 0.66% 误差 → 拦下


def test_collect_evidence_numbers_reads_nested_payload():
    """证据集合必须能读嵌套 payload（`display` / `official` / `terms` 里才是答案真正用到的写法）。"""
    payload = {"series": [{"value": 1e11, "display": "1,000.00 亿元",
                           "source": "income.OPERATE_INCOME"}]}
    got = numeric.collect_evidence_numbers(payload)
    assert 1e11 in got and 1000.0 in got


def test_extract_numbers_reports_positions_for_debugging():
    got = numeric.extract_numbers("毛利率 91.93%")
    assert len(got) == 1 and got[0]["start"] == 4
