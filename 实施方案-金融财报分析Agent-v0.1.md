# 金融研报 / 财报分析 Agent — 实施方案

> 版本：v0.1 | 日期：2026-09-22 | 状态：待评审
> 上游依据：[`架构设计文档-金融财报分析Agent-v1.0.md`](./架构设计文档-金融财报分析Agent-v1.0.md)
> 本文回答的是「**先做哪个文件、做完怎么验**」，不是再讲一遍架构。

---

## 0. 先摆事实：本次实测的数据源可用性

方案里的取数环节全部基于**实测结果**，不是"按文档推测"：

| 数据源 | 用途 | 实测结果 | 关键参数（踩过才知道） |
|---|---|---|---|
| **巨潮资讯** `hisAnnouncement/query` | 年报 / 公告 **PDF 原件** | ✅ 可查可下 | `stock` 必须传 **`代码,orgId`**（如 `600519,gssh0600519`）；`column` 要分 `sse`/`szse`；只传 `600519` 返回 **0 条**（静默空，不是被墙） |
| 巨潮静态资源 | PDF 下载 | ✅ | `http://static.cninfo.com.cn/` + 返回的 `adjunctUrl`（实测 `finalpage/2025-04-03/1222993920.PDF`，3.5MB） |
| **东财数据中心** `datacenter-web.eastmoney.com` | 三大报表 / 业绩指标 | ✅ 返回真实数据 | `reportName=RPT_LICO_FN_CPD` 等；实测拿到含 `2026-06-30` 报告期的营收/净利 |
| 东财行情 `push2.eastmoney.com` | 行情快照 | ✅ | `fs=m:0+t:6` 等板块过滤 |
| 新浪财经 `hq.sinajs.cn` | 行情兜底 | ✅（**必须带 `Referer: https://finance.sina.com.cn`**） | 不带 Referer 直接 **403** |
| PyPI | 装依赖 | ✅ 阿里云源 200 | 容器内清华源会 403，统一用阿里云 |

**结论**：数据侧不构成阻塞，W1 可以直奔真实数据，不需要先用假数据搭架子。

---

## 1. 总体策略：复用 workflow-agent 的工程骨架

不重造轮子。`workflow-agent` 已经把「双后端 / Checkpointer / 配置 / 容器化 / 文档体例」跑通过真实 Docker 部署，这些**与领域无关**，直接搬：

| 复用项 | 从哪来 | 为什么要复用 |
|---|---|---|
| 多通道 LLM 配置（`_secret()` 三级解析） | `workflow-agent/src/config.py` | 集中式 Key 管理是你的既定偏好；源码不留兜底 Key |
| Checkpointer 工厂（SQLite/MySQL） | `workflow-agent/src/checkpoint.py` | 本项目 HITL「低置信度人工确认」需要**跨进程可恢复**，直接继承已验证实现 |
| 双后端业务库 + `_MySQLConn` 包装 | `workflow-agent/src/db.py` | 审计日志/指标表要能进 MySQL；SQL 只写一份 |
| `Dockerfile` / `entrypoint.sh` / `docker-compose.yml` | `workflow-agent/` | 私有化交付卖点已跑通（含 seed 不能放 `/app/data` 等硬约束） |
| 测试隔离约定 | `workflow-agent/tests/conftest.py` | 强制单测跑 SQLite，不许意外打到真库 |
| `README / CHANGELOG / EXTENSION` 三件套体例 | `workflow-agent/` | 面试官看的是工程纪律一致性；**连续两个项目同一套规范**本身就是加分点 |
| **EXTENSION §5 坑表** | `workflow-agent/EXTENSION.md` | 直接继承，别再踩 `NOW()` 绑定参数、DictCursor、MySQL 卷只初始化一次… |

**一句话**：`src/graph`、`src/retrieve`、`src/ingest`、`src/tools` 是新的；`config / db / checkpoint / 部署 / 文档体例` 是搬的。

---

## 2. 目标目录结构

标 🆕 的是本项目新增，标 ♻️ 的是从 workflow-agent 搬改。

