"""网络搜索的契约用例 —— **纯函数 + 假 provider，绝不联网**。

这一批用例要固定下来的是**接口形状与判定规则**，不是"能不能搜到东西"：

- provider 层的结构（`SearchResult` / `SearchProvider` / `get_provider` 工厂）；
- 交叉验证的**按域名去重**与四档结论（这条最关键：转载会伪装成"多来源一致"）；
- 网络语料的**独立口径**（命中必须带 url / fetched_at / source_name，
  且**不得**出现 page_no / section —— 否则前端会把网络结果当年报引用渲染）；
- 入库**幂等**（按 URL 规范化后的哈希做主键）；
- **`absent_corpus_terms` 不受网络语料影响**（并入会污染"语料外实词"闸门）。

所有用例都不碰网络：需要正文的地方直接把 `text` 灌进 `SearchResult`，
交叉验证因此不需要去抓页面（真实抓取由 D1 的联网实测覆盖）。
"""
from __future__ import annotations

import base64
import dataclasses
import pathlib

import pytest


# ==================== 夹具 ====================

@pytest.fixture
def web_env(tmp_path, monkeypatch):
    """把网络语料目录与独立索引都指到临时目录，**不碰仓库里的 data/**。"""
    from src import config

    monkeypatch.setattr(config, "WEB_CORPUS_DIR", tmp_path / "web_corpus")
    monkeypatch.setattr(config, "WEB_BM25_PATH", tmp_path / "index" / "bm25_web.pkl")
    return tmp_path


def _result(domain: str, text: str, *, title: str = "示例标题",
            path: str = "/a", fetched_at: str = "2026-09-23 15:04:05"):
    """造一条带正文的搜索结果（正文已灌好 → 交叉验证无需联网）。"""
    from src.search.provider import SearchResult

    return SearchResult(
        title=title, url=f"https://{domain}{path}", snippet=text[:40],
        source_name=domain, fetched_at=fetched_at, text=text)


# ==================== ① provider 层契约 ====================

def test_search_result_fields():
    """`SearchResult` 必须是可序列化的数据类，字段就是**网络引用口径**那四件套 + 正文。"""
    from src.search.provider import SearchResult

    r = SearchResult(title="公司公告", url="https://www.cninfo.com.cn/x",
                     snippet="摘要", source_name="巨潮资讯网",
                     fetched_at="2026-09-23 15:04:05", text="正文")
    d = dataclasses.asdict(r)
    for k in ("title", "url", "snippet", "source_name", "fetched_at"):
        assert k in d, f"SearchResult 缺字段 {k}"
    assert d["url"] == "https://www.cninfo.com.cn/x"
    # 明确的**负向**断言：网络结果不该带年报口径的页码/章节
    assert "page_no" not in d and "section" not in d


def test_search_provider_protocol():
    """`SearchProvider` 是可运行时判定的协议：有 `name` 与 `search(query, *, limit)`。"""
    from src.search.provider import SearchProvider

    class Fake:
        name = "fake"

        def search(self, query: str, *, limit: int = 5) -> list:
            return []

    assert isinstance(Fake(), SearchProvider)


def test_get_provider_none_is_explicit_disable(monkeypatch):
    """`FA_WEB_SEARCH_BACKEND=none` → `get_provider()` 返回 None（显式禁用，离线验收用）。"""
    from src import config
    from src.search import provider

    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "none")
    assert provider.get_provider() is None
    assert provider.get_provider("none") is None


def test_get_provider_ddg_available(monkeypatch):
    """ddg 通道始终可用（装了库走库，没装走自写抓取），且 `name` 为 ddg。"""
    from src import config
    from src.search import provider

    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "ddg")
    p = provider.get_provider()
    assert p is not None and p.name == "ddg"
    assert callable(p.search)


def test_get_provider_bing_is_the_default(monkeypatch):
    """默认通道是 bing（免 Key 且国内可达），未显式指定时工厂返回它。"""
    from src import config
    from src.search import provider

    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "bing")
    p = provider.get_provider()
    assert p is not None and p.name == "bing"
    assert callable(p.search)


