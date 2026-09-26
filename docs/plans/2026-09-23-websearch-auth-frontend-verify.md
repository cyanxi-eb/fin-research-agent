# 前端与联网搜索验收记录（2026-09-23）

对应计划：[2026-09-23-websearch-auth-frontend-plan.md](./2026-09-23-websearch-auth-frontend-plan.md)

本文件是**实测证据的落点**：批次 B 的浏览器端到端手测（B3）、批次 D 的联网实测（D1）
与最终逐条验收（D4）都写在这里。**未跑通的项如实登记为「未验证」，不打勾。**

---

## 一、批次 B · B3 浏览器端到端手测

### 1. 环境与准备

| 项 | 值 |
|---|---|
| 被测前端 | `web/index.html`（单文件，72,180 字节，零 CDN / 零构建） |
| 服务 | `.venv/Scripts/python.exe -m uvicorn src.server:app --port 8000`（本机 127.0.0.1:8000） |
| 后端状态 | `/api/health`：`index.ok=true`（chunks=3161）、`llm.ready=true`（deepseek）、`vector.available=true`、`regulation.available=true`、`frontend.available=true` |
| 鉴权形态 | `FA_AUTH_ENABLED=1`、`FA_JWT_SECRET` 与 `SEED_ADMIN_PASSWORD` 用**一次性临时值**经环境变量注入（不入源码、不入文档） |
| 数据库 | 本机 SQLite `data/db/fin_research.db` |

**临时凭据的善后（重要）**：B3 需要一个可登录的演示账号才能验证登录门面，故本次用临时
`SEED_ADMIN_PASSWORD` 让 `ensure_seed_admin()` 建出 `admin`。实测结束后**已删除该行**
（`DELETE FROM users WHERE username='admin'`，实测 `deleted_rows=1`），并已停掉这个临时服务进程。
理由：`ensure_seed_admin()` 是**幂等**的 —— 若该账号残留，用户之后配置正式
`SEED_ADMIN_PASSWORD` 重启时会命中「已存在」分支而不覆盖口令，正式口令将不生效。
所以正式演示账号仍需由用户在批次 D 前用真实口令重新建号。

### 2. 验证方式

用浏览器子代理在 127.0.0.1:8000 上逐项操作并截图。截图已复制到本仓库
`docs/plans/assets/2026-09-23-b3/`（原始文件在 `%TEMP%\trae\screenshots\`）。
全过程**未观察到浏览器控制台报错**。

### 3. 逐项结论

| # | 验证项 | 结论 | 实测证据 | 截图 |
|---|---|---|---|---|
| ① | 未登录只见登录页 | 通过 | 打开首页后仅渲染登录卡片（用户名/口令 + 登录按钮 + 「演示账号与口令请见 README」提示），左侧会话侧栏、对话区、输入框、状态条**均不可见** | [step1-login-page.png](./assets/2026-09-23-b3/step1-login-page.png) |
| ② | 演示账号登录进主界面 | 通过 | 顶栏出现「已登录：admin（admin）」与「登出」按钮；左侧会话侧栏可见；欢迎卡含 3 条可点击示例问题与「能问什么/不能问什么」边界说明 | [step2-main-interface.png](./assets/2026-09-23-b3/step2-main-interface.png) |
| ③ | 正常问题走 analysis 链路 | 通过 | 提问 `贵州茅台2024年的营业总收入是多少`：先出现骨架屏占位，随后答案含数值「1,741.44亿元」，并出现引用折叠卡片（含 公司/年份/页码/章节 chip）与「引用校验」行；点开卡片能展开明细 | [step3-answer-detail.png](./assets/2026-09-23-b3/step3-answer-detail.png)、[step3-citation-card.png](./assets/2026-09-23-b3/step3-citation-card.png) |
| ④ | 库外问题走黄色拒答 info 卡 | 通过 | 提问 `公司食堂的菜谱是什么`：出现**黄色** info 卡，标题「这不是错误 · 系统选择拒答」，**不是**红色错误卡；卡内「展开说明（1 条）」可展开 | [step4-yellow-refusal.png](./assets/2026-09-23-b3/step4-yellow-refusal.png)、[step4-expanded-detail.png](./assets/2026-09-23-b3/step4-expanded-detail.png) |
| ⑤ | 清 token 后自动刷新续期 | 通过 | `localStorage.removeItem('fa.token')`（保留 `fa.refresh`）后提问 `贵州茅台2024年年报的审计机构是哪家`：提问**成功返回答案，未被踢回登录页** —— 即 401 触发了一次 `/api/auth/refresh` 并重试成功 | [step5-answer-after-token-refresh.png](./assets/2026-09-23-b3/step5-answer-after-token-refresh.png) |
| ⑥ | 清全部 token 后跳登录页 | 通过 | 清掉 `fa.token` / `fa.refresh` / `fa.user` 后提问：页面回到登录页并提示「登录已过期，请重新登录」 | [step6-no-token-redirect.png](./assets/2026-09-23-b3/step6-no-token-redirect.png) |
| ⑦ | 登出回登录页 | 通过 | 重新登录后点顶栏「登出」：页面回到登录页（后端只留痕不吊销，与 `/api/auth/logout` 的语义一致） | 与 ①/⑥ 同形态，见 [step1-login-page.png](./assets/2026-09-23-b3/step1-login-page.png) |
| ⑧ | 深色模式 + 对比页签 | 通过 | 点顶栏主题按钮后界面变深色（背景变暗、文字变浅），按钮文案变为「☀ 浅色」；切「对比分析」页签、默认 营业总收入 / 600519,000858 点「对比」后，出现列含「公司/期次/数值/单位/来源字段」的对比表（贵州茅台、五粮液数据）与手绘趋势图（纵轴刻度 + 横轴期次 + 图例） | [step8-dark-mode.png](./assets/2026-09-23-b3/step8-dark-mode.png)、[step8-compare-result.png](./assets/2026-09-23-b3/step8-compare-result.png) |

### 4. D4 补验（2026-09-23 15:40）：免登录形态 + 趋势图提示，**并抓到并修掉一个真 bug**

上面两行原先是「未验证」。本轮补做时**在真渲染里抓到一个 bug**，故一并记录。

**① 抓到的 bug：`hidden` 属性被作者样式压过 → 登录卡片在免登录形态（以及登录成功后）仍然渲染**

- **现象**（截图 [stepA0-noauth-BEFORE-fix-login-card-leaks.png](./assets/2026-09-23-b3/stepA0-noauth-BEFORE-fix-login-card-leaks.png)）：
  以 `FA_AUTH_ENABLED=0` 起服务后打开 `/`，主界面（侧栏、对话区、输入框、状态条）**正常出现**，
  但**登录卡片没有被隐藏**，与主界面同屏叠在一起；B3 的
  [step8-compare-result.png](./assets/2026-09-23-b3/step8-compare-result.png)（鉴权形态、已登录）
  顶部那一角圆角卡片残影，其实就是它的底边 —— 当时截图是滚动状态下拍的，**漏看了**。
- **根因**：`.login-view { display: flex }`（第 345 行的**作者样式**）优先级压过 UA 样式表的
  `[hidden] { display: none }`，于是 `showApp()` 里那句 `$(ID.viewLogin).hidden = true` 只是改了属性，
  元素照旧渲染。**JS 侧完全看不出来**（`el.hidden` 确实是 `true`），只有真渲染才暴露 ——
  这也解释了为什么 `test_frontend.py` 原有的 16 条静态断言全是绿的。
- **修法**：样式表里加一条硬规则（[web/index.html:86](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/web/index.html#L86)）
  `[hidden] { display: none !important; }` —— 一行收口，任何 `display` 规则都压不过它。
- **护栏（先 RED 后 GREEN）**：新增
  [test_hidden_attribute_really_hides](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_frontend.py#L90-L105)，
  断言样式表里存在该 `!important` 规则。**改 CSS 前**跑它：
  ```
  FAILED tests/test_frontend.py::test_hidden_attribute_really_hides -
    AssertionError: 样式表缺少 `[hidden] { display: none !important; }`：任何带 display 的作者样式都会让 hidden 失效
  1 failed in 1.20s
  ```
  **改 CSS 后**：`tests/test_frontend.py` → `17 passed in 0.35s`；全量 → **438 passed, 1 warning in 85.98s**。
- **真渲染复验**（cache-busting 重载 `http://127.0.0.1:8010/?cb=7731`，原始 JSON）：
  ```json
  {"url":"http://127.0.0.1:8010/?cb=7731","src_has_rule":true,"login_hidden_attr":true,
   "login_display":"none","card_h":0,"chip":"单机模式（免登录）","sidebar_display":"flex"}
  ```
  `login_display` 由修复前的 `"flex"` 变为 `"none"`、`card_h` 由 `432` 变为 `0`；
  截图 [stepA2-noauth-AFTER-fix.png](./assets/2026-09-23-b3/stepA2-noauth-AFTER-fix.png) 里
  已看不到登录卡片，直接是主界面（欢迎卡 + 3 条示例问题 + 「能问什么/不能问什么」），
  顶栏 chip 为「单机模式（免登录）」、状态条 `auth 关闭 (单机免登录)`。

