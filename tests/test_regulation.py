"""法规语料用例（切分 + 检索 + 版本处理）。

法规与年报最大的不同是**答案单位是"条"**：切错半条会让"引用第二十条"这件事
失效（用户拿引用去核对，对不上）。所以切分规则必须有用例守着。

两条实测事故写成了回归用例：
1. 噪音词表用了**子串**匹配，`简` 把「（以下简称《公司法》）」整行删掉 ——
   法条依据被静默删除，句子却读得通（最难发现的那种错）；
2. gov.cn 页脚导航把几十行站点链接粘进了最后一条（第六十七条）。

另外真实原文不在仓库里也能跑（`data/regulation/raw/` 缺失时跳过真实语料断言），
但**只要文件在，就必须断言条号连续无缺** —— 缺号意味着切分吞掉了内容。
"""
from __future__ import annotations

import pytest

from src.ingest import fetch_regulation as fr
from src import config
from src.retrieve import regulation


# ==================== 中文条号 ====================

@pytest.mark.parametrize("text,expect", [
    ("一", 1), ("十", 10), ("十一", 11), ("二十", 20), ("二十一", 21),
    ("三十", 30), ("九十九", 99), ("一百", 100), ("一百零三", 103), ("一百二十二", 122),
])
def test_cn_to_int(text, expect):
    assert fr.cn_to_int(text) == expect


def test_cn_to_int_rejects_unknown():
    with pytest.raises(ValueError):
        fr.cn_to_int("甲乙")


# ==================== 切分 ====================

DOC = {"doc_id": "t-1", "title": "测试办法", "doc_no": "测试令第1号",
       "status": "current", "effective_from": "2020-01-01", "source_name": "测试源",
       "url": "http://example.invalid"}


def test_split_keeps_parenthetical_abbreviation():
    """`简` 这类**短且常用**的词不能做子串过滤（实测把法条依据整行删掉过）。"""
    text = ("测试办法\n第一章　总　　则\n"
            "第一条\n根据《中华人民共和国公司法》（以下简称《公司法》），制定本办法。\n"
            "第二条\n本办法自公布之日起施行。\n")
    chunks = fr.split_articles(text, DOC)
    assert len(chunks) == 2
    assert "以下简称《公司法》" in chunks[0]["text"], "括号里的简称说明被删了"


def test_split_handles_article_number_on_same_line():
    """PDF 抽取常见 `第三条信息披露义务人应当…`（条号与正文同行）。"""
    text = ("第一章总\n则\n第一条\n为了规范行为，制定本办法。\n"
            "第三条信息披露义务人应当及时履行义务。\n")
    chunks = fr.split_articles(text, DOC)
    labels = [c["article_label"] for c in chunks]
    assert labels == ["第一条", "第三条"]
    assert chunks[1]["text"].startswith("第三条　信息披露义务人")


def test_split_recovers_chapter_title_split_across_lines():
    """章的标题被换行拆开（`第一章总` + `则`）时要拼回来，否则章节名变成"总"。"""
    text = "第一章总\n则\n第一条\n内容。\n"
    chunks = fr.split_articles(text, DOC)
    assert chunks[0]["chapter"] == "第一章 总则"


def test_split_cuts_site_navigation_out_of_last_article():
    """站点导航（`相关稿件` + 几十行链接）不能被粘进最后一条。"""
    text = ("第一条\n正文。\n"
            "相关稿件\n链接：全国人大|全国政协\n"
            "国务院部门网站\n地方政府网站\n"
            "中国证券监督管理委员会令（第1号）\n测试办法\n正文。\n")
    chunks = fr.split_articles(text, DOC)
    assert len(chunks) == 1
    assert chunks[0]["text"].rstrip() == "第一条　正文。"
    assert "全国政协" not in chunks[0]["text"]


def test_split_returns_empty_when_no_article_found():
    assert fr.split_articles("这是一段没有任何条号的文本。", DOC) == []


def test_chunk_carries_own_citation():
    """法规 chunk 自带 citation —— 年报的引用模板拼不出「文号+条号」。"""
    text = "第一条\n内容。\n"
    c = fr.split_articles(text, DOC)[0]
    assert c["citation"] == "《测试办法》（测试令第1号）第一条"
    assert c["chunk_id"] == "t-1#art1"


