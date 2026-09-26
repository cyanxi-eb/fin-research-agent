from __future__ import annotations
import json
import os
from pathlib import Path

from .core import _api_key, _db_local, _secret
from .core import ROOT_DIR, DATA_DIR, CONFIG_DIR, DB_DIR, INDEX_DIR, VECTOR_DIR, REGULATION_DIR
# ---- 解析与切分 ----
# 年报章节锚点：用于把页面归到章节，章节名会进 chunk 元数据（溯源要显示「第X节」）
REPORT_SECTIONS: list[str] = [
    "重要提示、目录和释义", "公司简介和主要财务指标", "管理层讨论与分析",
    "公司治理", "环境和社会责任", "重要事项", "股份变动及股东情况",
    "优先股相关情况", "债券相关情况", "财务报告",
]
# 一级章节在 PDF 里的常见「短标题」写法（各家年报体例不一，只收同义不收缩写）。
# 例：茅台把第一节写成独立的「释义」「重要提示」两行，不收进来这几页就没有章节标签；
#     中国平安走港式体例，整本报告找不到「管理层讨论与分析」这八个字，
#     对应的是「经营情况讨论及分析」「关于我们」「公司管治」。
SECTION_ALIASES: dict[str, list[str]] = {
    "重要提示、目录和释义": ["释义", "重要提示"],
    "公司简介和主要财务指标": ["关于我们", "我们是谁"],
    "管理层讨论与分析": ["经营情况讨论与分析", "经营情况讨论及分析"],
    # 注意：2025 年新体例把「公司治理」与「环境和社会责任」并成一节写作「公司治理、环境和社会」
    "公司治理": ["公司治理结构", "公司治理报告", "公司管治", "公司治理、环境和社会"],
    "环境和社会责任": ["环境与社会责任"],
    # 刻意不收「董事会报告和重要事项」为中国平安的别名：平安把「大节名」与「小节名」
    # 当两个页眉元素、换页时互换位置，收进来会让 P134-P147 在「重要事项」与「公司治理」
    # 之间逐页横跳（实测 8 次来回）。不收录则整段归入「公司治理」，单调且不误导。
    "股份变动及股东情况": ["股本变动及股东情况"],
    "财务报告": ["财务报表", "审计报告"],
}
# 章节标题只认「页面前 N 个非空行」内出现的那些。
#
# 为什么必须有这条：实测 000858 P37 / 002594 P72 / 300750 P64 的**正文中段**都有
# 独立成行的一个「财务报告」——那是「内部控制自我评价」表格里的单元格标签
# （与「非财务报告重大缺陷数量」成对出现），不是章节标题。
# 年报分节一律另起一页，所以「非页首的标题行」可直接判为噪声。
HEADING_TOP_LINES: int = int(os.getenv("HEADING_TOP_LINES", "5"))
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "512"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "64"))
# 太短的碎块不进索引（页尾孤字、页眉残留）
MIN_CHUNK_CHARS: int = int(os.getenv("MIN_CHUNK_CHARS", "40"))
# 页眉页脚清理：某行在 >= 该比例页面上重复出现且很短，就视为页眉页脚丢弃
RUNNING_HEADER_RATIO: float = float(os.getenv("RUNNING_HEADER_RATIO", "0.3"))

# ---- 索引产物 ----
CHUNKS_DIR: Path = INDEX_DIR / "chunks"
BM25_INDEX_PATH: Path = INDEX_DIR / "bm25.pkl"

# ---- 检索 ----
# bm25   = 仅字面检索（Step 1，零额外依赖）
# hybrid = BM25 + 向量 + RRF + rerank（Step 4 起）
RETRIEVE_MODE: str = os.getenv("RETRIEVE_MODE", "bm25").strip().lower()
# 已实现的检索模式白名单。**不是给用户随便填的字符串**：填错要能立刻报
# "未知模式 + 可选值"，而不是静默回落到 bm25（静默回落会让"我明明开了混合检索"变成谎话）。
RETRIEVE_MODES: frozenset[str] = frozenset({"bm25", "hybrid"})
RETRIEVE_TOPK: int = int(os.getenv("RETRIEVE_TOPK", "5"))
RECALL_TOPN: int = int(os.getenv("RECALL_TOPN", "20"))   # 单路召回候选数
RRF_K: int = int(os.getenv("RRF_K", "60"))               # RRF 融合常数
RERANK_ENABLED: bool = os.getenv("RERANK_ENABLED", "0").strip() == "1"
# BM25 短语命中加成。为什么需要：分词用 jieba search 模式（提召回），
# 代价是「资产负债率」会被额外切成 资产/负债/率，导致只沾了子词的段落也能得高分 ——
# 实测出现过 top1 完全不含该短语的情况。加一个整短语出现次数的加成即可压掉这种噪声。
# 上限 3 次，避免某页反复出现该词就霸榜。
BM25_PHRASE_BONUS: float = float(os.getenv("BM25_PHRASE_BONUS", "3.0"))
BM25_PHRASE_CAP: int = int(os.getenv("BM25_PHRASE_CAP", "3"))