**② `AUTH_ENABLED=0` 免登录形态（B1 ⑤）→ 现已实测**

在浏览器里打开即是主界面（**不需要登录**），`#view-login` 的 `hidden=true` 且
`getComputedStyle(...).display === "none"`、`card_h === 0`；chip 文案「单机模式（免登录）」、
登出按钮 `display:none`（不显示一个点了会失败的按钮）。证据见上：原始 JSON + 修复后截图。
**本项已可打勾。**

**③ 趋势图数据点 hover 提示 → DOM 级已验，像素级截图仍取不到（如实说明）**

复验时在免登录形态下点了「对比分析」→「对比」（默认 营业总收入 / 600519,000858），原始返回值：

```
circles=12, titles_with_text=12
sample: ["贵州茅台 2020-12-31：97,993,240,501.21", "贵州茅台 2021-12-31：109,464,278,563.89",
         "贵州茅台 2022-12-31：127,553,959,355.97", "贵州茅台 2023-12-31：150,560,330,316.45"]
悬停事件返回值：贵州茅台 2020-12-31：97,993,240,501.21
对比表行数：3（表头 + 两家公司）
```

即 **12 个数据点全部挂着 `<title>` 节点、文案就是「公司 期次：数值」**，派发 `mouseover` 能取到文案，
控制台无新增 JS 报错 —— 比修复前多了一条「真渲染 + 事件」的证据。
**仍未取到像素级截图**：原生 SVG `<title>` 提示由**宿主浏览器**绘制，CDP 截图拍不到它，
这是截图能力的固有边界，不是实现缺失。所以本项**按「DOM/事件级已验证、像素级截图未取到」如实登记**。

### 5. 静态断言（B1 / B2 的 verify 命令，实测输出）

| 命令 | 结果 |
|---|---|
| B1：断言含 `api/auth/login`、`api/auth/refresh`、`localStorage`、`view-login` | `ok` |
| B2：剔除 `127.0.0.1` / `localhost` 后断言无 `https://` / `http://`，且含 `prefers-color-scheme`、`skeleton` | `ok 72180`（B2 当时读数；断言本身与字节数无关，文件后续又改过，当前为 `ok 80548`，见第四部分第 2 节第 5 条） |
| `.venv/Scripts/python.exe -m pytest tests/test_server.py -q` | `13 passed, 1 warning in 8.89s` |

---

## 二、批次 C · 联网搜索

### 1. 用例与回归（实测输出）

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest tests/test_web_search.py -q` | 通过（provider / crossvalidate / web_corpus 的纯函数与假 provider，**全程不联网**） |
| `.venv/Scripts/python.exe -m pytest tests/test_frontend.py -q` | `16 passed in 0.24s`（C 批次当时读数；D4 加入第 17 条 `[hidden]` 护栏后为 `17 passed in 0.30s`） |
| `.venv/Scripts/python.exe -m pytest -q` | `433 passed, 1 warning in 52.23s`（C 批次当时读数；D4 后 `438 passed`，第五部分 launcher 护栏再 +10 后最终 `448 passed, 1 warning in 77.81s`；v0.7.0 基线 369 → 本轮 +79 条，无既有用例因语义改动失效） |

**C6 新增前端契约用例的有效性（红→绿）**：把 `tests/test_frontend.py` 与 `web/index.html` 一起复制到临时目录
（保持 `ROOT/tests`、`ROOT/web` 相对布局，不动仓库里的真文件），做 4 处突变
（把网络卡片左边框色改成 `border-left: 3px solid #000`、删掉「已入库，下次可直接命中」、
在 `renderWebCard` 体内塞入 `var page_no = 1`、把 `body.web_search = webSearchOn()` 改成 `body.web_search = true`），
对**突变副本**跑 `tests/test_frontend.py`（2026-09-23 复跑，17 条用例版本）：