```
fin-research-agent/
├─ 架构设计文档-金融财报分析Agent-v1.0.md          # 已存在
├─ 实施方案-金融财报分析Agent-v0.1.md              # 本文档
├─ README.md / CHANGELOG.md / EXTENSION.md        ♻️ 三件套
├─ requirements.txt / .env.example / .gitignore   ♻️
├─ Dockerfile / entrypoint.sh / docker-compose.yml ♻️
├─ launcher.py                                    ♻️ 一键启动（后置，Step 6）
│
├─ config/
│  └─ watchlist.yaml                              🆕 目标公司清单（代码/orgId/行业/报告期范围）
│
├─ data/                                          （全部 gitignore）
│  ├─ raw/{code}/{year}_annual.pdf                🆕 原始年报 PDF
│  ├─ parsed/{code}/{year}.json                   🆕 按页解析产物（含页码/章节）
│  ├─ index/bm25.pkl                              🆕 BM25 索引
│  ├─ vector/                                     🆕 向量库持久化目录
│  ├─ regulation/                                 🆕 法规文本入库源
│  ├─ db/                                         🆕 SQLite 业务库
│  ├─ db_keys.local.json(.example)                ♻️
│  └─ llm_keys.local.json                         ♻️
│
├─ src/
│  ├─ config.py                                   ♻️ 改：多 watchlist / 检索参数 / 单位口径
│  ├─ llm.py                                      ♻️ 搬：多通道 + 余额查询
│  ├─ db.py                                       ♻️ 改：换成本项目的表结构
│  ├─ checkpoint.py                               ♻️ 原样搬
│  │
│  ├─ ingest/                                     🆕 数据管线（架构 §4）
│  │  ├─ fetch_cninfo.py      # 查公告 → 下 PDF；orgId 映射与参数校验
│  │  ├─ fetch_eastmoney.py   # 三大报表/指标 → 结构化库（含 akshare 可选兜底）
│  │  ├─ parse_pdf.py         # PyMuPDF 按页提取 + 章节识别（页码必须留）
│  │  ├─ parse_tables.py      # 三大报表表格抽取 → Markdown + 结构化库
│  │  ├─ chunk.py             # 切分 + 元数据 {公司,年份,章节,页码,类型}
│  │  └─ index.py             # 建 BM25 索引 / 向量索引
│  │
│  ├─ retrieve/                                   🆕 检索层（架构 §4.2）
│  │  ├─ bm25.py              # 关键词检索（零外部依赖，最先可用）
│  │  ├─ vector.py            # 向量召回
│  │  ├─ fusion.py            # RRF 融合
│  │  ├─ rerank.py            # bge-reranker，缺模型时直通降级
│  │  └─ pipeline.py          # 统一入口：元数据过滤→召回→融合→重排→topk
│  │
│  ├─ tools/                                      🆕 工具层（架构 §5）
│  │  ├─ registry.py          # 工具注册表 + JSON Schema（供 function calling / 后置 MCP）
│  │  ├─ indicators.py        # get_financial_indicator / compare_companies
│  │  ├─ ratios.py            # calc_financial_ratio（返回计算过程，可审计）
│  │  ├─ regulation.py        # search_regulation（强制 BM25 + 必须带条文号）
│  │  ├─ announcements.py     # get_announcement
│  │  └─ charts.py            # plot_trend
│  │
│  ├─ graph/                                      🆕 LangGraph 编排（架构 §3）
│  │  ├─ state.py             # 状态 TypedDict（问题/意图/命中/工具结果/答案/引用/置信度）
│  │  ├─ router.py            # 意图路由（rag / analysis / compliance）
│  │  ├─ nodes.py             # 公共节点（retrieve / call_tools / generate / cite）
│  │  ├─ subgraph_rag.py      # 文档问答子图
│  │  ├─ subgraph_analysis.py # 指标分析子图（数值必须走工具）
│  │  ├─ subgraph_compliance.py # 合规核查子图
│  │  ├─ verify.py            # 引用校验 + 低置信度 → HITL 挂起
│  │  └─ builder.py           # 编译主图
│  │
│  ├─ answer.py               # 答案合成 + 引用列表 + 免责声明 + 拒答策略
│  ├─ audit.py                # 审计日志落库（问/检索/工具/答/引用）
│  └─ server.py               # FastAPI：SSE 问答 / 对比 / 引用详情 / HITL 确认 / health
│
├─ web/index.html                                 🆕 单文件前端（对话 + 引用卡片 + 对比表 + 图表）
│
├─ scripts/
│  ├─ probe_sources.py        🆕 数据源探活（把 §0 的实测固化成脚本）
│  ├─ init_db.py              ♻️ 改表结构
│  ├─ ingest_all.py           🆕 一键入库：fetch→parse→chunk→index
│  ├─ eval_rag.py             🆕 评估（RAGAS + 数值准确率 + 引用命中率）
│  ├─ smoke_qa.py             🆕 端到端冒烟（数值/法规/拒答/引用各一条）
│  └─ vm_ssh.py               ♻️ 原样搬（VM 部署用）
│
├─ eval/
│  ├─ golden_qa.jsonl         🆕 50-100 条测试集
│  └─ report.md               🆕 评估报告（简历量化素材）
│
└─ tests/
   ├─ conftest.py             ♻️ 强制 sqlite + FakeLLM + FakeRetriever
   ├─ test_probe_sources.py   🆕 源可用性（网络用例，可 skip）
   ├─ test_parse_page_meta.py 🆕 页码/章节元数据不丢
   ├─ test_chunk_meta.py      🆕
   ├─ test_retrieval.py       🆕 BM25/RRF 确定性用例
   ├─ test_tools_ratios.py    🆕 比率计算口径（数值断言）
   ├─ test_answer_citation.py 🆕 引用格式与拒答
   ├─ test_router.py          🆕 意图路由
   └─ test_mysql_checkpointer.py ♻️ 沿用
```

