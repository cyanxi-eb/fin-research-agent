"""RRF（Reciprocal Rank Fusion）—— 把 BM25 路与向量路合成一条排序。

## 为什么必须要 RRF，而不是"分数加权求和"

两路的分数**不同量纲**：
- BM25 分数无上界（词频/文档长度/语料统计决定，实测本项目落在 0~40 之间）；
- 余弦相似度落在 `[-1, 1]`，而中文 embedding 实际有效区间通常只有 0.2~0.7。

直接加权求和必须先做归一化（min-max / z-score），而归一化**依赖当前候选集合的分布**：
同一条片段，因为同批候选里多了一个高分片段，归一化后的分数就变了 —— 排序会随
"这次召回了什么"而抖动。RRF 只看**排名名次**，天然与量纲无关，也不需要调权重，
是混合检索里最稳的默认做法（Cormack et al. 2009）。

## 三个实现约定（不写清就会踩）

1. **名次从 1 开始**，`score = Σ_i w_i / (k + rank_i)`，`k` 默认 60（原论文值）。
2. **缺席不罚**：某条片段只出现在一路里时，另一路**贡献 0**，而不是按"最后一名"处理。
   把它当最后一名等于凭空给它一个负排名，会让"向量召回了但 BM25 没召回"的片段
   被系统性压低 —— 而那恰恰是我们加向量路想要捞回来的东西。
3. **并列必须稳定**：同分时按「单路最好名次」→「chunk_id」排序。
   评测要跑 Step3/Step4 两轮对比，排序不确定的话两次运行结果会抖，
   指标差异就分不清是策略带来的还是随机带来的。
"""
from __future__ import annotations

from src import config

# 每路在融合里的权重。默认等权 —— 先用等权测出基线，再谈调参；
# 一上手就调权重，等于把"哪一路更好"和"权重该给多少"两个问题搅在一起。
DEFAULT_WEIGHTS: dict[str, float] = {"bm25": 1.0, "vector": 1.0}


def fuse(routes: dict[str, list[dict]], *, k: int | None = None,
         weights: dict[str, float] | None = None,
         topk: int | None = None) -> list[dict]:
    """融合多路召回。`routes = {"bm25": [...hits], "vector": [...hits]}`。

    每路传入的列表必须**已按该路的相关性从高到低排好**（本项目的两条路都保证这点）。

    返回按 RRF 分降序的列表，每项：
    ```
    {"chunk_id", "rrf_score", "ranks": {"bm25": 2, "vector": 5}, "best_rank", "routes"}
    ```
    `ranks` 里没有某一路 = 该路没召回它。这些字段会进 `pipeline` 的 debug 输出，
    用来回答"这条到底是哪一路捞上来的" —— 没有它就没法解释指标变化。
    """
    k = config.RRF_K if k is None else k
    weights = {**(weights or DEFAULT_WEIGHTS)}

    merged: dict[str, dict] = {}
    for route, hits in routes.items():
        w = float(weights.get(route, 1.0))
        for rank, h in enumerate(hits, start=1):
            cid = h.get("chunk_id")
            if not cid:
                continue  # 没有 chunk_id 的候选无法去重，直接丢弃（而不是当成一条新片段）
            item = merged.setdefault(cid, {"chunk_id": cid, "rrf_score": 0.0,
                                           "ranks": {}, "routes": []})
            item["rrf_score"] += w / (k + rank)
            item["ranks"][route] = rank
            if route not in item["routes"]:
                item["routes"].append(route)

    out = list(merged.values())
    for it in out:
        it["rrf_score"] = round(it["rrf_score"], 8)
        it["best_rank"] = min(it["ranks"].values())
        it["routes"].sort()
    out.sort(key=lambda it: (-it["rrf_score"], it["best_rank"], it["chunk_id"]))
    return out[:topk] if topk else out


def explain(fused: list[dict], routes: dict[str, list[dict]], limit: int = 10) -> list[str]:
    """生成可读的融合解释（给 debug / 手工排查用）。

    每一行说明：RRF 分、各路的原始名次、这条是"哪一路独有"还是"两路共识"。
    **"两路共识"是最值得看的一类** —— 如果 top5 里一条共识都没有，
    说明两路检索的结果差异极大，融合只是把它们拼在一起，没有真正互相印证。
    """
    lines: list[str] = []
    for i, it in enumerate(fused[:limit], start=1):
        parts = []
        for route in routes:
            r = it["ranks"].get(route)
            parts.append(f"{route}={r if r else '—'}")
        tag = "两路共识" if len(it["routes"]) > 1 else f"仅{'/'.join(it['routes'])}"
        lines.append(f"  {i:>2}. rrf={it['rrf_score']:.5f}  {' '.join(parts)}  [{tag}] "
                     f"{it['chunk_id']}")
    return lines


if __name__ == "__main__":
    # 自检：不联网，用构造数据验证 RRF 的核心性质
    bm = [{"chunk_id": "A"}, {"chunk_id": "B"}, {"chunk_id": "C"}]
    ve = [{"chunk_id": "C"}, {"chunk_id": "D"}]
    fused = fuse({"bm25": bm, "vector": ve})
    print("RRF 融合结果：")
    for line in explain(fused, {"bm25": bm, "vector": ve}):
        print(line)
    print("\n注：C 在两路都靠前（bm25#3 + vector#1），RRF 分应高于单路第一的 A。")
    print("    这正是 RRF 的价值：**共识优先**，而不是「哪路分数大就听哪路」。")
