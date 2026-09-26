# CHANGELOG

## [0.9.0] - 2026-09-26（数据入库向导：自然语言→需求单→预览→勾选→入库）

结构化财务库此前只能靠脚本预取，"想临时看一家新公司"就得开终端 —— 对不碰代码的使用者是断头路。
本轮把入库变成前端「数据入库」页签里的五步，并保持三条既有纪律不破：
**LLM 草稿必过既有归一**（D2）、**只抓不写两条路径分离**（D1）、**入库必须留审计痕**（D3）。

### Added

- **`src/ingest/wizard.py`**：需求单模型 `IngestPlan` + `normalize_request`（乱输入→合法空单；
  公司走 `resolve_company`、指标/比率走 `config` 既有归一，认不上的**保序**进 `unresolved` 等人改，绝不发明）+
  `parse_request`（LLM 只出 JSON 草稿，`parse_judge_json` 剥壳；LLM 不可用→空单+note，**手填路径照常可用**）+
  `preview`（**只调 `collect_company` 采集补缺，绝不落库**；指标白名单过滤；单公司失败不整单失败）+
  `commit`（按勾选三元组过滤→`save_company` 既有 upsert；companies 表顺手补档案；审计 `data_ingest`，
  batch=uuid4 只活在审计里，无批次表）。
- **6 位代码直通通道**（Step 8 浏览器手测发现的死锁修复）：向导要入库的新公司必然不在 companies 表里、
  resolve 必然认不出 —— `_SYSTEM_PLAN` 要求 LLM 优先填 6 位代码，`normalize_request` / `_resolve_plan_companies`
  对"认不出但原文是 6 位纯数字"的输入直通当抓取代码（name 暂与代码相同）；库外**名字**仍进 unresolved ——
  宁可让人改，不可猜代码。
- **防呆上限（D6）**：`INGEST_MAX_COMPANIES=5 / INGEST_MAX_INDICATORS=12 / INGEST_MAX_PERIODS=10`
  （env `FA_INGEST_MAX_*` 可调）；超出截断，unresolved 语义是"认不出"不是"太多"。
- **`src/server.py`**：`POST /api/ingest/{plan,preview,commit}`；plan/preview 挂登录依赖，**commit 挂
  `require_admin`**（`src/auth.py` 新增；403 与 401 分离 —— 401=没登录、403=不够格）；
  `AUTH_ENABLED=0` 免登录形态 `require_admin` 放行（单机形态没有"管理员"概念可校验）。
- **`src/audit.py`**：`_ACTIONS` 登记 `data_ingest`。
- **前端**（`web/index.html`，零 CDN 零构建不变）：「数据入库」页签 + 五步卡片
  （需求 textarea → 需求单表单：unresolved 标红 + **不点 LLM 也可直接手填** → 抓取预览 →
  预览网格：值+单位+来源表名+勾选框，补充源标黄/失败标红 → 确认入库，成功提示「已入库 N 格，新数据立即可问」）；
  非 admin 显示只读提示；401 走既有 refresh/跳登录逻辑。

### Fixed

- **库外新公司死锁**（本轮实测发现）：修复前「把平安银行近3年年报的营业总收入和归母净利润加进来」
  → LLM 出公司名 → companies 表查无此人 → unresolved → 预览报「未识别的公司」→ 全流程 BLOCKED。
  修复后同句直达入库（见 Added 第 2 条）。
- **commit 补档案只对库外新公司**（Step 10 代码审查发现①）：upsert 的 ON CONFLICT 会用
  excluded 覆盖 market/industry/org_id，对既有公司恒传空串等于抹档案 —— 现仅对
  companies 表里查不到的代码补档案。
- **预览白名单并入 ratios**（审查发现②）：指标与比率同时点名时，比率行（如毛利率，
  source_table=main）不再被滤掉。
- **客户端直传 code 过 6 位校验**（审查发现③）：垃圾代码回落 name 解析，不再原样拼进东财 filter。

### Tests

- `tests/test_ingest_wizard.py`（22）：归一 5（含 None/纯文本乱输入防呆）、LLM→需求单 4、
  preview **只抓不写机器证明**（行数不变 + save_company 必红）4、commit 勾选+空勾选拒绝+审计 3、
  6 位代码通道 3、审查修复回归 3（均先 RED 后 GREEN）。
- `tests/test_server_ingest.py`（5）：三端点 401、analyst 403、admin 200、免鉴权形态可用。
- `tests/test_frontend.py` 新增 4 条静态护栏；全量 **484 passed**（2026-09-26 实测 = 453 + 本轮新增 31）。

### 已知不足（登记 [不足清单](./不足清单与处置方案.md)）

- 新公司档案 `name` 暂为代码原文（东财采集器按需取列不含简称），显示「000001」而非「平安银行」。
- analyst 提交 → admin 审批的两级入库流本期未做（现形态：admin 直接入库）。

## [0.8.0] - 2026-09-23（登录鉴权 + JWT + 库外问题联网兜底 + 前端工作台级改版）

在 v0.7.0（Step 6 交付）之上做一轮"从能用变成能看"的补强，三个特性一次收口。
执行顺序刻意是**鉴权 → 前端 → 网络搜索**：鉴权改的是 `src/server.py` 的**全局依赖与端点面**，
最后做会把前端与搜索的接口改动全部返工；网络搜索风险最大（外部网络、非确定性），放最后。

三条设计主线：

1. **留痕必须可信，所以 actor 不能自报。** 审计是"合规"这个卖点的一半；而 `audit_logs.actor`
   若采信请求体自报，则等于没有留痕。于是补 `users` 表 + JWT，`actor` 一律取自 token 的 `sub`。
2. **"库里没有"只有检索后才知道 → 联网是兜底节点，不是新意图。** 拒答三闸门
   （`no_evidence` / `out_of_corpus` / `low_coverage`）已给出确定性信号，把它接到 `verify` 之后的
   `websearch` 节点即可，路由表与全部路由用例**一行不改**。
3. **两套引用口径绝不混排，网络语料绝不并入年报索引。** 网络来源给 `url` + `fetched_at`，
   年报来源给 公司+年份+页码+章节；网络语料走**独立 BM25 索引**，否则会污染 Step 3/4 全部评测数字
   与"语料外实词"判据（与法规独立索引同一理由）。

### Added

- **`src/auth.py`**：口令用标准库 `hashlib.pbkdf2_hmac('sha256', …)` 加盐慢哈希 + `hmac.compare_digest`
  恒时比较（**刻意不引 passlib/bcrypt**：已知版本不兼容，且本项目一贯"能自写就不引重依赖"）；
  **PyJWT** 签发/校验 HS256（签名的东西不自己造轮子）；access/refresh 分开，过期/篡改/类型不符抛**三种异常**；
  `authenticate()` 对"用户不存在/口令错/已禁用"**一律返回 None**（不区分，避免用户名枚举）；
  `ensure_seed_admin()` 幂等（账号已存在不覆盖口令）。
- **`users` 表**（`db.py`）：`SCHEMA_SQL`（SQLite）与 `SCHEMA_MYSQL_TABLES`（MySQL）**两处都建** +
  `upsert_user_sql()` / `get_user()` / `insert_user()`；登录入参用 JSON（**不引 python-multipart**）。
- **`src/server.py`**：`/api/auth/{register,login,refresh,logout,me}`；业务端点走 Bearer **依赖注入**
  （非中间件），`/api/health` 与 `/api/auth/login` 始终公开；`ask` 的 `actor` 取自 token；
  `/api/health` 增 `auth` 段。`scripts/make_token.py` 供登录换 token。
- **`src/search/`**：`provider.py`（可插拔通道，**默认 `bing`，免 Key、国内可达**；`ddg` 备选；
  `tavily` 需 Key）、`fetch.py`（抓正文，走 `src/net.py` 薄封装）、`crossvalidate.py`
  （**按域名去重 + 关键数字有无交集** → `consistent` / `conflict` / `insufficient_sources`）、
  `web_corpus.py`（独立网络语料区：JSONL + **独立索引** `data/index/bm25_web.pkl`）。
- **联网兜底节点** `src/graph/websearch_node.py`：先查网络语料缓存（命中即 `source="corpus_cache"`、
  **不再联网**）→ 未命中且开启时 `provider.search` → `fetch` → `cross_validate` →
  **`consistent` 才 `ingest` 并置 `ingested=true`**；任何一步异常都吞进 `web.note` 并落 `audit web_search` 留痕。
- **接口与事件**：`GET /api/web/search`（手动触发/人工验证）、`GET /api/web/corpus`（看已入库条目）；
  SSE `EVENTS` 追加 `"web"`（**仅当兜底节点实际运行**时，在 `verify` 之后、`done` 之前发一个事件）；
  `/api/citations` 的 `spec` 增 `web_citation` 一项，写明"网络来源是另一套口径"。
- **前端**（仍是 `web/index.html` **单文件、零 CDN、零构建**）：登录页（未登录只显示登录）、登出、
  401 自动 `refresh` 续期 / 无 refresh 则跳登录并提示、联网搜索开关（默认开，状态存 localStorage）、
  网络结果卡片（`web-card`，与年报引用卡片视觉区分；交叉验证三档色；标注是否已入库；`note` 可折叠）、
  会话侧栏、骨架屏、空/错态、深浅色。
- **`tests/`**：`test_auth.py` / `test_server_auth.py` / `test_web_search.py` / `test_frontend.py`。
- **`.env.example` / `docker-compose.yml`**：新增鉴权与联网搜索变量（默认值**不含任何真实口令/密钥**）。

### Changed

- **默认配置新增**：`FA_AUTH_ENABLED`（默认 `1`，安全默认）、`FA_JWT_SECRET`（空则回落
  `data/db_keys.local.json` 的 `jwt_secret`；**两者皆空且鉴权开启 → 启动即失败**）、
  `SEED_ADMIN_USER`/`SEED_ADMIN_PASSWORD`、`FA_WEB_SEARCH_ENABLED`（默认 `1`）、
  `FA_WEB_SEARCH_BACKEND`（**默认 `bing`**）、`FA_WEB_SEARCH_{MAX_RESULTS,MAX_PAGES,TIMEOUT,DAILY_QUOTA,MIN_DOMAINS}`、
  `FA_WEB_CORPUS_TOP_K`。
- **默认搜索通道由 `ddg` 改为 `bing`**：验收机上 `duckduckgo.com` / `html.duckduckgo.com` **域名级阻断**
  （connect timeout），`ddg` 两条实现都出不了结果 —— "默认免 Key"只有在对方真连得上时才成立。
  `cn.bing.com/search` 可直连；实现里**固定带 `ensearch=1`**（不带会返回跑题结果），
  并**剥掉 `bing.com/ck/a?...u=a1<base64url>` 跳转壳**（不剥则 5 条结果域名全塌成 `bing.com`，
  按域名去重的交叉验证会永远判"只有 1 个来源"）。
