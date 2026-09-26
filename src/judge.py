"""答案级判定 —— 与 RAGAS 的定义对齐、但**自己实现**。

理由（这是刻意的取舍，不是"重复造轮子"）：
1. **可复现**：RAGAS 的判分 prompt / 解析 / 聚合口径会随版本漂移，而评测数字要能跨月对比；
   自己实现意味着"口径变了"必然是一次显式提交，而不是某次 `pip install -U`。
2. **零新增重依赖**：RAGAS 会拖进 datasets / langchain 的一大串传递依赖，
   与"容器镜像要小、离线要能跑"的交付目标冲突。本项目只需要两个指标，不值得。
3. **失败必须可统计**：判分是"会失败的外部调用"（超时 / 额度 / 模型不守 JSON 格式）。
   这里的每一层都把失败收敛成 `None`（= 未判），**绝不抛异常** ——
   判分失败不该把整轮评测带塌，也不该被悄悄算成 0 分（那会让人误以为质量变差）。

两个指标（同名口径见 RAGAS）：
- `faithfulness`（忠实度）：把答案拆成若干事实性断言，逐条判"是否被上下文支撑"，
  取 support 比例。它防的是**模型编造**（数字/结论在资料里找不到出处）。
- `answer_relevancy`（答案相关性）：判"答案是否直接回答了问题"，给 0~1 分。
  它防的是**答非所问**（说了很多正确的废话，但没回答被问的那件事）。

两者正交：忠实度高的答案可能完全不切题（照抄了无关段落），相关性高的答案可能全是编的。
所以两个都要有，不能用一个顶替另一个。

数值与引用的命中判定（`numeric_hit`）复用 `src/numeric.py` 的**同一份**数字抽取与容差，
不另写一套 —— 否则会出现"校验侧认为合法、判分侧认为对不上"的自相矛盾。
"""
from __future__ import annotations

import json
import re

from src import llm, numeric

# 判分模型构造**复用 `src/answer.py::_call_llm` 的同一套配置**（`llm.get_active_llm()`），
# 不新建 client、不把 base_url/api_key 写死 —— 换供应商只改一处配置。
_JUDGE_TEMPERATURE = 0.0

_SYSTEM_FAITHFULNESS = (
    "你是严格的事实核查员。你只能依据给定的【资料】判断【答案】里的每一项事实性断言"
    "是否有资料支撑。**只输出 JSON**，格式为 "
    '{"claims": [{"claim": "断言原文", "supported": true}]}；'
    "supported 是布尔值（true=资料里有支撑，false=资料里找不到）。"
    "不要输出任何解释文字，不要输出 Markdown 代码围栏。"
)
_SYSTEM_RELEVANCY = (
    "你是答案相关性评审员。请判断【答案】在多大程度上**直接回答**了【问题】。"
    "**只输出 JSON**，格式为 "
    '{"score": 0.0 到 1.0 之间的小数, "reason": "一句话理由"}。'
    "score 越接近 1 表示越直接地回答了问题。"
    "不要输出任何解释文字，不要输出 Markdown 代码围栏。"
)

# metric → 该指标判分结果里"有意义的那部分"该怎么取。
_FAITHFULNESS = "faithfulness"
_RELEVANCY = "answer_relevancy"
_TRUE_WORDS = frozenset({"true", "yes", "y", "1", "是", "支持", "有支撑", "supported"})


# ==================== 数值 / 引用命中（复用 numeric） ====================

# 直接别名到 `src/numeric.py` 的实现：判分侧与校验侧必须"什么算数字"完全一致。
extract_numbers = numeric.extract_numbers


