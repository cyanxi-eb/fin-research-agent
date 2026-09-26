from __future__ import annotations
import json
import os
from pathlib import Path

"""集中式配置 —— 所有手动可调项集中在 CONFIG 区，改这里即可。

骨架来自 workflow-agent/src/config.py（同一套工程规范），领域部分为本项目新增：
- 数据源与落盘路径（raw / parsed / index / vector / regulation）
- watchlist（目标公司清单，config/watchlist.yaml）
- 检索参数（Step 1 只用 BM25，Step 4 起启用向量与重排）
- 财务指标口径表 INDICATORS（中文名 ↔ 别名 ↔ 单位，收口在这里而不是散在代码里）

约定（README「手动修改接口约定」）：
- LLM 供应商/Key/可选模型：改 LLM_PROVIDERS（运行时每次读取，无需重启）
- 目标公司：改 config/watchlist.yaml
- 检索策略与 topk：改 RETRIEVE_* / BM25_* / CHUNK_*
"""


# ==================== 路径（须先于 CONFIG 区定义） ====================
ROOT_DIR: Path = Path(__file__).resolve().parent.parent.parent
DATA_DIR: Path = ROOT_DIR / "data"
CONFIG_DIR: Path = ROOT_DIR / "config"

# 原始年报 PDF、按页解析产物、索引、向量库、业务库、法规原文
RAW_DIR: Path = DATA_DIR / "raw"
PARSED_DIR: Path = DATA_DIR / "parsed"
INDEX_DIR: Path = DATA_DIR / "index"
VECTOR_DIR: Path = DATA_DIR / "vector"
DB_DIR: Path = DATA_DIR / "db"
REGULATION_DIR: Path = DATA_DIR / "regulation"

WATCHLIST_PATH: Path = CONFIG_DIR / "watchlist.yaml"

# 本地密钥文件：开发机免配环境变量，已在 .gitignore 内，严禁提交
LLM_KEYS_LOCAL_PATH: Path = DATA_DIR / "llm_keys.local.json"
# 基础设施口令（MySQL 等），同样不落盘仓库
DB_KEYS_LOCAL_PATH: Path = DATA_DIR / "db_keys.local.json"


def _api_key(env_name: str, provider: str) -> str:
    """解析 API Key：环境变量 > data/llm_keys.local.json > 空串。

    读不到就返回空串而非兜底真实 Key：源码里留兜底 Key 一旦仓库公开即泄露，
    且会让「未配置 Key」的界面提示永久失效。
    """
    env_val = os.getenv(env_name, "").strip()
    if env_val:
        return env_val
    try:
        if LLM_KEYS_LOCAL_PATH.exists():
            data = json.loads(LLM_KEYS_LOCAL_PATH.read_text(encoding="utf-8"))
            return str(data.get(provider, "")).strip()
    except Exception:
        pass  # 密钥文件损坏不应阻断启动，按未配置处理
    return ""


def _db_local(key: str, default: str = "") -> str:
    """读 data/db_keys.local.json 里的非敏感设置，便于免环境变量启动。"""
    try:
        if DB_KEYS_LOCAL_PATH.exists():
            data = json.loads(DB_KEYS_LOCAL_PATH.read_text(encoding="utf-8"))
            return str(data.get(key, default) or default).strip()
    except Exception:
        pass
    return default


def _secret(env_name: str, local_key: str) -> str:
    """解析基础设施口令：环境变量 > data/db_keys.local.json > 空串。"""
    env_val = os.getenv(env_name, "").strip()
    if env_val:
        return env_val
    return _db_local(local_key, "")


# ============================ CONFIG 区 ============================

# ---- 问答与拒答（Step 3）----
# 检索回来的候选比最终引用的多：多召回是为了让模型"有得挑"，
# 但引用只列前 N 条，避免答案后面挂一长串没人看的出处。
ANSWER_CANDIDATE_TOPK: int = int(os.getenv("ANSWER_CANDIDATE_TOPK", "8"))
ANSWER_MAX_CITATIONS: int = int(os.getenv("ANSWER_MAX_CITATIONS", "5"))
# 喂给模型的上限（按整块截断，不切半句）
ANSWER_CONTEXT_CHARS: int = int(os.getenv("ANSWER_CONTEXT_CHARS", "6000"))

