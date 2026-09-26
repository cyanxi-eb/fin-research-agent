"""答案合成 / 引用校验 / 拒答 —— Step 3 的核心约束用例。

这批用例要守的是**"答案可核验"这件事本身**，而不是某段文案。所以断言都冲着
"错出去会很难发现"的地方去：

1. 引用编号只认**交给过模型的那几段**（不认 `len(hits)`，否则等于我们替模型伪造出处）；
2. 证据覆盖度必须**在单段内**算（拼起所有召回去数词，互不相关的命中会凑出覆盖率）；
3. 拒答有两条闸门，且**拒答时不许凭空给引用**；
4. 无 Key / 模型不可用时必须**降级成有出处的摘录**，不是报错、更不是编一段话。

全部不联网：模型调用用 `_call_llm` 的替身，检索器不参与（`synthesize` 直接吃 `res`）。
"""
from __future__ import annotations

import json

import pytest

from src import answer, citation, config, llm
from src.retrieve import pipeline


# ==================== 夹具：hit 与 res 的构造 ====================

DEFAULT_TEXT = "中国平安2024年年报披露，归属于母公司股东权益为 9,286.00 亿元，净资产合计 13,047.12 亿元。"


def _hit(index: int, *, text: str = DEFAULT_TEXT, code: str = "601318",
         company: str = "中国平安", year: int = 2024, page_no: int = 18,
         section: str = "公司简介和主要财务指标") -> dict:
    """构造一条**检索层统一形状**的 hit（与 `pipeline.normalize_hit` 同形）。"""
    raw = {"company": company, "year": year, "report_type": "annual",
           "page_no": page_no, "section": section}
    return {
        "chunk_id": f"{code}-{year}-p{page_no}-{index}",
        "score": 10.0 - index, "signals": {"bm25": 10.0 - index, "phrase_hits": 0},
        "code": code, "company": company, "year": year, "page_no": page_no,
        "section": section, "part": index, "parts_total": 1,
        "citation": citation.format_citation(raw), "text": text, "chars": len(text),
    }


def _res(hits: list[dict], *, ok: bool = True, **over) -> dict:
    out = {"ok": ok, "error": None, "mode": "bm25", "question": "?",
           "filters": {}, "hits": hits,
           "stats": {"returned": len(hits),
                     "max_score": hits[0]["score"] if hits else None},
           "note": None}
    out.update(over)
    return out


@pytest.fixture
def fake_llm(monkeypatch):
    """把"模型通道就绪"和"模型输出"都换成可控替身。返回一个设置输出的函数。"""
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))

    def set_output(payload: dict | str) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        monkeypatch.setattr(answer, "_call_llm", lambda q, hits: text)

    return set_output


# ==================== 证据覆盖度（确定性拒答闸门）====================

def test_coverage_must_be_measured_within_one_passage():
    """回归用例：**互不相关的命中不能凑出覆盖率**。

    实测问「公司食堂菜谱有什么推荐」时，「食堂」（平安年报里的员工食堂）与
    「推荐」（董事会推荐某议案）分别落在不同公司、不同页的两个片段里，
    拼起来能算出 0.67 直接放行。而模型看到的是一段段原文 —— 证据必须来自同一段。
    """
    q = "公司食堂菜谱有什么推荐"
    hits = [
        _hit(1, text="公司设有员工食堂，为员工提供工作餐。", page_no=88),
        _hit(2, text="董事会推荐张先生为公司董事候选人。", page_no=30),
    ]
    ev = answer.evidence_coverage(q, hits)
    assert ev["ratio"] < config.ANSWER_MIN_COVERAGE, "单段覆盖度必须低于门槛"
    assert ev["ratio_union"] >= config.ANSWER_MIN_COVERAGE, \
        "拼全部召回会偏高 —— 这个字段就是用来暴露「靠拼凑达标」的"
    # 两段各命中 1 个词 → 并列，取**排在前面的那条**（并列时保守取高排名片段）
    assert len(ev["covered"]) == 1
    assert ev["best_hit"] == hits[0]["citation"]


