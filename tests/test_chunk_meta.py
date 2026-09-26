"""切分用例：不跨页、元数据完整、碎块合并且不丢页码。

核心断言是「**每个 chunk 都能拼出引用**」—— 少一个字段就拼不出
`[1] 贵州茅台2024年年报 P87 第三节`，溯源能力就断了。
"""
from __future__ import annotations

from src import config, citation
from src.ingest import chunk as chunk_mod


def _parsed(pages: list[tuple[int, str | None, str]]) -> dict:
    return {
        "code": "600000", "company": "测试公司", "year": 2024, "page_count": len(pages),
        "pages": [{"page_no": n, "section": s, "text": t, "chars": len(t)}
                  for n, s, t in pages],
    }


def test_chunks_never_cross_pages():
    """一块长文本被切多块时，每块都必须归属到同一页，页码不能变成区间。"""
    long_text = "公司经营情况讨论与分析。" * 200      # 远超 CHUNK_SIZE
    parsed = _parsed([(7, "管理层讨论与分析", long_text),
                      (8, "管理层讨论与分析", long_text)])
    chunks = chunk_mod.chunk_parsed(parsed)
    assert len(chunks) > 2, "长页应被切成多块"
    for c in chunks:
        assert c["page_no"] in (7, 8)
        # 去掉页内 overlap 后，块文本必须仍是本页文本的子串 —— 若跨页拼接就会失败
        assert c["text"] in long_text or \
            c["text"][config.CHUNK_OVERLAP:] in long_text


def test_chunk_metadata_is_complete_enough_for_citation():
    parsed = _parsed([(87, "管理层讨论与分析", "本报告期毛利率有所提升。" * 10)])
    chunks = chunk_mod.chunk_parsed(parsed)
    assert chunks
    for c in chunks:
        for field in ("chunk_id", "code", "company", "year", "report_type",
                      "section", "page_no", "part", "parts_total", "kind", "text"):
            assert field in c, f"缺字段 {field} → 引用拼不出来"
        text = citation.format_citation(c)
        assert "测试公司2024年年报" in text
        assert "P87" in text
        assert "管理层讨论与分析" in text


def test_short_page_stays_single_chunk():
    """整页就短的时候不要切碎（页尾孤字会污染索引）。"""
    parsed = _parsed([(3, None, "本节内容较少，仅一句话。")]
                     + [(i, None, "正常内容。" * 60) for i in range(4, 12)])
    chunks = [c for c in chunk_mod.chunk_parsed(parsed) if c["page_no"] == 3]
    assert len(chunks) == 1
    assert chunks[0]["parts_total"] == 1


def test_tiny_tail_merged_into_previous():
    """页尾碎块应并回前一块，而不是单独成 chunk。"""
    text = "甲公司经营情况。" * 60 + "尾"
    pieces = chunk_mod._merge_tiny(chunk_mod.split_text(text, 120, 0), config.MIN_CHUNK_CHARS)
    assert all(len(p) >= config.MIN_CHUNK_CHARS for p in pieces), pieces


def test_split_respects_upper_bound_with_fallback():
    """没有任何分隔符时也必须能切（兜底硬切），且不产生空块。"""
    pieces = chunk_mod.split_text("A" * 1000, 300, 0)
    assert len(pieces) >= 4
    assert all(pieces)
    assert "".join(pieces) == "A" * 1000


def test_empty_pages_are_skipped():
    parsed = _parsed([(1, None, ""), (2, None, "   "), (3, None, "有内容。" * 20)])
    chunks = chunk_mod.chunk_parsed(parsed)
    assert all(c["page_no"] == 3 for c in chunks)
    assert chunks


# ==================== 节名下沉到 chunk 级 ====================

def _parsed_with_marks(page_no: int, section: str | None, marks: list[dict], text: str) -> dict:
    return {
        "code": "600000", "company": "测试公司", "year": 2024, "page_count": 1,
        "pages": [{"page_no": page_no, "section": section, "section_marks": marks,
                   "text": text, "chars": len(text)}],
    }