def test_bing_parses_title_url_and_snippet(monkeypatch):
    """bing 结果页的解析规则：`b_algo` 切块 → `h2>a` 取标题与 URL → `p` 取摘要。

    用**固定 HTML 片段**而非联网，把这套正则钉死：哪天对方页面改版，
    要么这里红、要么 D1 实测红，不会出现"看起来在跑其实一条都解析不出"。
    摘要里的 `&ensp;` / `&#0183;` 必须被 unescape 掉，不能原样进引用卡片。
    """
    from src import config, net
    from src.search import provider

    page = (
        '<li class="b_algo"><div><h2><a href="https://www.cninfo.com.cn/new/a">'
        '贵州茅台 2024 年年报</a></h2><p>营业总收入&ensp;1,741.44&#0183;亿元</p></div></li>'
        '<li class="b_algo"><div><h2><a href="https://finance.sina.com.cn/b">'
        '茅台营收创新高</a></h2><p>同比增长 15.66%</p></div></li>'
    )
    monkeypatch.setattr(net, "get_text", lambda *a, **k: page)
    monkeypatch.setattr(config, "WEB_SEARCH_MAX_RESULTS", 5)

    results = provider.BingProvider().search("贵州茅台 2024 营业总收入")

    assert [r.url for r in results] == [
        "https://www.cninfo.com.cn/new/a", "https://finance.sina.com.cn/b"]
    assert results[0].title == "贵州茅台 2024 年年报"
    assert results[0].source_name == "cninfo.com.cn"
    assert results[0].snippet == "营业总收入 1,741.44·亿元"      # 实体已解、空白已并
    assert all("&ensp;" not in r.snippet and "&#0183;" not in r.snippet for r in results)
    assert all(r.fetched_at for r in results)


def test_bing_raises_when_structure_is_gone(monkeypatch):
    """页面一条 `b_algo` 都没有、又没提示"无结果" → 抛错，不静默返回空列表。"""
    from src import net
    from src.search import provider

    monkeypatch.setattr(net, "get_text", lambda *a, **k: "<html><body>改了版</body></html>")
    with pytest.raises(provider.ProviderUnavailable):
        provider.BingProvider().search("随便什么")


def test_bing_unwraps_redirect_shell():
    """`ck/a?u=a1<base64url>` 跳转壳必须剥掉。

    不剥的后果不是"链接难看"，而是**所有结果域名都变成 bing.com** ——
    按域名去重的交叉验证会判定只有一个来源、永远到不了 consistent。
    这条用例就是把这个坑钉死。
    """
    from src.search.provider import _clean_bing_href

    real = "https://quote.eastmoney.com/sh600519.html"
    enc = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    shell = "https://www.bing.com/ck/a?!&&p=abcDEF&u=a1" + enc + "&ntb=1"
    assert _clean_bing_href(shell) == real
    # 非壳链接原样返回（不带 ensearch 时 bing 给的就是直链）
    direct = "https://baike.baidu.com/item/%E8%8C%85%E5%8F%B0"
    assert _clean_bing_href(direct) == direct


def test_get_provider_tavily_without_key_raises(monkeypatch):
    """指定 tavily 但没配 Key：抛 `ProviderUnavailable`，消息里**必须写清缺哪个变量**。"""
    from src import config
    from src.search import provider

    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "tavily")
    monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "")
    with pytest.raises(provider.ProviderUnavailable) as ei:
        provider.get_provider()
    assert "TAVILY_API_KEY" in str(ei.value)


def test_get_provider_unknown_backend_raises(monkeypatch):
    """配置写错通道名不能静默回落成"能跑但结果为空"，要报错。"""
    from src import config
    from src.search import provider

    monkeypatch.setattr(config, "WEB_SEARCH_BACKEND", "no-such-channel")
    with pytest.raises(provider.ProviderUnavailable) as ei:
        provider.get_provider()
    # 报错要把**可用通道列出来**，否则用户只能猜（bing 是默认，必须出现在提示里）
    assert "bing" in str(ei.value)