- `requirements.txt` 新增 `PyJWT>=2.8`、`duckduckgo-search>=6.0`（后者为 ddg 备选通道的可选依赖，
  默认通道 `bing` 零依赖抓 HTML）。
- `tests/conftest.py` 与 `FA_AUTH_ENABLED` 同一处钉 `FA_WEB_SEARCH_ENABLED=0`，保证既有用例确定性。

### Fixed

- **`hidden` 属性此前形同虚设（登录卡片残留）**：`web/index.html` 里 `[hidden]` 依赖浏览器 UA 样式表的
  `display: none`，但**作者样式**（`.login-view { display: flex }`）优先级更高 —— 于是登录成功后、
  以及 `FA_AUTH_ENABLED=0` 的免登录形态下，`el.hidden = true` 只改了属性、**元素照旧渲染**，
  登录卡片与主界面同屏。JS 层看不出来（`el.hidden` 确实是 `true`），**真渲染才暴露**。
  修法：样式表加 `[hidden] { display: none !important; }` 收口；
  护栏：`tests/test_frontend.py::test_hidden_attribute_really_hides`（先 RED 后 GREEN）。
- **`python launcher.py` 必然崩：预检说"通过"、启动却死在缺 `FA_JWT_SECRET`**。鉴权默认开（`FA_AUTH_ENABLED=1`）
  而密钥为空时 `lifespan` 里 `require_jwt_secret()` 直接抛 `RuntimeError` —— 这是**刻意的安全默认**
  （用内置默认密钥签名等于没有鉴权），但它把"一条命令本机启动"这条主入口变成了必然 Traceback；
  更糟的是 `--check` 的预检清单里**根本没有这一项**，于是先报"预检通过"、紧接着启动失败。
  修法（本机形态自愈，容器形态只报不修）：
  ① 缺密钥时**现生成**一份随机串写进 `data/db_keys.local.json`（已 gitignore，与 `deploy_vm.py` 给 VM 侧生成 `.env` 同一做法）——
  鉴权照旧开着，也不必手抄环境变量；② 连 `SEED_ADMIN_PASSWORD` 也缺就一起现生成，**只在"本机新生成 + 账号还不存在"时打印一次**
  （否则起了服务也没账号可登、页面卡在登录页）；别人配的密钥/口令**一个字都不回显**，只给来源指针；
  ③ `--check` 增一条**只读**的 `✓ 鉴权：…`（不生成、不写文件）；④ `--docker` 预检在 `docker` 可用后补上"容器开了鉴权却没 `FA_JWT_SECRET`"的阻断项；
  ⑤ 新增 `--no-auth` 单机免登录开关（预检末尾的建议命令也带上它，否则会让人以为还得配密钥）。
  护栏：`tests/test_launcher_auth.py`（10 条，先 RED 后 GREEN）。
- **启动即失败的错误提示被当成鉴权 bug**：缺 `JWT_SECRET` 的报错文案已写明"请设 `FA_JWT_SECRET`，或写入
  `data/db_keys.local.json` 的 `jwt_secret`"，但当时没有任何一条路径会真的去写那个文件 —— 现在 `launcher.py` 会。

### ⚠️ 破坏性说明

- **业务端点默认要 token**：`FA_AUTH_ENABLED` 默认 `1`，`/api/ask`、`/api/ask/stream`、`/api/compare`、
  `/api/citations`、`/api/hitl/*`、`/api/audit` 全部需要 `Authorization: Bearer <access_token>`。
  只想单机免配请显式置 `FA_AUTH_ENABLED=0`。
- **配了鉴权却不给 `FA_JWT_SECRET` → 服务启动即失败**（刻意：用默认密钥签名等于没有鉴权且外部看不出来）。
- **登出不做服务端吊销**（JWT 无状态）：`/api/auth/logout` 只留审计痕，未到期的旧 token 在 TTL 内仍有效；
  无吊销黑名单、无 RBAC 细分、无多租户隔离（见 `不足清单与处置方案.md` G-11 的残留）。

### 实测（本机，2026-09-23）

- `python -m src.search.provider "贵州茅台 2024 年营业总收入"` → 退出码 0、命中 5、耗时 0.68s，域名是真实目标域名。
- `/api/ask` 问 `贵州茅台 2024 年换手率是多少` → `attempted=true`、`cv.status=consistent`（**4 个独立域名**）、
  `ingested=true`、每条 `web_citation` 带 `url` 与 `fetched_at`；**二次提问** → `source=corpus_cache`、
  `duration_ms=12`、`fetched_pages=0`、配额不再消耗。
- 问 `公司食堂的菜谱是什么` → `refused=true` **保持**、`insufficient_sources`、`ingested=false`
  （**不因为联网就放行编造答案**）。
- `absent_terms` 在网络语料入库前后对同一批词**逐字不变**（未污染年报索引的"语料外实词"判据）。
- 全量 `pytest -q`：**448 passed, 1 warning in 77.81s**（v0.7.0 基线 369 → 本轮 +79，
  其中 10 条为 `tests/test_launcher_auth.py` 的启动器鉴权就绪护栏；既有用例语义未改）。
- **VM 侧容器化验收（2026-09-23 15:33）**：`scripts/deploy_vm.py --with-env-keys` 在 `192.168.57.128` 上
  退出码 **0** —— `docker compose up -d --build` 完整重建镜像（pip 日志含 `PyJWT-2.14.0` / `duckduckgo-search-8.1.1`），
  容器 `(healthy)`；**六条**断言全过：health 六项（含 `auth.enabled=true` / `web.provider_available=true`）、
  无票访问 `/api/compare` → **401 + `WWW-Authenticate: Bearer`**、`/api/compare` 2 行、
  `/api/ask/stream` 首帧 `meta`、容器内联网实测 `通道=bing 命中=5 耗时=4.68s`、
  容器**不该以免鉴权形态**通过验收（`auth.enabled` 必须为 true）。
- **D4 补验（浏览器真渲染，2026-09-23 15:40）**：抓到并修掉上面的 `[hidden]` bug；
  `FA_AUTH_ENABLED=0` 免登录形态实测直接进主界面（`login_display: "none"`、`card_h: 0`、
  顶栏 chip 显示「单机模式（免登录）」）；趋势图数据点提示以 DOM 级证据确认（`<title>` 节点与文本按点生成）。
- **未验证（如实登记）**：`launcher.py --docker`、Ctrl+C 优雅回收、`deploy_vm.py --down`
  → 见 `不足清单与处置方案.md` G-20；D1③「来源冲突」的联网实测（G-24）；
  趋势图 hover 的**像素级截图**（DOM/事件级已验）。原始输出见
  `docs/plans/2026-09-23-websearch-auth-frontend-verify.md` 第四部分。

## [0.7.0] - 2026-09-22（Step 6：服务化补全 + 单文件前端 + 答案级评测 + 容器化交付）

Step 5 交出的是"会分诊、会挂起"的后端；本步把它补成**前端能直接用、一条命令能装进容器**的形态，
并把评测从"检索命中"推到"答案对不对"：SSE 流式、多公司对比、多轮指代消解、单文件前端（零 CDN）、
答案级评测（自实现 faithfulness / answer_relevancy）、Docker 双容器交付。

三条设计主线：

1. **流式要可降级，且终态唯一。** 流式只是"更早看到字"，**不是新的真相来源** ——
   `done` 携带与一次性响应**同形**的完整响应，前端收到后**终态覆盖**已渲染内容，
   于是流式丢字不会变成"答案丢字"；失败（超时/异常/模型不可用）一律回落为一次性输出 + `degraded`，**绝不静默中断**。
2. **容器交付的地基是"语料随镜像走、且不被卷遮住"。** 语料放 `/app/seed`（不放 `/app/data`），
   启动时 `cp -rn` 进卷；用 `-n` 保证"用户放了真语料就以卷为准"。
3. **评测要能自证口径。** 判分失败的题计为「未判」**不进分母**；LLM 指标必须与
   「判分模型名 + 生成时间」一起标注 —— 否则两个数放一起也没法比。

### Added

- **`src/streaming.py`**：SSE 事件流。事件序 `meta` → `token`* → `citations` → `verify` →（挂起时 `hitl`）→ `done`，
  异常另有 `error`；`meta` 必带 `intent` / `route` / `thread_id` 三个键。
  **数值（analysis）/ 合规（compliance）两条意图不流式**（答案本就是确定性拼装、无 token 可流）；
  流式失败回落为一次性 `token` + `done` 且 `done.response.degraded=true`、`notes` 写明原因。
- **`src/compare.py`**：多公司同指标对比。返回键固定
  `{ok, indicator, unit, rows, periods_consistent, chart, note}`；**复用已注册工具**
  `compare_companies` / `get_financial_indicator`，**不另写取数逻辑**（否则同一指标会有两套口径）。
- **`src/judge.py`**：自实现 faithfulness / answer_relevancy（与 RAGAS 同口径），**不引 ragas 重依赖**；
  另含 `numeric_hit`（数值判定：单位换算 / 千分位 / 符号）。
- **`web/index.html`**：单文件前端（内联 CSS/JS，**零外部 CDN、零构建步骤**），五项能力：对话（SSE + 终态覆盖）、
  引用卡片（原样展示 `citation` + 回源核对，无链接则按钮置灰）、对比表 + 手绘内联 SVG 折线图、
  越界拒答无引用、HITL 挂起确认面板（409 提示措辞为「这是待确认、不是错误」）。
- **`Dockerfile` / `.dockerignore` / `docker-compose.yml` / `entrypoint.sh`**：app + MySQL 双容器交付。
- **`launcher.py`**：一键启动。`--port`（默认 8000）/ `--host` / `--mode bm25|hybrid` / `--no-browser` / `--check` / `--docker`；
  预检 venv / `data/index/bm25.pkl` / 业务库（任一缺就给**可操作**指引，如「先跑 scripts/ingest_all.py」而不是「文件不存在」）；
  用 `urllib` 探 `/api/health`，已在运行就直接开浏览器（不重复起服务），否则起 `uvicorn` 子进程、就绪后 `webbrowser.open`。
- **`seed/`**：容器启动用的语料副本（`business.json` + `data/{parsed,index,vector,regulation}`），**必须与 seed 一起上传**。
- **`scripts/`**：`export_seed.py`（`--out`）、`load_seed.py`（`--seed` / `--dry-run`）、`smoke_step6.py`、
  `eval_rag.py`（`--judge`）、`deploy_vm.py`（`--verify-only` / `--with-env-keys`）、`vm_ssh.py`。
- **`tests/`**：`test_streaming.py` / `test_compare.py` / `test_history.py` / `test_eval_metrics.py` / `test_seed_db.py`。
- **`eval/report_answer.md` / `eval/report_answer.json`**：答案级评测报告与逐题原始分。
- **`.env.example`** 补 compose 相关键（`FA_DB_BACKEND` / `MYSQL_*` / `APP_PORT` 等）。

### Changed

