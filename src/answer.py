"""答案合成 + 引用 + 拒答 —— Step 3 的「最后一百米」。

本模块只做四件事，但每一件都对应一个**金融问答特有的失分点**：

1. **答案只能引用我们给它的片段**。做法是把检索片段编号成 `[1] [2] …` 后交给模型，
   并要求它返回**用到的编号列表**；返回后逐条校验编号是否真实存在，不存在的丢弃。
   —— LLM 会编页码，但编不出我们没给它的编号。这是"可核验引用"的机制保证，
   不是靠提示词求它别撒谎。
2. **拒答要走两条独立的闸门**（确定性 + 模型自评）：
   - 确定性闸门：问题里的**实词有多少真的出现在同一段**召回文本里（单段证据覆盖度）。
     问「公司食堂菜谱有什么推荐」时，最好的一段也只覆盖 1/3 的实词 → 判为无证据，
     **根本不调模型**（详见 `evidence_coverage`：必须单段统计，
     拼起所有召回去数词会让互不相关的命中凑出 0.67 直接放行）。
   - 模型闸门：要求模型输出 `insufficient` 标志；它说"资料不足以回答"时按拒答处理。
   只靠其中一条都不够：前者挡不住"词都在但答非所问"，后者挡不住"模型硬答"。
3. **降级路径不是可选项**。没有 API Key / 网络失败时必须仍能给出**有出处的摘录**，
   并明确标注"未调用大模型"。绝不能因为调不到模型就把链路断掉，
   更不能悄悄返回一段没有出处的话。
4. **免责声明与口径提示随答案一起返回**，不写在 UI 里 —— 换前端就丢的东西不算约束。

只做**原文转述**，不做数值推算：要算比率请走 `src/tools/`（那里的值自带口径来源）。
数值题若从原文里读，读到的往往是"上年同期"或"母公司口径"，是静默错误的高发区。

两个入口（**共用同一份实现**，不存在"图里走的路和直接调用走的路不一样"）：
- `answer_question(question, ...)`：检索 + 合成一步到位（冒烟脚本、单测、批处理用）；
- `synthesize(question, res)`：只做合成，`res` 由 `retrieve/pipeline.py` 给（LangGraph 节点用）。
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

from src import citation, config, llm
from src.retrieve.bm25 import content_terms, normalize_for_match
from src.retrieve.pipeline import render_context, rendered_count, retrieve

# 模型要求**结构化输出**，而不是自由文本。理由：要校验引用编号、要读 insufficient 标志，
# 从自然语言里抠这些信息等于用正则解析散文，脆弱且不可测。
_SYSTEM_PROMPT = """你是金融年报分析助手。你只能依据【资料】回答，必须遵守：

1. 只使用【资料】中的内容。**禁止**引入资料之外的知识、常识或推算。
2. 每个结论后面必须标注来源编号，格式 `[1]`、`[2]`。**只能使用【资料】里出现过的编号**，
   不要自己编造页码或编号。
3. 【资料】不足以回答问题（包括：资料讲的是另一个话题、只有不相关的片段）时，
   把 `insufficient` 设为 true，`answer` 里说明缺什么，不要强行作答。
4. 不做任何数值推算（不要自己算比率、不要做同比增减）。资料里没直接写出的数字就不要给。
5. 资料是 PDF 抽取的文本，可能有换行错位、表格串行；引用时不要把明显错乱的内容当作结论依据。

