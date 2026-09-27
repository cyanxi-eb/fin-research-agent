# CHANGELOG

## [0.10.0] - 2026-09-27（Docker Compose 部署 + 真实 LLM 激活）

从"可运行 Demo"到"VM 上真实可访问服务"的里程碑。核心完成：容器化部署、Qdrant 向量库、MySQL Checkpointer 稳定化、真实 LLM API Key 激活。

### 容器化部署

- **`Dockerfile`**：13 层构建，`COPY seed/ /app/seed/` 包含 BM25 pkl + vectors.npy + meta.jsonl 三类种子文件
- **`docker-compose.yml`**：4 服务（App + Nginx + MySQL 8 + Qdrant + Prometheus/Grafana 可选），VM 端口映射 8001→8000、8081→80（避让 Dify 的 80/443/8000）
- **`entrypoint.sh`**：容器启动时执行 `ensure_mysql_database()` + `setup_db_tables()` + Qdrant collection 初始化
- **`.dockerignore`**：三层 bug 修复（见归档 `dockerignore三层bug复盘.md`）：
  1. `.gitignore` 的 `seed/data/index/` glob 把 pkl 也排除 → 加 negation rules
  2. `.dockerignore` 路径段匹配 `data/index/` 误命中 `seed/data/index/` → 根锚 `/data/index/`
  3. Dockerfile COPY 顺序在修复后正常包含 pickle + numpy

### Qdrant 向量库

- 直接 upsert 既有 `seed/data/vector/vectors.npy`（3161 × 1024，text-embedding-v4），**跳过在线 re-embedding** 节省成本
- Point ID = `abs(hash(chunk_id)) % (2^31-1)`，payload 含 `code/year/company/page_no/section`
- Collection：`fin_research_vectors`，1024 维，COSINE 距离

### MySQL Checkpointer 稳定化（三次迭代）

| Commit | 方案 | 效果 |
|---|---|---|
| `f22d974` | `conn.ping(reconnect=True)` on cached conn | ❌ 不够（PyMySQLSaver 内部缓存旧 cursor） |
| `cad3b06` | **每次调用新建 conn + saver**（放弃进程内缓存） | ✅ 彻底消除 stale socket |
| `570fb2e` | MySQL `--wait-timeout=86400`（24h） | ✅ 双保险 |

根因详见归档 `checkpoint-mysql连接陷阱.md`。

### Nginx Resolver 修复

- **`fin-research.conf`**：加 `resolver 127.0.0.11 valid=30s ipv6=off` + `set $grafana_upstream grafana:3000` + `proxy_pass http://$grafana_upstream/`
- **根因**：Nginx 启动时 Grafana 可能还没起来，`proxy_pass grafana:3000` 预解析失败 → 容器 crash

### LLM 真实激活

- VM 上 `~/fin-research-agent/.env` 写入 `DEEPSEEK_API_KEY` + `QWEN_API_KEY`
- `.env` gitignored，**绝不入库**
- `/api/ask` 实测 `degraded=False`：
  - analysis 路径：「贵州茅台2024年营收」→ `1,741.44 亿元（来源：合并利润表）`
  - rag 路径：「茅台的文化核心价值观」→ 完整 LLM 生成答案 + 3 条 citations

### Added

- `scripts/vm_ssh.py`：paramiko-based SSH helper，env `VM_PASSWORD`，支持命令执行与文件上传
- `docs/ARCHITECTURE.md`：v2.0 已部署架构文档（容器拓扑 + 请求流程 + 关键决策 + 已知限制）
- `docs/archive/`：部署踩坑归档（.dockerignore 三层 bug、checkpoint MySQL 陷阱、VM 部署时间线）

---

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