- **`src/server.py`**：新增 `POST /api/ask/stream`（`media_type="text/event-stream"`）、`GET|POST /api/compare`、
  `GET /`（`FileResponse` 返 `web/index.html`）、`web/` 用 `StaticFiles` 挂到 `/static`；
  `/api/health` 增报 `frontend` / `retrieve_mode_default`。Step 5 的 `/api/citations`、
  `/api/hitl/{tid}/confirm`、`/api/audit` 继续用。
- **`src/retrieve/filters.py`**：新增 `resolve_entities` —— 多轮指代消解 5 条规则（唯一历史才沿用 /
  本轮显式实体优先 / 不唯一就不猜 / 年份不跨公司沿用 / 只保留最近 `FA_HISTORY_MAX` 轮）。
- **`docker-compose.yml`**：端口改可覆盖的 `${APP_PORT:-8000}:8000`（**容器内始终监听 8000**，只有宿主映射变）。
- **`scripts/deploy_vm.py`**：新增 `--with-env-keys`（**默认关闭**）—— 把本机 `data/llm_keys.local.json`
  的非空 Key 写成 VM 侧 `.env`（权限 600），**只打印键名与条数、绝不回显 Key 值**；
  不注 Key 时容器裸起，向量通道按设计降级为纯 BM25（`vector.available=false` 且 health 里明说原因）。
- 配置：新增环境变量 `FA_HISTORY_MAX`（多轮上下文保留轮数，默认 5）、`APP_PORT`、`MYSQL_*`、`FA_DB_BACKEND`。
- **`README.md` / `CHANGELOG.md` / `EXTENSION.md` / 不足清单** 四份文档收尾到 v0.7.0。

### Fixed / 踩过的坑

容器类（体例同前：现象 + 根因 + 解法）：

- **语料 seed 必须放 `/app/seed`，不能放 `/app/data`。** 现象：镜像里明明带了语料，容器起来库却是空的。
  根因：compose 给 `/app/data` 挂卷时，**卷会遮住镜像里同一路径的内容**（姊妹项目踩过）。
  解法：语料放 `/app/seed`，`entrypoint.sh` 里 `cp -rn /app/seed/data/. /app/data/` —— `-n` **不覆盖**，
  用户挂卷放了真语料就以卷为准。
- **启动顺序必须"响亮地失败"。** `entrypoint.sh`：复制 seed → 等 MySQL 就绪（socket 循环，最多 60s，
  超时**明确报错退出 1**、不无声继续）→ `python scripts/init_db.py` → `python scripts/load_seed.py` → `exec "$@"`。
- **MySQL Checkpointer 两个必要条件：`autocommit=True` + 显式调 `saver.setup()`。** 缺一个就建不出表
  （表现为"挂起能恢复"这个承诺悄悄失效）。
- **`PyMySQL[rsa]`**：认证插件要 rsa，不带 extra 会连不上。
- **`--default-time-zone=+08:00` 要写进 mysqld 的 command 参数，不要用 `TZ` 环境变量**：
  后者会让 MySQL 初始化慢十几倍。
- **MySQL 卷只在首次创建时初始化**：改了 `MYSQL_*` 必须 `docker compose down -v` 才会重新初始化，
  否则会把"改了没生效"误判成别的问题。
- **slim 镜像里没有 curl**：`HEALTHCHECK` 用 `python -c` 打 `/api/health`，不为此装 apt 包。
- **`.dockerignore` 与 `.gitignore` 是两套规则；且 Docker 的忽略规则锚定在构建上下文根、单个 `*` 不跨 `/`。**
  现象：裸写 `*.local.json` 挡不住 `data/db_keys.local.json`（`*` 不跨 `/`）。
  解法：写 `**/*.local.json`。别以为 `.gitignore` 里能用 `.dockerignore` 就能用。
- **VM 内存只有 3.8G（另有约 3.9G swap）**：给 app `mem_limit: 1200m` / web-db `mem_limit: 900m` 护栏，
  MySQL 另加 `--innodb-buffer-pool-size=128M` 与 `--performance-schema=OFF`（省 100~200MB）。
  护栏**刻意给得比实际需要宽** —— 它防的是"某进程失控拖死宿主机"，不是挤压正常运行
  （`mem_limit` 顶到会**直接 OOM-kill 容器**，而本 VM 有约 3G swap，真紧张时宁可换页慢一点也不要被杀）。
- **VM 加速器域名 NXDOMAIN，基础镜像拉不动。** 现象：`docker pull python:3.13-slim` 拉不到。
  根因：VM 的 `daemon.json` 里两个加速器域名 `mirror.baidubce.com` / `hub-mirror.c.163.com`
  都已 **NXDOMAIN**，而 `registry-1.docker.io` 只解析出 **IPv6 无路由**。
  解法：`docker pull docker.m.daocloud.io/library/python:3.13-slim`（实测该域名返回 **401、可达**）再
  `docker tag ... python:3.13-slim`。**未改 VM 的 `daemon.json`、未重启 docker** ——
  重启会把 Dify 13 个容器 + workflow-agent 全弹起来，属于动别人的栈。

前端 / 账户级：

- **SSE 事件顺序与"终态覆盖"。** 现象：流式渲染中途断流/丢字，页面显示出残缺答案。
  根因：把流式分片当成了真相来源。解法：**以 `done.response` 为准做终态覆盖** ——
  流式只是"更早看到字"，不是真相来源。
- **流式与一次性输出必须同形。** 否则前端要写两套渲染，且两边会漂移。`done` 直接带完整响应。
- **前端零 CDN 的取舍。** 单文件、内联 CSS/JS、零构建；图表手绘内联 SVG 而不引图表库 ——
  不引入一个"必须联网才能渲染"的依赖（引 ECharts / Chart.js 会带来几百 KB 外部依赖和构建配置）。

评测类：

- **`judge._signed_values` 把开头数字判成负数。** 现象：正值判错、负值判对。
  根因：负号判定写成 `prev in "-−–"`，而 `prev = ""` 时**空串是任意串的子串 → 恒为 True**，
  于是任何以数字开头的答案，首个数字都被取负。解法：改用**元组成员判断** `prev in ("-", "−", "–")`，
  并补**两条断言**守住这个陷阱。
- 口径：判分失败的题计为「未判」并单列计数、**不进分母**（本次 45 题全部判成，未判 0 题）——
  把未判当 0 分会凭空压低指标，直接丢掉又不留痕。

工具链：

- **PowerShell 引号陷阱。** 现象：计划里 D5 的 verify 断言含 `'exec \"$@\"'`，本机**必然假失败**
  （`SyntaxError: unterminated string literal`）。根因：PowerShell 传参时 `"` 会被吞掉。
  解法：换等价写法（如判 `exec` 与 `$@` 分别存在），不要用"看起来一样"的字符串硬比对。
- **Python 输出缓冲。** 现象：后台 job 的日志"卡住不动"。根因：非 TTY 时 stdout 全缓冲，
  日志只在进程结束时才 flush。解法：`-u` 或 `PYTHONUNBUFFERED=1` 才能实时看进度。

### 实测结果（VM 192.168.57.128 / Ubuntu 22.04.4 / Docker 29.1.3 + 真实模型 deepseek-flash）

答案级评测（`eval/report_answer.md`，生成时间 **2026-09-23 08:34:28**，本次耗时 823s，**判分模型 `deepseek-flash`**）：

| 指标 | 值 | 分母（口径） |
|---|---|---|
| faithfulness（忠实度） | 20.3% | 45 题（`answer_expect.judge=true`；未判 0 题） |
| answer_relevancy（答案相关性） | 83.6% | 45 题 |
| 数值准确率 | **100.0%** | 31 题（`gt.type==indicator`） |
| 引用命中率 | 20.0% | 40 题（`expected_pages` 非空） |

- **分桶读法**：有检索上下文（literal/article）14 题 → faithfulness 65.4% / answer_relevancy 47.5%；
  无检索上下文（indicator，走工具层）31 题 → faithfulness 0.0% / answer_relevancy 99.8%。
  **faithfulness 头条数字被 31 道无数值上下文题拉低，数值题的正确性看「数值准确率」（100.0%）。**
  辅助：证据完整性 63.3%（30 题）；路由命中率 91.1%（45 题）。样本量：数值题 1 题 = 3.2pp，LLM 指标 1 题 = 2.2pp。
- **⚠️ 答案级指标与本轮语料绑定，重入库后必须重跑**（沿用 G-15 的固化流程：
  `ingest_all --force` → `build_golden.py` → `eval_retrieval.py`，答案级再加 `eval_rag.py --judge`）。
  指标必须与「判分模型名 + 生成时间」一起读：**同模型重跑有 1~3pp 抖动**
  （实测同一 55 题两轮：23.7%/82.3% → 25.1%/79.8%）。

VM 上 `/api/health` 实测（节选）：`index.ok=true, chunks=3161, companies=5, company_years=6`；
`vector.available=true`（3161 向量 × dim 1024，`text-embedding-v4`）；`regulation.available=true`
（132 条 = 182 号 65 + 226 号 67）；`checkpointer.backend="mysql"`、`exists=true`；`frontend.available=true`；
`retrieve_mode_default="hybrid"`。闸门 `python scripts/deploy_vm.py --verify-only` 退出码 **0**
（断言 `/api/health` 四通道全 `ok` 且 `checkpointer.backend="mysql"`、`/api/compare` 返回 2 行、`/api/ask/stream` 首帧为 `meta`）。

容器构建与镜像：`docker compose up -d --build` 冷构建 ≈ **1478s**（`pip install -r requirements.txt` 1268.4s +
导出镜像层 212.2s），改 env 后热重建导出只 **9.4s**（依赖层全 `CACHED`）；
镜像权威口径 **176,676,355 B ≈ 168.5 MiB**（`docker image inspect`），`docker images` 表里
CONTENT SIZE **177 MB** / DISK USAGE **710 MB**（Docker 29 新口径，含 attestation / 未压缩层摊算）。
容器占用 app 222MiB / 1.172GiB（18.50%）、web-db 71MiB / 900MiB（7.91%）；宿主内存 available **1079M**、
swap 空闲 2967M、磁盘可用 **16G**；上传 **117 个文件 / 29.6 MB**；容器内实测载入
`companies 5 / reports 6 / financial_indicators 651`。

检索级评测（55 题，与 0.5.0 的 34 题旧值并列，同一脚本、不调模型）：

| 配置 | 0.5.0（34 题，旧值） | 0.7.0（55 题，新值） |
|---|---|---|
| ① bm25（无过滤） | 24.0% | 32.4% |
| ② bm25 + 自动过滤 | 40.0% | **54.0%** |
| ③ + 每页配额 | 40.0% | 54.0% |
| ④ hybrid + 过滤 + 配额 | 48.0% | 54.0% |
| ⑤ hybrid + rerank | 40.0% | 43.2% |