---

## 3. 分步实施（每步都是一次可演示的增量）

原则：**先让数据进来 → 先让字面检索能溯源 → 再上向量 → 最后上编排**。任何一步中断，手上都有一个能演示的东西。

### Step 0 — 骨架 + 数据源探活（半天）

| 文件 | 做什么 |
|---|---|
| 目录树 + 各 `__init__.py` | 建空包，锁死结构 |
| `requirements.txt` | **先最小集**：fastapi/uvicorn/langchain-openai/langgraph/pydantic/pymupdf/rank-bm25/jieba/pyyaml。**暂不放** chromadb、torch、ragas（后置步骤再加，避免第一天就卡在装包） |
| `src/config.py` | 搬 workflow-agent 的三级密钥解析；新增 `WATCHLIST`、`DATA_DIR` 子目录、检索参数（topk/权重）、单位口径表 |
| `scripts/probe_sources.py` | 固化 §0 的探测：4 个源 + 关键参数（新浪带 Referer、巨潮传 `代码,orgId`），逐个打印 OK/FAIL 与样本 |
| `README.md` / `CHANGELOG.md` / `EXTENSION.md` | 三件套起头；EXTENSION 直接继承 workflow-agent §5 坑表 |
| `.gitignore` / `.env.example` | 数据与密钥不入库 |

**验收**：`python scripts/probe_sources.py` 四源全绿并打印样本数据；`python src/config.py` 自检通过。

---

### Step 1 — 数据管线（一）：拿原文 + 建字面索引（1～1.5 天）

**为什么先不上向量**：BM25 零额外依赖，当天能跑通；且能立刻把「**答案带页码**」这个金融 RAG 的硬卖点立起来。向量是**提升**，不是**前提**。

| 文件 | 做什么 |
|---|---|
| `config/watchlist.yaml` | 3-5 家公司：`{code, name, org_id, market, industry, years}`。orgId 从巨潮查一次写死，避免每次反查 |
| `src/ingest/fetch_cninfo.py` | 按 watchlist 查 `hisAnnouncement/query` → 过滤年报 → 拼 `static.cninfo.com.cn` 下载到 `data/raw/{code}/{year}_annual.pdf`；**幂等**（已存在跳过，带 size 校验） |
| `src/ingest/parse_pdf.py` | PyMuPDF 逐页提取 → `data/parsed/{code}/{year}.json`，每页 `{page_no, text, section}`；章节识别用财报固定结构关键词 |
| `src/ingest/chunk.py` | 递归切分 512/64，**表格整块不切**；chunk 元数据 `{code, company, year, report_type, section, page_no, chunk_id}` |
| `src/retrieve/bm25.py` | jieba 分词 + `rank_bm25`，索引落 `data/index/bm25.pkl`；支持元数据过滤（公司/年份） |
| `scripts/ingest_all.py` | 串起 fetch→parse→chunk→index，输出统计（公司数/页数/chunk 数/耗时） |

**验收**：一条命令跑完，输出统计；搜「毛利率」能命中正确公司与页码；`tests/test_parse_page_meta.py`、`test_chunk_meta.py` 绿。

---

### Step 2 — 数据管线（二）：结构化库 + 工具层（1 天）

| 文件 | 做什么 |
|---|---|
| `src/db.py` | 换表：`companies` / `reports` / `financial_indicators` / `audit_logs` / `golden_qa`；双后端 DDL 两份 |
| `src/ingest/fetch_eastmoney.py` | 东财数据中心拉三大报表 → 清洗入 `financial_indicators`（实测量够用；akshare 作可选兜底开关） |
| `src/tools/registry.py` | 工具注册表：函数 + JSON Schema + 描述，统一导出给 LLM function calling |
| `src/tools/indicators.py` | `get_financial_indicator` / `compare_companies`，返回带 `unit` / `source` / `report_date` |
| `src/tools/ratios.py` | `calc_financial_ratio`（毛利率/净利率/ROE/资产负债率…）**返回计算过程**（分子分母都列出） |
| `scripts/init_db.py` | 建表 + 灌演示数据 |