# ==================== ② 交叉验证契约 ====================

def test_crossvalidate_fixed_keys_and_insufficient():
    """固定键 + 单来源 → `insufficient_sources`（一个来源谈不上"交叉"）。"""
    from src.search.crossvalidate import cross_validate

    out = cross_validate("贵州茅台2024年的营业总收入是多少",
                         [_result("a.com", "营业总收入 1741.44 亿元")])
    assert set(out) == {"status", "sources", "agree", "disagree",
                        "distinct_domains", "note"}
    assert out["status"] == "insufficient_sources"
    assert out["distinct_domains"] == 1
    assert out["note"]


def test_crossvalidate_dedupes_by_domain():
    """**同一域名的多篇转载算 1 个来源** —— 这条不守住，"交叉验证"能被转载刷成一致。"""
    from src.search.crossvalidate import cross_validate

    text = "2024年营业总收入 1741.44 亿元"
    out = cross_validate("贵州茅台2024年的营业总收入是多少", [
        _result("news.com", text, path="/a"),
        _result("news.com", text, path="/a-reprint", title="转载：同一篇"),
    ])
    assert out["distinct_domains"] == 1
    assert out["status"] == "insufficient_sources"
    assert len(out["sources"]) == 1


def test_crossvalidate_consistent_two_domains():
    """两个独立域名、关键数字一致 → `consistent`。"""
    from src.search.crossvalidate import cross_validate

    out = cross_validate("贵州茅台2024年的营业总收入是多少", [
        _result("a.com", "贵州茅台2024年营业总收入 1741.44 亿元，同比增长 15.66%。"),
        _result("b.com", "年报显示，2024年公司营业总收入为1741.44亿元。"),
    ])
    assert out["status"] == "consistent"
    assert out["distinct_domains"] == 2
    assert len(out["sources"]) == 2
    assert out["agree"]


def test_crossvalidate_conflict_on_number_mismatch():
    """两家独立来源都给了数字但**不一致** → `conflict`，且 `disagree` 要能指出分歧。"""
    from src.search.crossvalidate import cross_validate

    out = cross_validate("贵州茅台2024年的营业总收入是多少", [
        _result("a.com", "贵州茅台2024年营业总收入 1741.44 亿元。"),
        _result("b.com", "贵州茅台2024年营业总收入 1700.00 亿元。"),
    ])
    assert out["status"] == "conflict"
    assert out["distinct_domains"] == 2
    assert out["disagree"]


def test_crossvalidate_partial_when_evidence_missing():
    """两个独立域名，但只有一个真正给了关键数字 → `partial`（不算一致）。"""
    from src.search.crossvalidate import cross_validate

    out = cross_validate("贵州茅台2024年的营业总收入是多少", [
        _result("a.com", "贵州茅台2024年营业总收入 1741.44 亿元。"),
        _result("b.com", "贵州茅台公司公告目录页，请点击查看详情。"),
    ])
    assert out["status"] == "partial"
    assert out["distinct_domains"] == 2


# ==================== ③ 网络语料独立口径 ====================

def test_ingest_and_query_roundtrip(web_env):
    """入库 → 查询命中；命中的引用字段是**网络口径**，且**不得**混入年报字段。"""
    from src import config
    from src.search import web_corpus

    results = [
        _result("a.com", "贵州茅台2024年营业总收入 1741.44 亿元，同比增长 15.66%。"),
        _result("b.com", "年报显示，2024年公司营业总收入为1741.44亿元。"),
    ]
    out = web_corpus.ingest(results, status="consistent", query="贵州茅台2024年的营业总收入")
    assert out["ok"] is True and out["ingested"] == 2

    # 落盘：JSONL 一行一条 + 独立索引文件（**与年报索引、法规索引三份互不干扰**）
    jsonl = sorted(pathlib.Path(config.WEB_CORPUS_DIR).glob("*.jsonl"))
    assert jsonl, f"应在 {config.WEB_CORPUS_DIR} 下落 JSONL"
    assert pathlib.Path(config.WEB_BM25_PATH).exists()

    q = web_corpus.query_corpus("贵州茅台2024年的营业总收入", topk=5)
    assert q["ok"] is True and q["hits"], f"应命中刚入库的语料：{q}"
    assert q["source"] == "corpus_cache"
    h = q["hits"][0]
    for k in ("url", "fetched_at", "source_name"):
        assert h.get(k), f"网络引用口径缺 {k}：{h}"
    assert "page_no" not in h, "网络结果不得带年报页码"
    assert "section" not in h, "网络结果不得带年报章节"