- 年份精度 84.1%→100%；公司精度 52.4%→100%；拒答正确率 40%→80%；空召回 0。
- **评测集从 34 扩到 55，分母变了，所以与 0.5.0/0.6.0 不可直接比**（页级金标准 25 → 37）。
- 归因：①→② 只加自动元数据过滤 **+21.6pp**（年份 +15.6pp / 公司 +44.9pp）；④→⑤ 只加重排 **−10.8pp**
  （重排确实生效：**51/55** 题 top5 顺序被改变，丢 7 题、救回 3 题）。

测试：307 → **369 例**（新增 62 例：SSE 契约 / compare / 多轮指代 / 答案级判定 / seed 载入），
`pytest -q` → **369 passed, 1 warning**（54.11s）。该 warning 是 starlette 的 `anyio.abc.BlockingPortal`
弃用告警，与项目代码无关。

### 已知不足（详见 `不足清单与处置方案.md`）

- **4 道法规题被路由成 `rag`**（`src/graph/router.py` 的 `_COMPLIANCE_CUES` 未覆盖这些问法）——
  未修，属真实缺口；连带影响是这些法规题走年报检索、拿不到条文级引用。
- **引用命中率 20.0% 的分母含 30 道数值题**：数值题走工具层、`citations` 恒为空，**结构上不可能命中页码**，
  所以这个数不是「引用质量」的干净读数（未收窄分母，因为那会让分母随题型构成漂移、与历史口径不可比）。
- **SSE 未覆盖 `compliance` / `analysis` 两条意图的"流式"** —— 这两条答案本身是确定性拼装，没有 token 可流。
- `launcher.py --docker` 分支、Ctrl+C 优雅回收、`deploy_vm.py --down` **未在真机验证**（本机无 Docker）。
- 接口鉴权仍未做（演示用，已登记为 ⏸ 不修）。

## [0.6.0] - 2026-09-22（Step 5：完整编排 —— 路由分诊 + 引用校验 + HITL，且数值题不许编）

本步把前四步的零件拼成一台**会分诊、会自检、会挂起等人**的机器：
`规则路由 → {RAG | 数值分析 | 合规} 三条子图 → verify 校验 → (HITL 挂起) → finalize`，
外挂 SQLite Checkpointer（挂起可跨进程恢复）与审计留痕。

三条设计主线：

1. **分诊用规则、不用模型。** `compliance_cue` 优先于 `numeric_cue+indicator`，兜底 RAG。
   规则路由是**可解释、可回归**的：一条判定错了能一眼看出是哪条 cue 命中的，
   而"让模型选意图"错了只能靠调提示词反复试。
2. **数值题的零幻觉是结构性的，不是提示词求来的。** 数字只能来自只读工具层
   （`registry.dispatch`，每项自带 `来源=表.字段`），组句是**确定性拼装、不经模型**，
   答案里的每个数字再回归检查一遍。模型在这条链上**没有机会编造数字** —— 它没参与写数字。
3. **合规只给条文、不给结论。** 法规独立建索引（条级切分，引用带 `doc_no/status`），
   显式标注版本适用期（2025-07-01 起适用 226 号，此前 182 号），不对个案下合规判断。

### Added

- **`src/graph/checkpoint.py`**：SQLite Checkpointer 工厂。`check_same_thread=False`
  （FastAPI 线程池里必须）、进程内单例复用、独立落盘 `data/db/checkpoints.db`；
  未知后端**直接报错不静默降级** —— 降级成内存 saver 会让"挂起能恢复"这个承诺悄悄失效。
- **`src/numeric.py`**：数字抽取 / 单位换算（元↔亿元↔万元↔千元↔百元）/ 容差比对
  （`max(0.1% 相对, 0.005 绝对)`）/ `unsupported_numbers`（答案里找不到出处的数字）。
- **`src/graph/subgraph_analysis.py`**：数值分析子图。认不出公司/指标就**不调工具、不给数字**，
  改列"可查范围" —— 边界要显式，不能猜。
- **`src/graph/subgraph_compliance.py`**：合规子图，只查法规库、给条文原文、带版本提示。
- **`src/graph/verify.py`**：校验 + HITL 判定。证据集合**按意图分流**
  （analysis→`tool_results`、compliance→`regulation_hits`、rag→引用片段原文）；
  HITL 优先级 numeric > citation > low_conf。
- **`src/ingest/fetch_regulation.py`** + **`src/retrieve/regulation.py`**：
  法规入库与独立检索。226 号 67 条 + 182 号 65 条 = **132 条**；
  `prefer_current` 让现行版排前（仅正文不同才带出废止版）。
- **`src/audit.py`**：审计留痕（`ask / hitl_confirm`），**永不抛异常** ——
  留痕是旁路，不能因为它自身失败把主流程带塌。
- **`scripts/smoke_step5.py`**：三路由 + HITL **双真子进程**演练（状态只来自 `checkpoints.db`）。

### Changed

- **`src/graph/state.py`**：新增 `tool_results / regulation_hits / verify / force_intent / thread_id`。
- **`src/graph/nodes.py`**：新增 `router / analysis / compliance / verify / hitl / finalize` 六节点。
- **`src/graph/builder.py`**：`run_qa`（强制 RAG，不过路由、不接 Checkpointer）与
  `run_agent` / `resume_agent` / `agent_status` 并存 —— 两边输出**同形**，HTTP 面与脚本面不会漂移。
- **`src/server.py`**（`0.6.0`）：`/api/ask` 支持 `intent/thread_id/actor`；新增
  `/api/hitl/{tid}`（读回挂起）、`/api/hitl/{tid}/confirm`（approve/reject，无挂起返回 409）、
  `/api/citations`、`/api/audit`；`/api/health` 增报 regulation / checkpointer / hitl / audit。
  端点全用同步 `def`（交给线程池），避免阻塞事件循环。
- **`src/retrieve/bm25.py`**：citation 优先用 chunk 自带 `citation` —— 法规 chunk 没有公司/年份。

### Fixed / 踩过的坑

- **⚠️ 数据层真 bug（一）：报表主表金额被当成"页眉"整行删掉。**
  `parse_pdf._strip_running_headers` 的归一化把数字一律替成 `#`，于是金额行
  `127,187,293,298.17` → `#,#,#,#.#`，在多页重复出现（报表排版对齐所致），被判成跨页重复页眉
  **整行删除** —— 600519 年报 50 页后 40 页"零金额"，直接后果是"某科目金额是多少"这类
  **最常见的提问答不出**。
  判据错在：页眉页脚是**文字**（公司名/报告期/页码标记），纯数字行是**数据**。
  修法是加 `has_text()`（含中文或字母）过滤，**纯数字行不参与"重复"判定、也永不被删** ——
  这比维护"哪些行是页眉"的白名单稳得多（白名单永远会漏，漏一个就是一次**静默的数据丢失**）。
  回归用例 `tests/test_parse_page_meta.py::test_amount_only_lines_are_never_treated_as_running_headers`。
- **⚠️ 数据层真 bug（二）：每章起始页的首个 chunk 被标成上一章（引用里章节名写错）。**
  每页页首的印刷页码（「6」「146」，占 3~4 字符）位于章节标题之前，被判成"上一节的正文"，
  于是补了一条 offset=0 的上一节标记 —— **每章起始页的首块都挂到上一章**（实测 31 个 chunk / 1.0%）：
  601318 P150 的审计报告块被标成「公司治理」（实为「财务报告」）；
  000858 的 P6/P9/P25/P39 把第二/三/四/五节起始页分别标成上一节。
  修法：`_collect_marks` 加 `_is_header_noise()`（判据与页眉清理同源），
  页首只剩页码时**不补上一节标记**，把真实标题提前到 `offset=0`；
  **对照场景行为不变**（页首是真正文、标题在页中时，offset=0 那条仍是上一节）。
  回归用例 `test_page_number_before_heading_does_not_leak_previous_section`（含对照组）。
  **两个 bug 修复后都必须重跑管线**：`ingest_all --skip-fetch --force` + `index_vector --force`，
  重入库后 chunks **2650 → 3161**（金额回归后正文变长；第二个 bug 只改标签、不改切分文本）。
- **（三）金标准标注错了，不是检索不行**：`lit-601318-2024-auditor` 的 marker 原写「普华永道」，
  而平安的实际审计机构是**安永华明** ——「普华永道」命中的是 P118/P122 董事简历里的**前雇主**。
  于是"5 档全部 ✗"是**标注错误**（检索一直把正确的 P150 排在首位）。
  修法：marker 换成两条真正指向审计机构的短语，`expected_pages` 重算为 `[106, 150]`。
  **教训：marker 只能证明"这页有这几个字"，不能证明"这几个字回答了这个问题"**（登记 G-14）。
- **合规路径假阳性挂起**：答案里的文号（182/226）与日期被判"无出处" → 把 `号` 加入
  文号后缀表、新增 `regulation_hits` 状态字段、verify 对 compliance 改用 `regulation_hits` 作证据。
- **`prefer_current` 把废止版排前面**：原按分数排，废止版分数可能更高 → 改按
  `(status != "current", -score)` 排，现行版永远优先。
- **`force_intent` 耦合**：`resolve_names` 只看 `matched`，强制意图时 `matched` 为空导致指标名识别失败
  → 改为 matched 为空时从问句用 `indicator_aliases()` 重抽。
- **中文引号截断**（写合规文案时踩 3 次）：中文引号嵌进字符串 → SyntaxError。统一改用「」。
- 一条单测**假绿**：`test_retrieve_failure_is_not_pending` 的问句实际走了 analysis 分支、
  根本没测到检索失败 → 换成真走 rag 的问句。**"通过"不等于"测到了"。**

### 语料修复后必须重测：评测指标变化（2026-09-22 重跑）

修掉上面两个解析 bug 后重入库，chunk 集合 2650 → 3161；而金标准的 `expected_pages`
是**从源 PDF 解析产物扫出来的**，于是重跑 `build_golden.py` 后期望页本身也变了
（页级题 **21 → 25** —— 金额回归后数值 marker 更独特，更多题够格算"页级"）。
**所以这一版的指标与 0.5.0 不可直接比较**：

| 配置 | 0.5.0（旧语料） | 0.6.0（修复后重测） |
|---|---|---|
| ① bm25（无过滤） | 14.3% / 拒答 20.0% | **24.0% / 40.0%** |
| ② bm25 + 自动过滤 | 38.1% / 60.0% | **40.0% / 80.0%** |
| ③ + 每页配额 | 38.1% / 60.0% | **40.0% / 80.0%** |
| ④ hybrid + 过滤 + 配额 | 52.4% / 60.0% | **48.0% / 80.0%** |
| ⑤ hybrid + rerank | 52.4% / 60.0% | **40.0% / 80.0%** |

