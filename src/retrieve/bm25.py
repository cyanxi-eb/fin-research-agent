"""BM25 字面检索 —— Step 1 的检索实现，零外部服务。

为什么先做 BM25 而不是直接上向量：
- 零额外依赖（`rank_bm25` 是纯 Python），当天就能跑通「检索→溯源」全链路；
- 金融文本专有名词多（科目名、条文编号），字面精确匹配本来就有优势，
  后面做混合检索时它也是必需的一路，不是"临时替代品"。

中文分词用 `jieba.lcut_for_search`（search 模式会额外切出子词），
对「营业总收入」「资产负债率」这类复合词能同时命中整体与部件，召回更好。

索引落 `data/index/bm25.pkl`（只存 chunks + 分词结果，BM25 对象每次重建——
重建是确定性的且足够快，避免 pickle 对象跨版本不兼容）。
"""
from __future__ import annotations

import json
import pickle
import re
import time
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from src import config, citation

jieba.setLogLevel(20)  # 关掉 jieba 首次加载的构建日志

# 数字里的千分位先去掉：不然 "123,456.78" 会被切成 "123"/"456"/"78" 三段
_THOUSAND_SEP = re.compile(r"(?<=\d),(?=\d)")
_WS = re.compile(r"\s+")
# 纯数字/小数（45.2 / 123456.78）。财务文本里这类值很常见，必须留住：
# 它们不含中文，`str.isalnum()` 又会因小数点返回 False，不用正则单独放行就会被丢掉。
_DECIMAL = re.compile(r"^\d+(?:\.\d+)?$")


def tokenize(text: str) -> list[str]:
    """分词（**检索用**）。保留中文字词、数字与英文/数字混合词，丢掉纯标点。"""
    return _tokenize(text, search=True)


def tokenize_exact(text: str) -> list[str]:
    """分词（**判断"这句话在不在文本里"用**）—— 不做 search 模式的子词扩展。

    为什么需要两个：检索要的是**召回**，所以 search 模式把「毛利率」额外切成
    `毛利`/`利率` 是有利的（命中部件也算沾边）；但拿它算"问题里的实词有没有出现在
    召回文本里"就会**系统性虚高** —— 「利率」这种由切分凑出来的词很容易在年报里
    碰巧出现（"存款利率下降"），于是覆盖率被抬高，拒答门槛形同失效。
    """
    return _tokenize(text, search=False)


def _tokenize(text: str, *, search: bool) -> list[str]:
    if not text:
        return []
    text = _THOUSAND_SEP.sub("", text.lower())
    raw = jieba.lcut_for_search(text) if search else jieba.lcut(text)
    tokens: list[str] = []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        if any("\u4e00" <= ch <= "\u9fff" for ch in t):   # 含中文
            tokens.append(t)
        elif t.isalnum() or _DECIMAL.match(t):            # 纯英文/数字/小数
            tokens.append(t)
        # 其余（纯标点、单符号）丢弃
    return tokens


def normalize_for_match(text: str) -> str:
    """去空白 + 转小写，用于「整短语命中」判断。

    去空白是必须的：PDF 抽出来的中文常被换行/空格切断
    （`资产 负债\\n率`），不去掉的话短语匹配永远命中不了。
    """
    return _WS.sub("", (text or "").lower())


def content_terms(text: str) -> list[str]:
    """问题里的**实词**（去停用词、去单字、去重保序）—— 覆盖度与"语料外实词"共用。

    为什么要抽成一个共用函数：这两处判定必须用**同一套分词与同一张停用词表**。
    分开写的话，"覆盖度认为这个词是实词、语料检查认为它是停用词"这类不一致
    会直接变成"闸门 A 放行、闸门 B 拒答"的诡异行为，而两处各自的单测都会通过。

    用 `tokenize_exact`（不做 search 模式子词扩展）：子词会把覆盖率系统性抬高，
    详见 `tokenize_exact` 的说明。
    """
    terms = [t for t in tokenize_exact(text or "")
             if len(t) >= config.COVERAGE_MIN_TOKEN_LEN
             and t not in config.COVERAGE_STOPWORDS]
    seen: dict[str, None] = {}
    for t in terms:
        seen.setdefault(t, None)   # 去重保序：同一个词问了两遍不该让分母变大
    return list(seen)