def test_coverage_ignores_stopwords_and_short_tokens():
    """「公司」「什么」「多少」这类每页都有的词不该参与判定（否则覆盖率恒为 1）。"""
    ev = answer.evidence_coverage("公司的情况怎么样", [_hit(1)])
    assert ev["note"], "实词被停用词滤光时应给出说明"
    assert ev["ratio"] == 1.0, "无从判断时给中性值，而不是一律拒答"
    assert ev["total"] == 0


def test_coverage_dedupes_repeated_terms():
    """同一个词问两遍不该让分母变大（'茅台茅台' 与 '茅台' 一样）。"""
    a = answer.evidence_coverage("茅台的毛利率", [_hit(1, text="茅台毛利率为 92%。")])
    b = answer.evidence_coverage("茅台茅台茅台的毛利率毛利率", [_hit(1, text="茅台毛利率为 92%。")])
    assert a["total"] == b["total"]


def test_coverage_uses_exact_tokenize_not_search_mode():
    """必须用 `tokenize_exact`：search 模式会把「毛利率」切出「利率」，
    于是"存款利率下降"这类无关文本会白送命中，覆盖度系统性虚高。"""
    from src.retrieve.bm25 import tokenize, tokenize_exact
    assert "利率" in tokenize("毛利率")       # 检索用：子词扩展（有利召回）
    assert "利率" not in tokenize_exact("毛利率")  # 判定用：不许扩展


# ==================== 引用编号校验 ====================

def test_validate_citations_drops_fabricated_indices():
    hits = [_hit(i) for i in range(1, 4)]
    valid, invalid = answer.validate_citations([1, 2, 99, -3, 2], hits, limit=5)
    assert valid == [1, 2], "去重 + 排序"
    assert 99 in invalid and -3 in invalid


def test_validate_citations_accepts_numeric_strings_but_not_garbage():
    hits = [_hit(i) for i in range(1, 4)]
    valid, invalid = answer.validate_citations([1, "2", "x", None, 2.0], hits, limit=5)
    assert valid == [1, 2]
    assert len(invalid) == 3


def test_validate_citations_respects_limit():
    hits = [_hit(i) for i in range(1, 8)]
    valid, _ = answer.validate_citations([1, 2, 3, 4, 5, 6, 7], hits,
                                         limit=config.ANSWER_MAX_CITATIONS)
    assert valid == [1, 2, 3, 4, 5]


def test_citable_range_must_come_from_what_was_actually_sent():
    """**引用校验的上界必须是"送进上下文几段"，不是"召回几段"。**

    模型没见过第 N 段却写 `[N]`，若拿 `len(hits)` 当上界就会判为合法 ——
    那不是模型编造出处，是**我们替模型伪造了出处**，性质更坏。
    """
    # 每段很长 → 上下文预算只装得下前面几段
    hits = [_hit(i, text="经营情况讨论与分析。" * 400) for i in range(1, 13)]
    sent = pipeline.rendered_count(hits, 3000)
    assert 0 < sent < len(hits), f"上下文应当被预算截断（sent={sent}）"

    beyond = sent + 1
    assert answer.validate_citations([beyond], hits[:sent], 5)[0] == [], \
        "超出已送出范围的编号必须判为非法"
    # 对照：用全部 hits 当上界时它会被误判为合法 —— 这就是上界写错时的漏洞
    assert answer.validate_citations([beyond], hits, 5)[0] == [beyond]


def test_context_indices_align_between_render_and_count():
    """渲染上下文与"送出段数"必须同源，否则编号会错位。"""
    hits = [_hit(i, text="营业收入与营业成本。" * 200) for i in range(1, 10)]
    ctx = pipeline.render_context(hits, 2500)
    sent = pipeline.rendered_count(hits, 2500)
    for i in range(1, sent + 1):
        assert f"[{i}] " in ctx
    assert f"[{sent + 1}] " not in ctx