# 同页配额：截断前，同一 (公司, 年份, 页码) 最多保留几块。0 = 不限（Step 3/4 的原始行为）。
#
# 为什么需要：一页往往被切成 2~3 块，于是 top5 常被同一页的分块占满
# （评测里出现过 `9,9,56,16,16` 这种 top5），本可给其他页的名额被吃掉。
#
# 为什么这一项**只会让指标变好，不会变坏**（可证明的性质）：
# 配额只丢弃"某页的第 Q+1 及以后的块"，页面的**第 1 块永远保留**；
# 且第 j 个被选中的块在原始名次上必然 ≥ j，所以任何"第 1 块落在原始 top-k"的页面
# 一定仍然落在配额后的 top-k 里 → **top-k 覆盖的页面集合只会变大**。
# 单测 `test_page_quota_never_drops_first_chunk_of_a_page` 守这条性质。
# 取 2 而不是 1：同一指标常跨同页两块（如"毛利率"的分子分母分处两段），留 1 会丢证据。
RETRIEVE_MAX_PER_PAGE: int = int(os.getenv("RETRIEVE_MAX_PER_PAGE", "2"))

# ---- 向量库（Step 4）----
# 落盘三件套：向量矩阵 + 行序对齐的元数据 + 构建指纹（manifest）。
# manifest 的作用是**防止"用 A 模型建的库、拿 B 模型的查询向量去搜"**：
# 维度不同会立刻报错，维度相同（都是 1024）就会静默给出毫无意义的相似度排序，
# 那种错误从结果上完全看不出来，只能靠指纹拦。
VECTOR_MATRIX_NAME: str = "vectors.npy"
VECTOR_META_NAME: str = "meta.jsonl"
VECTOR_MANIFEST_NAME: str = "manifest.json"
VECTOR_PART_NAME: str = "vectors.part.npy"   # 断点续传用的中间产物
# 余弦相似度下限：低于它直接不返回。默认 0.0（只丢"负相关"）。
# 为什么不设一个"看起来更聪明"的阈值：中文 embedding 的余弦绝对值随模型而异，
# 拍一个 0.5 上去会把好结果也砍掉，而这种错误在结果上表现为"某些问题突然召不到了"，
# 极难归因。相关性交给 **RRF 的排名** 和 **拒答闸门** 去判，不在这里用绝对分数做。
VECTOR_MIN_SCORE: float = float(os.getenv("VECTOR_MIN_SCORE", "0.0"))
# 查询向量缓存条数：同一问题会被反复检索（评测要跑 Step3/Step4 两轮、前端会重试），
# 缓存能让两次运行**用完全相同的查询向量**，对比才公平（也省一次 API 调用）。
VECTOR_QUERY_CACHE: int = int(os.getenv("VECTOR_QUERY_CACHE", "256"))

# ---- 向量存储后端（Phase 2 批次 3）----
# numpy  = 精确余弦 + vectors.npy + meta.jsonl + manifest.json（默认，零外部依赖）
# qdrant = Qdrant 向量数据库（HNSW ANN + payload filter，需 FA_QDRANT_URL）
VECTOR_BACKEND: str = (os.getenv("FA_VECTOR_BACKEND", "numpy").strip().lower() or "numpy")
# 已实现的向量后端白名单 —— 填错要能立刻报错，而不是静默回退
VECTOR_BACKENDS: frozenset[str] = frozenset({"numpy", "qdrant"})
QDRANT_URL: str = os.getenv("FA_QDRANT_URL", "http://localhost:6333").strip()
QDRANT_COLLECTION: str = os.getenv("FA_QDRANT_COLLECTION", "fin_research_vectors").strip()

# ---- 法规语料（Step 5，合规核查子图）----
#
# **独立索引**，不混进年报索引。混进去会同时污染三样东西：
#   ① Step 3/4 的全部评测数字（年报检索会召回法规段落）；
#   ② 「语料外实词」判定（法规里的词会让"零出现"失效）；
#   ③ 引用格式（法规没有"第几页第几段"）。
# 独立的代价只是多一个索引文件与一次加载，收益是**既有结论全部保持可比**。
REGULATION_BM25_PATH: Path = INDEX_DIR / "bm25_regulation.pkl"
REGULATION_RAW_DIR: Path = REGULATION_DIR / "raw"
REGULATION_TOP_K: int = int(os.getenv("REGULATION_TOP_K", "5"))
# 合规回答最多列几条条文原文（条文很长，列太多会淹没结论）
REGULATION_MAX_ARTICLES: int = int(os.getenv("REGULATION_MAX_ARTICLES", "3"))