**口径集中管理**（关键，别散在代码里）：`src/config.py` 维护指标字典 `{中文名: {别名, 单位, 报表, 口径}}`，把「营业总收入/营业收入」「归母净利润/净利润」这类**别名映射**收口，否则 LLM 选指标会选错。

**验收**：`get_financial_indicator("贵州茅台","营业总收入")` 返回正确序列与单位；`calc_financial_ratio` 的分子分母可核对；`test_tools_ratios.py` 数值断言绿。

---

### Step 3 — RAG 问答子图 + 引用溯源（1.5～2 天）

**第一个「能问答」的闭环**。此步仍然只用 BM25，把链路和引用格式先跑通。

| 文件 | 做什么 |
|---|---|
| `src/graph/state.py` | 状态定义：`question / intent / hits / tool_results / answer / citations / confidence` |
| `src/retrieve/pipeline.py` | 统一检索入口（本步内部实现=BM25 直通，接口留好给 Step 4 插向量） |
| `src/graph/nodes.py` | `retrieve` → `generate` → `cite` 三个节点 |
| `src/answer.py` | 答案合成 + 引用列表格式化为 `[1] 贵州茅台2024年年报 P87 第三节` + **超范围拒答** + 免责声明 |
| `src/server.py`（最小） | 先只加 `/api/ask` 与 `/api/health`，够冒烟即可 |
| `scripts/smoke_qa.py` | 4-5 条代表性问题冒烟 |

**验收**：5 条问题中 ≥4 条**引用页码正确**（人工核对）；问「公司食堂菜谱」这类超范围问题能拒答而不是编。
`test_answer_citation.py` 绿。

---

### Step 4 — 向量召回 + RRF 融合 + 重排（1 天，可降级）

这一步才是「混合检索」面试题的答案。**必须留下 Step 3 vs Step 4 的指标对比**，否则讲不出提升。

| 文件 | 做什么 |
|---|---|
| `src/ingest/index.py` | 向量化入 Chroma，`data/vector/`；元数据同步（公司/年份/section/page） |
| `src/retrieve/vector.py` | 向量召回 top-20（带元数据过滤） |
| `src/retrieve/fusion.py` | RRF 融合 BM25 + 向量两路 |
| `src/retrieve/rerank.py` | bge-reranker 重排 top-5；**模型缺失时直通**（保证可降级跑） |
| `src/retrieve/pipeline.py` | 补全：过滤 → 双路召回 → RRF → rerank → topk |
| `src/config.py` | 检索参数（各路 topn、RRF k、是否启用 rerank）可配 |

**验收**：同一批问题，Step 4 的 `recall@5` / 引用命中率高于 Step 3，并把数字记入 `eval/report.md`。

---

### Step 5 — LangGraph 完整编排 + 引用校验 + HITL（1.5 天）

| 文件 | 做什么 |
|---|---|
| `src/graph/router.py` | 意图路由：`rag`（文档问答）/ `analysis`（数值）/ `compliance`（法规） |
| `src/graph/subgraph_analysis.py` | 数值子图：**强制走工具**，数值不允许 LLM 从原文读 |
| `src/graph/subgraph_compliance.py` | 法规子图：强制 BM25 权重，答案必须带条文号 |
| `src/graph/subgraph_rag.py` | 文档子图：检索 → 生成 → 引用 |
| `src/graph/verify.py` | **引用校验**：回查引用 chunk 是否真支持结论；低置信度 → 挂起进 HITL |
| `src/checkpoint.py` | ♻️ 复用；HITL 挂起要能**跨进程恢复** |
| `src/graph/builder.py` | 编译主图，接入 Checkpointer |
| `src/audit.py` | 审计日志：问/命中/工具调用/答/引用 全落库 |
| `src/server.py` | 补 SSE 流式 + `/api/compare` + `/api/citations` + `/api/hitl/{id}/confirm` |

**验收**：三类问题分别命中三条子图；数值题答案数值与库一致（零幻觉）；低置信度问题挂起 → 重启服务 → 仍能读回并人工确认（复用 workflow-agent 的跨进程验收套路）。

---

### Step 6 — 服务化 + 前端 + 评估 + 容器化（2 天）