def test_citations_from_answer_text_follows_appearance_order():
    assert answer._citations_from_answer_text("毛利率为 92%[3]，净利率 20%[1]，另见[3]。") == [3, 1]


# ==================== 拒答闸门 ====================

def test_empty_question_is_refused_without_retrieval(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("空问题不该触发检索")

    monkeypatch.setattr(answer, "retrieve", boom)
    out = answer.answer_question("   ")
    assert out["refused"] and out["refusal_reason"] == "empty_question"
    assert out["citations"] == []


def test_low_coverage_refuses_without_calling_model(fake_llm, monkeypatch):
    """确定性闸门必须**在调模型之前**拦住 —— 拦不住就等于白花 token 去买一个编造。"""
    called = []
    monkeypatch.setattr(answer, "_call_llm", lambda q, h: called.append(1))
    hits = [_hit(1, text="公司设有员工食堂，为员工提供工作餐。")]
    out = answer.synthesize("公司食堂菜谱有什么推荐", _res(hits))
    assert out["refused"] and out["refusal_reason"] == "low_coverage"
    assert called == [], "低覆盖度时不该调用大模型"
    assert out["citations"] == [], "拒答不许给出引用"
    assert out["confidence"] == 0.0


def test_no_hits_is_a_conclusion_not_a_failure():
    """一条都没召回是**结论**（没证据），不是系统故障 —— 两者对用户是不同的话。"""
    out = answer.synthesize("贵州茅台2024年的毛利率是多少", _res([]))
    assert out["ok"] is True
    assert out["refused"] and out["refusal_reason"] == "no_evidence"
    assert "未在已入库的年报中找到" in out["answer"]


def test_retrieval_backend_down_is_reported_as_system_state():
    out = answer.synthesize("x", _res([], ok=False, error="index_missing",
                                      message="索引不存在：data/index/bm25.pkl"))
    assert out["refused"] and out["refusal_reason"] == "index_missing"
    assert "检索不可用" in out["answer"]
    assert "不是'没有数据'" in out["notes"][0]


# ==================== 降级路径 ====================

def test_no_api_key_degrades_to_excerpt_with_citations(monkeypatch):
    """没配 Key 时必须给**有出处的摘录**并标明没调模型 —— 不能报错，也不能编一段话。"""
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "未配置 API Key"))
    hits = [_hit(1), _hit(2, page_no=19)]
    out = answer.synthesize("中国平安的归母净资产是多少", _res(hits))
    assert out["refused"] is False
    assert out["degraded"] is True
    assert "未调用大模型" in out["notes"][0]
    assert [c["index"] for c in out["citations"]] == [1, 2]
    assert all(c["page_no"] for c in out["citations"])
    assert "原文摘录" in out["answer"]


def test_model_exception_degrades_instead_of_breaking_the_chain(fake_llm, monkeypatch):
    def boom(q, h):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(answer, "_call_llm", boom)
    out = answer.synthesize("中国平安的归母净资产是多少", _res([_hit(1)]))
    assert out["degraded"] is True and out["refused"] is False
    assert "调用大模型失败" in out["notes"][0]
    assert out["citations"], "降级路径也要给出处"


def test_non_json_model_output_degrades(fake_llm):
    fake_llm("抱歉，我无法回答这个问题。")     # 没按约定出 JSON
    out = answer.synthesize("中国平安的归母净资产是多少", _res([_hit(1)]))
    assert out["degraded"] is True
    assert "不是约定 JSON" in out["notes"][0]


# ==================== 模型路径（含引用校验）====================