只输出 JSON，不要输出任何其他文字。格式：
{"answer": "带 [编号] 的中文回答", "used_citations": [1, 2], "insufficient": false}
"""


# ==================== 证据覆盖度（确定性拒答信号）====================

def evidence_coverage(question: str, hits: list[dict]) -> dict:
    """问题里的实词，有多少真的出现在**同一段**召回文本里。

    返回 `{"ratio", "ratio_union", "total", "covered", "missing", "terms",
    "best_hit", "note"}`。

    **两个"必须"：**

    1. ⚠️ 用 `bm25.content_terms`（内部走 `tokenize_exact`，不做 search 模式子词扩展）：
       子词会把覆盖率系统性抬高，详见 `bm25.tokenize_exact` 的说明。
       与「语料外实词」闸门共用这一个函数，确保两处闸门的实词口径完全一致。
    2. **必须在单段内统计，不能把所有召回文本拼成一大篇去数词**。拼起来数会让
       **互不相关**的命中凑出覆盖率：实测问「公司食堂菜谱有什么推荐」时，
       「食堂」（平安年报里的员工食堂）与「推荐」（董事会推荐某议案）
       分别出现在**不同公司、不同页**的两个片段里，拼起来算出 0.67 直接放行。
       而"证据来自同一段"本来就是生成侧的实际情况（模型读的是一段段原文），
       也是拒答该有的严格度。

    改判后的实测分布（topk=8）：
    - 正常问题最好一段的覆盖率 **0.67 ~ 1.00**（毛利率 0.75、归母净资产 0.75、
      研发投入 0.80、营业收入 1.00、资产负债率 1.00、商誉减值 0.67）；
    - 越界问题 **0.25 ~ 0.33**（食堂菜谱 0.33、公司章程在哪 0.25）。
    门槛 0.5 在两侧都留得开，所以取单段覆盖率作为**闸门值**，
    同时把 `ratio_union` 一并报出来供排查（同一问题时它偏高，正好用来发现"靠拼凑达标"）。
    """
    terms = content_terms(question)

    if not terms:
        # 实词全被停用词过滤掉了（如"公司怎么样"）→ 没法判断，给中性值而不是 0
        # （给 0 会把这类问题一律拒答，但它们其实可能是正常提问）
        return {"ratio": 1.0, "ratio_union": 1.0, "total": 0, "covered": [],
                "missing": [], "terms": [], "best_hit": None,
                "note": "问题中没有可用于判断的实词，跳过了覆盖度闸门"}

    best_cov: list[str] = []
    best_hit: dict | None = None
    for h in hits or []:
        hay = normalize_for_match(h.get("text") or "")
        cov = [t for t in terms if normalize_for_match(t) in hay]
        if len(cov) > len(best_cov):
            best_cov, best_hit = cov, h

    union_hay = normalize_for_match(" ".join(h.get("text") or "" for h in hits or []))
    union_n = sum(1 for t in terms if normalize_for_match(t) in union_hay)

    return {
        "ratio": round(len(best_cov) / len(terms), 4),
        "ratio_union": round(union_n / len(terms), 4),
        "total": len(terms),
        "covered": best_cov,
        "missing": [t for t in terms if t not in best_cov],
        "terms": terms,
        "best_hit": (best_hit or {}).get("citation"),
        "note": None,
    }


# ==================== 引用编号校验 ====================

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _parse_model_json(text: str) -> dict | None:
    """从模型输出里取出 JSON 对象。

    允许它裹在 ```json 里（哪怕提示词要求不要）—— 实测很难完全避免，
    所以这里做一次宽松提取，而不是直接判失败。
    """
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def validate_citations(used: object, hits: list[dict], limit: int) -> tuple[list[int], list[int]]:
    """校验模型给的引用编号，返回 `(有效编号, 被丢弃的编号)`。

    **丢弃非法编号，而不是报错重试**：伪编号说明模型在编，报错重试很可能再编一次；
    直接丢掉它反而退回"可信的部分"，同时记进 `invalid_citations` 让上层能看见。
    这个字段也是 Step 5 引用校验（`verify.py`）的输入。
    """
    if not isinstance(used, (list, tuple)):
        return [], []
    valid: list[int] = []
    invalid: list[int] = []
    for x in used:
        if not isinstance(x, int):
            # 模型偶尔给 "1" 这种字符串，接受；其它类型一律算非法
            if isinstance(x, str) and x.strip().isdigit():
                x = int(x.strip())
            else:
                invalid.append(-1)
                continue
        if 1 <= x <= len(hits):
            if x not in valid:
                valid.append(x)
        else:
            invalid.append(x)
    return sorted(valid)[:limit], invalid


def collect_citation_list(valid: list[int], hits: list[dict], limit: int) -> list[dict]:
    """把有效编号转成引用对象（序号 + 引用串 + 定位字段 + 原文片段）。

    带 `text` 片段是有意的：前端要能**直接展开核对**，
    而不是只给一个页码让用户自己去翻 PDF。
    """
    out = []
    for i in valid[:limit]:
        h = hits[i - 1]
        out.append({
            "index": i,
            "citation": h["citation"],
            "code": h["code"], "company": h["company"], "year": h["year"],
            "page_no": h["page_no"], "section": h["section"],
            "chunk_id": h["chunk_id"],
            "snippet": (h.get("text") or "")[:180],
        })
    return out


def _citations_from_answer_text(text: str) -> list[int]:
    """答案正文里出现过的 `[n]` 编号（按出现顺序去重）。

    用途：模型可能"在正文里引用了但忘了填 used_citations"。这种情况下
    正文的编号是更可信的信号 —— 它已经在向用户展示 `[3]` 了，
    若我们这边 citations 为空，前端就会出现"正文有 [3] 但引用列表是空的"。
    """
    out: list[int] = []
    for m in re.finditer(r"\[(\d{1,2})\]", text or ""):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out


# ==================== 主入口 ====================

def answer_question(question: str, *, code: str | None = None, year: int | None = None,
                    topk: int | None = None, mode: str | None = None,
                    use_llm: bool = True) -> dict:
    """问答主入口（检索 + 合成一步到位）。

    图节点用的是拆开的两半（`pipeline.retrieve` + `synthesize`），
    但"直接调一次就有答案"这个入口也有价值 —— 单测、脚本化批处理都靠它。
    两半共用同一份实现，不存在"图里走的路和直接调用走的路不一样"。

    ⚠️ 返回形状是 `_pack`，比图那条**少四个字段**：`intent / error / message / cite_check`。
    前三个是状态机固有槽位，`cite_check` 来自 cite 节点的一致性检查（本入口没有这一层）。
    要拿到带一致性检查的完整响应就用 `graph.builder.run_qa`；
    两者共有的字段（answer / citations / refused / confidence…）取值必然一致 ——
    有单测守着这条契约，不是口头承诺。
    """
    q = (question or "").strip()
    if not q:
        return _pack(q, refused=True, refusal_reason="empty_question",
                     answer="请提出具体问题。", notes=["问题为空。"])

    res = retrieve(q, topk=topk or config.ANSWER_CANDIDATE_TOPK, code=code, year=year,
                   mode=mode)
    return synthesize(q, res, use_llm=use_llm)


def synthesize(question: str, res: dict, *, use_llm: bool = True,
               raw_model_output: str | None = None) -> dict:
    """把检索结果变成答案（闸门 → 合成 → 引用）。`res` 为 `pipeline.retrieve` 的返回。

    拆成独立函数是为了让 LangGraph 节点能"检索"与"生成"分两步，
    同时**不复制任何逻辑** —— 图里和直接调用走的是同一份代码。

    `raw_model_output` 给**流式路径**用：流式已经把模型原文逐段发出去了，
    终态若再调一次模型就既浪费 token 又可能与已发出的 token 不一致。
    传入它就跳过 `_call_llm`，但**下游的解析/校验/降级一字不改** ——
    这是"流式与一次性输出必须同形"的保证点（见 `src/streaming.py` 的说明）。
    默认 None = 维持原行为（自己调模型）。
    """
    q = (question or "").strip()
    if not res.get("ok"):
        return _pack(q, refused=True, refusal_reason=res.get("error"),
                     answer=f"检索不可用：{res.get('message')}",
                     retrieval=_retrieval_digest(res),
                     notes=["检索后端未就绪，无法作答（这是系统状态，不是'没有数据'）。"])

    hits = res["hits"]
    evidence = evidence_coverage(q, hits)

    # 闸门 0：一条都没召回
    if not hits:
        return _pack(q, refused=True, refusal_reason="no_evidence",
                     answer=("未在已入库的年报中找到与问题相关的内容，因此不作回答。"
                             "可尝试换个说法，或确认所问公司/年份已在库内。"),
                     evidence=evidence, retrieval=_retrieval_digest(res),
                     notes=[res.get("note") or ""])

    # 闸门 0：问题里有**全部入库年报都没出现过**的实词 → 超出语料范围。
    #
    # 为什么这条比覆盖度更早判：覆盖度是"词出现过但可能答非所问"的场景，
    # 而这条是"词从来没出现过"。后者在金融语料上有很强的判别力 ——
    # 年报是高度格式化的文本，凡是公司真有的科目/事项，术语几乎必然出现；
    # 一个 2 字以上的实词全库零出现，通常意味着问的是**语料外的东西**
    # （「菜谱」「调查结果」）或**口语化的说法**。
    #
    # 实测（34 题金标准）：加上疑问词停用词修正后，这条规则只命中 2 题，
    # 且**两题都是标注为应当拒答的题**（ref-food / ref-nonsense）→ 误伤 0。
    # 已知残余风险：口语同义词（如「赚钱」全库不存在，而「盈利」存在）会被拒；
    # 所以拒答文案里**必须给出可操作的替换建议**，并且允许用 `ANSWER_OOC_GATE=0` 关掉。
    absent = [t for t in (res.get("absent_terms") or []) if t]
    if config.ANSWER_OOC_GATE and absent:
        return _pack(q, refused=True, refusal_reason="out_of_corpus",
                     answer=(f"问题里的「{ '、'.join(absent) }」在已入库的年报中**从未出现**，"
                             f"因此无法给出有出处的回答。\n\n"
                             f"这可能是因为：① 所问的事项确实不在年报披露范围内；"
                             f"② 用了口语化说法 —— 换成报表里的科目名通常就能查到"
                             f"（例如「赚钱」→「净利润」）。"),
                     evidence=evidence, retrieval=_retrieval_digest(res),
                     notes=[f"语料外实词：{ '、'.join(absent) }"
                            f"（在**全部已入库 chunk** 中零出现，不是「这一段没命中」）"])

    # 闸门 1：证据覆盖度（确定性，不调模型）
    if evidence["ratio"] < config.ANSWER_MIN_COVERAGE:
        msg = (f"问题中的关键词（{ '、'.join(evidence['missing']) }）"
               f"在检索到的年报原文中没有出现，判定为**超出资料范围**，不予回答。")
        return _pack(q, refused=True, refusal_reason="low_coverage",
                     answer=("该问题超出已入库年报的内容范围，我不做推测性回答。\n\n" + msg),
                     evidence=evidence, retrieval=_retrieval_digest(res),
                     notes=[f"单段证据覆盖度 {evidence['ratio']} < 门槛 "
                            f"{config.ANSWER_MIN_COVERAGE}"
                            f"（最相关的一段 {evidence.get('best_hit')} 只命中 "
                            f"{evidence['covered']}；拼全部召回可得 "
                            f"{evidence.get('ratio_union')}，但那不算证据）。"])

    # 闸门 2：模型自评（可能不可用 → 降级为摘录）
    # 传了 `raw_model_output` 就说明模型原文已经拿到（流式路径），不再重复就绪判断/调用。
    ready, why = llm.is_ready()
    if raw_model_output is None and not (use_llm and ready):
        return _fallback_excerpt(
            q, hits, evidence, res,
            reason="未调用大模型：" + (why if not ready else "use_llm=False"))

    if raw_model_output is not None:
        raw = raw_model_output
    else:
        try:
            raw = _call_llm(q, hits)
        except Exception as e:                                   # noqa: BLE001
            # 模型侧任何问题都不该让链路断掉 —— 退回"有出处的摘录"
            return _fallback_excerpt(q, hits, evidence, res,
                                     reason=f"调用大模型失败（{type(e).__name__}: {e}）")

    payload = _parse_model_json(raw)
    if payload is None:
        return _fallback_excerpt(
            q, hits, evidence, res,
            reason="模型返回的不是约定 JSON，已降级为原文摘录（避免展示无法校验的内容）")

    # 能引用到第几号 —— 由"实际送进上下文几段"决定（见 `pipeline.rendered_count`）。
    # 模型没见过 [8] 却写 [8]，必须判为非法引用，否则等于我们替它伪造出处。
    allowed = rendered_count(hits, config.ANSWER_CONTEXT_CHARS)
    citable = hits[:max(allowed, 1)]

    text = str(payload.get("answer") or "").strip()
    insufficient = bool(payload.get("insufficient"))

    if insufficient or not text:
        # **拒答照样要校验并保留引用**：模型常写成"资料里只有 X[2]、但没有 Y"，
        # 这时正文带着角标却没有 citations，前端就会出现"点了没反应的死链"。
        # 保留它真正引用到的片段还多一个用处：用户能核对"为什么答不了"。
        valid, invalid = validate_citations(
            payload.get("used_citations"), citable, config.ANSWER_MAX_CITATIONS)
        if not valid:
            valid, extra_invalid = validate_citations(
                _citations_from_answer_text(text), citable, config.ANSWER_MAX_CITATIONS)
            invalid = invalid + extra_invalid
        notes = ["模型判定资料不足，按拒答处理（这是结论，不是故障）。"
                 "下列引用是它实际看过的片段，可用来核对「为什么答不了」。"]
        if invalid:
            notes.append(f"模型给出 {len(invalid)} 个超出上下文的引用编号 {invalid}，已丢弃"
                         f"（这些片段没交给它，它引用不了）。")
        return _pack(q, refused=True, refusal_reason="model_insufficient",
                     answer=text or "资料不足以回答该问题。",
                     citations=collect_citation_list(valid, citable, config.ANSWER_MAX_CITATIONS),
                     evidence=evidence, retrieval=_retrieval_digest(res),
                     llm_info=_llm_digest(), notes=notes)

    # 引用编号：优先用模型自报的，为空时回落到正文里实际出现的编号
    valid, invalid = validate_citations(
        payload.get("used_citations"), citable, config.ANSWER_MAX_CITATIONS)
    if not valid:
        valid, extra_invalid = validate_citations(
            _citations_from_answer_text(text), citable, config.ANSWER_MAX_CITATIONS)
        invalid = invalid + extra_invalid

    notes = []
    if invalid:
        notes.append(f"模型给出 {len(invalid)} 个不存在的引用编号 {invalid}，已丢弃"
                     f"（这是编造出处，不是数据问题）。")
    if not valid:
        # 有答案但没有可核验的出处 → 按"不可核验"处理，降级展示原文摘录
        return _fallback_excerpt(
            q, hits, evidence, res,
            reason="模型回答未给出有效引用编号，无法核验出处，已降级为原文摘录")

    return _pack(q, answer=text,
                 citations=collect_citation_list(valid, citable, config.ANSWER_MAX_CITATIONS),
                 evidence=evidence, retrieval=_retrieval_digest(res),
                 llm_info=_llm_digest(), notes=notes)


def _llm_digest() -> dict:
    cfg = llm.get_active_llm()
    return {"provider": cfg["provider"], "model": cfg["model"]}


def _call_llm(question: str, hits: list[dict]) -> str:
    """调用激活通道，要 JSON 输出。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    cfg = llm.get_active_llm()
    model = ChatOpenAI(
        model=cfg["model"], base_url=cfg["base_url"], api_key=cfg["api_key"],
        temperature=0.0,                       # 金融场景不要"创意"
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    context = render_context(hits, config.ANSWER_CONTEXT_CHARS)
    msg = f"【资料】\n{context}\n\n【问题】\n{question}"
    out = model.invoke([SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=msg)])
    return out.content if isinstance(out.content, str) else str(out.content)


async def _astream_llm(question: str, hits: list[dict]) -> AsyncIterator[str]:
    """流式调用激活通道，逐块 yield 模型原文（**与 `_call_llm` 同 prompt / 同模型配置**）。

    为什么构造点只有这里与 `_call_llm` 两处：模型构造一旦散落各处，
    换供应商/改 base_url/加超时就得改好几处，漏一处就是"本地对、线上不对"，
    而且这种漂移只在特定环境暴露。**构造点收敛是刻意的**。

    yield 出的是提示词约定的 **JSON 文本**（与一次性路径同一份 `_SYSTEM_PROMPT`），
    终态仍由 `synthesize(..., raw_model_output=...)` 统一收口 ——
    所以流式与一次性两条路产出的 `done.response` 必然同形，前端不用写两套渲染。
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    cfg = llm.get_active_llm()
    model = ChatOpenAI(
        model=cfg["model"], base_url=cfg["base_url"], api_key=cfg["api_key"],
        temperature=0.0,                       # 金融场景不要"创意"
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    context = render_context(hits, config.ANSWER_CONTEXT_CHARS)
    msg = f"【资料】\n{context}\n\n【问题】\n{question}"
    async for chunk in model.astream(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=msg)]):
        text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
        if text:
            yield text


# ==================== 降级路径 ====================

def _fallback_excerpt(question: str, hits: list[dict], evidence: dict,
                      res: dict, *, reason: str) -> dict:
    """不调模型（或模型不可用）时的**有出处的摘录**。

    为什么值得专门写一条路径而不是直接报错：可核验的摘录本身就是有价值的交付物 ——
    用户拿到"这几段原文 + 精确页码"可以自己判断。而报错会把这种价值也一起丢掉。
    关键是把话说清楚：**这是摘录不是答案**，且标注为什么没走模型。
    """
    top = hits[:config.ANSWER_MAX_CITATIONS]
    lines = ["【原文摘录】（未能生成综合答案，以下为检索到的相关原文，可点开核对）", ""]
    for i, h in enumerate(top, start=1):
        snippet = (h.get("text") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {h['citation']}")
        lines.append(f"    {snippet[:220]}{'…' if len(snippet) > 220 else ''}")
        lines.append("")
    return _pack(question, answer="\n".join(lines).rstrip(),
                 citations=collect_citation_list(list(range(1, len(top) + 1)), hits,
                                                 config.ANSWER_MAX_CITATIONS),
                 evidence=evidence, retrieval=_retrieval_digest(res),
                 degraded=True,
                 notes=[reason, "已降级为原文摘录：只做转述、不含任何推断，出处可核对。"])


# ==================== 打包 ====================

def _retrieval_digest(res: dict) -> dict:
    """检索过程摘要（给前端和审计用，不含全文，避免响应体过大）。"""
    return {
        "mode": res.get("mode"),
        "filters": res.get("filters"),
        "returned": len(res.get("hits") or []),
        "max_score": (res.get("stats") or {}).get("max_score"),
    }


def _pack(question: str, *, answer: str, citations: list[dict] | None = None,
          refused: bool = False, refusal_reason: str | None = None,
          evidence: dict | None = None, retrieval: dict | None = None,
          llm_info: dict | None = None, degraded: bool = False,
          notes: list[str] | None = None) -> dict:
    """统一响应形状。**所有分支都走这里** —— 分支各自拼 dict 必然漂移。"""
    cites = citations or []
    return {
        "ok": True,
        "question": question,
        "answer": answer,
        "citations": cites,
        "refused": refused,
        "refusal_reason": refusal_reason,
        "degraded": degraded,
        "confidence": _confidence(refused, evidence, cites),
        "evidence": evidence,
        "retrieval": retrieval,
        "llm": llm_info,
        "notes": [n for n in (notes or []) if n],
        "disclaimer": config.ANSWER_DISCLAIMER,
    }


def _confidence(refused: bool, evidence: dict | None, citations: list[dict]) -> float:
    """0~1 的**启发式**置信度，供 Step 5 的 HITL 阈值使用。

    ⚠️ 名字叫 confidence 但**不是概率**，是"证据充分度"的加权代理指标：
    0.7 × **单段**实词覆盖率 + 0.3 × 有效引用条数占比（3 条封顶）。
    别把它当"正确答案的概率"讲给用户听 —— 那会变成另一种编造。
    拒答一律记 0。它是**排序/分流信号**，不是统计量。
    """
    if refused:
        return 0.0
    cov = float((evidence or {}).get("ratio") or 0.0)
    cite_part = min(len(citations), 3) / 3
    return round(min(1.0, 0.7 * cov + 0.3 * cite_part), 4)


if __name__ == "__main__":
    # 自检：python -m src.answer "贵州茅台2024年的毛利率是多少"
    import json as _json
    import sys as _sys

    _q = " ".join(a for a in _sys.argv[1:] if not a.startswith("--")) or \
        "贵州茅台2024年的毛利率是多少"
    print(_json.dumps(answer_question(_q), ensure_ascii=False, indent=2))