class BM25Index:
    """BM25 索引：chunks + 分词后的语料。"""

    def __init__(self, chunks: list[dict], tokens: list[list[str]]):
        if len(chunks) != len(tokens):
            raise ValueError("chunks 与 tokens 数量不一致，索引已损坏")
        self.chunks = chunks
        self.tokens = tokens
        # 空语料时 BM25Okapi 会除零，这里给个空壳避免崩
        self._bm25 = BM25Okapi(tokens) if tokens else None
        # 预归一化正文，供「整短语命中」判断用（每次查询都重算太浪费）
        self._norm = [normalize_for_match(c.get("text", "")) for c in chunks]

    # ---------- 构建与持久化 ----------

    @classmethod
    def build(cls, chunks: list[dict]) -> "BM25Index":
        return cls(chunks, [tokenize(c.get("text", "")) for c in chunks])

    @classmethod
    def build_from_disk(cls) -> "BM25Index":
        from src.ingest.chunk import load_all_chunks

        return cls.build(load_all_chunks())

    def save(self, path: Path | None = None) -> Path:
        path = path or config.BM25_INDEX_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({
                "chunks": self.chunks,
                "tokens": self.tokens,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "n": len(self.chunks),
            }, f)
        return path

    @classmethod
    def load(cls, path: Path | None = None) -> "BM25Index":
        path = path or config.BM25_INDEX_PATH
        if not path.exists():
            raise FileNotFoundError(f"索引不存在：{path}（先跑 scripts/ingest_all.py）")
        with path.open("rb") as f:
            data = pickle.load(f)
        return cls(data["chunks"], data["tokens"])

    # ---------- 检索 ----------

    def search(self, query: str, topk: int | None = None,
               code: str | None = None, year: int | None = None,
               section: str | None = None) -> list[dict]:
        """检索。

        过滤在打分之后做——这样「先限公司再取 topk」与「先 topk 再过滤」的区别
        不会让结果变少：我们得到的是**过滤集合内**的 topk。
        代价是全量打分，几千个 chunk 的量级完全无所谓。
        """
        topk = topk or config.RETRIEVE_TOPK
        if self._bm25 is None:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)
        q_norm = normalize_for_match(query)

        hits: list[dict] = []
        for i, score in enumerate(scores):
            # 整短语命中加成：压掉 search 模式分词带来的子词噪声（见 config 注释）
            bonus = 0.0
            phrase_hits = 0
            if len(q_norm) >= 2:
                phrase_hits = min(self._norm[i].count(q_norm), config.BM25_PHRASE_CAP)
                bonus = config.BM25_PHRASE_BONUS * phrase_hits
            if score <= 0 and bonus <= 0:   # 完全没命中，直接排除
                continue
            c = self.chunks[i]
            if code and c.get("code") != code:
                continue
            if year and c.get("year") != year:
                continue
            if section and section not in (c.get("section") or ""):
                continue
            hits.append({"chunk": c, "score": round(float(score) + bonus, 4),
                         "bm25": round(float(score), 4), "phrase_hits": phrase_hits,
                         # chunk 自带 `citation` 时优先用它：法规chunk的引用是
                         # 「《办法》（证监会令第226号）第二十条」，没有公司/年份/页码，
                         # 用年报的模板拼会得到"未知公司?年报告"这种垃圾串。
                         "citation": c.get("citation") or citation.format_citation(c)})
        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:topk]

    def stats(self) -> dict:
        companies = {c.get("code") for c in self.chunks}
        years = {(c.get("code"), c.get("year")) for c in self.chunks}
        return {
            "chunks": len(self.chunks),
            "companies": len(companies),
            "company_years": len(years),
            "avg_tokens": round(sum(len(t) for t in self.tokens) / len(self.tokens), 1)
            if self.tokens else 0,
        }


if __name__ == "__main__":
    # 自检：python -m src.retrieve.bm25 "毛利率" [--code 600519] [--topk 5]
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    args = _sys.argv[1:]
    query = args[0] if args and not args[0].startswith("--") else "毛利率"
    code = args[args.index("--code") + 1] if "--code" in args else None
    topk = int(args[args.index("--topk") + 1]) if "--topk" in args else 5

    idx = BM25Index.load()
    print(f"索引统计: {idx.stats()}")
    print(f"\n查询「{query}」" + (f"（限定 code={code}）" if code else "") + f" top{topk}：")
    for i, h in enumerate(idx.search(query, topk=topk, code=code), start=1):
        head = h["chunk"]["text"][:70].replace("\n", " ")
        print(f"  {i}. score={h['score']:<8} (bm25={h['bm25']} 短语={h['phrase_hits']}) "
              f"{h['citation']}")
        print(f"     {head}...")