def test_valid_answer_keeps_only_verifiable_citations(fake_llm):
    hits = [_hit(1), _hit(2, page_no=19), _hit(3, page_no=20)]
    fake_llm({"answer": "归母净资产为 9,286.00 亿元[2]。", "used_citations": [2], "insufficient": False})
    out = answer.synthesize("中国平安的归母净资产是多少", _res(hits))
    assert out["refused"] is False and out["degraded"] is False
    assert [c["index"] for c in out["citations"]] == [2]
    assert out["citations"][0]["citation"] == hits[1]["citation"]
    assert out["citations"][0]["snippet"], "引用要带原文片段，前端才能展开核对"
    assert out["confidence"] > 0


def test_fabricated_indices_are_dropped_and_reported(fake_llm):
    """模型编编号 → 丢弃并**记进 notes**，而不是静默吞掉或报错重试。"""
    hits = [_hit(1), _hit(2)]
    fake_llm({"answer": "归母净资产为 9,286.00 亿元[1][77]。", "used_citations": [1, 77],
              "insufficient": False})
    out = answer.synthesize("中国平安的归母净资产是多少", _res(hits))
    assert [c["index"] for c in out["citations"]] == [1]
    assert "不存在的引用编号" in " ".join(out["notes"]) and "77" in " ".join(out["notes"])


def test_answer_without_any_valid_citation_degrades(fake_llm):
    """有答案但出处不可核验 → 降级展示原文摘录（不展示无法校验的内容）。"""
    fake_llm({"answer": "归母净资产为 9,286.00 亿元[9]。", "used_citations": [9],
              "insufficient": False})
    out = answer.synthesize("中国平安的归母净资产是多少", _res([_hit(1)]))
    assert out["degraded"] is True
    assert "无法核验出处" in out["notes"][0]


def test_model_insufficient_refusal_still_keeps_its_citations(fake_llm):
    """模型说"资料不足"时，正文常带着 `[2]` 这类角标。

    若这时返回空引用列表，前端就会出现"点了没反应的死链"。
    保留它真正看过的片段还有一个用处：用户能核对**为什么答不了**。
    """
    hits = [_hit(1), _hit(2, page_no=19)]
    fake_llm({"answer": "资料里只有内含价值相关内容[2]，没有归母净资产。",
              "used_citations": [2], "insufficient": True})
    out = answer.synthesize("中国平安的归母净资产是多少", _res(hits))
    assert out["refused"] is True and out["refusal_reason"] == "model_insufficient"
    assert [c["index"] for c in out["citations"]] == [2]
    assert out["confidence"] == 0.0


def test_refusal_never_leaves_dangling_markers(fake_llm):
    """拒答路径也要走编号校验：正文里的角标必须在引用列表里有落点。"""
    from src.graph.nodes import cite_node

    hits = [_hit(1), _hit(2, page_no=19)]
    fake_llm({"answer": "只有[2]可用。", "used_citations": [2], "insufficient": True})
    out = answer.synthesize("中国平安的归母净资产是多少", _res(hits))
    chk = cite_node({"answer": out["answer"], "citations": out["citations"], "notes": []})
    assert chk["cite_check"]["dangling"] == []


def test_disclaimer_travels_with_every_answer(fake_llm):
    """免责声明随答案返回，不写在 UI 里 —— 换前端就丢的东西不算约束。"""
    fake_llm({"answer": "见[1]。", "used_citations": [1], "insufficient": False})
    cases = [
        answer.synthesize("中国平安的归母净资产是多少", _res([_hit(1)])),          # 正常
        answer.synthesize("公司食堂菜谱", _res([_hit(1, text="食堂。")])),          # 拒答
        answer.answer_question("  "),                                            # 空问题
    ]
    for c in cases:
        assert c["disclaimer"] == config.ANSWER_DISCLAIMER


# ==================== cite 节点：一致性检查 ====================