def _signed_values(text: str) -> list[float]:
    """抽出答案里的数字并**补上符号**。

    `numeric.extract_numbers` 的正则只匹配数字串、不含正负号（那是为"条文编号/引用角标"
    场景设计的）。但财务数据里负值很常见（投资活动现金流净额、净利润为负），
    不补符号会把「-17.85 亿元」与 `-1785202630.71` 判成对不上 → **正确答案被判成错**。
    所以这里在复用抽取结果的基础上，看数字前一个字符是不是减号。

    ⚠️ 判负号**不能用 `prev in "-−–"`**：数字出现在答案开头时 `prev` 是空串，
    而 `"" in "-−–"` 在 Python 里恒为 `True`（空串是任意串的子串）——
    于是**每个以数字开头的答案，首个数字都被整体取负**：正值答案被判成错（假阴性）、
    负值答案被判成对（假阳性）。所以这里用元组成员判断（空串不等于任何一个减号）。
    """
    t = text or ""
    out: list[float] = []
    for n in numeric.extract_numbers(t):
        start = n["start"]
        prev = t[start - 1] if start > 0 else ""
        out.append(-n["value"] if prev in ("-", "−", "–") else n["value"])
    return out


def _indicator_hit(answer: str, gt: dict) -> bool:
    """答案里是否出现了与 `gt.value` 相等的数值（含单位换算、千分位、符号）。"""
    raw = gt.get("value")
    if raw is None:
        return False
    try:
        gold = float(raw)
    except (TypeError, ValueError):
        return False
    for v in _signed_values(answer):
        # 方向与 `numeric.py` 自身的用法一致：**答案里的数**能否被**金标准值**解释
        # （含单位换算与展示层舍入）。反过来判会因舍入变体的不对称而放行错数：
        # 92.5 能"被 91.93 的四舍五入变体 92 解释"，但 91.93 不能被 92.5 解释。
        # 同时要求**符号一致**（负值写成正值是两个数）。
        if numeric.is_supported(abs(v), [abs(gold)]) and ((v < 0) == (gold < 0)):
            return True
    return False


def _article_hit(answer: str, gt: dict) -> bool:
    """法规题命中 = **文号 + 条号**同时出现在答案里。

    只对条号（"第十三条"）不足以判定：同一条号在不同版本的法规里含义不同，
    而 `gt` 已明确到 `doc_no`，所以缺文号一律不算命中。
    """
    text = answer or ""
    doc = gt.get("doc_no")
    if doc and doc not in text:
        return False
    label = gt.get("article_label")
    if label and label in text:
        return True
    art = gt.get("article") or gt.get("article_no")
    if art and f"第{art}条" in text:
        return True
    return False


def _literal_hit(answer: str, gt: dict) -> bool:
    """原文题命中 = 答案里出现了 `gt.any` 里的任一关键术语。"""
    text = answer or ""
    return any(str(s) in text for s in (gt.get("any") or []) if str(s))


def numeric_hit(answer: str, gt: dict | None) -> bool:
    """按 `gt.type` 判定答案是否命中金标准。认不出的类型返回 False（不抛异常）。

    - `indicator`：答案里有没有那个数（含单位换算与符号）
    - `article`  ：文号 + 条号是否同时出现
    - `literal`  ：是否出现 `any` 里的任一关键术语
    - `none`     ：应拒答的题不参与命中判定
    """
    if not isinstance(gt, dict):
        return False
    t = gt.get("type")
    if t == "indicator":
        return _indicator_hit(answer, gt)
    if t == "article":
        return _article_hit(answer, gt)
    if t == "literal":
        return _literal_hit(answer, gt)
    return False


# ==================== JSON 解析（失败返回 None） ====================

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _first_json_object(text: str) -> str | None:
    """从任意文本里找出**第一个完整的 JSON 对象**（按花括号配对，跳过字符串内的括号）。

    比"从第一个 `{` 切到最后一个 `}`"稳：后者在模型前后各写一段 JSON 时会把两段粘在一起。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_WORDS
    return False


def parse_judge_json(raw: object, *, metric: str) -> dict | None:
    """把模型返回的文本剥成 dict；剥不出（含非法 JSON / 非对象）**返回 None**。

    模型很爱加 markdown 代码围栏与前后寒暄（"好的，我的评审如下：…"），
    直接 `json.loads` 会失败 —— 那不是"判分失败"，只是**格式没守约定**，
    所以这里先剥围栏、再按花括号配对找对象。

    `metric` 用来做**结构性归一**：faithfulness 的 `supported` 常被写成字符串
    `"true"/"false"`，按 metric 转成布尔，省得下游每处都自己判。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    blob = _first_json_object(text)
    if blob is None:
        return None
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if metric == _FAITHFULNESS:
        claims = parsed.get("claims")
        if isinstance(claims, list):
            for c in claims:
                if isinstance(c, dict) and "supported" in c:
                    c["supported"] = _coerce_bool(c["supported"])
    return parsed