def test_query_corpus_empty_is_not_error(web_env):
    """索引不存在 → `{ok: True, hits: [], note: "网络语料库为空"}`（**是空，不是错**）。"""
    from src.search import web_corpus

    q = web_corpus.query_corpus("贵州茅台2024年的营业总收入", topk=3)
    assert q["ok"] is True
    assert q["hits"] == []
    assert "网络语料库为空" in q["note"]


def test_stats_shape(web_env):
    """`stats()` 供 `/api/health` 用：条数 / 独立域名数 / 最近入库时间。"""
    from src.search import web_corpus

    empty = web_corpus.stats()
    assert empty["rows"] == 0 and empty["domains"] == 0

    web_corpus.ingest([_result("a.com", "营业总收入 1741.44 亿元"),
                       _result("b.com", "营业总收入 1741.44 亿元")],
                      status="consistent", query="营业总收入")
    s = web_corpus.stats()
    assert s["rows"] == 2 and s["domains"] == 2 and s["last_ingest_at"]


# ==================== ④ 入库幂等（按 URL 规范化后的哈希做主键）====================

def test_ingest_idempotent_by_normalized_url(web_env):
    """同一 URL（大小写/尾斜杠/锚点差异都算同一篇）重复 ingest 不产生第二条。"""
    from src.search import web_corpus

    first = _result("news.com", "贵州茅台2024年营业总收入 1741.44 亿元。", path="/a?x=1")
    web_corpus.ingest([first], status="consistent", query="q")
    assert web_corpus.stats()["rows"] == 1

    # 同一篇文章的"变体 URL"：大小写域名 + 尾斜杠 + 锚点
    from src.search.provider import SearchResult

    variant = SearchResult(
        title=first.title, url="HTTPS://News.COM/a/?x=1#top",
        snippet=first.snippet, source_name=first.source_name,
        fetched_at=first.fetched_at, text=first.text)
    out = web_corpus.ingest([variant], status="consistent", query="q")
    assert out["ingested"] == 0, "同一 URL 的变体不该再入一条"
    assert web_corpus.stats()["rows"] == 1


# ==================== ⑤ absent_corpus_terms 不受影响 ====================

def test_absent_corpus_terms_only_reads_annual_index(web_env, monkeypatch):
    """入库网络语料**不得**影响"语料外实词"判据（否则闸门会被网络语料污染、误放行）。

    直接钉住 `pipeline.corpus_text`（只读年报语料）这一事实：
    网络语料入库前后，同一批词的判定必须一模一样。
    """
    from src import config
    from src.retrieve import pipeline
    from src.search import web_corpus

    monkeypatch.setattr(pipeline, "corpus_text", lambda: "贵州茅台的营业收入与净利润")
    terms = ["食堂", "菜谱"]

    before = pipeline.absent_corpus_terms(terms)
    assert before == terms, "这两个词在年报语料里确实不存在"

    web_corpus.ingest(
        [_result("canteen.com", "公司食堂的菜谱：本周供应红烧肉与青菜。")],
        status="consistent", query="公司食堂的菜谱是什么")
    assert pathlib.Path(config.WEB_BM25_PATH).exists(), "网络语料确实入库了"

    after = pipeline.absent_corpus_terms(terms)
    assert after == before == terms, "网络语料不得改变年报语料的「零出现」判据"