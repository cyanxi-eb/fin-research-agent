"""法规语料的取数与入库 —— **按「条」切分**，产出可条文级引用的独立索引。

## 为什么法规要独立索引（而不是并进年报索引）

法规与年报的**引用维度根本不同**：年报是「公司+年份+页码+章节」，法规是「文号+条号」。
并进一个索引会同时污染三样东西：
1. Step 3/4 的全部评测数字 —— 年报检索会召回法规段落，`page_hit@k` 直接失真；
2. 「语料外实词」闸门 —— 法规里的词会让"全库零出现"这条判据失效（误放行）；
3. 引用格式 —— `citation.format_citation` 是按年报元数据写的模板。

独立的代价只是一个索引文件与一次加载，收益是**既有结论全部保持可比**。

## 为什么按「条」切而不是按固定字数

合规问答的答案单位就是**条**："年报应在会计年度结束之日起 4 个月内披露（第二十条）"。
按固定字数切会把一条拆成两半，检索命中半条 → 引用"第二十条"却只给出半句正文，
用户核对时对不上，等于引用失效。按条切之后，一个 chunk 就是一条完整的规范，
`citation` 与 `text` 天然自洽。

## 版本问题（刻意保留而不是只留最新）

同一部办法会有多个修订版，条文**实质会变**（例如 226 号把披露主体从"董事、监事或
高级管理人员"改成"董事或者高级管理人员"）。只留最新版会让"历史年报当时适用哪一版"
无法回答；两版都留、并在返回值里带 `status`（current / superseded），
检索命中两版时就能显式提示"存在版本差异，以 current 为准"。**含糊其辞才是不合规的。**
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src import config
from src.ingest.html_text import strip_tags

# ==================== 语料清单（全部为官方来源，实测可下载）====================
#
# 每一条都记 `source_name` 与 `url` —— 合规回答必须能指向"这句话出自哪个官方文件的第几条"，
# 而"官方"是要能被验证的：URL 打不开或来源不明，答案就没有可核验性可言。
SOURCES: list[dict] = [
    {
        "doc_id": "xinpi-226",
        "title": "上市公司信息披露管理办法",
        "doc_no": "中国证监会令第226号",
        "promulgated": "2025-03-26",
        "effective_from": "2025-07-01",
        "status": "current",
        "supersedes": "中国证监会令第182号",
        "kind": "html",
        "source_name": "中国政府网·国务院公报 2025 年第 13 号",
        "url": ("https://www.gov.cn/gongbao/2025/issue_12026/"
                "202505/content_7022576.html"),
        "local": "gov_226_xinpi.html",
    },
    {
        "doc_id": "xinpi-182",
        "title": "上市公司信息披露管理办法",
        "doc_no": "中国证监会令第182号",
        "promulgated": "2021-03-18",
        "effective_from": "2021-03-18",
        "status": "superseded",
        "superseded_by": "中国证监会令第226号",
        "kind": "pdf",
        "source_name": "中国证监会官网",
        "url": ("https://www.csrc.gov.cn/csrc/c106256/c1653948/1653948/files/"
                "【182号】上市公司信息披露管理办法.pdf"),
        "local": "csrc_182_xinpi.pdf",
    },
]

# PDF 抽取出来的页眉页脚；HTML 抽取出来的站点导航/页脚。
# 不清理它们会被当成正文粘进最后一条，表现为"附则里混进了网站备案号"。
_PDF_NOISE = re.compile(r"^(证监会规章|证监会发布|-\s*\d+\s*-|\d+\s*/\s*\d+)$")

# 站点噪音词分成两类，**这个区分是必须的**（实测踩过）：
#
# - 子串匹配（`_SUBSTR_NOISE`）：这些词只可能出现在导航/页脚里，正文不会出现。
# - 精确匹配（`_EXACT_NOISE`）：这些词**本身很短且是常用字**，用子串匹配会误删正文。
#   实测事故：`简` 子串匹配把「《中华人民共和国公司法》（以下简称《公司法》）」
#   整行删掉了，于是第一条变成"…保护投资者合法权益，根据制定本办法。"——
#   引用看起来完整、句子读起来也顺，但法条引用的**依据被静默删掉**了。
#   这类"删得看起来很正常"的错误比崩溃危险得多。
_SUBSTR_NOISE = (
    "中国政府网", "京ICP备", "网站标识码", "京公网安备", "主办单位：", "版权所有：",
    "网站纠错", "无障碍", "国务院客户端", "客户端小程序", "微博、微信",
)
_EXACT_NOISE = frozenset({
    "简", "繁", "EN", "首页", "登录", "注册", "退出", "打印", "邮箱",
    "微博", "微信", "客户端", "小程序", "电脑版", "字号：", "超大", "大", "默认",
})
# 「强截断」标记：出现即**从该处切掉整行的剩余部分并结束正文**。
#
# 为什么不能只靠上面的短行过滤：gov.cn 把页脚导航摊成了几十行
# （`相关稿件` / `链接：全国人大|…|最高人民检察院` / `国务院部门网站` / …），
# 单看每行都"像正文"，只有把它们当整体看才知道是版权区。截断标记就是这个整体信号。
_STRONG_CUT = (
    "相关稿件", "国务院部门网站", "关于本网", "网站声明", "联系我们",
    "驻港澳机构网站", "驻外机构", "地方政府网站", "电脑版",
)

_CHAPTER_RE = re.compile(r"^第([一二三四五六七八九十百零]+)章[　\s]*(.*)$")
_ARTICLE_RE = re.compile(r"^第([一二三四五六七八九十百零]+)条[　\s]*(.*)$")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> int:
    """中文数字 → int（覆盖「一」到「一百二十」这类条文编号范围）。

    只处理法规编号会出现的形态：`十`、`二十`、`二十一`、`一百`、`一百零三`。
    不追求通用（"两"、"廿"在条文编号里不会出现），**边界清楚比功能多更重要** ——
    多支持一种形态就可能把某句正文误判成条号。
    """
    s = (s or "").strip()
    if not s:
        return 0
    total, section = 0, 0
    for ch in s:
        if ch == "百":
            section = (section or 1) * 100
            total += section
            section = 0
        elif ch == "十":
            section = (section or 1) * 10
            total += section
            section = 0
        elif ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        else:
            raise ValueError(f"无法解析的中文数字：{s!r}")
    return total + section


# ==================== 取数 ====================

def download_all(force: bool = False) -> list[dict]:
    """下载全部法规原文到 `data/regulation/raw/`（已存在则跳过，除非 force）。"""
    from src import net

    out: list[dict] = []
    config.REGULATION_RAW_DIR.mkdir(parents=True, exist_ok=True)
    for doc in SOURCES:
        dest = config.REGULATION_RAW_DIR / doc["local"]
        if dest.exists() and not force:
            out.append({**doc, "path": str(dest), "bytes": dest.stat().st_size,
                        "downloaded": False})
            continue
        n = net.download(doc["url"], dest)
        out.append({**doc, "path": str(dest), "bytes": n, "downloaded": True})
    return out


def read_text(doc: dict, path: Path) -> str:
    """把 HTML / PDF 读成纯文本（html 用标签剥离，pdf 用 pymupdf）。

    HTML 分支复用 `src/ingest/html_text.strip_tags`（与网络搜索的正文抓取**同一份**）：
    两处各写一份会漂移成"同一篇 HTML 抽出不同正文"，而两边单测都各自通过。
    替换规则与原实现逐字等价，故法规索引的既有结论保持可比。
    """
    if doc["kind"] == "pdf":
        import pymupdf

        with pymupdf.open(path) as d:
            return "\n".join(page.get_text() for page in d)
    return strip_tags(path.read_bytes())


# ==================== 切分 ====================

def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _PDF_NOISE.match(line):
            continue
        strong = min((line.find(m) for m in _STRONG_CUT if m in line), default=-1)
        if strong >= 0:
            if head := line[:strong].strip():
                lines.append(head)
            break                      # 强截断之后一定是重复摘要/页脚，整段丢弃
        if line in _EXACT_NOISE:
            continue
        # 站点导航/页脚：只按"短行"判，避免把正文里恰好含这些词的句子切掉
        if len(line) <= 40 and any(m in line for m in _SUBSTR_NOISE):
            continue
        lines.append(line)
    return lines


def split_articles(text: str, doc: dict) -> list[dict]:
    """按「章 / 条」切分成 chunk（带 doc 元数据与条号）。

    解析的两条关键规则：
    - 条号可以**与正文同行**（PDF 抽取常见：`第三条信息披露义务人应当…`），
      所以正则捕获条号后的剩余部分作为正文开头；
    - 章标题可能被换行拆开（`第一章总` + `则`），所以章的剩余部分为空时
      要看后续行拼回来 —— 否则章节名会变成"总"，做章节过滤时匹配不上。
    """
    lines = _clean_lines(text)
    # 正文起点：第一个"章"或"条"。之前的是标题/公布信息/导航。
    start = 0
    for i, line in enumerate(lines):
        if _CHAPTER_RE.match(line) or _ARTICLE_RE.match(line):
            start = i
            break
    else:
        return []

    chapters: list[str] = []
    articles: list[dict] = []
    cur_chapter = ""
    cur: dict | None = None

    def flush() -> None:
        if cur and cur["body"]:
            body = "".join(cur["body"]).strip()
            cur["text"] = f"{cur['label']}　{body}" if body else cur["label"]
            articles.append(cur)

    i = start
    while i < len(lines):
        line = lines[i]
        m_ch = _CHAPTER_RE.match(line)
        m_art = _ARTICLE_RE.match(line)

        if m_ch and not m_art:
            title = m_ch.group(2).strip()
            # 章的标题有三种实测形态（同一份 PDF 里都会出现）：
            #   ① 完整同行   `第四章信息披露事务管理`
            #   ② 只剩 1~2 字 `第一章总` + `则`、`第二章定期` + `报告`
            #   ③ 完全在下一行 `第五章` / `监督管理与法律责任`
            # 判据用"位置 + 长度"而不是词表：章的下一行**必是"第X条"**，
            # 所以这个位置出现的非条/章行只可能是章标题的一部分。
            # 维护一张"哪些词是章名"的表则一定会漏（六章之外还有别文件）。
            j = i + 1
            while j < len(lines) and not _CHAPTER_RE.match(lines[j]) \
                    and not _ARTICLE_RE.match(lines[j]):
                nxt = lines[j].strip()
                if not title:
                    title, j = nxt, j + 1
                    continue
                if len(title) <= 2 and len(nxt) <= 4:
                    title, j = title + nxt, j + 1
                    continue
                break
            cur_chapter = f"第{m_ch.group(1)}章 {title}".strip()
            chapters.append(cur_chapter)
            i = j
            continue

        if m_art:
            flush()
            num = cn_to_int(m_art.group(1))
            label = f"第{m_art.group(1)}条"
            cur = {"num": num, "label": label, "chapter": cur_chapter,
                   "body": [m_art.group(2).strip()] if m_art.group(2).strip() else []}
            i += 1
            continue

        if cur is not None:
            cur["body"].append(line)
        i += 1

    flush()

    chunks: list[dict] = []
    for a in articles:
        if a["num"] <= 0:
            continue
        citation = f"《{doc['title']}》（{doc['doc_no']}）{a['label']}"
        chunks.append({
            "chunk_id": f"{doc['doc_id']}#art{a['num']}",
            "doc_id": doc["doc_id"],
            "title": doc["title"],
            "doc_no": doc["doc_no"],
            "status": doc.get("status"),
            "effective_from": doc.get("effective_from"),
            "superseded_by": doc.get("superseded_by"),
            "source_name": doc.get("source_name"),
            "url": doc.get("url"),
            "chapter": a["chapter"],
            "article_no": a["num"],
            "article_label": a["label"],
            "text": a["text"],
            # 自带 citation：法规的引用维度是「文号+条号」，与年报的模板不兼容
            "citation": citation,
        })
    return chunks


def load_corpus() -> list[dict]:
    """读本地原文 → 切分成条文 chunk（不联网）。"""
    chunks: list[dict] = []
    for doc in SOURCES:
        path = config.REGULATION_RAW_DIR / doc["local"]
        if not path.exists():
            continue
        chunks.extend(split_articles(read_text(doc, path), doc))
    return chunks


def build_index(force_download: bool = False, offline: bool = False) -> dict:
    """取数（可选）→ 切分 → 建 BM25 索引 → 落盘。返回统计。"""
    from src.retrieve.bm25 import BM25Index

    if not offline:
        download_all(force=force_download)
    chunks = load_corpus()
    if not chunks:
        raise FileNotFoundError(
            f"法规语料为空。请先 `python -m src.ingest.fetch_regulation` 下载原文"
            f"（目标目录 {config.REGULATION_RAW_DIR}）。")
    index = BM25Index.build(chunks)
    path = index.save(config.REGULATION_BM25_PATH)
    return {
        "ok": True, "path": str(path), "articles": len(chunks),
        "docs": sorted({c["doc_id"] for c in chunks}),
        "chars": sum(len(c["text"]) for c in chunks),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="取数并构建法规索引（独立于年报索引）")
    ap.add_argument("--offline", action="store_true", help="只用本地已有原文，不联网")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--stats", action="store_true", help="只看统计，不重建")
    args = ap.parse_args()

    if args.stats:
        chunks = load_corpus()
        by_doc: dict[str, int] = {}
        for c in chunks:
            by_doc[c["doc_id"]] = by_doc.get(c["doc_id"], 0) + 1
        print(json.dumps({"articles": len(chunks), "by_doc": by_doc},
                         ensure_ascii=False, indent=2))
        return
    print(json.dumps(build_index(force_download=args.force, offline=args.offline),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