```
4 failed, 13 passed in 2.62s
FAILED ::test_web_search_flag_is_sent_with_every_question
FAILED ::test_web_card_looks_different_from_the_citation_card   AssertionError: 网络卡片没有独立的左边框色
FAILED ::test_web_card_marks_ingested_rows
FAILED ::test_web_card_keeps_the_two_citation_regimes_apart    AssertionError: 网络卡片里出现了年报口径字段：page_no
```

即在真实文件上 `17 passed` 不是"断言太松"，4 处突变各自精准打掉 1 条，其余 13 条不受影响（其中
`test_web_card_keeps_the_two_citation_regimes_apart` 守的是「网络卡片不得出现年报口径的
页码/章节」这条最容易悄悄倒退的约束）。

### 2. C6 的 verify 命令（逐字照抄计划，实测输出）

```
$ .venv/Scripts/python.exe -c "import pathlib,re; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); s=re.sub(r'http://127\.0\.0\.1|http://localhost','',h); assert 'https://' not in s and 'http://' not in s; assert 'web-card' in h and 'web_search' in h; print('ok')"
ok
```

### 3. 前端形态

`web/` 目录下**只有** `index.html` 一个文件（零 CDN / 零构建 / 断网可用），
本条由 `test_frontend_stays_a_single_html_file` 与 `test_no_external_http_resources` 持续守着。

---

## 三、批次 D · 联网实测（D1）

### 1. 本机联网能力探测（2026-09-23，实测原始结论）

| 目标 | 结果 |
|---|---|
| `https://www.baidu.com` | **OK**（227 字节） |
| `https://cn.bing.com/search?q=…` | **OK**（98,008 字节，含 10 个 `b_algo` 结果块） |
| `https://www.so.com` | **OK**（250,067 字节） |
| `https://www.sogou.com` | **OK**（21,834 字节） |
| `https://www.eastmoney.com` / `http://www.cninfo.com.cn` | **OK**（349,677 / 103,099 字节） |
| `https://duckduckgo.com` | **失败**：`ConnectTimeoutError`（重试 3 次后仍超时） |
| `https://html.duckduckgo.com/html/` | **失败**：同上，连接超时 |

### 2. 默认通道变更：`ddg` → `bing`（用户拍板，2026-09-23）

上表实测出的事实是：**本机网络下 DuckDuckGo 整体不可达（域名级阻断）**，
`ddg` 的两条实现（`duckduckgo-search` 库 / 自写 HTML 抓取）都出不了结果，
`python -m src.search.provider` 退出码为 **2**：

```
$ .venv/Scripts/python.exe -m src.search.provider "贵州茅台 2024 营业总收入"
RuntimeWarning: This package (`duckduckgo_search`) has been renamed to `ddgs`!
搜索失败：DuckDuckGoSearchException: https://www.bing.com/search?q=… return None.
exit code 2
HTML channel FAILED: RuntimeError 请求失败（重试 3 次）：
  https://html.duckduckgo.com/html/ -> ConnectTimeoutError(connect timeout=10.0)
```

**"默认免 Key"只有在对方真的连得上时才成立** —— 否则默认通道就是一个永远说"没搜到"的空壳。
经用户拍板：**新增 `bing` 通道并设为默认**（抓 `cn.bing.com/search` 结果页，零依赖、
国内可直连），`ddg` 保留为备选，`tavily` 不变。

实施中实测到两个**必须处理**的坑（都已修 + 已加断言，见下）：

| 坑 | 现象 | 处置 |
|---|---|---|
| 不加 `ensearch=1` 会**跑题** | 问"贵州茅台 2024 年营业总收入"，bing 返回的是**贵州省旅游攻略**；加 `ensearch=1` 才给出与查询相符的财经结果 | `BingProvider.search` 固定带 `ensearch=1`，并在源码里写明"这不是可选优化" |
| 结果链接是**跳转壳** | 带 `ensearch=1` 后 URL 变成 `https://www.bing.com/ck/a?...&u=a1<base64url(真实URL)>`，若不解码则 **5 条结果的域名全是 `bing.com`**，按域名去重的交叉验证将永远判"只有 1 个来源"、到不了 consistent | 新增 `_clean_bing_href()` 剥壳（`a1` 前缀 + base64url 解码）；`test_bing_unwraps_redirect_shell` 守着 |

新增/更新的用例（`tests/test_web_search.py`，**全部不联网**）：
`test_get_provider_bing_is_the_default`、`test_bing_parses_title_url_and_snippet`、
`test_bing_raises_when_structure_is_gone`、`test_bing_unwraps_redirect_shell`，
并让 `test_get_provider_unknown_backend_raises` 断言报错文案里列出 `bing`。
定向用例 `20 passed`；当时全量 **437 passed**（v0.7.0 基线 369 → +68；最终全量 **448 passed**，见第四部分第 2 节与第五部分）。

### 3. 新默认通道自检（原始输出与耗时，2026-09-23 14:22）

```
$ .venv/Scripts/python.exe -m src.search.provider "贵州茅台 2024 年营业总收入"
通道=bing 命中=5 耗时=0.68s 查询=贵州茅台 2024 年营业总收入
1. 茅台股份官网 - moutaichina.com
   https://www.moutaichina.com/mtgf/index/index.html
   moutaichina.com | 抓取于 2026-09-23 14:22:20
2. 贵州茅台 (600519)_最新价格_行情_走势图—东方财富网
   https://quote.eastmoney.com/sh600519.html
   quote.eastmoney.com | 抓取于 2026-09-23 14:22:20
3. 茅台股份官网 - moutai
   https://www.moutai.com.cn/mtgf/index/index.html
   moutai.com.cn | 抓取于 2026-09-23 14:22:20
4. 贵州茅台酒股份有限公司_百度百科
   https://baike.baidu.com/item/%E8%B4%B5%E5%B7%9E%E8%8C%85%E5%8F%B0%E9%85%92…
   baike.baidu.com | 抓取于 2026-09-23 14:22:20
exit code 0
```

**退出码 0**（有结果），命中 5 条、耗时 0.68s，域名已是**真实目标域名**（不再是 `bing.com` 壳）。

### 4. D1 逐项实测结果（2026-09-23 14:29–14:33，本机 `FA_WEB_SEARCH_BACKEND=bing`）

