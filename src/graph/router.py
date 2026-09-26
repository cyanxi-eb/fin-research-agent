"""意图路由 —— 把问题分到三条子图：`rag` / `analysis` / `compliance`。

```
question ─▶ router ─┬─▶ rag        文档问答（原文检索 + 引用）
                    ├─▶ analysis   数值分析（**强制走工具层**）
                    └─▶ compliance 合规核查（法规库，条文级引用）
```

## 为什么路由器不调模型（这是刻意的）

1. **路由是整条链路上最不该引入不确定性的地方**。它一旦飘，后面三条链路的行为都跟着飘；
   更糟的是**路由错了会伪装成"检索不准"或"模型不行"** —— 排查时会先去调检索参数、
   换模型，方向全错。规则路由的判据是可打印、可断言的。
2. **零成本**：路由发生在调模型之前。如果需要一次 LLM 调用才能决定"要不要调 LLM"，
   那拒答类问题的"零 token"承诺就破产了（see `answer.py` 的确定性闸门）。
3. **默认必须安全**：判不出来就 `rag`（最保守的一条：检索 + 引用 + 三道闸门）。

## 判据长什么样（两个条件同时成立才判 analysis）

数值题的判据是「**疑问形** + **库内指标名**」两条同时命中：

- 只有疑问形（"多少/几/对比/排名"）→ 不判；
- 只有指标名（"公司治理"里有"治理"）→ 不判。

**为什么必须要求指标名**：只看疑问词的话，「公司食堂菜谱有什么推荐」里的"什么"
会把一堆闲聊判成数值题，接着被工具层以"没有这个指标"拒掉 ——
用户看到的是"系统说查不到这个数"，而正确答案是"这个问题不归数值链路管"。
**错误的路由会把错误的形状带给用户**。

指标名表来自 `config.INDICATORS` 的别名（与工具层同一份），所以加指标只需要改一处。
"""
from __future__ import annotations

from src import config

# 数值意图的疑问形。注意这些是**多字词**：单字"几""谁"会误伤（"几年"能命中"几年"？不会，
# 因为分词后是"几"+"年"）。所以只收多字且语义明确的。
_NUMERIC_CUES: tuple[str, ...] = (
    "多少", "几多", "占比", "比例", "对比", "比较", "排名", "排序", "谁更", "哪个更",
    "分别是", "分别多少", "同比增长", "同比", "环比", "增减", "变化多少", "差多少",
    "高多少", "低多少", "增长率", "增速", "是多少", "有多少", "共计", "合计",
)

# 数值意图的行为形（"算一下""帮我算"）—— 同样是"想要一个数"，但可能没写疑问词
_NUMERIC_ACTIONS: tuple[str, ...] = ("计算", "算一下", "算出", "帮我算", "折算")

# 合规意图的判据：出现"规则/法规"语义的词才算。
# 刻意**不含**"是否""符合"这类通用词 —— 否则「是否符合会计准则」会被判成合规核查。
_COMPLIANCE_CUES: tuple[str, ...] = (
    "合规", "违规", "违法", "法规", "规定", "条款", "第几条", "第几章",
    "信息披露", "披露要求", "上市规则", "交易所规则", "证监会", "监管要求",
    "处罚", "罚款", "警示函", "问询函", "内幕", "关联交易是否", "独立董事是否",
)

INTENTS: frozenset[str] = frozenset({"rag", "analysis", "compliance"})


def indicator_aliases() -> list[str]:
    """库内所有指标/比率的名称与别名（含派生比率）。

    直接从 `config` 取，**不另维护一份表** —— 两份表迟早不一致，
    而不一致的后果是"工具层认识这个指标、路由层不认为它在问数值"→ 静默走错子图。
    """
    names: list[str] = []
    for spec in config.INDICATORS.values():
        names.append(spec.get("name") or "")
        names.extend(spec.get("aliases") or [])
    for spec in config.RATIOS.values():
        names.append(spec.get("name") or "")
        names.extend(spec.get("aliases") or [])
    return [n for n in dict.fromkeys(names) if n]


def route(question: str) -> dict:
    """判定意图。返回 `{"intent", "rule", "confidence", "matched", "reason"}`。

    - `rule` 是**命中的哪条规则**（`compliance_cue` / `numeric_cue+indicator` …），
      必须进响应与审计 —— 没有它，路由问题只能靠猜。
    - `confidence` 只在"判据充分"时给高分；默认兜底 rag 的置信度是 **0.3**，
      这样下游（HITL / 审计）能区分"确定是文档问答"与"兜底成文档问答"。
    - `matched` 列出命中的词，用于人工核对与用例断言。
    """
    q = (question or "").strip()
    ql = q.lower()
    if not q:
        return {"intent": "rag", "rule": "empty_question", "confidence": 0.0,
                "matched": [], "reason": "空问题，按文档问答走（下游会拒答）"}

    # ---- compliance 优先于 analysis ----
    # 为什么：合规问题里常同时出现数字（"第十二条""500 万处罚"），
    # 先判 analysis 会把它送进工具层，而工具层回答不了法规问题。
    comp = [c for c in _COMPLIANCE_CUES if c in ql]
    if comp:
        return {"intent": "compliance", "rule": "compliance_cue", "confidence": 0.9,
                "matched": comp,
                "reason": f"命中法规类表述：{ '、'.join(comp) }"}

    # ---- analysis：疑问形 + 库内指标名，两条都要 ----
    cues = [c for c in _NUMERIC_CUES if c in ql]
    actions = [a for a in _NUMERIC_ACTIONS if a in ql]
    hit_ind = [n for n in indicator_aliases() if n and n.lower() in ql]
    if (cues or actions) and hit_ind:
        return {"intent": "analysis", "rule": "numeric_cue+indicator", "confidence": 0.8,
                "matched": {"cues": cues, "actions": actions, "indicators": hit_ind},
                "reason": f"同时命中数值疑问形（{ '、'.join(cues + actions) }）"
                          f"与库内指标名（{ '、'.join(hit_ind) }）→ 走工具层，"
                          f"数值不允许从原文读"}
    if cues and not hit_ind:
        return {"intent": "rag", "rule": "numeric_cue_without_indicator", "confidence": 0.4,
                "matched": {"cues": cues},
                "reason": f"有数值疑问形（{ '、'.join(cues) }）但没命中库内指标名 —— "
                          f"不判数值题（否则闲聊会被送进工具层，"
                          f"用户会看到「查不到这个数」而不是「不归数值链路管」）"}

    return {"intent": "rag", "rule": "default", "confidence": 0.3, "matched": [],
            "reason": "未命中任何专用规则，按文档问答走（最保守：检索 + 引用 + 三道闸门）"}


if __name__ == "__main__":
    import json
    import sys

    samples = sys.argv[1:] or [
        "贵州茅台2024年的营业总收入是多少",
        "五粮液和贵州茅台2024年的毛利率对比",
        "这家公司的信息披露是否合规",
        "公司食堂的菜谱是什么",
    ]
    for s in samples:
        print(f"{s}\n  -> {json.dumps(route(s), ensure_ascii=False)}\n")