# ==================== 分数提取与聚合 ====================

def score_from_judge(parsed: dict | None, metric: str) -> float | None:
    """从判分结果里取出该指标的分（faithfulness = support 比例；relevancy = score）。

    - relevancy 的分**夹到 [0,1]**（模型偶尔给 1.2 / -0.3，那不是"更高/更低"，是越界）；
    - 缺字段 / 类型不对 → 返回 `None`（= 未判），**不返回 0** —— 0 是"判了、而且很差"，
      与"没判成"是两件事，混在一起会让指标失真。
    """
    if not isinstance(parsed, dict):
        return None
    if metric == _FAITHFULNESS:
        claims = parsed.get("claims")
        if not isinstance(claims, list) or not claims:
            return None
        flags = [_coerce_bool(c.get("supported")) for c in claims if isinstance(c, dict)]
        if not flags:
            return None
        return sum(1.0 if f else 0.0 for f in flags) / len(flags)
    # answer_relevancy
    raw = parsed.get("score")
    if raw is None:
        raw = parsed.get("answer_relevancy", parsed.get("relevancy"))
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, val))


def faithfulness(parsed: dict | None) -> float | None:
    """`faithfulness_prompt` 的配套解析：support 比例（0~1）；判不了返回 None。"""
    return score_from_judge(parsed, _FAITHFULNESS)


def answer_relevancy(parsed: dict | None) -> float | None:
    """`relevancy_prompt` 的配套解析：0~1 分；判不了返回 None。"""
    return score_from_judge(parsed, _RELEVANCY)


def aggregate(scores: list[float | None]) -> dict:
    """把逐题分数聚合成一个数：**None 与有效分分别计数，均值只按有效分算**。

    这正是"判分失败计为未判、不进分母"的落点：把 None 当 0 会凭空压低指标，
    直接把 None 丢掉又不留痕（看不出"这一轮有多少题没判成"）。
    """
    valid = [float(s) for s in scores
             if isinstance(s, (int, float)) and not isinstance(s, bool)]
    missing = [s for s in scores if s is None]
    return {
        "n_total": len(scores),
        "n_valid": len(valid),
        "n_missing": len(missing),
        "mean": (sum(valid) / len(valid)) if valid else None,
    }


# ==================== prompt 构造 ====================

def _render_contexts(contexts: object) -> str:
    """把上下文渲染成带编号的文本块。接受 str 列表或 `{"text": ...}` 列表。"""
    items: list[str] = []
    for c in (contexts or []):
        if isinstance(c, dict):
            items.append(str(c.get("text") or c.get("snippet") or ""))
        else:
            items.append(str(c))
    return "\n\n".join(f"[{i}] {t}" for i, t in enumerate(items, start=1))


def faithfulness_prompt(question: str, answer: str, contexts: object) -> list[dict]:
    """构造忠实度判分的 messages。**JSON 是唯一输出格式**。"""
    user = (f"【资料】\n{_render_contexts(contexts)}\n\n"
            f"【问题】\n{question}\n\n"
            f"【答案】\n{answer}")
    return [{"role": "system", "content": _SYSTEM_FAITHFULNESS},
            {"role": "user", "content": user}]


def relevancy_prompt(question: str, answer: str) -> list[dict]:
    """构造答案相关性判分的 messages。**JSON 是唯一输出格式**。"""
    user = f"【问题】\n{question}\n\n【答案】\n{answer}"
    return [{"role": "system", "content": _SYSTEM_RELEVANCY},
            {"role": "user", "content": user}]