测试方式：`TestClient` 直接打 `/api/ask`（`web_search=true`），环境
`FA_AUTH_ENABLED=0`、`FA_WEB_SEARCH_ENABLED=1`，业务库为仓库内 SQLite。

**为什么要用「换手率」而不是「菜谱」来取一致样例**：本库的拒答三闸门里，
`model_insufficient`（模型说资料不够）**不触发**联网；要触发联网，问题里得真有
**全库从未出现**的实词。实测 `content_terms("…股息率…")` 会被切成「股息」（库里存在）
→ 不触发；而 `换手率` 保持整词且全库零出现 → 触发 `out_of_corpus`。
于是选「贵州茅台 2024 年换手率是多少」——它既**真的是年报之外的信息**（该去网上找），
又是财经站点的标准字段。

| # | 验证项 | 结论 | 实测证据 |
|---|---|---|---|
| ① | 默认通道真实返回 + 耗时 | **通过** | `python -m src.search.provider` 退出码 0、命中 5、0.68s（见上节）；节点内 `web.duration_ms=19630`、`fetched_pages=3`、`provider="bing"` |
| ② | 首答 → 二次提问命中 `corpus_cache` 且不再联网 | **通过** | 首答：`source="live"`、`ingested=true`、`duration_ms=19630`、`fetched_pages=3`；二次：`source="corpus_cache"`、`provider=null`、`fetched_pages=0`、`duration_ms=12`、`ingested=false`，`web.note="命中网络语料缓存：本地已存有与本题相关的网络来源，本次未再联网。"` |
| ③ | 「来源冲突」样例 `status="conflict"` / `ingested=false` | **未验证（未取到 conflict）** | 见「6. 未验证项与判据观察」 |
| ④ | `absent_terms` 入库前后对同一批词不变 | **通过** | 探针词表 `["换手率","菜谱","营业总收入","股息率"]`：入库**前** `['换手率','菜谱','股息率']`（`corpus_rows=0`）→ 入库**后** `['换手率','菜谱','股息率']`（`corpus_rows=5`、`corpus_domains=4`）。**逐字相同**，网络语料没有污染年报索引的"语料外实词"闸门 |

**成功后与配额留痕**：`/api/health` 的 `web` 段在入库后为
`{"enabled":true,"backend":"bing","provider_available":true,"corpus_rows":5,"corpus_domains":4,"last_ingest_at":"2026-09-23 14:33:07","quota":{"used":3,"limit":200,"remaining":197}}`
—— `used` 从 0 涨到 3，正好对应 3 次真实联网（菜谱 1 次 + 换手率首答 1 次 + 重跑首答 1 次），
**二次提问那次没有消耗配额**，这是"未再联网"的第二份独立证据。

### 5. `success_criteria` 第 2、3、4 条的实测证据（原始响应片段）

**第 2 条（consistent + ingested + 网络引用口径齐全）**：

```
[贵州茅台 2024 年换手率是多少] 20.1s refused=True reason=out_of_corpus
   attempted=True source=live provider=bing ingested=True fetched_pages=3 duration_ms=19630
   cv.status=consistent domains=4
   web.note=交叉验证一致（4 个独立域名）：已入库 5 条，下次提问可直接命中缓存。
   citations: [('moutaichina.com','2026-09-23 14:33:07',True),
               ('quote.eastmoney.com','2026-09-23 14:33:07',True),
               ('moutai.com.cn','2026-09-23 14:33:07',True),
               ('baike.baidu.com','2026-09-23 14:33:07',True)]
```

每条 `web_citations` 都带 `url`（`True`）与 `fetched_at`（`2026-09-23 14:33:07`）
—— 与年报引用（公司+年份+页码+章节）**是另一套口径**，没有混在一起。

**第 3 条（二次提问命中缓存、不再联网）**：见上表 ② 那一行。

**第 4 条（应当拒答的题不因联网就放行编造）**：

```
[公司食堂的菜谱是什么] 12.8s refused=True reason=out_of_corpus
   attempted=True source=live provider=bing ingested=False fetched_pages=3 duration_ms=5165
   cv.status=insufficient_sources domains=1
   web.note=交叉验证未通过（insufficient_sources）：独立来源不足：仅 1 个域名（要求至少 2 个）……
            本次只并列展示来源，不入库。
   answer: 问题里的「菜谱」在已入库的年报中**从未出现**，因此无法给出有出处的回答。…
```

即：**联网了（`attempted=true`）但结论没变** —— `refused=true` 保持，交叉验证不通过
（1 个独立域名 < 2）故 `ingested=false`，`web.note` 写明分歧点，回答里**没有**编造菜谱。

### 6. 未验证项与判据观察（不打勾）

| 项 | 状态 | 说明 |
|---|---|---|
| D1 ③ 「来源冲突」`status="conflict"` | **未验证** | 本轮拿到的"未通过"样例是 `insufficient_sources`（只有 1 个独立域名），**没有**取到 `conflict`。要出 `conflict` 需「≥2 个独立域名都给了数字，但数字集合**完全无交集**」，而抓的是**整页正文**（含导航、日期、JS），数字集合天然重合，实测难以自然出现。建议按"两个来源对同一指标给出不同数值"构造专门样例，或把判据从"整页数字"收窄到"指标附近的数字"——已登记为不足项（**G-24**），本轮不改判据 |
| 网络语料缓存的**命中面** | **已观察到，待改进** | `query_corpus()` 是按 BM25 命中即复用：语料只有 5 条时，问"宁德时代 2024 年的成交量"也会命中（返回的是茅台的缓存来源）。这是"缓存优先"设计的必然副作用，语料长大后自然缓解；已把 `web.note` 文案从「**该问题**此前已通过交叉验证并入库」改为「本地已存有与**本题相关**的网络来源」以免语义不实。登记为 **G-25** |

---

## 四、最终验收（D4，2026-09-23）

验收环境：本机 Windows，`.venv`（Python 3.13），`FA_AUTH_ENABLED=1` / `SEED_ADMIN_*` 取本机
`.env`（随机生成、gitignore 覆盖，未写入源码或本文档），`FA_WEB_SEARCH_BACKEND=bing`。

### 1. 三条冒烟脚本（带 token 复跑，实测退出码）

