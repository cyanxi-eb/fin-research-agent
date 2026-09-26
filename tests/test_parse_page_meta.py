"""解析层用例：页码、章节识别、页眉页脚清理 —— 全是溯源能力的前提。

这三件事任一做错，引用就会指到错误的位置，而这种错在人工抽查时
**看起来像对的**（页码存在、章节名也存在），所以必须有确定性用例守住。
"""
from __future__ import annotations

from src.ingest import parse_pdf


def test_page_no_is_bound_to_page_text(synth_pdf):
    """页码必须逐页绑定：第 N 个元素就是第 N 页。"""
    r = parse_pdf.parse_pdf(synth_pdf, "600000", "测试公司", 2024)
    assert r["page_count"] == 6
    assert [p["page_no"] for p in r["pages"]] == [1, 2, 3, 4, 5, 6]
    for p in r["pages"]:
        assert p["chars"] == len(p["text"])
        assert p["text"]  # 每页都该有正文


def test_toc_page_does_not_hijack_section(synth_pdf):
    """目录页一次列全 10 个章节 —— 若不识别目录页，第 1 页就会被判成「财务报告」。"""
    r = parse_pdf.parse_pdf(synth_pdf, "600000", "测试公司", 2024)
    assert r["pages"][0]["section"] is None, "目录页不应产生章节归属"


def test_section_propagates_and_switches(synth_pdf):
    """章节要能向后传递，并在遇到新标题页时切换。"""
    r = parse_pdf.parse_pdf(synth_pdf, "600000", "测试公司", 2024)
    secs = [p["section"] for p in r["pages"]]
    assert secs == [
        None,                        # 目录页
        "重要提示、目录和释义",       # 第二节（P2）起
        "重要提示、目录和释义",       # P3 继承
        "管理层讨论与分析",           # P4 切换
        "管理层讨论与分析",           # P5 继承
        "管理层讨论与分析",           # P6 继承
    ]
    spans = r["section_pages"]
    assert len(spans) == 2
    assert spans[0] == {"section": "重要提示、目录和释义", "start_page": 2, "end_page": 3}
    assert spans[1] == {"section": "管理层讨论与分析", "start_page": 4, "end_page": 6}


def test_running_footer_is_stripped(synth_pdf):
    """跨页重复的页脚（公司名+年份+页码）必须清掉，否则会污染检索与引用。"""
    r = parse_pdf.parse_pdf(synth_pdf, "600000", "测试公司", 2024)
    assert r["dropped_headers"], "应识别出至少一种页眉页脚"
    assert any("年度报告" in h for h in r["dropped_headers"])
    for p in r["pages"]:
        assert "测试公司2024年年度报告" not in p["text"]


def test_missing_pdf_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        parse_pdf.parse_pdf(tmp_path / "nope.pdf", "600000", "测试公司", 2024)


def _make_pdf(tmp_path, pages: list[list[str]], name: str = "x.pdf"):
    import pymupdf

    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        y = 72
        for ln in lines:
            page.insert_text((72, y), ln, fontname="china-s", fontsize=11)
            y += 18
    p = tmp_path / name
    doc.save(p)
    doc.close()
    return p