| 文件 | 做什么 |
|---|---|
| `web/index.html` | 单文件：对话 + 可点击引用卡片 + 对比表 + 趋势图 |
| `eval/golden_qa.jsonl` | 50-100 条：数值题 / 法规题 / 拒答题 / 引用题 |
| `scripts/eval_rag.py` | RAGAS（faithfulness/answer_relevancy）+ 数值准确率 + 引用命中率 |
| `eval/report.md` | 评估报告（**简历量化素材**） |
| `Dockerfile` / `entrypoint.sh` / `docker-compose.yml` | ♻️ 搬改；Milvus 视决策点再决定是否进 compose |
| `launcher.py` | ♻️ 一键启动 |
| `README / CHANGELOG / EXTENSION` | 三件套补齐 + §5 坑表回填本项目新增的坑 |

**验收**：`eval/report.md` 三个数字齐全；`docker compose up -d` 后 `/api/health` 通过；README 一条命令可复现。

---

## 4. 两处需要拍板的取舍（我的建议 + 理由）

### 决策点 1：向量库 Chroma → Milvus

**建议：先 Chroma 跑通全链路，Milvus 作为 Step 6 的可选项。**

- Chroma 是 `pip install`，零运维，本机直接跑；Milvus 是独立服务（etcd+minio+proxy），**VM 上 Dify 已经占着 load 20+，再上 Milvus 不现实**。
- 简历写 Milvus 完全没问题，但前提是**真的跑过**——所以放在 Step 6 用 Docker 单机版验证一次即可，不必让它贯穿开发。
- 风险对冲：`src/retrieve/vector.py` 只暴露 `add / query / persist` 三个方法，换 Milvus 只改这一个文件。

### 决策点 2：Embedding / Reranker 用本地 bge 还是 API

**建议：开发期用 `bge-small-zh-v1.5`（约 95MB）跑通，交付前换 `bge-large-zh` + `bge-reranker-base`。**

- 本地模型是「私有化断网」卖点的**实质**，不能只写不跑；但这台机器无 GPU，`large` 会慢，开发期用小模型保持迭代速度。
- 模型大小写进 `src/config.py` 一处，切换只改配置——顺手又是一个"可切换"的工程点。
- 备选降级：若某天装 torch 出问题，`rerank.py` 已有直通分支，链路不会断。

### 另外两个小选择（不阻塞，先按建议走）

- **取数实现**：核心两件事（公告 PDF + 三大报表）**自己写薄封装**（已实测的 3 个接口足够），akshare 只作可选兜底。理由：akshare 依赖 pandas 全家桶、版本漂移快，而我们需要的是**可控 + 可测**。Hmm——如果你更想省事，也可以一开始就用 akshare，我在 `fetch_eastmoney.py` 里留了开关。
- **前端**：单文件 `web/index.html`（对齐 workflow-agent），不上 Vue。演示够用，省下的时间给评估集。

---

## 5. 风险与降级路径

| 风险 | 触发信号 | 降级/对策 |
|---|---|---|
| PyMuPDF 对某些年报版式解析乱 | 章节识别率低 | 退化为「按页 + 正则锚点」；不追求完美章节 |
| 财报表格抽取（Camelot 需 ghostscript）麻烦 | Step 2 卡住 | **表格不走 PDF**：三大报表直接取东财结构化接口（已实测可用），PDF 表格抽取降级为可选 |
| 向量/重排装不上的那天 | torch 装包失败 | `pipeline.py` / `rerank.py` 都留了直通开关，随时回到 BM25 单路 |
| VM 资源不够 | 部署时 OOM | 演示用小 embedding 模型；Milvus 不上 VM，或改用本机演示 |
| 时间不够 | — | 砍合规子图 + 砍前端图表，保「RAG 溯源 + 工具兜底」两大主线（与架构文档 §10 一致） |

---

## 6. 文档与版本约定（沿用 workflow-agent）

- 目录名跨版本保持稳定；每次迭代三件套同步：`README.md`（含手动修改接口约定表）、`CHANGELOG.md`（Added/Changed/Fixed/Verified）、`EXTENSION.md`（坑表 + 扩展点）。
- 本文档为 **v0.1**；每完成一步，把该步的实际产出与偏差回写，版本号 +0.1。
- 坑一律进 `EXTENSION.md §5`，体例沿用三列：坑 / 现象 / 解法。
- 复用来的骨架文件，文件头注释标明「来自 workflow-agent」，方便两个项目互相对照（也是面试时的"同一套工程规范"证据）。

---

## 7. 建议的起手动作

按依赖顺序，**下一步就是 Step 0 + Step 1 合并执行**（骨架 + 探活脚本 + 拿第一家公司的年报 PDF + 建 BM25 索引），
跑通后你会拿到第一个可验证的产物：**能按页码检索到真实年报原文的索引**。

确认这份方案后我就开工 Step 0。
