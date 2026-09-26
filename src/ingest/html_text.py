"""HTML → 纯文本 —— **法规抽取与网页抓取共用同一份**（不许两处各写一份漂移）。

为什么抽成一个模块：法规语料（`fetch_regulation.read_text`）与网络搜索的正文抓取
（`search/fetch.py`）都要做"去 script/style → 去标签 → 还原实体"这三步。
两处各写一份的直接后果是"同一篇 HTML 在两处得到不同正文"——
表现为法规条文里混进站点导航、或网络语料的数字与页面上不一样，
而两边的单测都各自通过。**同一口径只留一份**是最省事的止血方式。

刻意保持与 `fetch_regulation.read_text` 原实现**逐字等价**的替换规则：
法规索引是既有验收结论的一部分，换口径会让"法规召回"数字失去可比性。
"""
from __future__ import annotations

import re

_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style).*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
# 顺序不能反：`&amp;` 必须先还原成 `&` 之外的实体都还原完再处理（否则 `&amp;lt;` 会变成 `<`）
_ENTITIES = (("&nbsp;", " "), ("&#12288;", "　"), ("&gt;", ">"), ("&lt;", "<"),
             ("&amp;", "&"), ("&quot;", '"'))
_WS = re.compile(r"\s+")


def _as_text(raw: str | bytes) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw


def strip_tags(raw: str | bytes) -> str:
    """去标签，标签处换成换行（保留"原页面里是两行"这个信息）。"""
    text = _as_text(raw)
    text = _SCRIPT_RE.sub(" ", text)     # script/style 内容整段丢掉，不然 JS 会被当正文
    text = _TAG_RE.sub("\n", text)
    for a, b in _ENTITIES:
        text = text.replace(a, b)
    return text


def extract_title(raw: str | bytes) -> str:
    """取 `<title>`（取不到就返回空串，**不编造**）。"""
    m = _TITLE_RE.search(_as_text(raw))
    return _WS.sub(" ", m.group(1)).strip() if m else ""


def compact(raw: str | bytes) -> str:
    """去标签 + 压掉多余空白 —— 抓网页正文用（网页里导航/缩进会带出大量空行）。"""
    return _WS.sub(" ", strip_tags(raw)).strip()