# 「证据覆盖度」门槛 —— 拒答的**确定性**信号（不依赖模型自觉）。
#
# 为什么不直接用 BM25 分数阈值：字面检索里"公司""经营"这类词几乎每页都有，
# 问「公司食堂菜谱」也能拿到不低的分数。分数高 ≠ 有证据。
# 覆盖度看的是另一个维度：**问题里的实词，有多少真的出现在召回文本里**。
# 实测「公司食堂菜谱」只覆盖 1/3（只有"公司"命中），远低于门槛 → 拒答。
ANSWER_MIN_COVERAGE: float = float(os.getenv("ANSWER_MIN_COVERAGE", "0.5"))

# 「语料外实词」闸门（默认开）：问题里有**全部入库 chunk 都没出现过**的实词时直接拒答。
#
# 为什么需要它：覆盖度闸门只能发现"词在检索到的段落里没命中"，
# 而「菜谱」这种词在整个语料里零出现时，覆盖率可能仍因其他词（"食堂"）而达标 → 漏放行。
# 实测（34 题金标准）：本规则只命中 2 题，**两题都是标注应当拒答的**（误伤 0）。
#
# 残余风险（如实记着）：口语同义词会被拒 —— 「赚钱」在 5 份年报里零出现，
# 而「盈利」存在。所以拒答文案里带了"换成报表科目名"的可操作建议，
# 并且留了这个开关：设 `ANSWER_OOC_GATE=0` 可关掉。
ANSWER_OOC_GATE: bool = os.getenv("ANSWER_OOC_GATE", "1").strip() == "1"
# 覆盖度只统计长度 >= 该值的词：单字（的/是/年/了）命中没有证据意义。
COVERAGE_MIN_TOKEN_LEN: int = int(os.getenv("COVERAGE_MIN_TOKEN_LEN", "2"))
# 问句里的功能词/疑问词，不参与覆盖度计算（否则"多少""怎么样"会稀释指标）。
#
# 实测教训（Step 4 收尾）：`哪家` 漏在表外，导致
# 「中国平安2024年年报的审计机构是哪家」这类**正常问题**被算成"有一个实词没命中"，
# 覆盖率被稀释。停用词表是人工维护的，**漏一个通用词就会让一批正常问题更接近拒答线** ——
# 所以这张表要按"疑问/指代/泛指/计量口头语"四类整类清一遍，而不是碰到一个补一个。
COVERAGE_STOPWORDS: frozenset[str] = frozenset({
    # 疑问 / 指代
    "多少", "什么", "怎么样", "怎么", "怎样", "如何", "为何", "为什么", "是否",
    "哪些", "哪家", "哪一", "哪位", "哪个", "哪里", "哪儿", "谁", "几个", "几",
    "请问", "帮我", "一下", "这个", "那个", "这些", "那些", "我们", "他们", "以及",
    "有没有", "还有", "分别", "各自", "具体", "大概", "大约",
    # 泛指（年报里每页都有的词：命中等价于没命中，只会单调抬高所有问题的覆盖度）
    "公司", "年报", "报告", "数据", "情况", "关于", "相关", "信息", "内容", "时候",
    # 计量口头语
    "多少亿", "亿元", "万元", "千元", "百万元", "元", "人民币",
})

# 免责声明：随每次回答返回。金融场景下这不是走形式 ——
# 它同时也是"本系统只做原文转述，不做投资建议"的产品定位声明。
ANSWER_DISCLAIMER: str = (
    "以上内容基于公开年报原文的检索与摘录，仅供研究参考，不构成任何投资建议。"
    "数值口径与页数以引用的原文片段为准，请自行核对。")

# ---- 多轮对话（Step 6：指代消解）----
#
# 会话里保留多少轮"实体"（question/intent/code/year）用于消解"它/该公司"这类指代。
#
# 为什么要有上限、且不大：`filters.resolve_entities` **只看最近一轮**，
# 更早的轮次只会让"上一轮指哪家公司"变得模棱两可 —— 保留越多，误沿用的风险越大，
# 而收益为零。5 轮是"够用且不至于把久远话题带进来"的折中。
HISTORY_MAX: int = int(os.getenv("FA_HISTORY_MAX", "5"))
