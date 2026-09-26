"""网络搜索 provider 层 —— 可插拔通道，默认 `bing`（**免 Key**，国内可达）。

## 为什么要有这一层

"库中查不到就去网上找"这条链路里，唯一真正不确定的东西是**搜索通道**：
可能免 Key 的页面抓取被改版、可能用户想切到付费 API、也可能演示环境要完全禁止联网。
把通道收口成一个 `SearchProvider` 协议 + 一个工厂，这四件事就都变成了配置项，
而图上的兜底节点不必知道"结果是从哪来的"。

## 默认通道为什么是 bing 而不是 ddg

"默认免 Key"这句话只有在**对方真的连得上**时才成立。本项目验收机上
`duckduckgo.com` 与 `html.duckduckgo.com` 都 connect timeout（域名级阻断），
ddg 的两条实现都出不了结果 —— 于是默认通道变成了一个永远说"没搜到"的空壳。
`cn.bing.com/search` 在该网络下可直连，且 `<h2><a href>` 标题链接与 `<p>` 摘要
结构稳定可解析，所以默认改成 bing；ddg 保留为备选（海外网络更稳）。

## ddg 通道为什么是**双实现**

`duckduckgo-search` 是个可选依赖：装了就用它的解析（更稳），没装则回落到自写抓取
（`https://html.duckduckgo.com/html/`）。回落不是"锦上添花"，而是**免 Key 通道要能零依赖跑通**
这个目标的实现方式 —— 否则"默认免 Key"这句话在对方 API 变动时就变成一句空话。

## 一条硬规矩：解析失败必须抛错

自写抓取里，"页面结构已变导致一条也没解析出来"与"这个问题真的没有结果"是**两件事**：
前者静默返回空列表，表现为"联网搜索永远说没找到"；后者才是正常业务。
所以：页面上一条 `result__a` 都没有、又没出现"没有结果"的提示时，**必须抛异常**并把
"对方页面结构可能已变"写进消息里。宁可让这一次兜底失败可见，也不要让整条链路长期假死。
"""
from __future__ import annotations

import base64
import html
import re
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlparse

from src import config, net

# 抓取时间戳格式（与索引 built_at 同一形态，便于直接在报告里对比先后）
_STAMP = "%Y-%m-%d %H:%M:%S"

_BING_URL = "https://cn.bing.com/search"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"

# bing 结果页：每条结果是一个 `<li class="b_algo">` 块，块内 `<h2><a href>` 是标题链接、
# 第一个 `<p>` 是摘要。**第一个 b_algo 块不一定有 h2 锚点**（侧栏/百科卡），
# 所以切块后要逐块 search，不能假设块块都有。
_BING_BLOCK_RE = re.compile(r'<li class="b_algo"')
_BING_ANCHOR_RE = re.compile(
    r'(?is)<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>')
_BING_SNIPPET_RE = re.compile(r"(?is)<p[^>]*>(.*?)</p>")

