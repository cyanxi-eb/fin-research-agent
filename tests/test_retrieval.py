"""检索与引用用例（纯内存，不落盘、不联网）。

用确定性断言守两件容易悄悄坏掉的事：
1. **过滤不能漏**：问题里点名了公司/年份，结果就不能混进别家的内容 ——
   金融场景引错公司比答不上来更糟。
2. **零分不返回**：BM25 对完全没命中的 chunk 返回 0，必须排除，
   否则会把不相干内容当成"检索结果"喂给 LLM。
"""
from __future__ import annotations

from src import citation, config
from src.retrieve.bm25 import BM25Index, normalize_for_match, tokenize


def _chunk(code, company, year, page, section, text):
    return {"chunk_id": f"{code}-{year}-p{page}-1", "code": code, "company": company,
            "year": year, "report_type": "annual", "section": section, "page_no": page,
            "part": 1, "parts_total": 1, "kind": "text", "text": text,
            "chars": len(text)}


CORPUS = [
    _chunk("600519", "贵州茅台", 2024, 12, "管理层讨论与分析",
           "本期毛利率较上年同期提升，主要产品出厂价未发生重大变化。"),
    _chunk("600519", "贵州茅台", 2024, 45, "财务报告",
           "资产负债率维持在较低水平，货币资金充裕。"),
    _chunk("600519", "贵州茅台", 2025, 8, "管理层讨论与分析",
           "公司营业总收入同比增长，经营活动产生的现金流量净额稳定。"),
    _chunk("300750", "宁德时代", 2024, 60, "管理层讨论与分析",
           "毛利率受原材料价格波动影响，公司持续推进降本增效。"),
]


def _idx() -> BM25Index:
    return BM25Index.build(CORPUS)


def test_tokenize_handles_thousand_separator():
    """千分位必须先去掉，否则 123,456.78 会被切成 123/`,`/456.78 三段，和问句对不上。
    小数本身也要留住（财务文本里遍地都是）。
    """
    toks = tokenize("营业总收入 123,456.78 万元")
    assert "123456.78" in toks
    assert "万元" in toks
    # jieba 词典里没有「营业总收入」这个词，search 模式会拆成 营业/收入/总收入 ——
    # 这是设计内的（靠子词提召回），所以这里只断言确实拆出来了，而不是断言整体保留
    assert {"营业", "收入", "总收入"} <= set(toks)


def test_tokenize_drops_pure_punctuation():
    toks = tokenize("毛利率、净利率（%）")
    assert toks
    assert all(t.strip() and not t.isascii() or t.isalnum() for t in toks)
    assert "%" not in toks


def test_search_finds_relevant_chunk_with_citation():
    hits = _idx().search("毛利率", topk=5)
    assert hits, "必须能召回"
    assert all(h["score"] > 0 for h in hits)
    top = hits[0]
    assert "毛利率" in top["chunk"]["text"]
    assert "P" in top["citation"] and "2024年年报" in top["citation"]


def test_company_filter_excludes_others():
    """点名公司后不允许混入别家 —— 引错公司比拒答更严重。"""
    hits = _idx().search("毛利率", topk=10, code="600519")
    assert hits
    assert {h["chunk"]["code"] for h in hits} == {"600519"}
    assert all("宁德时代" not in h["chunk"]["text"] for h in hits)


def test_year_and_section_filter():
    idx = _idx()
    assert {h["chunk"]["year"] for h in idx.search("毛利率", topk=10, code="600519")} == {2024}
    hits = idx.search("货币资金", topk=5, section="财务报告")
    assert hits and all(h["chunk"]["section"] == "财务报告" for h in hits)


def test_no_match_returns_empty_not_garbage():
    """查不到就该返回空（上层据此拒答），不能拿零分结果凑数。"""
    assert _idx().search("量子计算机芯片制程", topk=5) == []
    assert _idx().search("", topk=5) == []


def test_metadata_filter_applied_before_topk_cut():
    """过滤后再取 topk：命中集合足够大时，限定公司仍应拿到自己那条。"""
    hits = _idx().search("毛利率 资产负债率 现金流", topk=1, code="600519")
    assert len(hits) == 1
    assert hits[0]["chunk"]["code"] == "600519"


def test_citation_format_variants():
    assert citation.format_citation(CORPUS[0], 1) == \
        "[1] 贵州茅台2024年年报 P12 管理层讨论与分析"
    # 缺 section / 公司名时不能崩，也不能拼出空引用
    t = citation.format_citation({"code": "300750", "year": 2024, "page_no": 3})
    assert t == "3007502024年年报 P3"
    # 多段时补段号
    multi = {**CORPUS[0], "part": 2, "parts_total": 3}
    assert "第2/3段" in citation.format_citation(multi, 2)


