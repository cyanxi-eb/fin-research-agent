"""前端单文件契约用例 —— 批次 B（登录/工作台）与批次 C（联网搜索卡片）的**回归护栏**。

## 为什么前端也要有 pytest 用例

`web/index.html` 的"零 CDN、零构建、单文件"不是审美偏好，是**部署约束**：
演示环境可能断网，页面必须能离线打开。这条约束此前只在计划里用一条 shell 断言守着，
换个批次就没人再跑；写成用例之后，任何一次改版只要引入外部资源就会红。

同理，"网络来源"与"年报原文"是**两套引用口径**：网络卡片上出现页码/章节
会让它看起来像年报结论。这条是本轮（C6）最容易被悄悄破坏的地方，
所以专门提取 `renderWebCard` 的函数体做断言，而不是全文找关键字。

全部用例**只读文件**，不起服务、不联网、不依赖浏览器。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(html: str) -> str:
    """内联脚本正文（`<script>` 与 HTML 结构分开断言，避免互相误伤）。"""
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "index.html 里找不到内联 <script> 块"
    return m.group(1)


@pytest.fixture(scope="module")
def web_card_body(script: str) -> str:
    """`renderWebCard` 的函数体 —— 口径分离的断言只在这一段里做。"""
    start = script.index("function renderWebCard(")
    end = script.index("\n}\n", start)
    return script[start:end]


# ==================== 形态：单文件 + 零外部资源 ====================

def test_frontend_stays_a_single_html_file():
    """页面目录里只能有 index.html —— 出现 .css/.js 就说明"单文件"被拆了。"""
    others = sorted(p.name for p in (ROOT / "web").iterdir()
                    if p.is_file() and p.name != "index.html")
    assert others == [], f"web/ 下出现了额外的静态文件：{others}"


def test_no_external_http_resources(html):
    """零 CDN：源码里不得出现任何外部 http(s) 资源（本地回环地址除外）。

    与计划里 C6 的 verify 同口径：先把 `http://127.0.0.1` / `http://localhost`
    剥掉（它们只是本机回环，不是外链），再断言一个都不剩。
    """
    stripped = re.sub(r"http://127\.0\.0\.1|http://localhost", "", html)
    assert "https://" not in stripped, "前端引入了外部 https 资源"
    assert "http://" not in stripped, "前端引入了外部 http 资源"
    assert "<link" not in html, "引入了 <link>（外部样式/字体）"
    assert "@import" not in html, "@import 会拉取外部样式"


# ==================== 登录门面（批次 B） ====================

def test_login_gate_is_present(html, script):
    assert 'id="view-login"' in html, "缺登录页容器"
    assert "api/auth/login" in script, "登录页没有接 /api/auth/login"
    assert "showLogin" in script and "showApp" in script
    assert "body.logged-out" in html, "未登录时应隐藏工作台部件（否则登录页形同虚设）"


def test_token_refresh_and_logout_are_wired(script):
    assert "api/auth/refresh" in script, "401 后没有刷新令牌的路径"
    assert "api/auth/logout" in script, "登出没有通知服务端留痕"
    assert '"fa.token"' in script and '"fa.refresh"' in script


def test_401_falls_back_to_login_with_a_clear_message(script):
    """401 必须回登录页并说清"登录已过期"，不能只把原始响应丢给用户。"""
    assert "resp.status === 401" in script
    assert "登录已过期" in script


def test_hidden_attribute_really_hides(html):
    """`hidden` 属性必须真的能隐藏元素 —— 否则 `showApp()` 里的 `hidden = true` 是句空话。

    踩过的坑（2026-09-23，浏览器实测抓到）：`.login-view { display: flex }` 是**作者样式**，
    优先级压过 UA 样式表里的 `[hidden] { display: none }`，于是登录成功后（以及
    `FA_AUTH_ENABLED=0` 免登录形态下）登录卡片**照旧渲染**，与主界面同屏叠在一起。
    JS 那边看不出问题（`el.hidden` 确实是 true），只有真渲染才暴露。

    护栏：样式表里必须有一条作用于 `[hidden]` 的硬规则（含 !important，确保压过任何作者 display）。
    """
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    assert m, "index.html 里找不到内联 <style> 块"
    css = m.group(1)
    assert re.search(r"\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", css, re.S), (
        "样式表缺少 `[hidden] { display: none !important; }`："
        "任何带 display 的作者样式（如 .login-view{display:flex}）都会让 hidden 失效")


# ==================== 联网开关（批次 C6 ①） ====================

def test_web_search_toggle_defaults_on_and_persists(html, script):
    assert re.search(r'<input[^>]*id="web-toggle"[^>]*checked', html), "开关默认不是开"
    assert "fa.webSearch" in script, "开关状态没有落 localStorage"
    assert "applyWebSearchPref" in script, "开关没跟随服务端 config.WEB_SEARCH_ENABLED"


def test_web_search_flag_is_sent_with_every_question(script):
    """每轮提问都显式带 `web_search` —— 用户关掉必须立刻生效，不能靠服务端默认。"""
    assert "body.web_search = webSearchOn()" in script


# ==================== 网络结果卡片（批次 C6 ②③④） ====================

def test_web_sse_event_renders_into_a_separate_card(script):
    assert 'frame.event === "web"' in script, "SSE web 事件事件没有处理分支"
    assert "renderWebCard" in script


def test_web_card_looks_different_from_the_citation_card(html):
    """视觉上必须与年报引用卡片分得开：独立左边框色 + 标题写「网络检索」。"""
    assert ".web-card" in html
    assert "border-left: 3px solid var(--web)" in html, "网络卡片没有独立的左边框色"
    assert "网络检索（" in html


def test_web_card_lists_url_domain_and_fetched_at_with_safe_links(web_card_body):
    assert "source_name" in web_card_body and "fetched_at" in web_card_body
    assert 'a.target = "_blank"' in web_card_body
    assert 'a.rel = "noopener noreferrer"' in web_card_body, "外链必须带 noopener noreferrer"


def test_web_card_marks_ingested_rows(web_card_body):
    assert "已入库，下次可直接命中" in web_card_body


def test_web_card_note_is_collapsible_so_failures_stay_visible(web_card_body):
    assert 'details' in web_card_body and "web-note" in web_card_body
    assert "web.note" in web_card_body, "失败原因（note）必须能看见"


def test_web_card_keeps_the_two_citation_regimes_apart(web_card_body):
    """网络卡片里**不得**出现年报引用口径的字段（页码 / 章节）。

    这是本轮最容易悄悄倒退的一处：一旦网络来源带上页码，它看起来就像年报结论，
    "这个数字出自哪"就再也核不实了（决策 D2）。
    """
    for banned in ("page_no", "section", "页码", "章节"):
        assert banned not in web_card_body, f"网络卡片里出现了年报口径字段：{banned}"


def test_all_four_cross_validation_statuses_have_a_color(script):
    """三档色标（绿 / 黄 / 灰）必须覆盖 four 档 status —— 漏一档就会显示成默认灰。"""
    m = re.search(r"const WEB_STATUS_CLS = \{(.*?)\};", script, re.S)
    assert m, "找不到色泽映射表"
    for status in ("consistent", "partial", "conflict", "insufficient_sources"):
        assert status in m.group(1), f"漏了 {status} 的色标"


# ==================== 拒答卡片 + 联网（批次 C6 ⑤） ====================

def test_refusal_card_bridges_to_web_but_keeps_the_disclaimer(script):
    assert "年报里没有 → 网络检索结果如下" in script, "拒答卡没有把「去看网络结果」说清楚"
    assert "本系统只做原文转述" in script, "联网之后免责声明被弄丢了"


# ==================== 引用校验行（2026-09-25「核对 true 条」事故护栏） ====================

def test_verify_line_renders_counts_not_booleans(script):
    """verify 明细的类型是固定的：checked 是**布尔**、unsupported / dangling 是**数组**、
    条数在 numbers_total。前端曾把布尔直接当条数打印（「核对 true 条」），
    把数组直接转字符串（「无出处数字」后面空白）—— 必须按类型取数。"""
    assert "numbers_total" in script, "核对条数要取 numbers_total，而不是 checked 布尔"
    assert "Array.isArray" in script, "unsupported / dangling 是数组，要取 .length"


def test_verify_line_prefers_note_when_present(script):
    """拒答/故障路径的 verify 只有 note（"拒答是结论，不是待确认"）——
    此时显示 note 比显示一排「?」计数诚实得多。"""
    assert "verify.note" in script, "verify.note 存在时应优先显示说明文字"


# ==================== 数据入库向导（批次 D，Step 7 静态护栏） ====================

def test_ingest_tab_and_view_are_present(html):
    """「数据入库」页签与视图容器必须存在，且是「对话/对比分析」的同级页签。"""
    assert 'id="tab-ingest"' in html, "缺「数据入库」页签按钮"
    assert 'id="view-ingest"' in html, "缺「数据入库」视图容器"
    assert "数据入库" in html


def test_ingest_wizard_calls_all_three_endpoints(script):
    """五步向导的三次交互都要接上服务端端点：plan / preview / commit。"""
    for path in ("/api/ingest/plan", "/api/ingest/preview", "/api/ingest/commit"):
        assert path in script, f"向导没接 {path}"


def test_ingest_admin_gate_hint_is_present(script):
    """非 admin 角色必须看到「需要管理员权限」的只读提示（设计 D3 的前端落点）——
    403 时也要有对应提示，不能把人踹回登录页。"""
    assert "需要管理员权限" in script


def test_ingest_commit_reports_rows_ingested(script):
    """成功提示要报告入库格数（"已入库 N 格"），让用户知道发生了什么。"""
    assert "已入库" in script and "新数据立即可问" in script


# ==================== DOM id 自洽（防"引用尚未创建的节点"） ====================

def test_every_referenced_dom_id_exists(html, script):
    """`$(ID.x)` 引用的 id 必须真实存在于 HTML —— 空节点会让整页脚本在启动时抛错。

    唯一例外是欢迎卡：它是 `buildWelcome()` 里动态创建并赋 id 的。
    """
    id_start = html.index("const ID = {")
    # 截到表结束的 `};` 为止（自适应长度）：早年用固定 1200 字符窗口，
    # ID 表一加长就会把尾部条目截掉，"用了 ID 表里没有的键"全是误报。
    id_table_src = html[id_start:html.index("\n};", id_start)]
    table = dict(re.findall(r'(\w+):\s*"([\w-]+)"', id_table_src))
    dom_ids = set(re.findall(r'id="([\w-]+)"', html[:html.index("<script>")]))
    used = set(re.findall(r"\$\(ID\.(\w+)\)", script))

    assert used - set(table) == set(), "用了 ID 表里没有的键"
    missing = {table[k] for k in used if k in table} - dom_ids
    assert missing <= {"welcome-card"}, f"HTML 里不存在这些被引用的 id：{sorted(missing)}"
    assert "box.id = ID.welcomeCard" in script, "welcome-card 不再是动态创建的，断言需更新"