# ---- 网络搜索兜底（本轮新增）----
#
# 定位：**「库里查不到」之后的兜底**，不是新意图。触发信号来自 answer.py 的三道闸门
# （no_evidence / out_of_corpus / low_coverage），因此路由表与 intent 集合一律不动。
#
# 默认通道 `bing` 是**免 Key 且国内可达**的（抓 cn.bing.com/search 结果页，零依赖）；
# ddg 保留为备选（装了 duckduckgo-search 更稳，海外网络更合用）；
# 要更稳可以切 tavily（需 TAVILY_API_KEY）。默认开，但**入库要过交叉验证**：
# 宁可少给，也不给一个"看起来有据其实单源"的结论。
WEB_SEARCH_ENABLED: bool = os.getenv("FA_WEB_SEARCH_ENABLED", "1").strip() == "1"
WEB_SEARCH_BACKEND: str = (os.getenv("FA_WEB_SEARCH_BACKEND", "bing").strip().lower() or "bing")
# Key 走与 LLM 供应商同一条解析路径（环境变量 > data/llm_keys.local.json），不另写一套
WEB_SEARCH_API_KEY: str = _api_key("TAVILY_API_KEY", "tavily")
WEB_SEARCH_MAX_RESULTS: int = int(os.getenv("FA_WEB_SEARCH_MAX_RESULTS", "5"))
# 正文抓取页数上限：交叉验证要读正文，但**不能把搜到的 5 条全抓一遍**（慢且不礼貌）
WEB_SEARCH_MAX_PAGES: int = int(os.getenv("FA_WEB_SEARCH_MAX_PAGES", "3"))
# 单页正文字节上限：网页里广告/内联数据可能几百 KB，截断即可（判数字一致性不需要全文）
WEB_SEARCH_MAX_BYTES: int = int(os.getenv("FA_WEB_SEARCH_MAX_BYTES", str(2 * 1024 * 1024)))
WEB_SEARCH_TIMEOUT: float = float(os.getenv("FA_WEB_SEARCH_TIMEOUT", "10"))
# 每日联网次数上限：无 Key 通道被大量调用会被对方限流/封禁，超出直接不联网并写明原因
WEB_SEARCH_DAILY_QUOTA: int = int(os.getenv("FA_WEB_SEARCH_DAILY_QUOTA", "200"))
# 交叉验证至少要有几个**独立域名**（同一家媒体的转载算 1 个）
WEB_SEARCH_MIN_DOMAINS: int = int(os.getenv("FA_WEB_SEARCH_MIN_DOMAINS", "2"))
WEB_SEARCH_QUOTA_PATH: Path = DB_DIR / "web_search_quota.json"

# 哪些拒答原因才值得去联网。三类都在里，因为它们的共同语义是「本地资料里没有」；
# 而 `model_insufficient`（模型自己说证据不够）不在里面 —— 那种情况去联网无助于回答。
WEB_TRIGGER_REASONS: frozenset[str] = frozenset(
    {"no_evidence", "out_of_corpus", "low_coverage"})

# 网络语料区：**独立目录 + 独立索引**，与年报索引、法规索引三者互不干扰。
# 并入年报索引会同时污染 Step 3/4 全部评测数字与「语料外实词」判据（与法规独立索引同一理由）。
WEB_CORPUS_DIR: Path = DATA_DIR / "web_corpus"
WEB_BM25_PATH: Path = INDEX_DIR / "bm25_web.pkl"
WEB_CORPUS_TOP_K: int = int(os.getenv("FA_WEB_CORPUS_TOP_K", "5"))

MYSQL: dict = {
    "host": os.getenv("MYSQL_HOST", "") or _db_local("mysql_host", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "") or _db_local("mysql_port", "3306")),
    "user": os.getenv("MYSQL_USER", "") or _db_local("mysql_user", "root"),
    "password": _secret("MYSQL_PASSWORD", "mysql_password"),
    "database": os.getenv("MYSQL_DATABASE", "") or _db_local("mysql_database", "fin_research"),
}


def mysql_conn_string() -> str:
    """拼 MySQL 连接串（mysql://user:pwd@host:port/db），口令做 URL 转义。"""
    from urllib.parse import quote

    m = MYSQL
    return (f"mysql://{quote(m['user'], safe='')}:{quote(m['password'], safe='')}"
            f"@{m['host']}:{m['port']}/{m['database']}")