# ==================== 判分入口 ====================

def _build_model(cfg: dict):
    """与 `src/answer.py::_call_llm` **同一套构造**（同配置来源、同温度、同 JSON 模式）。"""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=cfg["model"], base_url=cfg["base_url"], api_key=cfg["api_key"],
        temperature=_JUDGE_TEMPERATURE,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def _to_messages(prompt: list[dict]):
    from langchain_core.messages import HumanMessage, SystemMessage

    out = []
    for m in prompt:
        content = m.get("content", "")
        out.append(SystemMessage(content=content) if m.get("role") == "system"
                   else HumanMessage(content=content))
    return out


def _run_metric(model, prompt: list[dict], metric: str, notes: list[str]) -> float | None:
    """跑一个指标：调用 → 解析 → 取分。**任何一步失败都收敛成 None + 一条 note**。"""
    try:
        out = model.invoke(_to_messages(prompt))
        content = out.content if isinstance(out.content, str) else str(out.content)
    except Exception as e:  # noqa: BLE001 —— 判分是外部调用，失败必须被统计而不是上抛
        notes.append(f"{metric} 调用失败（计为未判）：{type(e).__name__}: {e}")
        return None
    parsed = parse_judge_json(content, metric=metric)
    if parsed is None:
        notes.append(f"{metric} 输出无法解析为 JSON（计为未判）：{content[:120]}")
        return None
    score = score_from_judge(parsed, metric)
    if score is None:
        notes.append(f"{metric} JSON 缺字段（计为未判）："
                     f"{json.dumps(parsed, ensure_ascii=False)[:120]}")
    return score


def judge_answer(question: str, answer: str, contexts: object, *,
                 model: object | None = None) -> dict:
    """汇总 faithfulness 与 answer_relevancy。

    返回 `{"faithfulness": float|None, "answer_relevancy": float|None,
    "judge_model": str, "notes": [...]}`。

    - `model` 可注入（单测用假模型，不联网、不花 token）；
    - 未注入时按 `llm.get_active_llm()` 构造（与 `answer._call_llm` 同一套配置）；
    - **任何失败只填 None + notes，绝不抛异常** —— 判分失败要能被统计成"未判"。
    """
    notes: list[str] = []
    cfg = llm.get_active_llm()
    judge_model = getattr(model, "model_name", None) or cfg.get("model") or "unknown"

    if model is None:
        try:
            model = _build_model(cfg)
        except Exception as e:  # noqa: BLE001 —— 见 docstring
            notes.append(f"判分模型初始化失败（两项均计为未判）：{type(e).__name__}: {e}")
            return {"faithfulness": None, "answer_relevancy": None,
                    "judge_model": judge_model, "notes": notes}

    faith = _run_metric(model, faithfulness_prompt(question, answer, contexts),
                        _FAITHFULNESS, notes)
    relev = _run_metric(model, relevancy_prompt(question, answer), _RELEVANCY, notes)
    return {"faithfulness": faith, "answer_relevancy": relev,
            "judge_model": judge_model, "notes": notes}


if __name__ == "__main__":
    # 自检（不联网的部分）：python -m src.judge
    _gt = {"type": "indicator", "indicator": "营业总收入",
           "value": 174144069958.25, "unit": "元"}
    print("numeric_hit(1,741.44 亿元) =", numeric_hit("营业总收入 1,741.44 亿元", _gt))
    print("numeric_hit(1,730.00 亿元) =", numeric_hit("营业总收入 1,730.00 亿元", _gt))
    print("parse 围栏:", parse_judge_json('```json\n{"score":0.8}\n```', metric=_RELEVANCY))
    print("parse 垃圾:", parse_judge_json("不是 JSON", metric=_RELEVANCY))
    print("aggregate:", aggregate([0.5, None, 1.0]))
    print("active llm:", llm.get_active_llm()["provider"], llm.get_active_llm()["model"])