def test_cite_node_reports_dangling_and_unused_but_keeps_all_citations():
    """不过滤 citations：那几条同样是真证据，删掉等于**替用户隐藏来源**。"""
    from src.graph.nodes import cite_node

    out = cite_node({"answer": "结论见 [3]。",
                     "citations": [{"index": 1}, {"index": 2}], "notes": []})
    assert out["cite_check"] == {"dangling": [3], "unused": [1, 2], "cited": [3], "listed": [1, 2]}
    assert len(out["citations"]) == 2
    assert any("角标点击无落点" in n for n in out["notes"])


# ==================== 置信度（启发式，不是概率）====================

def test_confidence_is_zero_when_refused_and_bounded_otherwise():
    assert answer._confidence(True, {"ratio": 1.0}, [1, 2, 3]) == 0.0
    assert answer._confidence(False, {"ratio": 1.0}, [{}, {}, {}]) == 1.0
    assert answer._confidence(False, {"ratio": 0.0}, []) == 0.0
    mid = answer._confidence(False, {"ratio": 0.5}, [{}])
    assert 0.0 < mid < 1.0


# ==================== 图与直接调用必须同形 ====================

def test_graph_and_direct_call_agree_on_shared_fields(monkeypatch):
    """图里走的路和直接调用走的路必须**同形、同值**。

    ⚠️ 不是"键完全一样"：图这条多出状态机固有槽位（`intent / error / message`）、
    cite 节点的一致性检查（`cite_check`）、Step 5 新增的编排字段
    （`route / thread_id / tool_calls / verify / hitl`），以及本轮新增的联网兜底
    （`web` 与独立口径的 `web_citations` —— 兜底节点挂在**主图** `verify` 之后，
    `answer_question` 是图之前的便捷入口，**没有**这一层）。
    所以契约是：直接调用的键是图的**子集**，且共有字段取值一致。
    """
    from src.graph import builder

    hits = [_hit(1)]
    fake = lambda *a, **k: _res(hits)          # noqa: E731
    # 两处都要打：节点用 pipeline.retrieve，answer_question 用的是导入时绑定的名字
    monkeypatch.setattr(pipeline, "retrieve", fake)
    monkeypatch.setattr(answer, "retrieve", fake)
    monkeypatch.setattr(llm, "is_ready", lambda: (False, "无 Key"))

    graph_out = builder.run_qa("中国平安的归母净资产是多少", use_llm=True)
    direct = answer.answer_question("中国平安的归母净资产是多少", use_llm=True)

    assert set(direct) <= set(graph_out), \
        f"直接调用多出了图不认识的字段：{set(direct) - set(graph_out)}"
    assert set(graph_out) - set(direct) == {
        "intent", "error", "message", "cite_check",
        "route", "thread_id", "tool_calls", "verify", "hitl",
        "web", "web_citations"}, \
        "图多出的字段集合变了 —— 若不是有意新增，说明有字段被悄悄改名/删掉了"
    for k in ("answer", "refused", "refusal_reason", "degraded", "confidence",
              "citations", "evidence", "notes", "disclaimer"):
        assert graph_out[k] == direct[k], f"字段 {k} 两边不一致"
    assert direct["citations"][0]["index"] == 1
    assert direct["degraded"] is True, "无 Key 时必须降级而不是报错"
    # `run_qa` 是"强制 RAG"入口：即使不过路由，也要把 route 标成被强制，
    # 免得事后把它误读成"路由器判的"
    assert graph_out["route"]["rule"] == "forced_rag"


# ==================== 闸门 0：语料外实词（Step 4 收尾新增）====================