- **①→④ 提升 +24.0pp**（0.5.0 记的是 +38.1pp）；逐年份/公司精度仍是 **100%**、**0 空召回**。
- **拒答正确率 40% → 80%**：语料修好后"该答的题答得出"，剩下的拒答才是真该拒的。
- ⑤ 比 ④ 低 8pp → **重排负收益的结论不变**（默认 `RERANK_BACKEND=passthrough` 不只是省钱）。
- 报告里"配额取 1 时 bm25 档 44.0%（+4.0pp）"是本次实测值，并已把样本量提醒
  （"1 题 = 4.0pp"）从硬编码改成**按数据算** —— 硬编码的提醒会随语料变化立刻变错。

> **流程固化（G-15）**：`ingest_all --force` 之后必须依次跑
> `build_golden.py` → `eval_retrieval.py`。期望页来自解析产物，产物变了期望就变了 ——
> 这不是"指标在抖"，而是**标注在跟着语料校准**。


### 实测结果（真实模型 deepseek-flash）

三条意图端到端全通，verify 全部 `supported=true` 且 `unsupported=[]`：

| 问题 | 路由 | 结果 | verify |
|---|---|---|---|
| 贵州茅台2024年的毛利率是多少 | analysis（`numeric_cue+indicator`，0.8） | 91.93%，带公式 + 分子分母来源字段 + 官方口径对账（差异 -0.00pp） | 11 个数字全有出处 |
| 贵州茅台2024年年报中货币资金余额是多少 | rag（`numeric_cue_without_indicator`，0.4） | 59,295,822,956.89 元（合并）/ 77,252,079,198.82 元（母公司） | 3 个数字全有出处 |
| 上市公司定期报告披露期限有何规定 | compliance（`compliance_cue`，0.9） | 226 号第三十二条等条文原文 + 现行状态 | 15 个数字全有出处 |

数字**人工回源核对**通过：P11「研发投入合计 695,376,735.81」、P13「货币资金 59,295,822,956.89」。
数值题实测（`scripts/smoke_qa.py` 已改为**按设计路径判**）：「中国平安的归母净资产是多少」
→ 路由到工具层 → **9,286.00 亿元**（来源 `sina_balance.归属于母公司的股东权益合计`），6 期全带来源，
`verify.supported=true`。该用例原先按"翻年报原文"判，实测 BM25 会把「内含价值」页排前
（P66 的「净资产」是内含价值口径的"调整后资产净值"，与归母净资产不是一回事）→ 形成误引；
改成断言"路由→analysis + 答案值 + verify"后通过。
离线冒烟（`smoke_qa.py --no-llm`）：**6 条 → 通过 6 / 基线 0 / 失败 0**，引用回源 PDF **20/20 命中**。
HITL 用 `FA_HITL_FORCE_REASON=low_confidence` 演练：进程 A 跑出挂起 → 清 saver 缓存（模拟换进程）
→ 进程 B 读回并 confirm，审计记 `hitl_confirm`。
测试：205 → **307 例**（新增 102 例：路由 9 / 数字 9 / 分析子图 13 / verify+HITL 14 / 法规 / 主图 12 / HTTP 11 / 解析回归 2）。

### 已知不足（详见 `不足清单与处置方案.md`）

- 数值题目前只覆盖**比率/指标**（走 registry）；"某科目余额是多少"这类**报表科目**仍走 RAG
  读原文 —— 有出处、可核验，但不如结构化取数稳定（口径依赖原文表述）。
- HITL 目前只有 approve/reject 两态，**没有"改完再放行"**（人工修正答案后继续）。
- 审计只留痕、未做**回放**（用留痕重跑一遍验证可复现）。

## [0.5.0] - 2026-09-22（Step 4：混合检索 —— 召回从 14.3% 提到 52.4%）

本步交付 `元数据过滤 → BM25 + 向量双路召回 → RRF 融合 → 重排` 的完整混合检索链路，
并按实施方案要求**留下 Step 3 vs Step 4 的指标对照**（`eval/report.md`，34 题消融）。
**默认 `RETRIEVE_MODE=bm25`** —— Step 3 的基线原样可复现，混合检索是"可选启用"，
这样才能把两轮实验的输入对齐，差异才归因得清。

### 实测结果（34 题消融，不调模型、可免费重跑）

| 配置 | page_hit@5 | 年份精度 | 公司精度 | 拒答正确率 | 空召回 |
|---|---|---|---|---|---|
| ① bm25（无过滤，= Step 3 行为） | 14.3% | 86.2% | 49.0% | 20.0% | 0 |
| ② bm25 + 自动过滤 | 38.1% | **100%** | **100%** | 60.0% | 0 |
| ③ hybrid（双路 + RRF）+ 自动过滤 | **52.4%** | **100%** | **100%** | 60.0% | 0 |
| ④ hybrid + rerank | 52.4% | 100% | 100% | 60.0% | 0 |

- **page_hit@5 14.3% → 52.4%（+38.1pp）**；归因拆开：只加自动过滤值 **+23.8pp**，
  再加向量+RRF 值 **+14.3pp**。年份/公司精度 **100%**，**0 空召回**。
- 向量库：**2650 × 1024**（`text-embedding-v4`，建库 **298.9s** / 265 次 API 调用）。
- 端到端冒烟（hybrid 调真实模型）：**6 条 → 通过 4 / 基线 2 / 失败 0**，引用回源 PDF **11/11 命中**。
- 测试：149 → **205 例**（新增 56 例：RRF 性质 / 重排降级 / 过滤纪律 / 向量错配不变量）。
- ④ 与 ③ 同分是**刻意的**：重排通道接好且实测可用（`gte-rerank-v2`），
  但默认 `RERANK_BACKEND=passthrough` 不花钱，所以报告里它按原序直通。

### Added

- **`src/embedding.py`**：可插拔向量化通道（`api` / `local` / `none`），
  入库与查询**共用同一份预处理**，独立异常 `EmbeddingUnavailable`，分批 + 断点回调，
  返回按 `index` 重排（防错位），条数不符直接报错。返回向量已 L2 归一化（余弦 = 点积）。
- **`src/ingest/index_vector.py`**：向量入库。落盘四件套 `vectors.npy` / `meta.jsonl` /
  `manifest.json` / `vectors.part.npy`（断点）。`chunks_fingerprint()` **顺序敏感**；
  `load()` 严格校验「矩阵行数 = meta 行数 = dim」并与 manifest 的模型名比对。
  矩阵只存一份 `meta.jsonl`（而不是 pickle 整个 chunk）—— pickle 会把入库时的字段结构
  焊死在文件里，而本项目的 chunk 字段还在演进。
- **`src/retrieve/vector.py`**：向量召回。只返回「候选 + 相似度」，**不存正文**
  （正文唯一来源是 BM25 索引的 chunk 存储）。精确余弦（非 ANN，理由见模块头）；
  元数据摊平成 numpy 数组以便过滤；查询向量 LRU 缓存（评测两轮用同一份查询向量）。
- **`src/retrieve/fusion.py`**：RRF 融合。**共识优先 / 缺席不罚 / 稳定排序**，等权起步。
- **`src/retrieve/rerank.py`**：重排（`passthrough` / `api` / `local`）。
  **永不抛异常**：不可用 / 超时 / 条数不符 / 缺正文 → 原序直通 + `note` 说明。
  不做绝对分数阈值（不同模型刻度不同，换个模型就会全丢或全留）。
- **`src/retrieve/filters.py`**：从问句抽公司/年份 → 元数据过滤。纯函数，不读索引不联网。
  **所有判断要求唯一命中，命中多条放弃过滤并把歧义写进 note —— 绝不猜**。
- **`scripts/probe_embedding_sources.py`**：embedding / rerank 通道探活（含 batch 上限实测）。
- **`scripts/index_vector.py`**：建库脚本。幂等 + 断点续传 + `--status` / `--force`。
  独立于 `ingest_all.py`，因为它是**唯一花钱且慢**的一步，绑在一起会让
  "想重建一次向量就得重跑整条管线"。
- **`eval/golden_qa.jsonl`**：评测金标准 34 题（21 页级 / 1 宽泛 / 8 章节级 / 5 拒答）。
  页码由 `scripts/build_golden.py` 用**标记串扫源 PDF 解析产物**定位，页级定位失败降级章节级。
- **`scripts/eval_retrieval.py`** + **`eval/report.md` / `report.json`**：四配置消融评测。
- **`tests/{test_fusion,test_rerank,test_query_filter,test_vector}.py`**：纯内存不联网。

### Changed

- **`src/retrieve/pipeline.py`**：`retrieve()` 支持 `mode=bm25|hybrid`、`auto_filter`、
  `rerank_backend`。`_hybrid()` 的顺序是「双路召回 → 先给向量候选补正文 → 融合 →
  **合并两路的 signals（不覆盖）** → 重排 → 截断」。新增 `chunk_store()` 作为**正文唯一来源**。
  向量/重排不可用时 `hybrid` **自动退回 BM25**，并在 `degraded` + `note` 里说明原因。
- **`src/config.py`**：新增 `EMBEDDING_*`（backend/provider/model/batch/timeout/retry/interval/
  max_chars）、`VECTOR_*`（dir/matrix/meta/manifest/part/min_score/query_cache）、
  `RERANK_*`（backend/provider/api_model/top_n/max_doc_chars/timeout/url）、`RRF_K`。
- **`src/server.py`**：`APP_VERSION` → `0.5.0`；`/api/health` 增报 `vector` / `rerank` 通道状态；
  `AskRequest` 的 `code` / `year` 注释改为"留空则从问题自动识别"。
- **`scripts/smoke_qa.py`**：新增 `--mode bm25|hybrid`；年份观察文案区分两种模式。
- **`requirements.txt`**：显式声明 `numpy`；`chromadb` / `sentence-transformers` 标注为
  "仅 local 通道 / 换 ANN 时才需要"。

### Fixed / 踩过的坑（都是实测暴露的）

- **embedding batch 上限是 10，不是文档写的 25**：`batch=25` 返回
  `HTTP 400 InvalidParameter: batch size is ...`。照抄文档会在批量入库时炸。
- **DeepSeek 没有 embeddings 接口**（`/embeddings` → 404）—— 不能想当然复用它的 Key 建向量库。
- **融合时 `signals` 被覆盖**：`by_id` 若取 bm25 或 vector 任一份，会丢掉"两路共识"这个信号。
  改为分别 merge 两路 `signals` 再更新 `rrf` / `ranks`。
- **向量候选补正文的时序错了**：原实现把 `fused` 传给 `_materialize`，导致先融合（此时向量候选
  还没正文）再补正文。改为先 `_materialize(vector_hits)` 再 `fuse`。
- **`WindowsPath + ".json"` 抛 TypeError**（断点文件路径拼接）：改 `Path(str(PART_PATH) + ".json")`。
- **评测页码误判命中**：基线里"只有页码对"就把别家公司的 P18 算成命中。
  修正为 `page_no` + `code` + `year` 三者全一致才算命中 —— 所以本版基线数字（14.3%）
  比调试期的临时数字更低，**那是修正后的真值**。
