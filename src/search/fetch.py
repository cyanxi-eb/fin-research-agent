"""网页正文抓取 —— 交叉验证的输入。

## 为什么要抓正文，而不是只看搜索结果摘要

搜索返回的 `snippet` 是**对方平台截出来的**一段话：它可能在关键数字前后就断了，
也可能把"同比增长"那半句省掉。拿摘要去做"两家来源数字是否一致"的判定，
会把"摘要恰好看不出分歧"读成"两来源一致"——**这正是最危险的一类假一致**。
所以交叉验证读的是正文；摘要只用于展示。

## 成本控制

抓正文是有明确代价的（慢、可能被限流）。两个上限：
- `config.WEB_SEARCH_MAX_PAGES`：一次兜底最多抓几页（不是"搜到几条就抓几条"）；
- `config.WEB_SEARCH_MAX_BYTES`：单页最多读多少字节（判数字一致性不需要全文）。

## Content-Type 从哪来

`net.download_meta` 会回传响应头的 Content-Type —— 不用 URL 后缀判类型，
因为重定向/网关后面 URL 常常看不出真实形态（`.html` 实际返回 PDF 的站点真的有）。
HTML 去标签与法规抽取共用 `src/ingest/html_text`（同一口径只留一份）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src import config, net
from src.ingest import html_text
from src.search.provider import SearchResult, now_stamp


def fetch_page(url: str, *, max_bytes: int | None = None) -> dict:
    """抓一个页面 → `{url, title, text, fetched_at, content_type}`。

    抓失败**不吞**：调用方（交叉验证 / 兜底节点）自己决定是"少一个来源"还是"整次失败"。
    """
    max_bytes = max_bytes or config.WEB_SEARCH_MAX_BYTES
    with tempfile.TemporaryDirectory(prefix="fa-web-") as td:
        dest = Path(td) / "page"
        _size, content_type = net.download_meta(url, dest)
        raw = dest.read_bytes()[:max_bytes]

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as doc:
            text = "\n".join(page.get_text() for page in doc)
        title = ""
    else:
        title = html_text.extract_title(raw)
        text = html_text.compact(raw)

    return {"url": url, "title": title, "text": text,
            "fetched_at": now_stamp(), "content_type": content_type or "text/html"}


def fetch_texts(results: list[SearchResult], *, max_pages: int | None = None) -> list[str]:
    """给前 `max_pages` 条结果回填正文，返回**抓取失败的说明**列表（空列表 = 全成功）。

    为什么只抓前 N 条：搜索结果是有序的，靠后的条目相关性低，
    "多抓几条以凑够两个域名"的收益远小于成本；而**凑不出两个独立域名本身就是结论**
    （`insufficient_sources`），不是需要靠多抓来掩盖的状态。

    为什么失败只记不抛：抓不到正文的结果仍然可以展示（有标题、URL、摘要），
    只是不参与"数字一致性"判定 —— 少一个来源 ≠ 整次兜底失败。
    """
    limit = config.WEB_SEARCH_MAX_PAGES if max_pages is None else max_pages
    problems: list[str] = []
    for r in results[:max(0, limit)]:
        if r.text:
            continue                      # 调用方已灌好正文（单测/缓存复用）
        try:
            page = fetch_page(r.url)
        except Exception as e:            # noqa: BLE001 —— 单个来源失败不该毁掉整次判定
            problems.append(f"{r.url} -> {type(e).__name__}: {e}")
            continue
        r.text = page["text"]
        if not r.title and page["title"]:
            r.title = page["title"]
    return problems