_DDG_ANCHOR_RE = re.compile(
    r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>')
_DDG_SNIPPET_RE = re.compile(
    r'(?is)<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>')
_TAG_RE = re.compile(r"(?s)<[^>]+>")


class ProviderUnavailable(RuntimeError):
    """通道不可用：缺 Key、通道名写错、或依赖缺失且自写回落也不可用。"""


def now_stamp() -> str:
    """抓取时间（**网络引用的时间口径**：引用页面上"什么时候抓的"就靠它）。"""
    return time.strftime(_STAMP)


def domain_of(url: str) -> str:
    """取域名（去掉 `www.`）—— 交叉验证"按域名去重"就用这个键。"""
    host = (urlparse(url or "").netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class SearchResult:
    """单条搜索结果。

    前五个字段就是**网络引用口径**（title / url / source_name / fetched_at + 摘要），
    **刻意没有 page_no / section** —— 那两个字段属于年报引用，混进来会让前端
    把网络来源当年报原文渲染，而"这个数字出自哪一页"就再也核不实了。
    `text` 是抓取正文后回填的（交叉验证要读它），空串表示"还没抓"。
    """

    title: str
    url: str
    snippet: str
    source_name: str
    fetched_at: str
    text: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """搜索通道协议：一个名字 + 一次搜索。"""

    name: str

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        ...


# ==================== bing 通道（默认，免 Key，国内可达）====================

def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", fragment or "")).strip()


def _clean_text(fragment: str) -> str:
    """去标签 + **解 HTML 实体** + 合并空白。

    bing 的标题/摘要里带 `&ensp;`、`&#0183;` 这类实体与 Unicode 空白，
    不 unescape 会把 `&ensp;` 原样留在摘要里、进而在前端引用卡片上显示成乱码。
    """
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", fragment or ""))).strip()


def _clean_bing_href(href: str) -> str:
    """剥掉 bing 的跳转壳，取回真正的目标 URL。

    bing 会把结果链接包成 `https://www.bing.com/ck/a?...&u=a1<base64url(真实URL)>`
    （`ensearch=1` 模式下尤其常见）。**必须剥**：不剥的话 5 条结果的域名全是
    `bing.com`，按域名去重的交叉验证会判定"只有一个来源"、永远到不了 consistent ——
    通道看着能出结果，交叉验证却等于被废掉，这是最难察觉的一种坏。
    """
    href = (href or "").strip()
    parsed = urlparse(href)
    if "bing.com" in (parsed.netloc or "").lower() and "/ck/a" in parsed.path:
        vals = parse_qs(parsed.query).get("u")
        if vals:
            raw = vals[0]
            if raw.startswith("a1"):          # bing 在真正的 base64 前固定加 "a1"
                raw = raw[2:]
            try:
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                url = decoded.decode("utf-8", "replace")
                if url.startswith("http"):
                    return url
            except Exception:
                pass                          # 解不开就退回壳链接，由抓取阶段如实失败
    return href


class BingProvider:
    """Bing 搜索页通道（免 Key）。解析不出结构就抛错，绝不静默返回空列表。"""

    name = "bing"

    def search(self, query: str, *, limit: int | None = None) -> list[SearchResult]:
        limit = limit or config.WEB_SEARCH_MAX_RESULTS
        # `ensearch=1` **不能省**：实测不带它时，bing 对"公司名 + 指标词"这类查询会
        # 返回**完全跑题**的结果（问"贵州茅台 2024 年营业总收入"，回的是贵州省旅游攻略），
        # 带了才给出与查询相符的财经结果。这是本条通道能用的前提，不是可选优化。
        page = net.get_text(_BING_URL, params={"q": query, "ensearch": "1"},
                            timeout=config.WEB_SEARCH_TIMEOUT)
        blocks = _BING_BLOCK_RE.split(page)[1:]
        if not blocks:
            if "没有与此相关的结果" in page or "no results" in page.lower():
                return []                 # 对方明确说了"没有结果"——这是正常业务
            raise ProviderUnavailable(
                "Bing 搜索页解析为空且页面未提示「无结果」："
                "对方页面结构可能已变（未匹配到 b_algo 结果块）。"
                "可改用 FA_WEB_SEARCH_BACKEND=tavily（需 TAVILY_API_KEY）。")
        out: list[SearchResult] = []
        for seg in blocks:
            m = _BING_ANCHOR_RE.search(seg)
            if not m:
                continue                  # 没有 h2 锚点的 b_algo 块（侧栏卡）跳过即可
            url = _clean_bing_href(html.unescape(m.group(1)))
            if not url:
                continue
            sm = _BING_SNIPPET_RE.search(seg)
            out.append(SearchResult(
                title=_clean_text(m.group(2)),
                url=url,
                snippet=_clean_text(sm.group(1)) if sm else "",
                source_name=domain_of(url) or "bing",
                fetched_at=now_stamp()))
            if len(out) >= limit:
                break
        return out


# ==================== ddg 通道（备选，免 Key）====================

def _clean_ddg_href(href: str) -> str:
    """DDG 的 HTML 结果链接是跳转壳（`//duckduckgo.com/l/?uddg=<真正的 URL>`），要剥一层。"""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        vals = parse_qs(urlparse(href).query).get("uddg")
        if vals:
            return unquote(vals[0])
    return href


class DDGProvider:
    """DuckDuckGo 通道：装了 `duckduckgo-search` 用库，缺库回落自写抓取。"""

    name = "ddg"

    def search(self, query: str, *, limit: int | None = None) -> list[SearchResult]:
        limit = limit or config.WEB_SEARCH_MAX_RESULTS
        try:
            return self._search_lib(query, limit=limit)
        except ImportError:
            # 只有"库没装"才回落；库装了但在使用过程中报别的错，说明是通道问题，让它抛出去
            return self._search_html(query, limit=limit)

    def _search_lib(self, query: str, *, limit: int) -> list[SearchResult]:
        from duckduckgo_search import DDGS          # 缺库 → ImportError → 回落

        out: list[SearchResult] = []
        with DDGS() as ddgs:
            for row in ddgs.text(query, max_results=limit):
                url = str(row.get("href") or row.get("url") or "").strip()
                if not url:
                    continue
                out.append(SearchResult(
                    title=str(row.get("title") or "").strip(),
                    url=url,
                    snippet=str(row.get("body") or "").strip(),
                    source_name=domain_of(url) or "duckduckgo",
                    fetched_at=now_stamp()))
        return out

    def _search_html(self, query: str, *, limit: int) -> list[SearchResult]:
        """自写抓取（零依赖通道）。解析不出结构就抛错，绝不静默返回空列表。"""
        html = net.get_text(_DDG_HTML_URL, params={"q": query},
                            timeout=config.WEB_SEARCH_TIMEOUT)
        anchors = _DDG_ANCHOR_RE.findall(html)
        if not anchors:
            if "no results" in html.lower():
                return []                 # 对方明确说了"没有结果"——这是正常业务
            raise ProviderUnavailable(
                "DuckDuckGo HTML 解析为空且页面未提示「无结果」："
                "对方页面结构可能已变（未匹配到 result__a 块）。"
                "可改用 FA_WEB_SEARCH_BACKEND=tavily（需 TAVILY_API_KEY）。")
        snippets = _DDG_SNIPPET_RE.findall(html)
        out: list[SearchResult] = []
        for i, (href, title_html) in enumerate(anchors[:limit]):
            url = _clean_ddg_href(href)
            if not url:
                continue
            out.append(SearchResult(
                title=_strip_tags(title_html),
                url=url,
                snippet=_strip_tags(snippets[i]) if i < len(snippets) else "",
                source_name=domain_of(url) or "duckduckgo",
                fetched_at=now_stamp()))
        return out


# ==================== tavily 通道（付费，更稳）====================

class TavilyProvider:
    """Tavily 搜索 API（JSON 体入参，返回里直接带正文片段）。"""

    name = "tavily"

    def __init__(self, api_key: str):
        self._key = api_key

    def search(self, query: str, *, limit: int | None = None) -> list[SearchResult]:
        limit = limit or config.WEB_SEARCH_MAX_RESULTS
        data = net.post_json(_TAVILY_URL, {
            "api_key": self._key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        }, timeout=config.WEB_SEARCH_TIMEOUT)
        out: list[SearchResult] = []
        for row in ((data or {}).get("results") or [])[:limit]:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            out.append(SearchResult(
                title=str(row.get("title") or "").strip(),
                url=url,
                snippet=str(row.get("content") or "").strip(),
                source_name=domain_of(url) or "tavily",
                fetched_at=now_stamp()))
        return out


# ==================== 工厂 ====================

def get_provider(name: str | None = None) -> SearchProvider | None:
    """按 `FA_WEB_SEARCH_BACKEND`（或显式 `name`）取通道。

    `none` **显式返回 None**（而不是抛错）：离线验收与单测要的就是"确定不联网"，
    让调用方拿 None 去判断"要不要走网络"比让它捕获异常清楚得多。
    """
    key = (name or config.WEB_SEARCH_BACKEND or "bing").strip().lower()
    if key in ("", "none", "off", "disabled"):
        return None
    if key == "bing":
        return BingProvider()
    if key == "ddg":
        return DDGProvider()
    if key == "tavily":
        if not config.WEB_SEARCH_API_KEY:
            raise ProviderUnavailable(
                "通道 tavily 需要 API Key：请配置环境变量 TAVILY_API_KEY"
                "（或 data/llm_keys.local.json 里的 tavily 项）；"
                "也可以改用免 Key 通道 FA_WEB_SEARCH_BACKEND=bing。")
        return TavilyProvider(config.WEB_SEARCH_API_KEY)
    raise ProviderUnavailable(
        f"未知的网络搜索通道「{key}」，可选 bing / ddg / tavily / none")


def main(argv: list[str] | None = None) -> int:
    """自检入口：`python -m src.search.provider "贵州茅台 2024 营业总收入"`。

    存在的理由很具体：**联网实测要能看到原始返回与耗时**（D1 的验收要求），
    而不是只能通过"提一个问题看看前端"这种间接方式猜通道是否可用。
    """
    import argparse

    ap = argparse.ArgumentParser(description="网络搜索通道自检（联网）")
    ap.add_argument("query", help="搜索词")
    ap.add_argument("--backend", default=None, help="覆盖 FA_WEB_SEARCH_BACKEND")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    provider = get_provider(args.backend)
    if provider is None:
        print("联网搜索通道已禁用（FA_WEB_SEARCH_BACKEND=none），不发起请求。")
        return 1

    limit = args.limit or config.WEB_SEARCH_MAX_RESULTS
    t0 = time.monotonic()
    try:
        results = provider.search(args.query, limit=limit)
    except Exception as e:                       # noqa: BLE001 —— 自检要把失败原样打出来
        print(f"搜索失败：{type(e).__name__}: {e}")
        return 2
    cost = time.monotonic() - t0

    print(f"通道={provider.name} 命中={len(results)} 耗时={cost:.2f}s 查询={args.query}")
    for i, r in enumerate(results, start=1):
        print(f"{i}. {r.title}")
        print(f"   {r.url}")
        print(f"   {r.source_name} | 抓取于 {r.fetched_at}")
        if r.snippet:
            print(f"   {r.snippet[:100]}")
    return 0 if results else 3


if __name__ == "__main__":
    raise SystemExit(main())