- **宽泛题被当 0 分**：聚合类问题（如"总资产是多少"有 6 个候选页）原来只按单一期望页判。
  改为宽泛题也计入 `page_hit`（仅聚合口径），单列一栏说明。

### 已知不足（写在文档里，不藏着）

1. **确定性闸门在 2 道越界题上失效**（`ref-food`「食堂菜谱」/ `ref-out-of-scope`「锂电池产能」）：
   问题里的部分实词在年报里**真实存在**（员工食堂、基酒产能、锂电相关表述），
   词面单段覆盖率刚好越过 0.5 门槛。这**不是参数没调好，而是词面指标的能力边界** ——
   补齐要靠第二道闸门（模型自评 `insufficient`，实测这两题能正确拒答）。
2. **同页多块占 top5 名额**：top5 里常出现同一页的 2~3 个分块。下一步可加"每页最多 N 块"配额。
3. **跨年可比列不回退**：问「五粮液 2023 年营收」时库里只有 2024 年报，严格年份过滤会拒答。
   2024 年报的可比列**确实**含 2023 年数据，但"确认那一列真有该年份"要在引用回查之后做
   （Step 5 的 verify）。宁可拒答并说明，也不要硬凑一个其他年份的数。
4. **相对年份不解析**（去年 / 今年 / 前年）：同一句话在元旦前后含义不同，而答案会被引用很久。

## [0.4.0] - 2026-09-22（Step 3：第一个"能问答"的闭环，且引用可核验）

本步交付 `检索 → 生成 → 引用校验` 三节点 LangGraph 子图 + FastAPI 最小服务。
仍是 **BM25 单路**（向量召回是 Step 4 的事），目标是把"答案带可核验出处"这条链路先立起来。

### 实测结果（不是"应该能"，是跑出来的）

| 项 | 数字 |
|---|---|
| 冒烟用例（调真实模型） | **6 条 → 通过 4 / 基线 2 / 失败 0** |
| 冒烟用例（`--no-llm` 离线） | **6 条全部通过** |
| 引用页码核验（拿片段回源 PDF 该页找原文） | 调模型 **8/8 命中**；离线 **25/25 命中**，对不上 0 |
| 超范围拒答 | 「公司食堂菜谱有什么推荐」→ 拒答，**不调模型**、不给引用 |
| 测试 | 115 → **149 例**（新增 26 例：覆盖度/编号校验/两条闸门/降级/图与直调同值） |
| 章节精度修正 | 节名下沉到 chunk 级后，**19 个 chunk** 的章节标签被改正；**切分正文逐字节未变** |

### Added

- **`src/retrieve/pipeline.py`**：全项目唯一的"句子 → hits"出口。`mode` 一个开关决定用哪几路召回
  （本步只有 `bm25`，`hybrid` 是 Step 4 的槽位），**统一 hit 形状**（`normalize_hit`）——
  让 Step 4 插向量召回时不必改调用方。`ok=False` 只表示"检索做不了"（索引缺失），
  "没召回任何东西"是 `ok=True + hits=[]`（**结论**，不是故障）。
- **`src/answer.py`**：答案合成 + 引用编号校验 + 双闸门拒答 + 降级摘录 + 免责声明。
  两个入口共用同一份实现：`answer_question()`（一步到位）与 `synthesize()`（图节点用）。
- **`src/graph/{state,nodes,builder}.py`**：`retrieve → generate → cite` 三节点子图。
  节点**薄**（只读写状态 + 兜异常），逻辑全在纯函数里 —— 因为节点里的逻辑只能靠跑图来测，
  而跑图要先建索引、要联网调模型。**编译不缓存**（编译产物不可跨进程 pickle）、
  **不接 Checkpointer**（本步没有挂起需求）。
- **`src/llm.py`**（♻️ 搬自 workflow-agent）：多供应商通道 + `is_ready() -> (bool, reason)`，
  后者用来决定走不走降级路径。
- **`src/server.py`**（最小）：`GET /api/health`（不调模型、不花 token，可当探针）、
  `POST /api/ask`。`/api/ask` 写成**同步 `def`** —— 它要联网调模型（秒级阻塞），
  写在 `async def` 里会卡死整个事件循环，连 health 都不响应。
- **`scripts/smoke_qa.py`**：端到端冒烟，带**三类分开统计**的判定（通过 / 基线 / 失败）。
- **`scripts/_archive/`**：把 3 个已被 `probe_app_sources.py` 覆盖的探针移进来（移动而非删除）。

### 关键机制（三条，都有确定性用例守着）

1. **引用可核验**：检索片段编号成 `[1][2]…` 后交给模型，要它返回 `used_citations`，
   逐条校验编号是否真实存在，**不存在的丢弃并记进 notes**。
   LLM 会编页码，但编不出我们没给它的编号 —— 这是机制保证，不是靠提示词求它别撒谎。
2. **引用校验的上界 = "实际送进上下文几段"**，不是 `len(hits)`。
   模型没见过第 8 段却写 `[8]`，若拿 `len(hits)` 当上界就会判为合法引用 ——
   那不是模型编造出处，是**我们替模型伪造了出处**。为此把 `render_context` 拆出
   `rendered_count()`，两者同源。
3. **双闸门拒答**：确定性闸门（**单段**证据覆盖度）+ 模型自评（`insufficient` 标志）。
   只靠一条都不够：前者挡不住"词都在但答非所问"，后者挡不住"模型硬答"。

### Fixed（都是实跑才暴露出来的）

- **节名没有下沉到 chunk 级**：茅台把「第三节」与「管理层讨论与分析」分成两行写在**页中段**，
  按页打标签会把该页上半（其实是第二节「非经常性损益」表格的续页）算进第三节 ——
  引用出处直接错一节。现在 `parse_pdf` 记录每页的 `section_marks`（**页内字符偏移 + 章节名**），
  `chunk.py` 按块的起始偏移判定，并补一条"块首未归属但块内含节标题 → 采纳块内第一个节标题"
  （修正 002594 P5：正文首行是页眉残留「2024 年年度报告」，真正的第一节标题跟在后面）。
  实测 **19 个 chunk** 的章节改正，且**切分正文与改动前逐字节一致**（只改标签、不改块）。
- **证据覆盖度按"拼所有召回"统计 → 太松**：「公司食堂菜谱有什么推荐」里
  「食堂」（平安年报的员工食堂）与「推荐」（董事会推荐某议案）分别落在**不同公司不同页**，
  拼起来算出 0.67 直接放行。改判**单段**覆盖度后：正常问题 0.67~1.00、越界问题 0.25~0.33
  （门槛 0.5 两侧都留得开）。`ratio_union` 一并返回，专门用来暴露"靠拼凑达标"。
- **模型判 `insufficient` 时答案正文仍带角标、引用列表却是空的** → 前端出现"点了没反应"的死链。
  拒答分支现在**照样走编号校验并保留引用**：既能消除死链，用户还能核对"为什么答不了"。
- **`_pack()` 形参名不一致**（形参 `llm_info`，实参 `llm=`）—— 只在**真调到模型**的分支才触发，
  离线跑多少次都不会炸。第一次真实调用即 `TypeError`。**教训**：
  只有"走真模型 / 走真网络"才会经过的分支，必须在实跑里过一遍，单测和 `--no-llm` 都不算。
- **"空页不给章节标记"会把整份报告的章节归属删掉**：章节归属是按页推进的，
  封面/插图页/被当作跨页页眉清空的页都会落进"本页没有节起始点"这一支。
  原本想省掉空页的标记，结果这些页之后的所有页都丢了标签。改为**每页至少一条 offset=0 标记**。

### 已知限制（作为 Step 4/5 的对照基线，不是"忘了做"）

- **检索精度**：题面问 2024 年时，BM25 会把措辞几乎相同的 **2025 年报**段落先召回
  （离线实测 6/20 条引用落在题面年份之外 = **30%**）。引用页码是真的，
  但用别年的数答这一年的题属于**静默错误** → Step 4 混合检索 + Step 5 路由把年份抽成过滤条件。
- **跨公司与数值题**：「中国平安的归母净资产」的准确值本就在结构化库（Step 2、新浪源补的），
  而年报原文里那句话（`归属于母公司股东权益`）在 601318 P18/P19/P157 等 6 处**确实存在**，
  只是 BM25 把「内含价值」类页面排到了前面 → Step 4 召回 + Step 5 路由到工具层。
- 章节识别仍只到**一级章节**，二级小节未细分。

---

## [0.3.0] - 2026-09-22（第二数据源：东财拿不到的去别处拿，而不是宣布"没有"）

起因是一句很关键的反馈：**「中国平安有归母净资产 / 毛利率 / 营业成本这三项」** ——
用户在券商 App 上看到了，而我们上一版报的是"数据源未提供"。
查完之后三种情况各占一种，**没有一种是"我们之前判断对了"**：

| 用户看到的 | 事实 | 处理 |
|---|---|---|
| 归母净资产 | 数据客观存在，**东财全系不提供**（我们选源不够） | 接入新浪财经 `fzb` 补齐 |
| 营业成本 | 保险业**确实没有**这个科目，成本行叫「营业支出」 | 新增「营业支出」指标 + 让工具层给出口径替代项 |
| 毛利率 | 东财/同花顺都没有这个字段，App 上的数**是它自己算的** | 新增「毛利率(保险口径)」派生比率并公开算式 |

### 教训（已写进 `fetch_eastmoney.py` 模块文档与探针脚本）

**把「我没取到」当成「数据不存在」是错的** —— 这两种结论的证据强度完全不同，
前者是我的问题，后者是客观事实。在宣布"数据源不提供"之前，
至少要换一个源、并且拿会计恒等式交叉验证一次。

### Added

- **第二数据源：新浪财经移动端财报**（`src/ingest/fetch_sina.py`）
  - 它是**唯一**能拿到保险股「归属于母公司股东权益」的路：
    东财 F10 资产负债表对 601318 整表为空、数据中心资产负债简表没有 PARENT_EQUITY 列、
    主要指标只有 `TOTAL_EQUITY_PK`（**含少数股东**，是另一个口径，不能顶替）。
  - 实测交叉验证：归母 9,286.00 亿 + 少数股东 3,761.12 亿 = 13,047.12 亿
    **= 东财 `TOTAL_EQUITY_PK`，恒等式零误差**。
  - 三个实测坑：① 表代号是 **`fzb`** 不是 `zcfz`（传错返回 `data:null`，看着像"这只股票没有资产负债表"）；
    ② 期次类型要按 `date_description` 中文文本筛（`2024年报` / `2025半年报`），
    **认不出的期次直接丢，不猜**；③ 项目名是中文，直接写进口径表当字段名用
    （取数层因此不需要行业分支）。
- **多源注册表 `config.DATA_SOURCES`**（原 `EM_SOURCES` 改名）：按 `api` 分派
  `securities`（东财 F10）/ `datacenter`（东财数据中心）/ `sina`（新浪）。
