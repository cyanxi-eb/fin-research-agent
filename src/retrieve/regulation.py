"""法规检索 —— 独立索引的读取与条文级引用。

与 `retrieve/pipeline.py` 的关系是**并列**而不是包含：两条索引语料不同、引用维度不同、
适用意图不同。唯一的共享是 `BM25Index`（打分与分词）这一层 —— 复用它是为了
"同一套中文分词与短语加成"，不是为了把两条语料混在一起（混一起的代价见
`ingest/fetch_regulation.py` 的模块说明）。

## 版本处理（这一层就要定，不能留给上层猜）

同一部办法存在多个修订版，条文可能实质不同。`search()` 如实返回各自版本，
`prefer_current()` 负责"同一条号同时命中两版时以现行版为准，并把被取代的那版一起带出来"。
为什么要把旧版也带出来：问"2021 年的年报适用哪条"时，答案必须引 182 号而不是 226 号 ——
一刀切只留最新版会让这种问题**错误地给出正确答案**（用新法回答旧事）。
"""
from __future__ import annotations

from src import config
from src.retrieve.bm25 import BM25Index

_INDEX: BM25Index | None = None


def reset() -> None:
    """丢掉缓存（单测/重建索引用）。"""
    global _INDEX
    _INDEX = None


def available() -> bool:
    return config.REGULATION_BM25_PATH.exists()


def index() -> BM25Index:
    """加载法规索引（进程内缓存）。

    未入库时抛 `FileNotFoundError` 而不是返回空索引：**"没入库"与"没命中"是两件事**，
    前者是环境没准备好（要去跑取数脚本），后者是结论（库里确实没有相关规定）。
    用空索引把两者都变成"没命中"，用户就永远不知道其实该去建库。
    """
    global _INDEX
    if _INDEX is None:
        if not available():
            raise FileNotFoundError(
                f"法规索引不存在：{config.REGULATION_BM25_PATH}"
                f"（先跑 python -m src.ingest.fetch_regulation）")
        _INDEX = BM25Index.load(config.REGULATION_BM25_PATH)
    return _INDEX


def stats() -> dict:
    if not available():
        return {"available": False, "path": str(config.REGULATION_BM25_PATH)}
    idx = index()
    docs: dict[str, int] = {}
    for c in idx.chunks:
        docs[c.get("doc_id", "?")] = docs.get(c.get("doc_id", "?"), 0) + 1
    return {"available": True, "path": str(config.REGULATION_BM25_PATH),
            "articles": len(idx.chunks), "by_doc": docs,
            "titles": sorted({f"{c.get('title')}({c.get('doc_no')})" for c in idx.chunks})}


def search(query: str, topk: int | None = None) -> list[dict]:
    """检索条文，返回 `[{chunk, score, citation, ...}]`（含各版本）。"""
    idx = index()
    return idx.search(query, topk=topk or config.REGULATION_TOP_K)


def prefer_current(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一条号命中多版时：现行版留主位，被取代的版本单独返回供提示。

    返回 `(primary, superseded_same_article)`。两条规则都是有意的：

    1. **排序先按现行/被取代，再按分数**。只按分数排会出现"合规回答的第一条引用
       已经废止的版本"—— 用户看到的是权威口吻的旧条文，而新版就排在下面一条。
       `status` 是确定性的，分数只是相关性，两者的优先级不能倒过来。
    2. `superseded` 只在**两版条文正文不同**时返回 —— 文字完全一致时提示"版本差异"
       是噪音，会让用户以为有实质变化，反而削弱真实差异的可信度。
    """
    by_article: dict[int, list[dict]] = {}
    for h in hits:
        by_article.setdefault(h["chunk"].get("article_no") or 0, []).append(h)

    primary: list[dict] = []
    superseded: list[dict] = []
    for _num, group in by_article.items():
        cur = [h for h in group if (h["chunk"].get("status") or "current") == "current"]
        old = [h for h in group if (h["chunk"].get("status") or "") != "current"]
        chosen = (cur or old)[0]
        primary.append(chosen)
        for h in old:
            if _body(h) != _body(chosen):
                superseded.append(h)
    primary.sort(key=lambda h: ((h["chunk"].get("status") or "current") != "current",
                                -h["score"]))
    return primary, superseded


def _body(hit: dict) -> str:
    """条文正文（去掉条号前缀与空白），用于版本间比对。"""
    text = (hit["chunk"].get("text") or "")
    label = hit["chunk"].get("article_label") or ""
    return text.replace(label, "").replace("　", "").replace(" ", "").strip()


def render_context(hits: list[dict]) -> str:
    """拼给模型/答案用的条文块（每条都带完整引用串）。"""
    blocks = []
    for i, h in enumerate(hits, start=1):
        c = h["chunk"]
        blocks.append(f"[{i}] {h['citation']}\n{c.get('text')}\n"
                      f"（来源：{c.get('source_name')}；{c.get('effective_from')} 施行；"
                      f"{c.get('url')}）")
    return "\n\n".join(blocks)


def citations(hits: list[dict]) -> list[dict]:
    """条文级引用对象（结构与年报引用保持同样的 `index/citation/snippet` 外观）。"""
    out: list[dict] = []
    for i, h in enumerate(hits, start=1):
        c = h["chunk"]
        out.append({
            "index": i,
            "citation": h["citation"],
            "doc_id": c.get("doc_id"),
            "title": c.get("title"),
            "doc_no": c.get("doc_no"),
            "article_no": c.get("article_no"),
            "article_label": c.get("article_label"),
            "chapter": c.get("chapter"),
            "status": c.get("status"),
            "effective_from": c.get("effective_from"),
            "source_name": c.get("source_name"),
            "url": c.get("url"),
            "snippet": (c.get("text") or "")[:220],
        })
    return out


if __name__ == "__main__":
    # 自检：python -m src.retrieve.regulation "重大事件 立即披露" [--topk 5]
    import json as _json
    import sys as _sys

    args = _sys.argv[1:]
    query = args[0] if args and not args[0].startswith("--") else "年报 披露 期限"
    topk = int(args[args.index("--topk") + 1]) if "--topk" in args else 5
    print(_json.dumps(stats(), ensure_ascii=False, indent=2))
    for h in search(query, topk=topk):
        print(f"\n[{h['score']}] {h['citation']}")
        print("   " + (h["chunk"].get("text") or "")[:160].replace("\n", " "))