# ==================== 真实原文（文件在才跑）====================

def _has_raw() -> bool:
    return all((config.REGULATION_RAW_DIR / d["local"]).exists() for d in fr.SOURCES)


@pytest.mark.skipif(not _has_raw(), reason="未下载法规原文（python -m src.ingest.fetch_regulation）")
def test_real_corpus_article_numbers_are_gapless():
    """真实原文切分后**条号必须连续无缺** —— 缺号就说明切分吞掉了内容。"""
    chunks = fr.load_corpus()
    by_doc: dict[str, list[int]] = {}
    for c in chunks:
        by_doc.setdefault(c["doc_id"], []).append(c["article_no"])
    assert by_doc, "真实原文存在却切出 0 条，是切分规则失效"
    for doc_id, nums in by_doc.items():
        assert nums == sorted(nums), f"{doc_id} 条号不递增"
        missing = [n for n in range(1, max(nums) + 1) if n not in nums]
        assert missing == [], f"{doc_id} 缺失条号 {missing}"


@pytest.mark.skipif(not _has_raw(), reason="未下载法规原文")
def test_real_corpus_keeps_first_article_reference_clause():
    """第一条里的**制定依据**（根据《公司法》《证券法》…）必须在。

    这是上面那条"子串误删"事故的门禁：删掉后句子依然通顺，只有专门断言才守得住。
    """
    first = [c for c in fr.load_corpus() if c["article_no"] == 1]
    assert first
    for c in first:
        assert "《中华人民共和国公司法》" in c["text"]
        assert "《中华人民共和国证券法》" in c["text"]


@pytest.mark.skipif(not _has_raw(), reason="未下载法规原文")
def test_real_corpus_last_article_has_no_site_footer():
    last = {}
    for c in fr.load_corpus():
        last[c["doc_id"]] = c
    for doc_id, c in last.items():
        assert "京ICP备" not in c["text"], f"{doc_id} 最后一条混进了页脚"
        assert "相关稿件" not in c["text"], f"{doc_id} 最后一条混进了站点导航"


# ==================== 检索与版本 ====================

@pytest.mark.skipif(not regulation.available(), reason="法规索引未构建")
def test_search_finds_article_and_citation_is_article_level():
    hits = regulation.search("定期报告 披露 期限", topk=5)
    assert hits
    top = hits[0]
    assert top["citation"].startswith("《上市公司信息披露管理办法》")
    assert "第" in top["citation"] and "条" in top["citation"]


@pytest.mark.skipif(not regulation.available(), reason="法规索引未构建")
def test_prefer_current_puts_current_version_first():
    """同一问题会同时命中新旧两版；**现行版必须排前面**。

    只按分数排会出现"合规回答的第一条引用已经废止的版本" —— 用户看到的是
    权威口吻的旧条文，而新版就排在下面一条。`status` 是确定性的，优先级不能低于分数。
    """
    hits = regulation.search("未在规定期限内披露年度报告", topk=8)
    primary, _superseded = regulation.prefer_current(hits)
    assert primary
    if any((h["chunk"].get("status") == "current") for h in primary):
        assert primary[0]["chunk"]["status"] == "current"


def test_prefer_current_keeps_superseded_only_when_text_differs():
    """两版文字完全一致时不提示"版本差异"（噪音会削弱真实差异的可信度）。"""
    def _hit(status, text):
        return {"chunk": {"article_no": 20, "status": status, "article_label": "第二十条",
                          "text": text, "doc_id": f"d-{status}"}, "score": 1.0,
                "citation": f"({status}) 第二十条"}

    primary, sup = regulation.prefer_current([_hit("superseded", "第二十条　一样的内容"),
                                             _hit("current", "第二十条　一样的内容")])
    assert len(primary) == 1 and primary[0]["chunk"]["status"] == "current"
    assert sup == []

    primary, sup = regulation.prefer_current([_hit("superseded", "第二十条　旧内容"),
                                             _hit("current", "第二十条　新内容")])
    assert len(sup) == 1, "正文不同时必须提示版本差异"