| 脚本 | 形态 | 退出码 | 关键输出 |
|---|---|---|---|
| `scripts/smoke_step6.py` | `TestClient` + lifespan + 登录换票 | **0** | `鉴权形态：enabled=True jwt_secret_configured=True users_count=1 seed_admin_present=True`；`令牌：以 admin 登录取得`；①–⑤ 全 ✅，含「无 token 访问 `/api/compare` → 401」 |
| `scripts/smoke_qa.py` | 进程内跑图（不发 HTTP） | **0** | `结论：6 条用例 → 通过 5 / 基线 1 / 失败 0`；页码核验 `命中 8 / 对不上 0 / 无法核验 0` |
| `scripts/smoke_step5.py` | 进程内跑图 + 跨进程 HITL | **0** | `✅ Step 5 冒烟全部通过`；审计快照 `by_action={ask:406, auth_login:5, auth_logout:1, auth_refresh:1, hitl_confirm:81, web_search:31}` |

命令（PowerShell，把 `.env` 的键值注入环境变量后依次执行）：

```powershell
$envFile = Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' }
foreach ($line in $envFile) { $kv = $line -split '=',2; Set-Item -Path ("Env:" + $kv[0]) -Value $kv[1] }
$env:FA_SMOKE_USER = $env:SEED_ADMIN_USER
$env:FA_SMOKE_PASSWORD = $env:SEED_ADMIN_PASSWORD
& .venv/Scripts/python.exe scripts/smoke_step6.py ; Write-Output "EXIT6=$LASTEXITCODE"
& .venv/Scripts/python.exe scripts/smoke_qa.py     ; Write-Output "EXITQA=$LASTEXITCODE"
& .venv/Scripts/python.exe scripts/smoke_step5.py  ; Write-Output "EXIT5=$LASTEXITCODE"
```

- `smoke_step6` 是**唯一直打 Bearer 鉴权层**的一条（`with TestClient(app)` 触发 lifespan；
  未配置凭据时退出码为 1 并打印「跳过鉴权端点」——不静默降级）。
- `smoke_qa` / `smoke_step5` 是进程内跑图，脚本自身会打印「不经过 Bearer 鉴权层，
  本结果不代表鉴权路径已被验证」，避免被读成"鉴权也测过了"。

### 2. 逐条对照 `success_criteria`（6 条）

**第 1 条 · `pytest -q` 全绿且用例数 ≥ 430，新增用例覆盖六个面**

```
$ .venv/Scripts/python.exe -m pytest -q
438 passed, 1 warning in 85.98s (0:01:25)
```

v0.7.0 基线 369 → **438**（+69；其中 D4 补验新增 `[hidden]` 护栏 1 条）。
后续第五部分的 launcher 鉴权就绪护栏再 **+10**，最终全量为 **448**。新增/相关用例的文件与用例数：