def test_heading_like_noise_is_not_a_section_heading(tmp_path):
    """回归用例：只有「整行就是章节名（+白名单后缀）」才算标题。

    曾经用宽松的 `startswith(章节名) and 剩余<=6字符`，于是：
    - 正文句子碎片「经审计后确认财务报告的真实性」被命中成「财务报告」；
    - 页眉「公司治理报告」（= 公司治理 + 报告）被命中成「公司治理」，
      并一路带到全书末尾，导致整本报告章节标签全错。
    现在「公司治理报告」只作为**显式别名**为港式体例（中国平安）收录，其它后缀一律不认。
    """
    body = "本报告期公司经营情况稳定，各项业务有序开展。" * 3
    pdf = _make_pdf(tmp_path, [
        ["经审计后确认财务报告的真实性", body],   # 句子碎片
        ["公司治理报告情况说明", body],           # 章节名 + 任意后缀（不在白名单）
        [body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert [p["section"] for p in r["pages"]] == [None, None, None], \
        "句子碎片与任意后缀都不能被当成章节标题"
    assert r["section_pages"] == []


def test_numbered_anchor_is_not_fooled_by_body_noise(tmp_path):
    """回归用例：内控自我评价表里有独立成行的「财务报告」。

    它与「非财务报告重大缺陷数量」是同一张表里的成对单元格标签，
    实测命中了 000858 P37 / 002594 P72 / 300750 P64 —— 按名字匹配会凭空切出一个
    假的「财务报告」区间，把真正的「环境和社会责任」「重要事项」全吞掉。
    只要报告带「第X节」锚点，就只认锚点，这类噪声一个都不会误命中。
    """
    body = "公司内部控制制度健全，报告期内未发现重大缺陷。" * 3
    pdf = _make_pdf(tmp_path, [
        ["第一节 重要提示、目录和释义", body],
        ["第二节 公司简介和主要财务指标", body],
        ["第三节 管理层讨论与分析", body],
        ["第四节 公司治理", body],
        ["财务报告", "财务报告重大缺陷数量（个）", body],   # 表格标签，不是标题
        ["第五节 环境和社会责任", body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert r["section_mode"] == "节号锚点"
    assert [p["section"] for p in r["pages"]] == [
        "重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
        "公司治理", "公司治理",          # 噪声页不生效，继续归属「公司治理」
        "环境和社会责任"]


def test_numbered_heading_layouts_and_dotted_leader(tmp_path):
    """节号锚点的两种版式都要认，且标题行上的目录点线要先剥掉。

    - 五粮液/比亚迪/宁德：`第九节 债券相关情况` 同行
    - 贵州茅台：`第九节` 换行再写 `债券相关情况`
    - 目录行残留：`第十节 财务报告 ..................`
    """
    body = "本报告期公司业务保持稳定。" * 3
    pdf = _make_pdf(tmp_path, [
        ["第一节 重要提示、目录和释义", body],
        ["第二节 公司简介和主要财务指标", body],
        ["第三节 管理层讨论与分析", body],
        ["第四节 公司治理", body],
        ["第九节", "债券相关情况", body],                  # 版式二：分两行
        ["第十节 财务报告 ..................", body],       # 带目录点线残留
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert r["section_mode"] == "节号锚点"
    assert [p["section"] for p in r["pages"]] == [
        "重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
        "公司治理", "债券相关情况", "财务报告"]


def test_toc_page_caught_by_standalone_toc_line(tmp_path):
    """中国平安的目录页只列 5 行，凑不满「一页命中 >=4 个章节」的判据，
    靠**独立成行的「目录」**兜住。若不兜，P2 的「财务报表」会被带到 149 页。
    （注意这里刻意只放 3 个可识别章节名，确保测的是「目录」判据而不是数量判据。）
    """
    body = "本报告期公司经营情况稳定。" * 3
    pdf = _make_pdf(tmp_path, [
        ["目录", "关于我们", "经营情况讨论及分析", "公司管治"],
        ["关于我们", body],
        [body],
        ["经营情况讨论及分析", body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert r["section_mode"] == "页首标题"
    assert [p["section"] for p in r["pages"]] == [
        None,                              # 目录页不产生归属
        "公司简介和主要财务指标",
        "公司简介和主要财务指标",           # 向后传递
        "管理层讨论与分析",
    ]


def test_allowed_suffix_still_accepted(tmp_path):
    """白名单后缀要保留：续页标记（续）应仍然生效。"""
    body = "本节续接上页内容，继续说明。"
    pdf = _make_pdf(tmp_path, [
        ["管理层讨论与分析", body],
        ["管理层讨论与分析（续）", body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert [p["section"] for p in r["pages"]] == ["管理层讨论与分析", "管理层讨论与分析"]


def test_section_alias_is_accepted(tmp_path):
    """各家体例不同：茅台把第一节写成独立的「释义」一行，走别名收进来。"""
    body = "下列词语在本报告中具有如下含义。"
    pdf = _make_pdf(tmp_path, [["释义", body], [body]])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert r["pages"][0]["section"] == "重要提示、目录和释义"
    assert r["pages"][1]["section"] == "重要提示、目录和释义"


def test_section_marks_carry_in_page_offset(tmp_path):
    """节标题落在**页中段**时，页内偏移必须记下来（「节名下沉到 chunk 级」的依据）。

    实测 600519/2024 P7：`第三节` 与 `管理层讨论与分析` 分两行出现在该页第 32 行，
    而上半页是第二节「非经常性损益」表格的续页。只给整页一个标签，
    这上半页就会被算进第三节 —— 引用出处直接错一节。
    """
    body = "本报告期公司经营情况稳定。" * 3
    pdf = _make_pdf(tmp_path, [
        ["第一节 重要提示、目录和释义", body],
        ["第二节 公司简介和主要财务指标", body],
        ["上半页仍是第二节的续页内容",
         "第二行也属于第二节",
         "第三节", "管理层讨论与分析", body],          # 节标题在页中段
        ["第四节 公司治理", body],
        ["第五节 环境和社会责任", body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    marks = r["pages"][2]["section_marks"]
    assert [m["section"] for m in marks] == ["公司简介和主要财务指标", "管理层讨论与分析"]
    assert marks[0]["offset"] == 0, "页首到第一个节标题之间要显式补一条（继承上一节）"
    assert marks[1]["offset"] > 0, "节标题在页中段 → 偏移必须 > 0"
    # 页级标签仍是「最后一处标题」，section_pages 的语义不变
    assert r["pages"][2]["section"] == "管理层讨论与分析"


def test_section_marks_on_every_page(tmp_path):
    """每页至少有一条 offset=0 的标记 —— 让下游不必跨页传递"上一节是什么"。

    **页是空页也要给**：章节归属按页推进，封面/插图页/被当作跨页页眉清空的页
    都会落进"本页没有节起始点"这一支；这里若不补标记，
    后面所有页都会丢掉章节标签（本用例的 body 行正好会被当成跨页页眉清掉，
    顺手覆盖了这个场景）。
    """
    body = "本报告期公司经营情况稳定。" * 3
    pdf = _make_pdf(tmp_path, [
        ["第一节 重要提示、目录和释义", body],
        [body],
        [body],
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    for p in r["pages"]:
        assert p["section_marks"], f"P{p['page_no']} 没有章节标记"
        assert p["section_marks"][0]["offset"] == 0
    assert [p["section"] for p in r["pages"]] == ["重要提示、目录和释义"] * 3


# ==================== 页眉页脚清理：不能误删数据行（实测事故回归）====================

def test_amount_only_lines_are_never_treated_as_running_headers():
    """**纯数字行绝不参与"跨页重复"判定** —— 否则报表金额会被整片删掉。

    实测事故：归一化把 `127,187,293,298.17` 换成 `#,#,#,#.#`、`248,513,280.00`
    换成 `#,#,#.#`。资产负债表各页的金额**形状**只有那么几种，于是在几十页上
    "重复出现"，被判成页眉 → 整行删除。后果是 600519 年报 50 页之后有 40 页金额全空，
    而 RAG 因此答不了任何"某科目金额是多少"的问题。

    注意合成数据要**像真的**：每页的**科目名**必须不同（真实报表就是这样），
    因为归一化会把数字抹成 `#`，只剩下文字部分参与"是否重复"的比较。
    如果每页都放同一个科目名，那它本身就是"跨页重复行"，被判成页眉删除是正确行为 ——
    用例会因此假红，反而教人怀疑正确的实现。
    """
    items = ["货币资金", "拆出资金", "交易性金融资产", "应收票据",
             "应收账款", "预付款项", "其他应收款"]
    pages = []
    for i, item in enumerate(items, start=1):
        pages.append("\n".join([
            f"贵州茅台酒股份有限公司2024 年年度报告  {i} / 143",       # 真页眉（应删）
            f"{item}  {i}  {i},{i:03d},{i:03d},{i:03d}.17  {i},{i:03d}.58",  # 数据行（应留）
            f"{i},{i:03d},{i:03d}.00",                                    # 只有金额的行（应留）
            f"第{i}页",
        ]))
    cleaned, dropped = parse_pdf._strip_running_headers(pages)

    assert any("年度报告" in d for d in dropped), "真页眉应当仍被识别出来"
    for i, (item, text) in enumerate(zip(items, cleaned), start=1):
        assert item in text, f"数据行被删掉了：{item}"
        assert f"{i},{i:03d},{i:03d}.00" in text, "纯金额行被当成页眉删掉了"
    assert not any(d.strip("#,. ") == "" for d in dropped), \
        f"页眉集合里不该出现纯数字形状：{dropped}"


# ==================== 页首页码不得单独成区间（实测事故回归）====================

def test_page_number_before_heading_does_not_leak_previous_section(tmp_path):
    """页首只有印刷页码时，**不该为那几个字符补一条"上一节"标记**。

    实测事故：每页页首的印刷页码（「6」「146」）占 3~4 个字符，章节标题紧跟在它后面。
    于是"页首到第一个标题之间仍有内容"成立 —— 而那段"内容"其实只是页码，
    却被当成上一节的正文，补了一条 offset=0 的上一节标记。后果是
    **每一章起始页的第一个 chunk 都被标成上一章**（实测 31 个 chunk / 1.0%）：
    601318/2024 P150 的审计报告块被标成「公司治理」；000858/2024 的 P6/P9/P25/P39
    把第二/三/四/五节的起始页分别标成上一节。
    而引用里的章节名是**可核验的出处文字** —— 标错等于给出错误的证据。

    对照（本用例第 5 页）：页首若是**真正文**（上一节的续页）再接标题，
    offset=0 那条必须**保留**为上一节，否则会把上一节那几百字吞进新章节
    （600519/2024 P7 的第 32 行「第三节」正是这种情形）。
    """
    def body(tag: str) -> str:
        # 每页正文必须**各不相同**：相同的行会被 `_strip_running_headers`
        # 判成跨页页眉删掉，用例就不再覆盖"页首是真正文"这个原始场景了。
        return f"{tag}：本报告期公司经营情况稳定，各项业务按计划推进，未发生重大不利变化。"

    pdf = _make_pdf(tmp_path, [
        ["第一节 重要提示、目录和释义", body("一")],        # P1 第一节起
        [body("二")],                                      # P2 继承
        ["3", "第二节 公司简介和主要财务指标", body("三")],  # P3 ← 事故现场：页码 + 标题
        [body("四")],                                      # P4 继承
        [body("五"), "第三节 管理层讨论与分析", body("六")],  # P5 ← 对照：真正文 + 页中标题
        ["6", "第四节 公司治理", body("七")],               # P6 同事故现场
        ["7", "第五节 环境和社会责任", body("八")],         # P7 同事故现场
    ])
    r = parse_pdf.parse_pdf(pdf, "600000", "测试公司", 2024)
    assert r["section_mode"] == "节号锚点"   # 前置：本用例走的是 A 股主流程那条路
    secs = [p["section"] for p in r["pages"]]

    # 事故现场：章节起始页必须归**它自己的**章节，而不是上一节
    assert secs[2] == "公司简介和主要财务指标", \
        f"章节起始页被判成上一节了：{r['pages'][2]['section_marks']}"
    assert r["pages"][2]["section_marks"][0] == {"offset": 0, "section": "公司简介和主要财务指标"}
    assert secs[5] == "公司治理", f"P6 被标成上一节：{r['pages'][5]['section_marks']}"
    assert secs[6] == "环境和社会责任", f"P7 被标成上一节：{r['pages'][6]['section_marks']}"

    # 对照组：页首是真正文时，offset=0 那条仍是上一节，页中标题在更大偏移处生效
    m5 = r["pages"][4]["section_marks"]
    assert m5[0] == {"offset": 0, "section": "公司简介和主要财务指标"}, \
        "页首是真正文时不该丢掉上一节标记（否则上一节内容会被吞进新章节）"
    assert m5[-1]["section"] == "管理层讨论与分析"
    assert m5[-1]["offset"] > 0, "页中标题必须保留真实偏移，不能提前到 0"
    assert secs[4] == "管理层讨论与分析"