def test_phrase_hit_is_rewarded_over_subtoken_noise():
    """回归用例：jieba search 模式会把「资产负债率」额外切成 资产/负债/率，
    导致只沾了子词的段落也能拿高分 —— 实测出现过 top1 完全不含该短语。
    修法是给「整短语命中」加分，这里断言加成确实生效且只给命中短语的那一条。

    注意语料不能只有两条：BM25Okapi 的 IDF = log((N-df+0.5)/(df+0.5))，
    N=2 且 df=1 时恰好为 0，全部文档都会得 0 分，那就变成在测 IDF 而不是测加成。
    所以下面补了几条无关文档把 N 抬起来。
    """
    import pytest

    exact = _chunk("600519", "贵州茅台", 2024, 30, "财务报告",
                   "公司资产负债率为45.2%，处于行业合理区间。")
    scattered = _chunk("600519", "贵州茅台", 2024, 31, "财务报告",
                       "资产总额上升，负债结构中长期负债占比提高，税率保持稳定。")
    fillers = [
        _chunk("600519", "贵州茅台", 2024, 32 + i, "财务报告",
               f"公司持续推进产能建设与市场拓展，第{i}部分说明如下。")
        for i in range(4)
    ]
    idx = BM25Index.build([exact, scattered, *fillers])
    hits = idx.search("资产负债率", topk=10)

    by_id = {h["chunk"]["chunk_id"]: h for h in hits}
    a, b = by_id[exact["chunk_id"]], by_id[scattered["chunk_id"]]
    assert a["phrase_hits"] >= 1, "含整短语的段落必须被判为短语命中"
    assert b["phrase_hits"] == 0, "只沾子词的段落不该拿到短语加成"
    assert a["score"] - a["bm25"] == pytest.approx(
        a["phrase_hits"] * config.BM25_PHRASE_BONUS)
    assert a["score"] > b["score"], "短语命中的段落应排在只有子词命中的段落之前"


def test_phrase_hit_normalizes_whitespace():
    """PDF 抽出来的中文常被换行/空格切断，短语匹配必须先去掉空白。"""
    assert normalize_for_match("资产 负债\n率") == "资产负债率"
    broken = _chunk("600519", "贵州茅台", 2024, 32, "财务报告",
                    "公司资产 负债\n率为 45.2%。")
    idx = BM25Index.build([broken])
    hits = idx.search("资产负债率", topk=1)
    assert hits and hits[0]["phrase_hits"] >= 1


# ==================== 同页配额（Step 4 收尾新增）====================

def _hit_seq(pages, *, code="600519", year=2024):
    """按给定页码序列造 hits（顺序即名次）。"""
    return [{"chunk_id": f"{code}-{year}-p{p}-{i}", "code": code, "year": year,
             "page_no": p, "score": 100.0 - i} for i, p in enumerate(pages)]


def test_page_quota_keeps_top_ranked_chunks_of_each_page():
    from src.retrieve.pipeline import apply_page_quota

    hits = _hit_seq([9, 9, 9, 16, 16, 56])
    kept = apply_page_quota(hits, 2)
    assert [h["page_no"] for h in kept] == [9, 9, 16, 16, 56]
    assert [h["chunk_id"] for h in kept][:2] == [h["chunk_id"] for h in hits][:2], \
        "保留的必须是该页**名次靠前**的块"


def test_page_quota_never_drops_first_chunk_of_a_page(monkeypatch):
    """核心性质：配额后 top-k 覆盖的**页面集合只增不减**。

    这是选它当默认值的依据 —— 对本项目最关心的 `page_hit@k` 而言，
    它只可能持平或上升，不可能把原本命中的页挤掉。
    证明：某页的第 1 块永远不被丢弃，而第 j 个被选中的块在原名次上必然 >= j，
    所以"第 1 块落在原始 top-k"的页面一定仍在配额后的 top-k 里。
    这里用**穷举小序列**把它钉住，而不是靠一句话。
    """
    from itertools import product
    from src.retrieve.pipeline import apply_page_quota

    topk, quota = 4, 2
    for pages in product([1, 2, 3], repeat=6):        # 3^6 = 729 种排列，穷举
        hits = _hit_seq(list(pages))
        plain = {h["page_no"] for h in hits[:topk]}
        kept = {h["page_no"] for h in apply_page_quota(hits, quota)[:topk]}
        assert plain <= kept, f"序列 {pages} 丢了页面：{plain - kept}"


def test_page_quota_key_includes_company_and_year():
    """同一页码在不同公司/年份是**不同的页** —— 键少了 code/year 会误判成同页。"""
    from src.retrieve.pipeline import apply_page_quota

    a = {"chunk_id": "a", "code": "600519", "year": 2024, "page_no": 13}
    b = {"chunk_id": "b", "code": "000858", "year": 2024, "page_no": 13}
    c = {"chunk_id": "c", "code": "600519", "year": 2025, "page_no": 13}
    d = {"chunk_id": "d", "code": "600519", "year": 2024, "page_no": 13}
    assert [h["chunk_id"] for h in apply_page_quota([a, b, c, d], 1)] == ["a", "b", "c"]


def test_page_quota_zero_means_unlimited():
    from src.retrieve.pipeline import apply_page_quota

    hits = _hit_seq([9, 9, 9])
    assert len(apply_page_quota(hits, 0)) == 3, "0 = 不限（Step 3/4 原始行为，评测基线要用它）"
    assert len(apply_page_quota(hits, None)) == 3 or config.RETRIEVE_MAX_PER_PAGE > 0


def test_page_key_is_the_identity_of_a_page():
    from src.retrieve.pipeline import page_key
    assert page_key({"code": "600519", "year": 2024, "page_no": 8}) == ("600519", 2024, 8)
    assert page_key({}) == (None, None, None), "字段缺失也不能抛异常"