- **新增指标「营业支出」**（保险/金融口径，`income.TOTAL_OPERATE_COST` /
  `dc_income.OPERATE_EXPENSE` / `sina_income.营业支出`）。
  **单独立项而不并进「营业成本」** —— 合进去会让"制造业营业成本"和"保险营业支出"
  混成一条序列，是最典型的静默口径错误。
- **新增比率「毛利率(保险口径)」**：`(营业收入 − 营业支出) / 营业收入`，
  别名 `保险毛利率`。东财与同花顺都不提供官方值（**不是我们没查，是确实没有**），
  故自行派生并公开算式；note 明确写"与制造业毛利率不可横向比较"。
- **口径替代机制**（`config.INDICATORS[*]["counterpart"]` +
  `config.RATIOS[*]["alternatives"]`）：指标/比率对某行业不适用时，
  工具层不是只回"没有"，而是**把替代口径的名称、公式、数值一起带出来**。
  - `get_financial_indicator(中国平安, 营业成本)` → 空序列 + `counterpart`：
    营业支出 8,628.10 亿元 `<- income.TOTAL_OPERATE_COST`。
  - `calc_financial_ratio(中国平安, 毛利率)` → `insufficient_components` + `alternatives`：
    毛利率(保险口径) = 17.87%。
  - 立场：直接用替代口径的数顶替原名**是造假**，只说"没有"**是把问题推回去**，
    所以必须"给数 + 标明它叫什么、怎么算的"。
- **探针 `scripts/probe_app_sources.py`**：一次打印某公司在**全部源**上的收入/成本/权益类字段
  （东财 F10 ×3 + 数据中心 ×2 + 新浪 ×2 + 同花顺），用于回答"App 上这个数到底映射到哪个字段"。
  副产品：同花顺 `flashData` 是**二次转义的 JSON 字符串**，直接对整体做中文匹配会全部落空
  （看上去像"这个源没有中文科目名"）—— 必须 `json.loads` 两次。

### Fixed

- **期间轴被补充源污染 → 缺失统计误报（严重）**：新浪会给出主源范围之外更早的一期
  （601318 主源 2020–2025 共 6 期，新浪多给 2019）。若取各源期次**并集**，
  就会凭空多出一个只有新浪 3 个字段的残缺期，其余指标在该期全部"缺失"，
  汇总后被打印成"**全源缺失（数据源未提供）**"—— 把"某期没覆盖"和"压根没这个字段"
  混成一件事。现在期间轴**只由主源决定**，补充源仅在主源已确定的期上补字段。
- **`missing` 语义分裂为三档**（此前三种"没取到"混在一个列表里）：
  - `missing` = 每一期都取不到 → 数据源确实不提供该字段（如保险股的毛利率）
  - `partial` = 只在部分期缺 → 覆盖度问题，换期数/换年份可能就有了，**且已排掉 missing 的指标**
  - `source_errors` = 接口故障，与"数据不存在"分开上报
- **`python -m src.ingest.fetch_eastmoney --code 601318` 参数解析错位**：
  用 `args` 的下标去索引 `_sys.argv`（整体差 1 位），把 `"--code"` 本身当成股票代码，
  取数静默返回 0 期 —— 表现是"这家公司没数据"，实际是参数解析错。
- 东财 `dc_balance` / `dc_cashflow` / `dc_income` 与新浪源的失败**不再静默**：
  `fetch_sina.take_errors()` 会把源级错误带进 `collect_company` / `save_company` 结果，
  CLI 用 `✗` 标出来。这类静默失败的后果正是"保险股悄悄少了归母净资产"。

### Changed

- `config.EM_SOURCES` → **`config.DATA_SOURCES`**（已不只有东财源，名字要与事实一致）。
- `config`：新增 `SINA_REPORT_API` / `SINA_REFERER` / `SUPPLEMENT_APIS` /
  `is_supplement_source()` / `REPORT_TYPE_CANON` / `normalize_report_type()` /
  `report_type_of_description()`；指标 21 → **22** 个，比率 5 → **6** 个。
- `config` 自检输出改为按 `api` 统计源数量（`{'securities': 4, 'datacenter': 3, 'sina': 2}`）。

### 实测结果（5 家公司 · 6 期年报）

| 公司 | 指标值 | 命中源 | 全期缺失 |
|---|---|---|---|
| 600519 贵州茅台 | 132（22/22） | balance, cashflow, income, main | — |
| 000858 五粮液 | 132（22/22） | balance, cashflow, income, main | — |
| 300750 宁德时代 | 132（22/22） | balance, cashflow, income, main | — |
| 002594 比亚迪 | 132（22/22） | balance, cashflow, income, main | — |
| 601318 中国平安 | **120（20/22）** | + **sina_balance** | 毛利率、营业成本（保险业确无这两项） |

中国平安从 **108 → 120** 个值：补上「归母净资产」（新浪）与「营业支出」。
会计恒等式 5 家公司**全部 0.0000% 误差**；贵州茅台毛利率派生值
91.93% vs 官方 91.9312 → 差 −0.00pp「一致」（回归无影响）。

### 测试

**90 → 115 例**，全绿。新增两个文件：
- `tests/test_fetch_sina.py`（12 例）：表代号必须是 `fzb`、期次按文本筛且认不出就丢、
  非数值项丢弃、中文项目名与口径表逐字对齐、单源失败进 `take_errors()` 不静默、缓存去重。
- `tests/test_collect_company.py`（6 例）：期间轴来自主源而非并集、补充源补字段并如实标注命中源、
  `missing` / `partial` 分开且不重复计数、源级错误上报、源注册表形状。

`tests/conftest.py` 的 601318 合成数据改为**按真实形态**构造（补上营业收入/营业支出/
来自 `sina_balance` 的归母净资产），并把"ROE 派生不出来"的用例改用平安银行覆盖 ——
该用例的旧断言建立在"保险股没有归母净资产"这个**已被推翻的前提**上。

## [0.2.0] - 2026-09-22（Step 2：结构化财务库 + 只读工具层）

把「数值」也纳入可核验体系。Step 1 解决的是"原文在哪一页"，
这一步解决"这个数从哪张报表哪个字段来、用的是哪个口径"。

### Added

- **业务库 `src/db.py`**：5 张表 `companies` / `reports` / `financial_indicators` /
  `audit_logs` / `golden_qa`，双后端（SQLite 默认 / MySQL 8）各一份显式 DDL。
  - `financial_indicators` 用**长表**（一行 = 一个指标一期）：宽表的话
    「这个值取自哪张报表哪个字段」只能写死在列名里；长表把 `source_table` / `source_field`
    存成**数据**，于是每个值都能自证口径 —— 这是本项目的立身之本。
  - 沿用 workflow-agent 的连接层约定：`_MySQLConn` 把 `?` 转 `%s`（全项目 SQL 只写一份）、
    `now_expr()` 拼进 SQL 文本（**不能当绑定参数**，MySQL 会报 1292）、
    `_ensure_mysql_indexes()` 查 `information_schema` 补建索引（MySQL 不支持
    `CREATE INDEX IF NOT EXISTS`）。
- **结构化取数 `src/ingest/fetch_eastmoney.py`**：东财 F10 三大报表 + 主要指标 →
  清洗入长表。每公司 21 个指标口径 × 6 期。
  - **多源优先级回退**：每个指标在 `config.INDICATORS[*]["sources"]` 里声明
    `(源, 字段)` 优先级列表，代码取第一个非空值，并落**实际命中**的源（不是声明的首选源）。
  - **"轻量元数据"取数**：`columns` 只取用到的字段 + `REPORT_DATE`，
    不拉 `ALL`（三大报表 200~320 列，`ALL` 会让返回体大好几倍而大部分用不上）。
- **工具层 `src/tools/`**（4 个只读工具，`registry.py` + JSON Schema 统一导出）：
  - `get_financial_indicator`：单公司单指标序列，每个值带 `period`/`unit`/`source`。
  - `compare_companies`：多公司同指标排名；**各家期次不一致时 `periods_consistent=false`
    并给出告警**（否则就是拿 2025 年比 2024 年还叫"排名"）。
  - `calc_financial_ratio`：**返回完整计算过程**（分子分母的每个分项、符号、取值、来源），
    并对官方口径做**交叉对账**（差异分级：一致 / 口径差异 / 需复核）。
  - `list_supported`：列出已入库公司与可用指标/比率名（防止模型编造库外公司或自造指标名）。
  - 工具层**永不抛异常**：漏参/多参/编造工具名/下游异常全部转成结构化错误，
    让模型能自我纠正而不是把整条链路打断。
- **比率口径表 `config.RATIOS`**：把比率的口径也收口到配置（不散在代码里）。
  5 个比率：毛利率 / 净利率 / 资产负债率 / ROE / 经营现金流净利润比。
- **建库脚本 `scripts/init_db.py`**：建表 + 同步公司/年报清单（`--fetch` 再拉东财数据）。
- **验收脚本 `scripts/verify_step2_db.py`**：五段核对（库内计数 / 会计恒等式 /
  工具层三条验收标准 / 保险股口径边界 / 注册表错误兜底）。
- **schema 导出 `scripts/list_tools.py`**：把 function calling schema 写到
  `data/tools_schema.json`，改接口时能 diff 出契约变化。
- **探针 `scripts/probe_dc_insurance.py`**：专门排查"数据中心报表对保险股到底返回什么"。
- **测试 +63 例（27 → 90）**：`test_db.py` / `test_tools_indicators.py` /
  `test_tools_ratios.py` / `test_tools_registry.py`。
  工具层测试跑在**合成数据**（`synth_db` 夹具）上：可手算的整十亿数 + 刻意构造的
  缺分项 / 期次不一致 / 简称歧义边界。

### Fixed

取数链路上五个「静默错误」——都表现为"看起来正常，其实错了"：

1. **数据中心源的客户端年报期筛选永远筛不到东西**。`REPORT_DATE` 形如
   `"2024-12-31 00:00:00"`，代码用 `str(...).endswith("12-31")` 匹配，末尾是时间部分，
   **恒为 False** → 数据中心源静默返回 0 行。因为保险股的 F10 资产负债表整表为空、
   全靠它兜底，症状就表现为"中国平安取不到数"。**修复：先截到日期部分再比**。
   该 bug 让中国平安的指标值从 108 掉到 72（现金流三项全缺）。
2. **`upsert_report_sql` 对字符串取 `len`**。`marks = ", ".join(["?"] * len(cols))`
   里 `cols` 是**字符串**（长度 104）而不是列数（11）→ 报
   `sqlite3.OperationalError: 104 values for 11 columns`。**改为先建 `cols_list` 再取 `len`**。