def test_section_at_picks_last_mark_before_offset():
    marks = [{"offset": 0, "section": "A"}, {"offset": 100, "section": "B"}]
    assert chunk_mod.section_at(marks, 0, 50, None) == "A"
    assert chunk_mod.section_at(marks, 99, 150, None) == "A"
    assert chunk_mod.section_at(marks, 100, 200, None) == "B"
    # 完全没有标记时才用兜底值
    assert chunk_mod.section_at([], 10, 20, "兜底") == "兜底"


def test_chunk_section_uses_in_page_offset_not_page_label():
    """同一页里，**节标题之前**的块必须归属上一节，而不是整页一个标签。

    这是 600519/2024 P7 的回归用例：页级标签是「重要事项」（最后一处标题），
    但该页第一块在节标题之前，属于「环境和社会责任」。
    """
    head = "上一节续页内容。" * 6                     # 60 字，落在第一块内
    text = head + "本节正文内容。" * 200
    parsed = _parsed_with_marks(
        40, "重要事项",
        [{"offset": 0, "section": "环境和社会责任"},
         {"offset": len(head), "section": "重要事项"}],
        text)
    chunks = chunk_mod.chunk_parsed(parsed)
    assert len(chunks) > 1, "长页应被切成多块"
    assert chunks[0]["section"] == "环境和社会责任"
    assert chunks[-1]["section"] == "重要事项"


def test_unattributed_chunk_adopts_section_starting_inside_it():
    """块首"未归属"但块内含节标题 → 采纳块内第一个节标题。

    回归用例 002594/2024 P5：正文首行是页眉残留「2024 年年度报告」（12 字），
    真正的「第一节 重要提示、目录和释义」跟在后面，整页只有一个块。
    只看起始位置会判成"未归属"，而这块 97% 的内容都属于第一节。
    """
    lead = "2024 年年度报告 "
    text = lead + "第一节 重要提示、目录和释义 " + "正文内容。" * 60
    parsed = _parsed_with_marks(
        5, "重要提示、目录和释义",
        [{"offset": 0, "section": None},
         {"offset": len(lead), "section": "重要提示、目录和释义"}],
        text)
    chunks = chunk_mod.chunk_parsed(parsed)
    assert len(chunks) == 1
    assert chunks[0]["section"] == "重要提示、目录和释义"


def test_chunk_with_clear_start_section_is_not_hijacked_by_later_mark():
    """反向约束：块首已有明确章节时**不许**被后半段的标题带走。

    回归用例 601318/2024 P111：页首两行是「股本变动及股东情况」「公司管治」，
    正文讲的是股本变动 —— 按"覆盖字符最多"去判会被后半段带成「公司治理」，
    那是把对的判成错的。所以只对 None 做提升。
    """
    text = "股本变动及股东情况\n公司管治\n" + "股份变动情况表及说明。" * 60
    parsed = _parsed_with_marks(
        111, "公司治理",
        [{"offset": 0, "section": "股份变动及股东情况"},
         {"offset": 10, "section": "公司治理"}],
        text)
    chunks = chunk_mod.chunk_parsed(parsed)
    assert chunks[0]["section"] == "股份变动及股东情况"


def test_locate_offsets_is_monotonic_and_accurate():
    """偏移定位必须单调、不越界，且第一块落在文本开头（兜底也不能乱序）。"""
    text = "公司经营情况讨论与分析。" * 200
    pieces = chunk_mod.split_text(text, 200, 30)
    offs = chunk_mod.locate_offsets(text, pieces, 30)
    assert len(offs) == len(pieces)
    assert offs[0] == 0
    assert offs == sorted(offs)
    assert all(0 <= o <= len(text) for o in offs)


def test_legacy_parsed_without_marks_still_works():
    """旧解析产物（没有 section_marks）要能直接跑 —— 退回页级标签而不是崩。"""
    parsed = _parsed([(3, "管理层讨论与分析", "本报告期毛利率有所提升。" * 20)])
    chunks = chunk_mod.chunk_parsed(parsed)
    assert chunks
    assert all(c["section"] == "管理层讨论与分析" for c in chunks)