def test_content_terms_is_shared_and_stable():
    """实词抽取是**两处闸门共用的**，口径必须一致且可预期。

    它同时被「覆盖度」和「语料外实词」使用。若两处各写一遍，
    会出现"覆盖度认为「哪家」是实词、语料检查认为它是停用词"这种
    自相矛盾的结论，而两边各自的用例都会通过。
    """
    from src.retrieve.bm25 import content_terms

    # 疑问词/泛指词不进实词表（`哪家` 曾经漏在表外，稀释了一批正常问题的覆盖度）
    for bad in ("哪家", "哪些", "多少", "什么", "如何", "公司", "年报", "情况"):
        assert bad not in content_terms(f"中国平安的{bad}是多少"), f"{bad} 应被当作停用词"
    # 单字不进（"的/是/年"命中没有证据意义）
    assert "的" not in content_terms("茅台2024年的净利润")
    # 去重保序：同一个词问两遍不该让分母变大
    got = content_terms("茅台茅台2024年净利润净利润")
    assert got == list(dict.fromkeys(got))
    assert "茅台" in got and "净利润" in got


def test_out_of_corpus_gate_refuses_before_model(fake_llm, monkeypatch):
    """问题里的实词**全库零出现** → 直接拒答，且不调模型。

    与「覆盖度」的区别是本质的：覆盖度说"证据不够"，这里说"范围之外"。
    实测漏网的那道题（「公司食堂的菜谱是什么」）就是靠这条兜住的 ——
    「食堂」在年报里真有（员工食堂），覆盖度因此达标；但「菜谱」全库零出现。
    """
    hit = _hit(1, text="公司设有员工食堂，为员工提供工作餐。")
    called = {"n": 0}
    monkeypatch.setattr(llm, "is_ready", lambda: (True, "ok"))
    monkeypatch.setattr(answer, "_call_llm",
                        lambda q, hits: called.__setitem__("n", called["n"] + 1) or "{}")

    out = answer.synthesize("公司食堂的菜谱是什么",
                            _res([hit], absent_terms=["菜谱"]), use_llm=True)

    assert out["refused"] is True
    assert out["refusal_reason"] == "out_of_corpus"
    assert called["n"] == 0, "确定性闸门必须在调模型之前拦住（一个 token 都不该花）"
    assert "菜谱" in out["answer"], "拒答文案要点名是哪个词不在语料里"
    # 拒答文案必须**可操作**：告诉用户换成报表科目名通常就能查到
    assert "科目" in out["answer"]
    assert out["citations"] == [], "确定性拒答不给引用（给了出处就不算拒答）"


def test_out_of_corpus_gate_can_be_disabled(fake_llm, monkeypatch):
    """开关可关：口语同义词（「赚钱」全库不存在）是这条规则的已知残余风险。

    所以它必须是**一个能关的开关**，而不是写死的断言 ——
    否则一个口语提问会把整条链路挡在门外，且用户无法自救。
    """
    monkeypatch.setattr(config, "ANSWER_OOC_GATE", False)
    # 注意：例句里要带上「茅台」「2024」—— 否则覆盖度闸门会因为"这段根本没提这家公司"
    # 先把它拒掉，测出来的就不是"闸门 0 被关掉"这件事了（用例之间会互相污染结论）
    hits = [_hit(1, text="贵州茅台2024年净利润同比增长，盈利能力保持稳定。")]
    fake_llm({"answer": "净利润同比增长[1]。", "used_citations": [1], "insufficient": False})

    out = answer.synthesize("茅台2024年赚钱了吗", _res(hits, absent_terms=["赚钱"]),
                            use_llm=True)
    assert out["refused"] is False, "关掉闸门后不应因语料外实词拒答"
    assert out["refusal_reason"] != "out_of_corpus"


def test_absent_terms_absent_means_no_gate(fake_llm):
    """没有语料外实词时，闸门 0 必须**完全不介入**（否则会误伤正常问题）。"""
    hits = [_hit(1)]
    fake_llm({"answer": "归母净资产 9,286.00 亿元[1]。", "used_citations": [1],
              "insufficient": False})
    out = answer.synthesize("中国平安2024年末的归母净资产是多少", _res(hits), use_llm=True)
    assert out["refused"] is False
    assert out["citations"][0]["index"] == 1