3. **保险股（中国平安 601318）在 F10 资产负债表/现金流量表接口下整表返回空**
   （`success=false, message=返回数据为空`）。**修复：按指标声明多源优先级**，
   回退到数据中心批量报表（`RPT_DMSK_FN_*`）与主要指标（`TOTAL_ASSETS_PK` / `LIABILITY` /
   `TOTAL_EQUITY_PK` 会计恒等式字段）。修复后 601318 从 72 → 108 个指标值。
   仍缺的 3 项（归母净资产 / 毛利率 / 营业成本）是保险口径下**天然不存在**的科目，
   如实报缺而不是找近似值顶替。
4. **`python -m src.tools.registry` 打印"已注册 0 个工具"**。`-m` 会把文件当 `__main__`
   **再加载一份**，`@tool` 注册进的是 `src.tools.registry._TOOLS`，而 `__main__._TOOLS`
   是另一个空 dict。**修复：自检入口挪到 `scripts/list_tools.py`**；
   并把重复注册守卫改精确 —— 用 `__code__.co_filename` 判"同一文件重入"（允许覆盖），
   只有**不同文件撞名**才报错。
5. **`config.ratio_meta` 里 `needed_indicators` 的推导写法晦涩**（`for ind, _ in [pair]`），
   改成直白的集合推导。

### Changed

- `src/config.py` 的 `INDICATORS` 从"单一字段"升级为**多源优先级列表** `sources`，
  共 21 个指标口径（利润表 7 / 资产负债表 4 / 现金流量表 3 / 主要指标 7）。
  `indicator_meta()` 额外给出 `source_table` / `source_field`（首选源，用于展示与期望）。
- `src/retrieve/bm25.py` 的 `tokenize` 保留小数（此前 `45.2` 这类会被当非字母数字丢掉）。
- `src/ingest/chunk.py` 的 `_split_by_separators` 不再在最后一段后追加分隔符，
  并新增 `_has_content` 过滤纯标点碎片 —— 修掉"合成出 `。。`"导致 chunk 不是页面子串的问题。
- `tests/` 成为包（加 `__init__.py`），让 `tests.conftest` 成为确定可导入路径，
  避免 conftest 被加载两份。

### 实测对账（2026-09-22）

| 公司 | 2024 年报 | 值 | 源 |
|---|---|---|---|
| 贵州茅台 600519 | 营业总收入 | 1,741.44 亿 | `income.TOTAL_OPERATE_INCOME` |
| 贵州茅台 600519 | 归母净利润 | 862.28 亿 | `income.PARENT_NETPROFIT` |
| 贵州茅台 600519 | 加权 ROE | 36.02% | `main.ROEJQ` |
| 贵州茅台 600519 | 毛利率 | 91.93%（派生 91.9312 vs 官方 91.9312，差 −0.00pp） | `main.XSMLL` |
| 中国平安 601318 | 总资产 | 129,578.27 亿 | `dc_balance.TOTAL_ASSETS` |
| 中国平安 601318 | 总负债 | 116,531.15 亿 | `dc_balance.TOTAL_LIABILITIES` |
| 中国平安 601318 | 所有者权益合计 | 13,047.12 亿 | `dc_balance.TOTAL_EQUITY` |

**会计恒等式核对（总资产 − 总负债 = 所有者权益合计）**：
5 家公司（600519 / 000858 / 300750 / 002594 / 601318）在 2024-12-31 全部 **0.0000% 误差**。
这一条同时证明了"多源回退没有串口径"——若 601318 的总资产取自一个源、权益取自另一个口径，
恒等式必然对不上。

---

## [0.1.0] - 2026-09-22（Step 0 + Step 1：年报入库 → 按页解析 → BM25 检索 → 引用）

首个可运行版本。目标是把「**可核验的溯源**」这条链路打通：
输入公司的年报 PDF，产出带完整元数据的 chunk 与 BM25 索引，
检索结果能拼出 `[1] 贵州茅台2024年年报 P87 管理层讨论与分析 (第2/3段)`。

### Added

- **Step 0 数据源实测**（`scripts/probe_sources.py`）：把四类数据源的可用参数固化下来，
  换机器先跑一遍就知道能不能干活。实测结论见 `EXTENSION.md` §2 取数类。
  - 巨潮 `hisAnnouncement/query`（年报清单）、`topSearch/query`（orgId 查询）、`static.cninfo.com.cn`（PDF）
  - 东财 `datacenter-web`（结构化财报）、东财行情 `push2`
  - 新浪 `hq.sinajs.cn`（必须带 Referer）
- **集中式配置**（`src/config.py`）：路径、取数参数、章节锚点与别名、切分参数、
  检索参数（含短语加成）、指标口径表 `INDICATORS`、多供应商 `LLM_PROVIDERS`、
  业务库双后端（SQLite/MySQL）。密钥解析统一走 **环境变量 > `data/*.local.json` > 空串**。
- **HTTP 薄封装**（`src/net.py`）：统一限速、UA/Referer、仅对 5xx 重试（4xx 是参数问题，
  重试无用）、下载先写 `.part` 再改名（实现幂等）。
- **年报取数**（`src/ingest/fetch_cninfo.py`）：orgId 自动查询与缓存、
  按市场选 `column`、标题黑名单过滤摘要/英文版/更正公告、同年取最新一份、
  按 `manifest.json` 做幂等下载（按字节数校验）。
- **按页解析**（`src/ingest/parse_pdf.py`）：页码与文本逐页绑定；页眉页脚清理
  （归一化数字后按跨页重复率判定）；章节识别**双模式**（见 Changed）。
  产出 `data/parsed/{code}/{year}.json`，含 `section_pages`（章节页码区间）与
  `section_mode`（走了哪条识别路径），便于人工核对。
- **切分**（`src/ingest/chunk.py`）：**按页切、不跨页**，overlap 只在页内做；
  每个 chunk 带完整溯源元数据；`kind` 字段为 Step 2 的表格块预留（表格整块不切）。
- **BM25 检索**（`src/retrieve/bm25.py`）：jieba `lcut_for_search` 分词、
  千分位处理、小数保留、**整短语命中加成**（压掉 search 模式分词的子词噪声）、
  公司/年份/章节元数据过滤、零分不返回。
- **引用格式**（`src/citation.py`）：唯一拼装出口，支持多段页段号、
  缺字段时降级不崩。
- **一键流水线**（`scripts/ingest_all.py`）：取数 → 解析 → 切分 → 建索引，
  支持 `--code/--years/--force/--skip-fetch/--no-index`。
- **目标公司清单**（`config/watchlist.yaml`）：600519 贵州茅台、000858 五粮液、
  300750 宁德时代、002594 比亚迪、601318 中国平安。
- **测试 27 例**（`tests/`）：解析（页码绑定、目录页、章节双模式、页眉清理、
  别名与后缀白名单）、切分（不跨页、元数据完整性、碎块合并、兜底硬切）、
  检索（过滤、零分、引用变体、短语加成）。
- `README.md` / `CHANGELOG.md` / `EXTENSION.md` 三件套；`EXTENSION.md` 记录
  **取数类 7 条 + 解析切分类 13 条**实测坑与 10 条诚实不足。

### Fixed

解析层这一轮修的都是**"错了也看不出来"**的静默错误（引用仍会显示页码和章节名）：

- **章节识别整体重写为双模式**（原实现只按章节名匹配，且用宽松的
  `startswith + 剩余<=6 字符`）：
  - *误命中 1*：正文句子碎片「经审计后确认财务报告的真实性」被判成「财务报告」。
  - *误命中 2*：内控自我评价表里**独立成行**的「财务报告」单元格标签
    （与「非财务报告重大缺陷数量」成对）被判成章节标题，凭空切出假区间，
    吞掉真正的「环境和社会责任」等章节 —— 实测命中 000858 P37 / 002594 P72 / 300750 P64。
  - *修正*：报告带「第X节」锚点时**只认行首锚点**（正文交叉引用
    「详见第十节财务报告-九、…」不以「第X节」开头，可整类挡掉）；
    无锚点的港式体例（中国平安）退回「页首标题」。
  - *一次失败的尝试*：曾试图用「标题必须在页首」挡住上一条噪声，
    结果茅台的章节标题常落在页面中段/上一页页尾（P22 第 34 行、P7 页尾），
    一刀切把 600519 从 8 段切没了 6 段 —— 已弃用位置约束，改用锚点。
- **中国平安整本章节错乱**：原目录页判据是「一页命中 >=4 个不同章节」，
  而平安目录页只有 5 行，凑不满，导致 P2 的「财务报表」一路带到 149 页。
  → 增加判据「前 8 个非空行里有独立成行的**目录**」（实测 5 家年报全带这一行）。
- **港式体例支持**：平安没有 A 股那 10 个章节名，新增别名
  「关于我们」「经营情况讨论与分析 / 经营情况讨论及分析」「公司管治」
  「公司治理报告」「股本变动及股东情况」，以及 2025 年新体例的
  「公司治理、环境和社会」「环境与社会责任」。
- **平安 P134-P147 章节逐页横跳 8 次**：其"大节名/小节名"两个页眉元素换页时互换位置，
  `董事会报告和重要事项` 与 `公司管治` 交替生效。→ **刻意不收录**该别名，
  整段单调归入「公司治理」。
- **节号与标题分两行**（贵州茅台 `第九节`↵`债券相关情况`）识别不到 → 支持取下一行；
  节号在页尾、标题落到下一页页首时，该节从下一页算起。
- **标题行带目录点线与页码残留**（`释义 ......... 12`）匹配不到 → `_strip_leader`
  先剥点线页码再匹配。
- **前置页无节号**：茅台封面页只有一行「重要提示」，只用节号会让第一章节整段消失。
  → 首个锚点之前的页面退回「页首标题」兜底（范围受限于锚点之前，不会重新引入正文噪声）。
- **切分会在页尾多吐一个分隔符**：按 `。` 切时结尾空分段又被补上分隔符，
  拼出 `……分析。。`，使该块**不再是原文子串**（等于凭空多一个字符进索引）。
  → `_split_by_separators` 最后一段后不再补分隔符，并丢掉纯标点碎片。
- **小数被分词整类丢弃**：`45.2` / `123456.78` 不含中文、`str.isalnum()` 又因小数点
  返回 False，被当纯标点丢掉。→ 加 `_DECIMAL` 正则单独放行。
- 测试侧修正两处**用例自身的问题**：`len(config.CHUNK_OVERLAP)` 对 int 取长度；
  以及 BM25 短语加成用例只用 2 篇语料 —— `BM25Okapi` 的 IDF 在 N=2、df=1 时
  恒为 0，全部文档得 0 分，测的其实是 IDF 而不是加成（已补填充文档）。

### Changed

- `parse_pdf._detect_sections` 返回值由 `list` 改为 `(list, mode)`，
  产物 JSON 新增 `section_mode` 字段（`节号锚点` / `页首标题`）。
- 章节识别弃用位置约束，改用「第X节」行首锚点 + 目录页双重判据。
- 章节别名表按实测体例扩充，并删除会造成横跳的「董事会报告和重要事项」。
- 解析层新增 5 条回归用例（噪声行、节号锚点两种版式、目录页兜底判据、后缀白名单）。