| 覆盖面（criteria 原文） | 文件 | 证据 |
|---|---|---|
| 用户体系与 JWT 契约 | [test_auth.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_auth.py) | 8,483 字节；pbkdf2 盐随机/校验、三异常分抛、`authenticate` 三态均回 None、seed 幂等 |
| 鉴权依赖层（非中间件） | [test_server_auth.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_server_auth.py) | 7,997 字节；无票 401 + `WWW-Authenticate`、带票 200、`/api/health` 与 login 始终公开 |
| search provider 接口 | [test_web_search.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_web_search.py#L48-L191) | `SearchResult` 字段、协议、`none` 显式禁用、tavily 缺 Key 报错、bing 默认 + 跳转壳剥壳 |
| 交叉验证判定 | 同上 [L192-L259](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_web_search.py#L192-L259) | 固定键、按域名去重、consistent / conflict / partial / insufficient_sources 四态 |
| 网络语料入库 / 命中 | 同上 [L260-L345](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_web_search.py#L260-L345) | 入库→查询往返、空库非错误、`stats` 形状、按规范化 URL 幂等、`absent_corpus_terms` 不受影响 |
| 图上的 websearch 节点与 SSE `web` 事件 | [test_streaming.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_streaming.py#L342-L408) + [test_server.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_server.py#L273-L312) | 关闭时事件序列逐字不变且 `web=None`；开启且真兜底时 `verify<web<done` 且载荷与终态同物；正常答出时**不联网**；`/api/web/search` 与自动兜底同路径、conflict 不入库 |

定向复跑：`.venv/Scripts/python.exe -m pytest tests/test_auth.py tests/test_server_auth.py tests/test_web_search.py tests/test_frontend.py -q --collect-only` → `60 tests collected`。

**第 2 条 · 库外问题在联网开启时返回 `web` 块，交叉验证结论决定入库与否** —— 通过

原始响应片段（`/api/ask`，`web_search=true`，详见第三部分第 5 节）：

```
[贵州茅台 2024 年换手率是多少] 20.1s refused=True reason=out_of_corpus
   attempted=True source=live provider=bing ingested=True fetched_pages=3 duration_ms=19630
   cv.status=consistent domains=4
   web.note=交叉验证一致（4 个独立域名）：已入库 5 条，下次提问可直接命中缓存。
   citations: [('moutaichina.com','2026-09-23 14:33:07',True), …]
```

不通过的形态（`insufficient_sources`）同样是 `ingested=false` 且在 `web.note` 写明分歧点
（见第 4 条）。`consistent` / `conflict` 两条分支另有确定性用例守着：
`test_crossvalidate_conflict_on_number_mismatch`、`test_web_search_endpoint_reports_conflict_without_ingesting`。

**第 3 条 · 二次提问命中 `corpus_cache` 且不再联网** —— 通过

```
首答：source="live"  provider=bing  fetched_pages=3  duration_ms=19630  ingested=true
二次：source="corpus_cache"  provider=null  fetched_pages=0  duration_ms=12  ingested=false
       web.note="命中网络语料缓存：本地已存有与本题相关的网络来源，本次未再联网。"
```

两份独立证据：① 节点内 `provider=null` / `fetched_pages=0`（配置层面确实没发出检索）；
② `/api/health` 的 `web.quota.used` 从 0 涨到 3，恰好对应 3 次真实联网
（菜谱 1 次 + 换手率首答 1 次 + 重跑首答 1 次）——**二次提问那次没消耗配额**。
审计侧同样有留痕：`smoke_step5` 的审计快照里 `by_action.web_search=31`。

**第 4 条 · 金标准应拒答的题不因联网就放行编造** —— 通过

```
[公司食堂的菜谱是什么] 12.8s refused=True reason=out_of_corpus
   attempted=True source=live provider=bing ingested=False fetched_pages=3
   cv.status=insufficient_sources domains=1
   web.note=交叉验证未通过（insufficient_sources）：独立来源不足：仅 1 个域名（要求至少 2 个）……
   answer: 问题里的「菜谱」在已入库的年报中**从未出现**，因此无法给出有出处的回答。…
```

即 `attempted=true`（确实去联网了）但 `refused` 仍为 true、`ingested=false`、回答里没有编造内容。

**第 5 条 · 前端仍是单文件、零 CDN，含登录/登出/401 跳转/联网开关/网络卡片** —— 通过

```
$ .venv/Scripts/python.exe -c "import pathlib,re; h=pathlib.Path('web/index.html').read_text(encoding='utf-8'); s=re.sub(r'http://127\.0\.0\.1|http://localhost','',h); assert 'https://' not in s and 'http://' not in s; assert 'web-card' in h and 'web_search' in h; assert 'api/auth/login' in h and 'api/auth/refresh' in h and 'localStorage' in h and 'view-login' in h; print('ok', len(h))"
ok 80548
```

- `web/` 目录下**只有** `index.html`（`Get-ChildItem web` → `index.html`），单文件形态成立。
- 剔除回环地址后**不含任何外部 `http(s)` 资源**（这条由 `test_no_external_http_resources` 持续守着）。
- 登录/登出/401 跳转/联网开关/网络卡片这些行为的**浏览器端到端结论**见第一部分 B3。
- 另有 17 条前端契约用例（`tests/test_frontend.py`）守着上述字样与卡片结构。

**第 6 条 · 四份文档与实现一致（版本号 / G-11 已修 / 新依赖与新环境变量齐备）** —— 通过

```
$ .venv/Scripts/python.exe -c "import pathlib,re; t=pathlib.Path('不足清单与处置方案.md').read_text(encoding='utf-8'); assert 'G-17' in t and 'G-18' in t and 'G-19' in t; r=pathlib.Path('README.md').read_text(encoding='utf-8'); assert 'api/auth/login' in r; print('ok')"
ok
$ .venv/Scripts/python.exe -c "import pathlib; t=pathlib.Path('docker-compose.yml').read_text(encoding='utf-8'); assert 'FA_JWT_SECRET' in t and 'FA_AUTH_ENABLED' in t and 'FA_WEB_SEARCH_ENABLED' in t; print('ok')"
ok
```

| 要求 | 证据 |
|---|---|
| `VERSION 0.7.0 → 0.8.0` | [server.py:68](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/src/server.py#L68) `APP_VERSION = "0.8.0"`；实测 `/api/health` 回 `"version":"0.8.0"`（smoke_step6 原始输出） |
| G-11 标记为已修 | [不足清单与处置方案.md](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/不足清单与处置方案.md) §0.7 新增 F-19/F-20/F-21；G-11 改为「审计已写入 ✅ / 鉴权 ✅（v0.8.0 已修）」并写 4 条残留 |
| 新增依赖齐备 | [requirements.txt:21-22](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/requirements.txt#L21-L22) `PyJWT>=2.8`、`duckduckgo-search>=6.0`；[EXTENSION.md:103-104](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/EXTENSION.md#L103-L104) 依赖表同项 |
| 新增环境变量齐备 | [EXTENSION.md:113-119](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/EXTENSION.md#L113-L119)（`FA_AUTH_ENABLED`/`FA_JWT_SECRET`/`SEED_ADMIN_*`/`FA_WEB_SEARCH_BACKEND` 等）；[docker-compose.yml](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/docker-compose.yml) 7 个变量均 `${VAR:-默认}` 且默认值不含真实凭据（D2 已记）；[.env.example](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/.env.example) 同项 |
| CHANGELOG 有 0.8.0 段 | [CHANGELOG.md:3](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/CHANGELOG.md#L3) `## [0.8.0] - 2026-09-23`（含破坏性说明：业务端点默认要 token） |

### 3. VM 侧容器化部署与验收（`scripts/deploy_vm.py`，2026-09-23 15:14–15:33）

本机无 Docker，故 `docker compose build` 的实跑放在 VM `192.168.57.128` 上做。口令**只从环境变量注入**
（脚本刻意不硬编码），日志里以 `***` 屏蔽：

```powershell
$env:VM_PASSWORD='***'   # 值不回显
& .venv/Scripts/python.exe scripts/deploy_vm.py --with-env-keys
```

原始输出（构建 + **六条断言**，**exit=0**）：

```
[deploy] SSH 已连接 vcvvcv@192.168.57.128:22；远端目录 /home/vcvvcv/fin-research-agent
[deploy] 清空远端项目目录（保证干净重传；--keep 可跳过）…
[deploy] 已上传 128 个文件，共 29.9 MB
[deploy] 已写 VM 侧 /home/vcvvcv/fin-research-agent/.env（权限 600）：FA_AUTH_ENABLED, FA_JWT_SECRET,
         SEED_ADMIN_USER, SEED_ADMIN_PASSWORD, FA_WEB_SEARCH_ENABLED, FA_WEB_SEARCH_BACKEND,
         QWEN_API_KEY, DEEPSEEK_API_KEY —— 值不回显
[deploy] docker compose up -d --build（长任务，读超时设 None）；宿主端口 APP_PORT=8001（容器内仍是 8000）…
… Successfully installed PyJWT-2.14.0 PyMySQL-1.2.3 … duckduckgo-search-8.1.1 … langgraph-1.2.11 …
#9 DONE 600.4s                       ← pip install 新依赖
#20 exporting layers 168.4s done
#20 unpacking to docker.io/library/fin-research-agent-app:latest 124.5s done
#20 DONE 294.0s
[stderr] Image fin-research-agent-app Building
 Image fin-research-agent-app Built
 Container fin-research-agent-web-db Running
 Container fin-research-agent Recreate
 Container fin-research-agent Recreated
 Container fin-research-agent-web-db Healthy
 Container fin-research-agent Started
[deploy] /api/health 原文：
{ "ok": true, "app": "Fin Research Agent", "version": "0.8.0", "pid": 1,
  "index": {"ok": true, "chunks": 3161, "companies": 5, "company_years": 6, "avg_tokens": 174.9},
  "regulation": {"available": true, "articles": 132, …},
  "llm": {"provider": "deepseek", "model": "deepseek-flash", "ready": true, "reason": null},
  "vector": {"available": true, "reason": "ok", "stage": "ok", "vectors": 3161, "dim": 1024,
             "model": "text-embedding-v4", "backend": "api", "companies": 5, "company_years": 6},
  "rerank": {"available": false, "backend": "passthrough", "reason": "未启用（RERANK_BACKEND=passthrough…）"},
  "checkpointer": {"backend": "mysql", "path": "web-db:3306/fin_research", "exists": true, "threads": 3},
  "hitl": {"enabled": true, "min_confidence": 0.4, …},
  "audit": {"last_error": null, "rows": 3, "by_action": {"ask": 3}},
  "auth": {"enabled": true, "jwt_secret_configured": true, "algorithm": "HS256",
           "access_ttl_min": 120, "refresh_ttl_days": 7, "min_password_len": 8,
           "seed_admin_user": "admin", "seed_admin_present": true, "users_count": 1,
           "public_paths": ["/", "/api/auth/login", "/api/auth/refresh", "/api/auth/register", "/api/health"],
           "protected_prefixes": ["/api/ask","/api/audit","/api/auth/logout","/api/auth/me",
                                  "/api/citations","/api/compare","/api/hitl"],
           "revocation": "none（无黑名单，登出靠客户端丢弃 token）"},
  "web": {"enabled": true, "backend": "bing", "provider_available": true, "provider_reason": null,
          "corpus_rows": 0, "corpus_domains": 0, "last_ingest_at": "",
          "quota": {"date": "2026-09-23", "used": 0, "limit": 200, "remaining": 200},
          "min_domains": 2, "trigger_reasons": ["low_coverage", "no_evidence", "out_of_corpus"]},
  "frontend": {"available": true, "path": "/app/web/index.html"},
  "retrieve_modes": ["bm25", "hybrid"], "retrieve_mode_default": "hybrid",
  "intents": ["analysis", "compliance", "rag"] }
[deploy] ✅ /api/health：ok=true、index/vector/regulation 可用、checkpointer.backend=mysql、auth 已启用、web 通道可用
[deploy] 已从 VM 侧 .env 读到演示账号 admin（口令不回显）
[deploy] ✅ 无票访问 /api/compare → 401（WWW-Authenticate='Bearer'）
[deploy] ✅ /api/compare 返回 ok=true 且 rows 长度 2（indicator='营业总收入'）
[deploy] ✅ /api/ask/stream 首个事件 = meta（head='event: meta\ndata: {"intent": "rag", …'）
[deploy] ✅ VM 侧联网实测：provider 自检退出码 0 —— 通道=bing 命中=5 耗时=4.68s 查询=贵州茅台 2024 年营业总收入
[deploy] ✅ 容器化验收全部通过：health 六项（含 auth/web）+ 无票 401 闸门 + /api/compare 2 行
         + /api/ask/stream 首帧 meta + VM 侧联网实测
```

容器日志侧的第二份独立证据（同一次部署，`docker compose logs app`）：

```
[entrypoint] 载入 seed 业务数据：python scripts/load_seed.py
已从 /app/seed/business.json 灌入后端 mysql
  financial_indicators 651 行 / reports 6 行 / companies 5 行
[entrypoint] 启动服务：uvicorn src.server:app --host 0.0.0.0 --port 8000
[auth] 演示账号：created（用户 admin）
INFO:     127.0.0.1:60098 - "GET /api/compare?indicator=营业总收入&codes=600519 HTTP/1.1" 401 Unauthorized
INFO:     127.0.0.1:60106 - "POST /api/auth/login HTTP/1.1" 200 OK
INFO:     127.0.0.1:60114 - "GET /api/compare?indicator=营业总收入&codes=600519,000858 HTTP/1.1" 200 OK
INFO:     127.0.0.1:38716 - "POST /api/ask/stream HTTP/1.1" 200 OK
```

要点：
- **v0.8.0 的镜像确实在 VM 上重新构建成功**（不是复用 v0.7.0 镜像）：`fin-research-agent-app:latest` 的
  构建时间戳为本次、pip 日志里出现 `PyJWT-2.14.0` / `duckduckgo-search-8.1.1` 两个新依赖；
  容器 `Recreated` → `(healthy)`。
- 容器是**在安全默认真值下**验收的：`auth.enabled=true` + `jwt_secret_configured=true` +
  `seed_admin_present=true`（口令本机现生成写入 VM 侧 `.env`，权限 600，**值不回显**）；
  「不给密钥起不来」这条设计在本次部署里被真实走到（`.env` 必须先写）。
- **无票 401 闸门在容器里也成立**：`WWW-Authenticate='Bearer'`（不只是本机 TestClient 里成立）。
- **D1 的 VM 侧联网复跑完成**：容器内 `python -m src.search.provider` 退出码 0、bing 命中 5、4.68s
  —— 与第三部分第 3 节的本机结果（0.68s / 命中 5）同形；VM 侧更慢属网络路径差异，结论一致。
- 探针一律 `docker compose exec` **进容器内**打 `127.0.0.1:8000`，故不依赖 VM 宿主端口
  （VM 的 8000 被姊妹项目占用，本次宿主端口用 `APP_PORT=8001`）。

### 4. 本轮未验证项（如实登记，不打勾）

| 项 | 状态 | 说明 |
|---|---|---|
| D1 ③ 「来源冲突」`status="conflict"` 的**联网实测** | **未实测** | 本机与 VM 取到的"不通过"形态都只有 `insufficient_sources`。该分支由**确定性用例**覆盖（`test_crossvalidate_conflict_on_number_mismatch`、`test_web_search_endpoint_reports_conflict_without_ingesting`），但"从真实网页自然抓到 conflict"这一步没做到。原因与建议见第三部分第 6 节 + 不足清单 G-24 |
| 趋势图数据点 hover 的**像素级截图** | **未验证（DOM/事件级已验）** | 无截图证据（第一部分第 4 节已记 DOM 级证据：`<title>` 节点与文本确实按数据点生成），代码层面已实现，不作「像素级已验证」结论 |
| `launcher.py --docker` / Ctrl+C 优雅回收 / `deploy_vm.py --down` | **未验证** | 本机无 Docker；容器化的 `up -d --build` 已在 VM 实跑通过（第 3 节），这三条不含在内。已登记不足清单 G-20 |

> **注**：原表中「`AUTH_ENABLED=0` 免登录形态的浏览器交互」一项**已在 D4 补验中完成**，
> 故从本表移出（浏览器实测 + 截图 `stepA2-noauth-AFTER-fix.png`，见第一部分第 4 节）。

**补充（本轮新做的 API 级实测，原「留待批次 D」那一项的一半）**：

```
$ .venv/Scripts/python.exe -c "import os; os.environ['FA_AUTH_ENABLED']='0'; os.environ['FA_WEB_SEARCH_ENABLED']='0'; from fastapi.testclient import TestClient; import src.server as s; c=TestClient(s.app); c.__enter__(); h=c.get('/api/health').json(); t=c.get('/').text; print('auth.enabled =', (h.get('auth') or {}).get('enabled')); … c.__exit__(None,None,None)"
[auth] FA_AUTH_ENABLED=0：业务端点不校验令牌（单机/单测形态）。
auth.enabled = False
web.enabled  = False
page bytes   = 80263   # 当次读数；D4 修掉 [hidden] bug 后页面为 80548
single-machine label present = True
```

即免登录形态下 `/api/health` 的 `auth.enabled=false`，页面里也确实带着「单机模式（免登录）」文案
（[web/index.html:681-683](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/web/index.html#L681-L683)）。
**该形态的浏览器渲染与交互随后在 D4 补验中实测通过**（修复前登录卡片会残留，即本形态暴露出的真 bug）。

**验收结论**：6 条 `success_criteria` 均有实测证据支撑（第 1/5/6 条为命令级绿色输出，
第 2/3/4 条为原始响应片段 + 用例守着），三条冒烟退出码全 0；容器化一侧在 VM 上**完整重建并跑通**
（第 3 节，`deploy_vm.py` exit=0，含无票 401 闸门与容器内联网实测）；上表 3 项未验证项
**如实登记、不打勾**，阻断原因是**判据难以在真实网络上自然出现**（conflict，已登记 G-24 并给出改进建议）
与**本机无 Docker**（`launcher.py --docker` / Ctrl+C / `--down` → G-20）、**像素级截图取不到**（趋势图 hover）。

---

## 第五部分 · 后续补强：`launcher.py` 鉴权就绪（2026-09-23 晚）

**触发**：用户实跑 `python launcher.py` —— `--check` 打印「预检通过」，随后 5 种启动方式
（默认 / `--mode hybrid` / `--no-browser --port 8123` / …）**全部** `Application startup failed. Exiting.`：
`FA_AUTH_ENABLED` 默认 `1` 而 `FA_JWT_SECRET` 为空 → `lifespan` 里 `require_jwt_secret()` 抛 `RuntimeError`。
根因是**预检清单里根本没有这一项**（缺陷登记 **F-23**）：安全默认本身没错，错在它把"一条命令本机启动"这条主入口
变成了必然 Traceback，且 `--check` 还先报"通过"。

**修法**（本机形态**自愈**；容器形态**只报不修** —— 部署配置不该被启动器改）：

1. 缺 `FA_JWT_SECRET` → **现生成**随机串写进 `data/db_keys.local.json`（已 gitignore，
   与 `scripts/deploy_vm.py` 给 VM 侧生成 `.env` 同一做法）；
2. 缺 `SEED_ADMIN_PASSWORD` → 一并现生成，**只在"本机新生成 + 账号不存在"时打印一次**
   （否则服务起来了也没账号可登，页面卡在登录页）；别人配的密钥/口令**一个字都不回显**，只给来源指针；
3. `--check` 增一条**只读** `✓ 鉴权：…`（不生成、不写文件）；
4. `--docker` 预检在 `docker` 可用后补"容器开了鉴权却没 `FA_JWT_SECRET`"的阻断项；
5. 新增 `--no-auth` 单机免登录开关（子进程里显式置 `FA_AUTH_ENABLED=0`）。

**护栏（先 RED 后 GREEN）**：[tests/test_launcher_auth.py](file:///c:/Users/Administrator/WorkBuddy/2026-09-21-10-27-36/projects/fin-research-agent/tests/test_launcher_auth.py) 10 条。
RED 首跑 `1 failed, 8 errors`（`AttributeError: module 'launcher' has no attribute '_seed_admin_exists'`、
`TypeError: run_precheck() got an unexpected keyword argument 'no_auth'`）→ GREEN `9 passed in 0.59s`；
收尾时补一条"预检末尾的建议命令要带上 `--no-auth`"（原先漏了，会让人以为还得配密钥）→ 最终 `10 passed in 1.53s`。

**实机端到端**（本机，从"缺密钥、无演示账号"的**干净现场**起，口令为启动器本次现生成）：

```
$ .venv/Scripts/python.exe -u launcher.py --no-browser --port 8000
[鉴权] 已启用；密钥缺失 → 本次已生成并写入 data/db_keys.local.json（已 gitignore，每台机器一份；源码里没有默认密钥）
[鉴权] 演示账号（本次现生成，只打印这一次）：admin / E4iJFEM-******
[鉴权] ↑ 已写进 data/db_keys.local.json（已 gitignore）；换台机器会另生成一份 —— 源码与文档里没有默认口令。
[启动] … -m uvicorn src.server:app --host 127.0.0.1 --port 8000  (RETRIEVE_MODE=bm25)
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
[auth] 演示账号：created（用户 admin）
[就绪] http://127.0.0.1:8000/（本窗口保持开着 = 服务运行中；Ctrl+C 停止服务）
$ GET  /api/health      → 200；auth.enabled=true jwt_secret_configured=true
                          seed_admin_present=true users_count=1 version=0.8.0
$ POST /api/auth/login（用上面那个现生成的口令）→ 200（token_len 275）
$ GET  /api/compare 带票 → 200 ok=true rows=2
$ GET  /api/compare 无票 → 401 WWW-Authenticate=Bearer
```

> 复验时把口令**截断显示**：上面这行是本次终端里的原话，但它是**一次性凭据**，
> 文档里只留前缀（与 `deploy_vm.py` 日志以 `***` 屏蔽口令同一口径）。
> 也就是说：这一段既证明了"口令会打印"，也没有把可用的口令抄进文档。

`--check` 只读成立：跑前跑后 `data/db_keys.local.json` 的键集合不变（**只读**断言由用例守着）。

**全量回归**：`.venv/Scripts/python.exe -m pytest -q` → **448 passed, 1 warning in 77.81s**（基线 369 → +79）。

**仍未验证（如实登记，不打勾）**：`--docker` 形态的**密钥阻断项**与 `--no-auth` 的容器形态
只在**预检层面**覆盖（本机无 Docker；容器化的 `up -d --build` 已在 VM 实跑通过，见第 3 节）；
`python launcher.py --docker --no-auth` 未实跑 —— 并入 **G-20**。