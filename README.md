# fin-research-agent — 金融财报分析 Agent（v0.10.0，Docker 部署 + LLM 激活）

> 面向券商研究所 / 合规 / 投研场景的**年报问答与分析 Agent**。
> 核心卖点是**可核验的溯源**：每条结论都能落到
> `[1] 贵州茅台2024年年报 P87 管理层讨论与分析 (第2/3段)` 这种可人工翻页核对的出处，
> 而不是一段没有依据的文字。

**VM 已部署**（2026-09-27）：`http://192.168.57.128:8001/api/health` —— `llm.ready=True`。

上游文档（先文档、后代码）：
[架构设计文档 v2.0（已部署）](./docs/ARCHITECTURE.md) ·
[架构设计文档 v1.0（原始设计）](./架构设计文档-金融财报分析Agent-v1.0.md) ·
[企业级落地方案](./企业级落地方案.md) ·
[实施方案 v0.1](./实施方案-金融财报分析Agent-v0.1.md) ·
[项目总结](./项目总结.md) ·
[项目讲解稿](./项目讲解稿.md) ·
[踩坑归档 →](./docs/archive/) ·
[扩展与已知坑](./EXTENSION.md) · [版本记录](./CHANGELOG.md) ·
[检索评测报告](./eval/report.md) · [答案级评测报告](./eval/report_answer.md)

---

## 快速测试

```bash
# VM 上直接打接口
curl -X POST http://192.168.57.128:8001/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"贵州茅台2024年营收","use_llm":true}'

# 本机 Docker Compose（需要 .env 写 DEEPSEEK_API_KEY / QWEN_API_KEY）
APP_PORT=8001 FA_AUTH_ENABLED=0 FA_VECTOR_BACKEND=qdrant \
  docker compose up -d --build
```

---

## 当前进度（Step 7 已完成：Docker Compose 部署 + LLM 激活；v0.10.0）

| 本轮（v0.10.0） | 内容 | 状态 |
|---|---|---|
| ① Docker Compose 4 容器 | App (FastAPI) + Nginx + MySQL 8 + Qdrant（3161 pts × 1024 dim） | ✅ |
| ② LLM 真实激活 | VM `.env` 写入 API Key；`/api/ask` 返回 `degraded=False` 中文答案 | ✅ |
| ③ Checkpointer 稳定化 | 放弃 MySQL 长连接缓存（三次迭代后每次新建 conn + saver） | ✅ |
| ④ `.dockerignore` 三层 bug 修复 | 根锚路径段 + gitignore negation rules | ✅ |
| ⑤ Nginx resolver 启动修复 | `resolver 127.0.0.11` + 变量 `proxy_pass` | ✅ |

| v0.8.0 | 内容 | 状态 |
|---|---|---|
| ① 登录 + JWT 鉴权 | `users` 表（pbkdf2 口令）+ 注册/登录/刷新/注销 + Bearer 保护业务端点 + `actor` 取自 token（修不足 G-11 的鉴权半边） | ✅ |
| ② 库外问题联网兜底 | 检索拒答时自动去网上找 → **≥2 个独立域名交叉验证一致才展示并入库**（独立网络语料区，不污染年报索引） | ✅ |
| ③ 前端工作台级改版 | 登录页 / 会话侧栏 / 骨架屏 / 深浅色 / 网络结果卡片，**仍是单文件零 CDN 零构建** | ✅ |

| Step | 内容 | 状态 |
|---|---|---|
| 0 | 骨架 + 数据源可用性实测 | ✅ |
| 1 | 年报 PDF 入库 + 按页解析 + BM25 字面索引 + 引用格式 | ✅ |
| 2 | 结构化财务库（东财 + 新浪双源）+ 只读工具层 | ✅ |
| 3 | RAG 问答子图 + 引用生成与校验 + 双闸门拒答 + 最小 HTTP 服务 | ✅ |
| 4 | 向量召回 + RRF + 重排（混合检索） | ✅ |
| 5 | 完整 LangGraph 编排（路由 / 三子图 / 引用校验 / HITL / 审计） | ✅ |
| 6 | 服务化补全（SSE 流式 / 多公司对比 / 多轮指代）+ 单文件前端 + 答案级评估 + 容器化交付 | ✅ |
| **7** | **Docker Compose 4 容器 + Qdrant 向量库 + LLM API Key 激活 + Checkpointer 稳定化** | ✅ |

**Step 1 实测规模**：5 家公司 / 6 个公司-年度 / 3161 个 chunk / BM25 索引 8.4MB，
全流程（解析→切分→建索引）冷跑约 34s。

**Step 2 实测规模**：5 家公司 / **22 个指标口径** × 6 期 ——
制造业四家各 132 个值（22/22 满覆盖），中国平安 120 个值（20/22，
缺的「毛利率」「营业成本」是**保险业确实没有的科目**，不是抓取缺陷）；
会计恒等式（总资产 − 总负债 = 所有者权益合计）**五家全部 0.0000% 误差**；
工具层 4 个工具 / 6 个比率口径。

**Step 3 实测结果**（`scripts/smoke_qa.py`，细节见下节）：

| 项 | 数字 |
|---|---|
| 冒烟用例（调真实模型） | 6 条 → **通过 4 / 基线 2 / 失败 0** |
| 冒烟用例（`--no-llm` 离线） | **6 条全部通过** |
| 引用页码核验（拿片段回**源 PDF** 该页找原文） | 调模型 **8/8 命中**；离线 **25/25 命中**，对不上 0 |
| 超范围拒答 | 「公司食堂菜谱有什么推荐」→ 拒答，**不调模型**、不给引用 |
| 测试 | **149 例**（Step 2 结束时 115 例） |

**Step 4 实测结果**（`eval/report.md`，34 题消融评测，**当时口径**——Step 6 评测集已扩到 55 题，见下文，两者分母不同不可直接比）：

| 配置 | page_hit@5 | 年份精度 | 公司精度 | 拒答正确率 |
|---|---|---|---|---|
| ① bm25（无过滤，= Step 3 行为） | 24.0% | 84.1% | 52.4% | 40.0% |
| ② bm25 + 自动过滤 | 40.0% | 100% | 100% | 80.0% |
| ③ bm25 + 过滤 + 每页配额 | 40.0% | 100% | 100% | 80.0% |
| ④ **hybrid（双路 + RRF）+ 过滤 + 配额** | **48.0%** | **100%** | **100%** | 80.0% |
| ⑤ hybrid + rerank | 40.0% | 100% | 100% | 80.0% |

- **page_hit@5 24.0% → 48.0%（+24.0pp）**，且 **0 空召回**；
  拆开看：只加自动过滤值 +16.0pp，再加向量+RRF 值再 +8.0pp。
- 向量库：**3161 × 1024**（`text-embedding-v4`，建库 312s）；
  端到端冒烟（hybrid 调真实模型）**6 条 → 通过 4 / 基线 2 / 失败 0**，引用回源 PDF **11/11 命中**。
- 测试 **149 → 205 例**（新增 56 例：RRF 性质 / 重排降级 / 过滤纪律 / 向量错配不变量）。
- ⑤ 比重排前**低 8pp**（48.0% → 40.0%），与 Step 4 的结论一致：交叉编码器优化"语义相关"，
  而本评测的期望是"这一页里有那个数字"，两者不总一致 → 默认 `RERANK_BACKEND=passthrough`。

> ⚠️ **以上数字是 2026-09-22 语料修复后重测的**（Step 5 期间修掉两个解析 bug → 重新入库 →
> chunk 2650 → 3161 → 金标准 `expected_pages` 由 `build_golden.py` 重算 → 页级题 21 → 25）。
> Step 4 当时（旧语料）的数字是 14.3% / 38.1% / 52.4% / 52.4%，保留在 CHANGELOG 0.5.0 里。
> **规则**：重入库后必须重跑 `build_golden.py` + `eval_retrieval.py` —— 期望页来自解析产物，
> 产物变了期望就变了，这不是"指标在抖"，而是**标注在跟着语料校准**。

> ⚠️ 默认 `RETRIEVE_MODE=bm25` —— **Step 4 的能力是"可选启用"而不是"悄悄换掉基线"**，
> 这样 Step 3 的对照实验随时能原样复现。要开混合检索：`RETRIEVE_MODE=hybrid`（或 `--mode hybrid`）。

**Step 5 实测结果**（真实模型 `deepseek-flash`，三条意图端到端，详见下节）：

| 问题 | 路由（规则优先） | 答案 | verify |
|---|---|---|---|
| 贵州茅台2024年的毛利率是多少 | `analysis`（`numeric_cue+indicator`，0.8） | **91.93%**，带公式 + 分子分母来源字段 + 官方口径对账（差异 -0.00pp） | 11 数字全有出处 |
| 贵州茅台2024年年报中货币资金余额是多少 | `rag`（`numeric_cue_without_indicator`，0.4） | **59,295,822,956.89 元**（合并）/ 77,252,079,198.82 元（母公司） | 3 数字全有出处 |
| 上市公司定期报告披露期限有何规定 | `compliance`（`compliance_cue`，0.9） | 226 号第三十二条等条文原文 + 现行状态 | 15 数字全有出处 |

- 三条全部 `verify.supported = true`、`unsupported = []`、`dangling_citations = []`。
- 数字**人工回源核对**通过：P11「研发投入合计 695,376,735.81」、P13「货币资金 59,295,822,956.89」。
- 法规库 **132 条**（226 号 67 条 + 182 号 65 条，两份官方原文，条级引用带现行/废止状态）。
- HITL 演练（`FA_HITL_FORCE_REASON=low_confidence`）：进程 A 挂起 → 清 saver 缓存（模拟换进程）
  → 进程 B 读回并 confirm，审计记 `hitl_confirm`。
- 数值题实测（`scripts/smoke_qa.py` 已改为按设计路径判）：「中国平安的归母净资产是多少」
  → 路由到工具层 → **9,286.00 亿元**（来源 `sina_balance.归属于母公司的股东权益合计`），6 期全带来源。
- 测试 **205 → 307 例**（新增 102 例）。

> 🐞 Step 5 期间发现并修复了**两个解析层的真 bug**（都会让数据/引用悄悄错掉，不报错）：
> ① 报表主表**金额被当成"跨页页眉"整行删除** → "某科目金额是多少"这类最常见提问答不出；
> ② 每章起始页的**首个 chunk 被标成上一章**（页首印刷页码被当成"上一节的正文"）→ 引用里的章节名写错。
> 两个都改了代码并**重新入库**（只改代码不改语料等于没修），详见下面 Step 5 章节与 CHANGELOG 0.6.0。

**Step 6 实测结果**（服务化 + 前端 + 答案级评估 + 容器化，细节见下节 Step 6）：

答案级评测（`eval/report_answer.md`，生成时间 **2026-09-23 08:34:28**，本次耗时 823s，**判分模型 `deepseek-flash`**）：

| 指标 | 值 | 分母（口径） |
|---|---|---|
| **数值准确率** | **100.0%** | 31 题（`gt.type==indicator`，含单位换算/千分位/符号） |
| **answer_relevancy**（答案相关性） | **83.6%** | 45 题（`answer_expect.judge=true`） |
| **faithfulness**（忠实度） | **20.3%** | 45 题（同上；未判 0 题） |
| **引用命中率** | **20.0%** | 40 题（`expected_pages` 非空） |

> ⚠️ **faithfulness 的头条数字要按「有无检索上下文」分桶读**：
> 有检索上下文（literal / article）14 题 → faithfulness **65.4%** / answer_relevancy 47.5%；
> 无检索上下文（indicator，走工具层）31 题 → faithfulness **0.0%** / answer_relevancy **99.8%** ——
> 数值题走工具层、送进判分 prompt 的【资料】本就为空，判成 0 分是**结构性**的，不是「答案在编造」。
> **数值题的正确性看「数值准确率」（100.0%）**，faithfulness 才是引用题/法规题的读数。

检索级评测（`eval/report.md`，生成时间 **2026-09-23 04:16:29**，耗时 53s，55 题）见下节 Step 6；
最省事的容器交付是 `docker compose up -d --build`，**容器链路只在 VM `192.168.57.128` 实测**（本机 Windows 无 Docker）。

## 目录结构

```
fin-research-agent/
├─ 架构设计文档-金融财报分析Agent-v1.0.md   # 设计文档（先文档后代码）
├─ 实施方案-金融财报分析Agent-v0.1.md       # 分 Step 的落地方案
├─ README.md / CHANGELOG.md / EXTENSION.md
├─ Dockerfile / .dockerignore / docker-compose.yml / entrypoint.sh   # ★Step 6：容器化交付
├─ launcher.py                              # ★Step 6：一键启动（预检 + 起 uvicorn + 开浏览器；--docker 改走 compose）
├─ .env.example                             # 容器/服务形态的变量模板（含 compose 相关键）
├─ web/
│  └─ index.html                            # ★Step 6：单文件前端（内联 CSS/JS，五项能力，零 CDN、零构建）
├─ seed/                                    # ★Step 6：容器启动用的语料副本
│  ├─ business.json                         # 业务库种子（financial_indicators ≥ 640 行；容器内实测载入 651 行）
│  └─ data/{parsed,index,vector,regulation}/  # 解析/索引/向量/法规产物（必须与 seed 一起上传）
├─ config/
│  └─ watchlist.yaml                        # ★手动入口：目标公司清单（改这里加公司/年份）
├─ src/
│  ├─ config.py                             # ★集中式配置（路径/检索参数/指标与比率口径/多模型通道）
│  ├─ net.py                                # HTTP 薄封装（限速 + 重试 + UA/Referer + 断点续传式下载）
│  ├─ citation.py                           # ★引用格式（唯一出口，改格式只改这里）
│  ├─ embedding.py                          # ★可插拔向量化通道（api / local / none，统一入库与查询口径）
│  ├─ answer.py                             # ★答案合成 + 引用编号校验 + 双闸门拒答 + 降级摘录
│  ├─ llm.py                                # 多供应商通道（DeepSeek/通义/本地）+ is_ready()
│  ├─ numeric.py                            # ★Step 5：数字抽取 / 单位换算 / 容差比对 / 无出处数字检测
│  ├─ audit.py                              # ★Step 5：审计留痕（永不抛异常，旁路不能带塌主流程）
│  ├─ auth.py                               # ★v0.8.0：口令哈希（pbkdf2）+ PyJWT 签发/校验 + Bearer 依赖 + 演示账号
│  ├─ streaming.py                          # ★Step 6：SSE 事件流（meta→token→citations→verify→[web]→hitl→done；失败降级为一次性输出）
│  ├─ compare.py                            # ★Step 6：多公司对比（复用已注册工具，不另写取数逻辑）
│  ├─ judge.py                              # ★Step 6：自实现 faithfulness / answer_relevancy（与 RAGAS 同口径，不引重依赖）
│  ├─ search/                               # ★v0.8.0：网络搜索层
│  │  ├─ provider.py                        # 可插拔通道（bing 默认免 Key / ddg / tavily）+ SearchResult（网络引用口径，无页码/章节）
│  │  ├─ fetch.py                           # 抓取结果页正文（走 src/net.py 的薄封装）
│  │  ├─ crossvalidate.py                   # 交叉验证：按域名去重 + 关键数字有无交集 → consistent/conflict/insufficient_sources
│  │  └─ web_corpus.py                      # 独立网络语料区（JSONL + 独立 BM25 索引 bm25_web.pkl，不入年报索引）
│  ├─ server.py                             # ★FastAPI：/api/health、/api/ask[/stream]、/api/compare、/api/hitl/{tid}[/confirm]、/api/citations、/api/audit、★v0.8.0：/api/auth/*、/api/web/*、★v0.9.0：/api/ingest/*
│  ├─ db.py                                 # 业务库（双后端 sqlite/mysql）：公司/年报/长表指标/审计/评估集
│  ├─ ingest/
│  │  ├─ fetch_cninfo.py                    # 巨潮：orgId 查询 + 年报筛选 + 幂等下载
│  │  ├─ fetch_eastmoney.py                 # 东财：三大报表 + 主要指标 → 长表（多源优先级回退）
│  │  ├─ fetch_sina.py                      # 新浪：补齐东财不提供的科目（保险股归母净资产）
│  │  ├─ fetch_regulation.py                # ★Step 5：法规入库（226 号 gov.cn HTML / 182 号 csrc PDF → 按条切分）
│  │  ├─ wizard.py                          # ★v0.9.0：数据入库向导（自然语言→需求单→预览→勾选→入库；LLM 草稿必过归一）
│  │  ├─ parse_pdf.py                       # 按页解析（页码绑定 + 章节起始点 + 页眉页脚清理）
│  │  ├─ chunk.py                           # 按页切分 + 按页内偏移判章节 + 完整元数据
│  │  └─ index_vector.py                    # ★向量入库：vectors.npy + meta.jsonl + manifest.json（断点续传）
│  ├─ retrieve/
│  │  ├─ pipeline.py                        # ★统一检索入口（唯一"句子→hits"出口；mode 决定 bm25 / hybrid）
│  │  ├─ bm25.py                            # BM25 索引（jieba 分词 + 短语命中加成 + 过滤）
│  │  ├─ filters.py                         # ★从问题抽公司/年份 → 元数据过滤（抽不到就不加，绝不猜）
│  │  ├─ vector.py                          # ★向量召回（精确余弦 + 过滤 + 查询向量 LRU 缓存）
│  │  ├─ fusion.py                          # ★RRF 融合（共识优先 / 缺席不罚 / 稳定排序）
│  │  ├─ regulation.py                      # ★Step 5：法规独立检索（条级引用 + prefer_current 现行版优先）
│  │  └─ rerank.py                          # ★重排（passthrough / api / local，任何失败降级原序 + note）
│  ├─ tools/                                # ★只读工具层（LLM function calling 的接口面）
│  │  ├─ registry.py                        # 工具注册表：函数 + JSON Schema + 描述（永不抛异常）
│  │  ├─ indicators.py                      # get_financial_indicator / compare_companies / list_supported
│  │  └─ ratios.py                          # calc_financial_ratio（返回分子分母与官方口径交叉对账）
│  └─ graph/                                # Step 3 起：LangGraph 子图；Step 5 扩成完整主图
│     ├─ state.py                           # 状态 TypedDict（子图与主图共用的接口契约）
│     ├─ nodes.py                           # Step3:retrieve/generate/cite；Step5:+router/analysis/compliance/verify/hitl/finalize
│     ├─ checkpoint.py                      # ★Step 5：SQLite Checkpointer 工厂（跨进程恢复挂起）
│     ├─ subgraph_analysis.py               # ★Step 5：数值分析子图（数字只来自工具层，组句不经模型）
│     ├─ subgraph_compliance.py             # ★Step 5：合规子图（只给条文原文 + 版本适用期，不下结论）
│     ├─ verify.py                          # ★Step 5：引用/数字校验 + HITL 判定（证据按意图分流）
│     ├─ websearch_node.py                  # ★v0.8.0：联网兜底节点（先查网络语料缓存 → 未命中再联网 → 交叉验证过才入库）
│     └─ builder.py                         # 编译子图 + run_qa() + build_agent_graph()/run_agent()/resume_agent()
├─ scripts/
│  ├─ probe_sources.py                      # 数据源可用性自检（换机器先跑这个）
│  ├─ probe_embedding_sources.py            # ★embedding/rerank 通道探活（换 Key/换模型先跑；batch 上限实测）
│  ├─ probe_eastmoney_finance.py            # 东财字段名实测探针（改口径表前先跑）
│  ├─ probe_dc_insurance.py                 # 数据中心报表对保险股返回什么（排查取数缺口）
│  ├─ probe_app_sources.py                  # ★全源字段对照：App 上那个数到底映射到哪个字段
│  ├─ probe_{equity_sources,insurance_fields,sina_items,ths_fields}.py   # 单点排查探针（按需用）
│  ├─ ingest_all.py                         # 取数→解析→切分→建索引 一键流水线（全程离线可跑）
│  ├─ index_vector.py                       # ★建向量库（幂等 + 断点续传；唯一花钱且慢的一步，故独立成脚本）
│  ├─ init_db.py                            # 建表 + 同步公司/年报清单（+ --fetch 拉结构化数据）
│  ├─ verify_step2_db.py                    # ★Step 2 验收核对（库层数值 + 工具层行为）
│  ├─ smoke_qa.py                           # ★Step 3/4 验收冒烟（`--mode bm25|hybrid`；引用页码回源 PDF 核验）
│  ├─ smoke_step5.py                        # ★Step 5 验收冒烟（三路由 + HITL 双真子进程演练）
│  ├─ build_golden.py                       # ★评测金标准页码解析（标记串扫源 PDF 产物；`--grep` 排查）
│  ├─ eval_retrieval.py                     # ★Step 3 vs Step 4 消融评测 → `eval/report.md`
│  ├─ eval_rag.py                           # ★Step 6：答案级评测（`--judge` 走当前 LLM 通道判分）→ `eval/report_answer.md`
│  ├─ smoke_step6.py                        # ★Step 6：服务化冒烟（SSE 事件序 / compare / 多轮指代）
│  ├─ make_token.py                         # ★v0.8.0：登录换 token（调 /api/auth/login；供冒烟与手工验证用）
│  ├─ export_seed.py                        # ★Step 6：导出容器语料副本到 seed/（`--out`）
│  ├─ load_seed.py                          # ★Step 6：把 seed/business.json 载入业务库（`--seed` / `--dry-run`）
│  ├─ deploy_vm.py                          # ★Step 6：VM 部署 + 闸门（`--verify-only` 只跑断言；`--with-env-keys` 注 Key）
│  ├─ vm_ssh.py                             # ★Step 6：VM 侧命令封装（供 deploy_vm.py 复用）
│  ├─ _archive/                             # 已被覆盖的旧探针（保留可回溯，不再维护）
│  └─ list_tools.py                         # 导出 function calling schema 到 data/tools_schema.json
├─ eval/                                    # 评测资产（Step 4 起）
│  ├─ golden_qa.jsonl                       # ★金标准 55 题（数值 31 / 引用 10 / 法规 4 / 拒答 10）
│  ├─ report.md                             # ★检索级对照报告（55 题；Step 3 vs Step 4 消融）
│  ├─ report.json                           # 逐题逐配置原始结果（机器读，可复算）
│  ├─ report_answer.md                      # ★Step 6：答案级评测报告（人读）
│  └─ report_answer.json                    # ★Step 6：答案级逐题结果（含 faithfulness / answer_relevancy 原始分）
├─ data/
│  ├─ raw/{code}/                           # 年报 PDF + manifest.json（幂等清单）
│  ├─ regulation/raw/                       # ★Step 5：法规原文（226 号 HTML / 182 号 PDF）
│  ├─ vector/                               # 向量库四件套（vectors.npy / meta.jsonl / manifest.json / *.part.npy）
│  ├─ parsed/{code}/{year}.json             # 按页文本 + 章节起始点（溯源的原始凭据）
│  ├─ index/chunks/{code}_{year}.json       # 切分产物（带完整元数据）
│  ├─ index/bm25.pkl                        # 年报索引（重建是确定性的）
│  ├─ index/bm25_regulation.pkl             # ★Step 5：法规索引（132 条，条级切分）
│  ├─ index/bm25_web.pkl                    # ★v0.8.0：网络语料索引（独立，不入年报索引，避免污染评测）
│  ├─ web_corpus/web_corpus.jsonl           # ★v0.8.0：交叉验证通过后入库的网络来源（带 url/fetched_at）
│  ├─ db/fin_research.db                    # 业务库（SQLite 默认；MySQL 走配置）
│  ├─ db/checkpoints.db                     # ★Step 5：LangGraph 挂起状态（跨进程恢复靠它）
│  ├─ db/web_search_quota.json              # ★v0.8.0：联网每日配额计数
│  ├─ tools_schema.json                     # 工具契约快照（改接口时 diff 看得见）
│  ├─ smoke_qa_result.json                  # 冒烟结果快照（--json 时产出）
│  └─ llm_keys.local.json                   # 密钥（已 gitignore，源码不留兜底 Key）
└─ tests/                                   # 448 例确定性测试，不联网、不落盘
   ├─ test_{streaming,compare,history,eval_metrics,seed_db}.py   # ★Step 6：SSE 契约 / 对比 / 多轮指代 / 答案级判定 / seed 载入
   └─ test_{auth,server_auth,web_search,frontend}.py             # ★v0.8.0：鉴权契约 / 接口层鉴权 / 网络搜索 / 前端契约
```

## 快速开始

```bash
# 1) 建虚拟环境（必须 3.13；本机用 managed python）
"C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe" -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt

# 2) 数据源自检（换机器/换网络后先跑，任何一项失败都别往下走）
./.venv/Scripts/python.exe scripts/probe_sources.py
#    想用混合检索再跑这个：embedding / rerank 通道探活（含 batch 上限实测）
./.venv/Scripts/python.exe scripts/probe_embedding_sources.py

# 3) 全流程：取数 → 按页解析 → 切分 → 建 BM25 索引（全程离线可跑）
./.venv/Scripts/python.exe scripts/ingest_all.py
#    已有 PDF 想重跑解析（跳过下载）：
./.venv/Scripts/python.exe scripts/ingest_all.py --skip-fetch --force
#    只处理一家公司：
./.venv/Scripts/python.exe scripts/ingest_all.py --code 600519

# 4) 建向量库（Step 4；唯一花钱且慢的一步：3161 chunks ≈ 312s / 316 次 API 调用）
./.venv/Scripts/python.exe scripts/index_vector.py            # 幂等：指纹一致就跳过
./.venv/Scripts/python.exe scripts/index_vector.py --status   # 只看现状，不建
./.venv/Scripts/python.exe scripts/index_vector.py --force    # 强制重建
#    没有 embedding Key 也能跳过这一步：hybrid 会自动降级为纯 BM25，并在 note 里说明原因

# 4b) 法规入库（Step 5：合规子图的前置；两份官方原文 → 按条切分 → 独立索引）
./.venv/Scripts/python.exe -m src.ingest.fetch_regulation     # 下载 + 切分 + 建 bm25_regulation.pkl
./.venv/Scripts/python.exe -c "from src.retrieve import regulation as r; print(r.stats())"

# 5) 检索自检（会打印引用，用来肉眼验证溯源对不对）
./.venv/Scripts/python.exe -m src.retrieve.bm25 "资产负债率" --topk 3
./.venv/Scripts/python.exe -m src.retrieve.bm25 "研发投入" --code 002594
#    混合检索（需先建好向量库）：
./.venv/Scripts/python.exe -m src.retrieve.pipeline "五粮液2024年的毛利率" --mode hybrid --topk 5

# 6) 建业务库（SQLite 默认）+ 同步公司/年报清单（不联网）
./.venv/Scripts/python.exe scripts/init_db.py
#    再拉一次东财三大报表（联网，约 30~60s）
./.venv/Scripts/python.exe scripts/init_db.py --fetch

# 7) Step 2 验收核对：库层数值 + 工具层行为（推荐先跑这个看结果）
./.venv/Scripts/python.exe scripts/verify_step2_db.py

# 8) 工具层自检与 schema 导出
./.venv/Scripts/python.exe scripts/list_tools.py          # 打印 4 个工具 + 写 data/tools_schema.json
./.venv/Scripts/python.exe -m src.tools.indicators         # 指标查询示例输出（JSON）
./.venv/Scripts/python.exe -m src.tools.ratios             # 比率计算示例输出（含分子分母）

# 9) Step 3/4 验收冒烟（6 条代表性问题；引用会回源 PDF 逐条核页码）
./.venv/Scripts/python.exe scripts/smoke_qa.py             # 调真实模型
./.venv/Scripts/python.exe scripts/smoke_qa.py --no-llm    # 不花 token，只验检索/引用/拒答
./.venv/Scripts/python.exe scripts/smoke_qa.py --mode hybrid   # 走混合检索链路
./.venv/Scripts/python.exe scripts/smoke_qa.py --json       # 结果落 data/smoke_qa_result.json

# 10) 检索评测：Step 3 vs Step 4 消融对照（不调模型、可免费重跑；产出 eval/report.md）
./.venv/Scripts/python.exe scripts/build_golden.py         # 先复核金标准页码（标记串扫源 PDF 产物）
./.venv/Scripts/python.exe scripts/eval_retrieval.py       # 四配置全跑
./.venv/Scripts/python.exe scripts/eval_retrieval.py --only hybrid   # 只跑指定配置

# 11) 问答自检（不经过 HTTP，直接跑子图 / 主图）
./.venv/Scripts/python.exe -m src.graph.builder "贵州茅台2024年的毛利率是多少"   # 强制 RAG 子图
#    Step 5 完整主图（会分诊 + 校验 + 可能挂起）：见 scripts/smoke_step5.py 与 src.graph.builder.run_agent

# 12) 起 HTTP 服务（同步 def 端点，会在线程池里跑，不阻塞事件循环）
./.venv/Scripts/python.exe -m uvicorn src.server:app --port 8000
#    GET  http://127.0.0.1:8000/api/health                  （报 index/vector/rerank/regulation/checkpointer/hitl/audit）
#    POST http://127.0.0.1:8000/api/ask   body: {"question": "五粮液2024年的营业收入是多少",
#                                                 "mode": "hybrid", "code": "", "year": null,
#                                                 "intent": "auto", "thread_id": "", "actor": "demo"}
#    GET  http://127.0.0.1:8000/api/hitl/{thread_id}         （读回被挂起的流程，等人工确认）
#    POST http://127.0.0.1:8000/api/hitl/{thread_id}/confirm body: {"decision": "approve", "reviewer": "me"}
#    GET  http://127.0.0.1:8000/api/citations?thread_id=xxx  （会话引用明细 + 口径）
#    GET  http://127.0.0.1:8000/api/audit                    （审计留痕：ask / hitl_confirm）
#    POST http://127.0.0.1:8000/api/ask/stream   （Step 6：SSE 流式，事件序见下面 Step 6 章节）
#    GET  http://127.0.0.1:8000/api/compare?indicator=营业总收入&codes=600519,000858
#    GET  http://127.0.0.1:8000/                             （Step 6：单文件前端 web/index.html）
#    --- v0.8.0 新增 ---
#    POST http://127.0.0.1:8000/api/auth/register  body: {"username": "...", "password": "..."}   （role 固定 analyst，不接受入参）
#    POST http://127.0.0.1:8000/api/auth/login     body: {"username": "...", "password": "..."}   （返回 access/refresh token）
#    POST http://127.0.0.1:8000/api/auth/refresh   body: {"refresh_token": "..."}                  （换新 access token）
#    POST http://127.0.0.1:8000/api/auth/logout    （只留审计痕，不吊销：JWT 无状态）
#    GET  http://127.0.0.1:8000/api/auth/me        （需 Bearer：回当前用户）
#    GET  http://127.0.0.1:8000/api/web/search?q=...&limit=5   （手动触发联网搜索：看通道/交叉验证/是否入库）
#    GET  http://127.0.0.1:8000/api/web/corpus?limit=20        （看已入库的网络语料条目）
#    --- v0.9.0 新增（数据入库向导） ---
#    POST http://127.0.0.1:8000/api/ingest/plan    body: {"text": "把平安银行近3年年报的营业总收入和归母净利润加进来"}
#                                                  （自然语言→需求单草稿；LLM 只听懂人话，归一必过既有解析）
#    POST http://127.0.0.1:8000/api/ingest/preview body: IngestPlan   （只抓不写：collect_company 采集补缺）
#    POST http://127.0.0.1:8000/api/ingest/commit  body: {"plan": IngestPlan, "selected": [["000001","2024-12-31","营业总收入"],...]}
#                                                  （勾选格入库 + companies 补档案 + 审计；**需要管理员权限**）
#    鉴权开启时，业务端点（/api/ask、/api/compare、/api/audit…）都要带 `Authorization: Bearer <access_token>`；
#    /api/health 与 /api/auth/login 始终公开。

# 13) Step 5 验收冒烟（三条路由 + HITL 双真子进程演练；状态只来自 checkpoints.db）
./.venv/Scripts/python.exe scripts/smoke_step5.py
#    强制走 HITL（只改挂起结论、不改判定明细，用于可复现验收）：
FA_HITL_FORCE_REASON=low_confidence ./.venv/Scripts/python.exe scripts/smoke_step5.py

# 14) 一键启动（Step 6：预检 venv / bm25.pkl / 业务库 / 鉴权就绪 → 起 uvicorn → 就绪后开浏览器）
./.venv/Scripts/python.exe launcher.py                    # 默认 127.0.0.1:8000，bm25；缺密钥/口令时本机现生成
./.venv/Scripts/python.exe launcher.py --no-auth          # 单机免登录形态（不校验令牌、不用配密钥）
./.venv/Scripts/python.exe launcher.py --check            # 只做预检，不起服务（退出码=是否通过；只读，不生成密钥）
./.venv/Scripts/python.exe launcher.py --mode hybrid --port 8123 --no-browser
./.venv/Scripts/python.exe launcher.py --docker           # 改走 docker compose up -d 并等 health

# 15) 容器化交付（Step 6；需 Docker。app + MySQL 双容器）
docker compose up -d --build                              # 冷构建很慢（见下节 Step 6 实测），热重建秒级
#    宿主端口可用 APP_PORT 覆盖（容器内**始终监听 8000**，只有宿主映射变）：
APP_PORT=8001 docker compose up -d                        # ← VM 上实测就是这么起的（8000 被别的容器占着）
```

## 首次使用（v0.8.0：登录鉴权 + 联网搜索）

### 1. 鉴权：默认开启，先配密钥再建演示账号

服务**默认开启鉴权**（`FA_AUTH_ENABLED=1`）：业务端点一律要 `Authorization: Bearer <token>`，
`/api/health` 与 `/api/auth/login` 始终公开。启用前按下面三步走：

```powershell
# 1) 生成一个随机密钥（**不要**写在源码/文档里，放进环境变量或本地 .local.json）
python -c "import secrets; print(secrets.token_urlsafe(48))"

# 2) 起服务前注入（裸机示例；容器形态写进 .env）
$env:FA_JWT_SECRET="<上一步的随机串>"
$env:SEED_ADMIN_USER="admin"
$env:SEED_ADMIN_PASSWORD="<一个 ≥8 位的口令>"     # 首次启动用它建出演示账号（幂等：已存在则不覆盖口令）
./.venv/Scripts/python.exe -m uvicorn src.server:app --port 8000

# 3) 拿一个 token（脚本会打登录接口；也可直接 POST /api/auth/login）
./.venv/Scripts/python.exe scripts/make_token.py --username admin --password $env:SEED_ADMIN_PASSWORD
```

**注意事项**：

- `FA_JWT_SECRET` 为空且鉴权开启时，**服务启动即失败**（这是刻意的：用默认密钥签名等于没有鉴权，且从外部完全看不出来）。
- **没有 `FA_JWT_SECRET` 也想跑**：把 `FA_AUTH_ENABLED=0` —— 进入**单机免登录形态**，前端会显示「单机模式（免登录）」。
  更省事的是 `python launcher.py --no-auth`（等价，且子进程里显式置 `FA_AUTH_ENABLED=0`）。
- **`launcher.py` 会自愈（本机形态）**：`FA_JWT_SECRET` 为空时，启动前**现生成**一份随机密钥写进
  `data/db_keys.local.json`（已 gitignore，每台机器一份）；若 `SEED_ADMIN_PASSWORD` 也为空，
  连演示口令一起现生成并**只打印这一次**（源码与文档里没有默认口令；账号已存在或口令是别人配的，一律不回显、只给来源指针）。
  预检里那条 `✓ 鉴权：…` 就是这条逻辑的只读版本 —— `--check` **不生成、不写任何文件**。
  容器形态**不自愈**（部署配置不该被启动器改）：缺 `FA_JWT_SECRET` 时预检直接报错，要么补 `.env`、要么 `--docker --no-auth`。
- `POST /api/auth/register` **不接受 `role` 入参**（新用户一律 `analyst`），防止自助注册提权。
- **登出语义是诚实的**：JWT 无状态，`/api/auth/logout` 只留审计痕、**不做服务端吊销**，未到期的旧 token 在 TTL 内仍有效
  （前端登出＝丢弃本地 token）。无吊销黑名单、无 RBAC 细分、无多租户隔离 —— 见 [不足清单 G-11](./不足清单与处置方案.md)。

### 2. 联网搜索：库中查不到时的兜底（默认开）

- **触发条件**：本地检索拒答且原因是 `no_evidence` / `out_of_corpus` / `low_coverage`
  （即"本库确实没有"），**不是**模型的 `model_insufficient`。
- **通道**：默认 `bing`（**免 Key**、国内可达，抓结果页）；可切 `ddg`（装了 `duckduckgo-search` 更稳）；
  付费更稳可切 `tavily`（需 `TAVILY_API_KEY`）。
- **交叉验证**：按**域名**去重，**≥2 个独立域名且关键数字无冲突**才算 `consistent` → 才展示并入库；
  不通过只并列展示来源、`ingested=false`、`note` 写明分歧点，**不因为联网就放行编造答案**。
- **独立口径**：网络引用给的是 **URL + 抓取时间**，年报引用给的是 **公司+年份+页码+章节**，
  两者**分成两套、不混排**；网络语料走**独立索引**，绝不并入年报索引（否则污染全部评测数字）。
- **可控开关**：`FA_WEB_SEARCH_ENABLED`、`FA_WEB_SEARCH_BACKEND`、`FA_WEB_SEARCH_DAILY_QUOTA`（默认 200）、
  `FA_WEB_SEARCH_MIN_DOMAINS`（默认 2）等，见 [EXTENSION.md](./EXTENSION.md) 环境变量表。
- **失败必留痕**：联网失败/超限一律写进响应的 `web.note` 与 `audit` 的 `web_search`，**绝不静默降级**。

### 3. 能力边界（"能问什么 / 不能问什么"）

- **能问**：年报原文可溯源的问题（数值、管理层讨论、审计机构、分红方案…）、多公司同指标对比、合规条文检索、库外问题联网兜底。
- **不能问 / 会拒答**：本库与网络**都**找不到一致说法的问题（如"公司食堂的菜谱"）；对个案下合规结论；预测未来数据。
- **可信度排序**：**年报原文 > 法规条文 > 网络搜索结果**。网络结果经交叉验证才展示，但它是**确定性启发式**
  （按域名去重 + 数字有无交集），**不做语义级事实核验**（见 [不足清单 G-23](./不足清单与处置方案.md)），
  因此**可信度低于年报原文**，且始终带 `url` + `fetched_at` 供人工复核。

## 数据入库向导（v0.9.0）：在界面上加新公司新数据

结构化财务库此前只能靠脚本预取（`scripts/ingest_all.py --fetch` / `init_db.py --fetch`），
"想临时看一家新公司"就得开终端。v0.9.0 在前端加了「数据入库」页签，五步走完：

```
自然语言需求 → LLM 转需求单（可手改） → 抓取预览（只抓不写） → 勾选 → 确认入库
```

- **入口**：顶部「数据入库」页签。鉴权开启时**仅管理员可入库**（analyst 见只读提示，commit 返回 403）；
  免登录形态（`--no-auth`）直接可用。LLM 不可用时**手填表单照常能走完全程**（空草稿 = 合法空单）。
- **信任边界**：LLM 只出 JSON 草稿，认公司走 `resolve_company`、认指标/比率走 `config` 既有归一，
  认不上的进 `unresolved` 标红等人改，**绝不发明**（与数值子图「宁可拒答不猜」同一条纪律）。
  **库外新公司填 6 位股票代码**（如平安银行填 000001）—— 新公司必然不在库里，
  代码是唯一抓取凭证；库外名字没有捷径，必须让人改成代码。
- **只抓不写**：预览只调 `collect_company`（采集补缺），确认入库才走 `save_company` 既有 upsert 路径 ——
  两条路径本就分离，"预览不改库"由测试的行数不变断言守着。
- **防呆上限**：单次入库最多 5 家公司 / 12 个指标 / 10 个期数（`FA_INGEST_MAX_*` 可调，见 [EXTENSION.md](./EXTENSION.md)）。
- **审计**：每次入库写一条 `data_ingest` 审计（batch id + 需求单 + 勾选明细），没有批次表 —— batch 只活在审计里。
- **已知不足**：新公司档案的 `name` 暂为代码原文（东财采集器按需取列不含简称），显示为「000001」而非「平安银行」；
  简称回填登记在 [不足清单](./不足清单与处置方案.md)，后续版本处理。

## Step 2 可用的只读工具

4 个工具，全部**只读**（不写库、不联网），可被 LLM 反复调用而无需二次确认：

| 工具 | 用途 | 返回里必须有的溯源信息 |
|---|---|---|
| `get_financial_indicator` | 单公司单指标的历年序列 | 每个值的 `period` / `unit` / `source`（`表.字段`） |
| `compare_companies` | 多公司同指标排名 | 每行的 `period` / `source`；各家期次不一致时 `periods_consistent=false` + 告警 |
| `calc_financial_ratio` | 派生比率（毛利率 / **毛利率(保险口径)** / 净利率 / 资产负债率 / ROE / 经营现金流净利润比） | **完整算式**：分子分母的每个分项、符号、取值、来源；外加官方口径值与差异判读 |
| `list_supported` | 列出已入库公司与可用指标/比率名 | —（防止模型编造库外公司或自造指标名） |

三条口径纪律（对应 `src/config.py` 里的注释，改口径只改那里）：

1. **`营业总收入` ≠ `营业收入`**：茅台 2024 年 1741.44 亿 vs 1708.99 亿，差 32.45 亿（1.9%）。
   两者都在库里、都是独立指标，绝不互相顶替。
2. **比率口径写进配置**：毛利率分母用 `营业收入`（与东财官方 `XSMLL` 同口径，实测派生差 `-0.00pp`）；
   净利率分母用 `营业总收入`。**同一张表里两个比率分母不同，这是刻意的。**
3. **派生值 ≠ 官方值是有效信息**：ROE 派生值用期末归母净资产（简化口径），官方 `ROEJQ` 是加权平均，
   差 1~3pp 属正常。工具把差异分级（一致 / 口径差异 / 需复核）如实交出，不取其一掩盖。

## 第二数据源：为什么"多源"不只是容错

最初的判断是"保险公司没有归母净资产/毛利率/营业成本这三项"。
**这个判断错了两条半** —— 起因是用户在券商 App 上明明看到了这三项。
查完之后的真相是三种情况各占一种：

| App 上看到的 | 事实 | 处理 |
|---|---|---|
| 归母净资产 | 数据客观存在，**东财全系不提供**（F10 对保险整表空、数据中心简表没有该列、主要指标只有**含少数股东**的 `TOTAL_EQUITY_PK`）→ 是**我们选源不够** | 接入新浪财经 `fzb`（资产负债表）|
| 营业成本 | 保险利润表**确实没有**这个科目，成本行叫「营业支出」 | 新增「营业支出」指标（**不并进营业成本**）+ 工具层给出口径替代项 |
| 毛利率 | 东财与同花顺都没有这个字段，App 上的数是**它自己用营业支出算的** | 新增「毛利率(保险口径)」派生比率并公开算式 |

**教训：把「我没取到」当成「数据不存在」是错的** —— 两者证据强度完全不同。
在宣布"数据源不提供"之前，至少要换一个源、并用会计恒等式交叉验证一次。

新浪源的三条实测纪律（`src/ingest/fetch_sina.py` 顶部有完整说明）：

1. **表代号是 `fzb` 不是 `zcfz`**：传错返回 `{"status":{"code":0},"data":null}` ——
   HTTP 200、状态码 0、就是没数据，看着像"这只股票没有资产负债表"。利润表是 `lrb`。
2. **期次按中文文本筛**（`2024年报` / `2025半年报`），且**认不出的期次直接丢，不猜类型**
   —— 猜错会把中报混进年报序列。
3. **补充源不参与决定期间轴**：新浪会给到主源范围之外更早的一期（601318 主源 2020–2025，
   新浪多给 2019）。若取各源期次的**并集**，就会凭空多出一个只有 3 个字段的残缺期，
   其余指标在该期全部"缺失"，被汇总成"全源缺失" —— 把"某期没覆盖"和"压根没这个字段"
   混成一件事。**期间轴只由主源决定，补充源只在主源已确定的期上补字段。**

### "缺数据"要分三档说，不能混成一个 `missing`

| 字段 | 含义 | 补救动作 |
|---|---|---|
| `missing` | 每一期都取不到 → 数据源确实不提供该字段 | 无（这是结论）；有 `counterpart` 时会给出替代口径 |
| `partial` | 只在部分期缺 → 覆盖度问题 | 加期数 / 补取该年 |
| `source_errors` | 接口本身失败 → 故障，不是"没有" | 重试 / 换源 |

### 口径替代机制：不说"没有"，也不拿替代项冒充原名

`config.INDICATORS[*]["counterpart"]` 与 `config.RATIOS[*]["alternatives"]` 声明替代口径，
工具层在缺数据时会**把替代口径的名称、公式、数值一起带出来**：

```
get_financial_indicator(中国平安, 营业成本)
  → count=0，counterpart = {营业支出, 8,628.10 亿元, <- income.TOTAL_OPERATE_COST,
                            why: "保险/金融业利润表无「营业成本」科目…"}

calc_financial_ratio(中国平安, 毛利率)
  → insufficient_components（缺营业成本），
    alternatives = [毛利率(保险口径) = 17.87%，公式 (营业收入 − 营业支出)/营业收入]
```

**立场**：直接用替代口径的数顶替原名**是造假**（那不是毛利率）；
只回"没有"**是把问题推回去**。所以必须"给数 + 标明它叫什么、怎么算的"。

## 问答链路（Step 3）：三节点子图 + 三条防编造机制

```
问题 ──▶ retrieve ──▶ generate ──▶ cite ──▶ 响应
          BM25       闸门→合成      一致性检查
         (pipeline)    (answer)      (nodes)
```

节点**刻意写得很薄**：逻辑全在 `src/retrieve/pipeline.py` 与 `src/answer.py` 的纯函数里，
节点只负责"读写状态 + 兜住异常"。原因很实际 —— **节点里的逻辑只能靠跑图来测**，
而跑图要先建索引、要联网调模型；留在纯函数里就能直接单测。

### 防编造：三条机制，都不依赖"提示词求模型别撒谎"

| 机制 | 做法 | 为什么这么做 |
|---|---|---|
| **引用编号只从我们给的选** | 检索片段编号成 `[1][2]…` 交给模型，要它返回 `used_citations`，逐条校验编号是否真实存在，**不存在的丢弃并记进 `notes`** | LLM 会编页码，但**编不出我们没给它的编号**。另外：校验的上界是"**实际送进上下文几段**"（`rendered_count`），不是 `len(hits)` —— 否则模型写一个我们没给它的编号也会被判为合法，那就成了**我们替模型伪造出处** |
| **双闸门拒答** | ① 确定性闸门：问题实词有多少出现在**同一段**召回文本里（单段证据覆盖度，门槛 0.5）；② 模型自评：要求输出 `insufficient` 标志 | 只靠一条都不够：前者挡不住"词都在但答非所问"，后者挡不住"模型硬答"。确定性闸门在**调模型之前**拦住，超范围问题**一个 token 都不花** |
| **降级路径不是可选项** | 没 Key / 模型失败 / 输出不是约定 JSON → 返回**有出处的原文摘录**，明确标注"未调用大模型" | 绝不能因为调不到模型就把链路断掉，更不能悄悄返回一段没有出处的话。报错会把"可核对的摘录"这份价值也一起丢掉 |

### 为什么证据覆盖度必须"单段"统计

拼起全部召回文本去数词，会让**互不相关**的命中凑出覆盖率：

```
问：公司食堂菜谱有什么推荐
  [1] 中国平安 P88 …公司设有员工食堂…        ← 命中「食堂」
  [2] 中国平安 P30 …董事会推荐张先生为…      ← 命中「推荐」
  拼接统计 → 0.67  ✅ 放行   ❌ 但没有任何一段在讲食堂菜谱
  单段统计 → 0.33  ⛔ 拒答   ✅ 正确
```

改判后的实测分布（topk=8）：**正常问题 0.67~1.00**（毛利率 0.75、归母净资产 0.75、
研发投入 0.80、营业收入 1.00、资产负债率 1.00）、**越界问题 0.25~0.33**。
门槛 0.5 两侧都留得开。响应里同时给出 `ratio_union`（拼接口径），
专门用来暴露"靠拼凑达标"。

### 冒烟脚本怎么判"过"

`scripts/smoke_qa.py` 把结论分成**三类分开统计**，不混成一个通过率：

| 桶 | 判据 |
|---|---|
| ✅ 通过 | 有引用、每条都能拼出 `P<页码>`、公司没串、`dangling` 为空、该拒答的拒了 |
| 📉 基线（Step 4/5 待改进） | **完整性没问题，只是没作答**：该答的题被拒答/降级，且该用例标了归因（如"BM25 召回精度"） |
| ❌ 失败 | 引用缺页码、串了别家公司、正文角标是死链、该拒答却答了 |

之所以要单独一桶：Step 3 刻意只用 BM25，召回精度本来就有限，而 Step 4 的验收标准明确要求
"留下 Step 3 vs Step 4 的指标对比"。把"被召回精度卡住"和"引用是假的"混成同一个失败，
要么高估 Step 3、要么低估它。卡住就**如实记成基线 + 写清归因**。

**页码核验走独立证据链**：脚本把每条引用片段**重新拿到源 PDF 的那一页原文里找一遍**
（`--no-pdf-check` 可关）。片段是从我们自己的 chunk 里取的，用它反查自己的页码等于自证；
重新开 PDF、只按 `page_no` 取页、再找文字，才能发现"切分/元数据把页码绑错了"这类错 ——
而其它检查全都发现不了。剩下要人判断的只有一件事：
**这一页的文字是否真的支持那个结论**（这部分刻意不做成断言，断言会把"没核"伪装成"核过了"）。

## 混合检索（Step 4）：两路召回 → RRF 融合 → 重排，外加一层元数据过滤

```
问题 ──▶ filters ──▶ ┌ BM25 路（字面）┐ ──▶ RRF ──▶ rerank ──▶ top-k ──▶ 统一 hits
       抽公司/年份   └ 向量路（语义）┘   融合      精排(可降级)          (normalize_hit)
```

四件事按"性价比"排序，也就是实现的顺序（全部落在 `pipeline.retrieve(mode="hybrid")` 里）：

| 顺序 | 做的事 | 收益（34 题消融，见 `eval/report.md`） |
|---|---|---|
| 1 | **自动元数据过滤**：问句点名了公司/年份，就只在该范围内检索 | 年份精度 **86.2% → 100%**；公司精度 **49.0% → 100%**；page_hit@5 单独贡献 +23.8pp |
| 2 | **向量路 + RRF 融合**：字面与语义两路，按名次融合 | page_hit@5 再 +14.3pp（38.1% → **52.4%**）|
| 3 | **重排**：交叉编码器给「问题 × 候选」逐对精排 | 通道已接好且实测可用（`gte-rerank-v2`），但默认 `passthrough` 不花钱 → 本报告里与 ③ 同分 |
| 4 | 每页配额 / 多粒度切分 | 未做，已记入 `eval/report.md` 的「已知不足」 |

### 为什么是 RRF，而不是"分数加权求和"

两路分数**不同量纲**：BM25 无上界（本项目实测落在 0~40），余弦相似度有效区间只有 0.2~0.7。
加权求和必须先归一化，而归一化**依赖当前候选集合的分布** —— 同一条片段会因为"这次多召回了
一个高分片段"而排序抖动。RRF 只看**名次**，天然与量纲无关、不用调权重（Cormack et al. 2009）。

三条实现约定（`tests/test_fusion.py` 守着）：

1. **缺席不罚**：只出现在一路里的片段，另一路**贡献 0**，而不是按"最后一名"处理。
   否则"向量召回了、BM25 没召回"的片段会被系统性压低 —— 而那恰恰是加向量路想捞回来的东西。
2. **并列稳定**：同分按「单路最好名次」→「chunk_id」排。**评测要跑两轮对比**，
   排序不确定的话两次运行结果会抖，指标差异就分不清是策略带来的还是随机带来的。
3. **等权起步**：`DEFAULT_WEIGHTS = {bm25: 1.0, vector: 1.0}` —— 先等权测出基线再谈调参，
   否则"哪一路更好"和"权重该给多少"两个问题会搅在一起。

### 向量库为什么不用 Chroma / FAISS

`2650 × 1024` float32 ≈ 11 MB，一次 `matmul` 就是全量精确余弦，单查 < 1 ms。
这个量级上 ANN 索引**只会引入近似召回损失**，而 Step 4 的评测恰恰是在测召回变化 ——
**用一个会丢召回的索引去测召回，基线就被污染了**。所以默认走精确检索，
把加载/检索接口留清楚（`load()` / `search()`），数据量上万后换 HNSW 只替换这一层实现。

### 向量库落盘：三件套 + 断点，且**加载必须校验**

| 文件 | 内容 |
|---|---|
| `data/vector/vectors.npy` | `(n, dim)` float32，**已 L2 归一化**（余弦 = 点积） |
| `data/vector/meta.jsonl` | 第 i 行 ↔ 矩阵第 i 行，**只存溯源字段**（`chunk_id/code/year/page_no`…） |
| `data/vector/manifest.json` | 模型名 / 维度 / 条数 / **chunk 指纹** / 构建时间 / 耗时 |
| `data/vector/vectors.part.npy` | 断点续传中间产物，正常完成后删除 |

两条纪律，都是冲着"**静默出错**"去的：

- **指纹顺序敏感 + 矩阵/meta/dim 三方对齐**：chunk 列表重排后指纹就变。
  load 时校验「矩阵行数 = meta 行数 = dim」且模型名与当前配置一致 —— 否则会出现
  "**用 B 模型的查询向量去搜 A 模型建的库**"：维度不同会报错，**维度相同就会静默返回
  毫无意义的排序**，这是最难发现的一类错。
- **正文只存一份**：向量库只存向量 + 元数据，**不存正文**；正文一律回查 BM25 索引的
  chunk 存储（`pipeline.chunk_store()`，全项目唯一正文来源）。正文是本项目**演进最快**的字段
  （Step 3 刚给它加了节名判定），存两份迟早不一致，而不一致会表现成
  "引用指向 P87、但引用片段显示的是另一页的文字" —— 那比召不回来严重得多。

### 自动过滤的纪律：**抽不到就不加，绝不猜**

猜错是不对称的：漏抽（该过滤没过）→ 结果混进别年/别家，**用户看得出来**；
抽错（把"比亚迪"认成"中国平安"）→ 结果**全是错的却看起来很对** → 灾难。
所以 `filters.py` 的所有判断都要求**唯一命中**，命中多条一律放弃过滤并把歧义写进 `note`；
问句里出现多个年份（对比题"2023 和 2024 相比"）同理不过滤 —— 过滤掉哪个都是错的。
**相对年份（去年 / 今年）刻意不解析**：同一句话在元旦前后含义不同，而答案会被引用很久。
与其猜，不如让 `note` 提示用户说清年份。

### 重排是"可以坏"的：任何失败都降级原序

重排是外部依赖（API 或本地模型），它挂了**绝不能让整条问答链路挂**。
`rerank()` 设计成**永不抛异常**：不可用 / 超时 / 返回条数不符 / 缺正文 → 一律**原序直通，
并在 `note` 里写清为什么**。静默直通不可接受 —— "这次没重排"和"重排了但结果一样"
对用户是两件事。另外**不做绝对分数阈值**：不同 rerank 模型分数刻度不同
（有的 0~1、有的 logits 无界），拿写死的阈值卡"低于它就丢"，换个模型就会全丢或全留。

## 完整编排（Step 5）：规则分诊 → 三条子图 → 自检 → 挂起等人

主图长这样（`src/graph/builder.py::build_agent_graph`）：

```
START → router ─┬─ rag        ─┐
                ├─ analysis   ─┼→ verify ─┬─(待确认)→ hitl ─┐
                └─ compliance ─┘          └─(通过)───────→ finalize → END
```

### 分诊为什么用规则、不用模型

`router` 是**纯规则**的，三条 cue 按优先级命中：

| 意图 | 触发条件 | 置信度 |
|---|---|---|
| `compliance` | 命中合规词（规定/披露期限/是否违反…） | 0.9 |
| `analysis` | **疑问形 cue** + **指标名同时命中库内指标** | 0.8 |
| `rag` | 兜底；只有疑问形 cue 时 0.4 | 0.4 |

两个细节是刻意的：
- **必须"疑问形 + 指标名"同时命中**才算数值题。「公司食堂菜谱有什么推荐」里有疑问形但没有库内指标，
  不该被当成数值题 —— 反例已进单测。
- 规则路由**可解释、可回归**：错了一眼能看出是哪条 cue 命中的；"让模型选意图"错了只能靠调提示词反复试。

### 数值题的数字，模型没机会编

`analysis` 子图的约束是结构性的，不是"提示词求它别撒谎"：

1. 数字**只能**来自只读工具层 `registry.dispatch`（每个工具都返回 `来源=表.字段`）；
2. 组句是**确定性拼装**（`compose`），**不经模型** —— 模型在这条链上没参与写数字；
3. 认不出公司 / 指标就**不调工具、不给数字**，改列"可查范围"（边界显式，不猜）；
4. 答案里的每个数字再经 `verify` 回归检查一遍，找不到出处就**挂起人工确认**。

### verify 的证据集合按意图分流

这是 Step 5 最容易写错的地方：**三种意图的证据不是同一种东西**。

| 意图 | 证据来源 | 为什么 |
|---|---|---|
| `analysis` | `tool_results`（工具返回值） | 数字来自工具层，就得用工具层当尺子 |
| `compliance` | `regulation_hits`（法规条文） | 条文里的文号（226/182）与日期也是"有出处"的，用 RAG 的片段当尺子会**假阳性挂起** |
| `rag` | 引用片段原文 | 有出处的数字必须能在被引的那段里找到 |

HITL 触发优先级：`numeric_mismatch` > `citation_unsupported` > `low_confidence`。
**确定性拒答与检索故障不挂起** —— 那是结论（"我确实答不了"），不是"待确认"。

### HITL 能跨进程恢复（状态在 Checkpointer 里，不在内存）

用 `langgraph-checkpoint-sqlite`，落盘 `data/db/checkpoints.db`。
`hitl` 节点调 `interrupt(payload)` 把**已经落盘的答案 + verify 明细**一起交出去，
客户端拿 `thread_id` 走 `/api/hitl/{tid}/confirm` 恢复。`check_same_thread=False`
是必须的（FastAPI 线程池里会换线程）。

演练用 `FA_HITL_FORCE_REASON=low_confidence` 强制挂起（**只改挂起结论、不改判定明细**），
所以验收可复现：进程 A 跑出挂起 → 清 saver 缓存（模拟换进程）→ 进程 B 读回并 confirm。

### 合规只给条文、不给结论

法规**独立建索引**（`bm25_regulation.pkl`，按条切分，引用带 `doc_no/status`），
226 号 67 条 + 182 号 65 条 = 132 条。`prefer_current` 让现行版永远排前面
（**按分数排会把废止版排上去**，踩过），并且只有正文确实不同才带出废止版。
答案里显式给版本适用期（2025-07-01 起适用 226 号），**不对个案下合规判断**。

### 🐞 期间修掉两个解析层真 bug（"清洗/标注"把数据搞错了）

**① 报表金额被当成"跨页页眉"整行删掉。**

**症状**：问"某科目金额是多少"（如货币资金）答不出，报表主表里金额**整列全空**。

**根因**：`parse_pdf._strip_running_headers` 为了匹配"同一行页眉"会把数字归一化成 `#`，
于是金额行 `127,187,293,298.17` → `#,#,#,#.#`；报表排版对齐使这类行在多页重复出现，
**被判成跨页重复页眉而整行删除** —— 600519 年报 50 页之后 40 页"零金额"。

**判据错在**：页眉页脚是**文字**（公司名/报告期/页码标记），而纯数字行是**数据**。
**修法**：加 `has_text()`（含中文或字母）过滤 —— **纯数字行不参与"重复"判定、也永不被删**。
比维护"哪些行是页眉"的白名单稳得多：白名单永远会漏，**漏一个就是一次静默的数据丢失**。

**② 每章起始页的首个 chunk 被标成上一章（引用里章节名写错）。**

**症状**：引用写着「中国平安2024年年报 **P150 公司治理** 第1/2段」，但 P150 的内容明明是**审计报告**。

**根因**：每页页首有印刷页码（「6」「146」，占 3~4 字符），章节标题紧随其后。
`_collect_marks` 的规则"页首到第一个标题之间若仍有内容 → 补一条上一节标记"，
**把那几个字符的页码当成了"上一节的正文"**，补出来的上一节标记吞掉整页 ——
于是**每一章起始页的首块都挂到上一章**（实测 **31 个 chunk / 1.0%**）。

**修法**：`_is_header_noise()` —— 与页眉清理**同一判据**（含中文或字母才算内容）。
页首只剩页码时不补上一节标记，把真实标题提前到 `offset=0`。
**对照场景必须保住**：页首是真正文（上一节续页）再接标题时，`offset=0` 那条仍须是上一节
（600519 P7 第 32 行「第三节」就是这种），否则会把上一节几百字吞进新章节 —— 用例里留了对照组。

修完必须**重跑管线**才算落地（代码改了、语料没改等于没修）：

```bash
.venv/Scripts/python.exe scripts/ingest_all.py --skip-fetch --force   # parse → chunk → BM25
.venv/Scripts/python.exe scripts/index_vector.py --force              # 向量库（约 5 分钟）
.venv/Scripts/python.exe scripts/build_golden.py                      # 金标准期望页重算
.venv/Scripts/python.exe scripts/eval_retrieval.py                    # 指标重测
```

重入库后 chunks **2650 → 3161**（金额回归后正文变长；第二个 bug 只改标签不改切分文本）。
回归用例：`test_amount_only_lines_are_never_treated_as_running_headers`、
`test_page_number_before_heading_does_not_leak_previous_section`。

> 教训一：**静默的数据丢失比报错危险得多**。抽取层的每一条"清洗"规则，
> 都要能用"它删的到底是不是数据"来质疑。
> 教训二：**引用里的章节名是可核验的出处文字**，标错等于给出错误证据 ——
> 而"页码对、章节错"这种错，不逐页核对根本发现不了。

### 🐞 顺带查出的第三个问题：金标准标注错了（不是检索不行）

`lit-601318-2024-auditor`「中国平安2024年年报的审计机构是哪家」的 marker 原写**「普华永道」**，
而平安的实际审计机构是**安永华明** ——「普华永道」命中的是 **P118/P122 董事简历里的前雇主**
（付欣"曾任普华永道执行总监"、吴先生"历任普华永道中国业务主管合伙人"）。
所以"5 档全部 ✗"**不是检索不行，是标注错了**（检索一直把正确的 P150 排在首位）。

修法：marker 换成两条真正指向审计机构的短语 → `expected_pages` 重算为 `[106, 150]`。
教训：**marker 只能证明"这页有这几个字"，不能证明"这几个字回答了这个问题"**。
这条风险对所有 literal 题都存在（已登记 G-14：需要人工复核 9 道题）。

## 服务化与交付（Step 6）：SSE 流式 + 多公司对比 + 多轮指代 + 单文件前端 + 容器 + 答案级评估

Step 5 交出的是"会分诊、会挂起"的后端；Step 6 把它补成**能直接给前端用、能一条命令装进容器**的形态，
并把评测从"检索命中"推到"答案对不对"。

### SSE 流式：事件协议固定，失败必须显式降级

`POST /api/ask/stream`（`media_type="text/event-stream"`）的事件序：
`meta` → `token`* → `citations` → `verify` →（挂起时 `hitl`）→ `done`；异常时另有 `error`。

| 事件 | 何时发 | 关键字段 |
|---|---|---|
| `meta` | 首帧，必发 | `intent` / `route` / `thread_id`（三键必有） |
| `token` | 逐块/逐字 | 增量文本 |
| `citations` | 答案分片后 | 引用列表（与一次性响应同形） |
| `verify` | 校验后 | 校验明细（`supported` / `unsupported` / `dangling_citations`） |
| `hitl` | **仅挂起时** | 挂起原因 + 已落盘候选答案 |
| `done` | 末帧，必发 | 与一次性响应**同形**的完整响应 + `degraded` + `notes` |
| `error` | 异常时 | 错误信息 |

四条纪律，都是冲着"前端不用猜、链路不许静默断"去的：

1. **数值（analysis）/ 合规（compliance）两条意图不流式**：它们的答案本来就是确定性拼装（不经模型），
   **没有 token 可流** → 直接给整段。这不是缺陷，而是不做"假打字机"。
2. **流式失败（超时 / 异常 / 模型不可用）必须回落为一次性 `token` + `done`**，
   且 `done.response.degraded=true`、`notes` 写明原因，**绝不静默中断**。
3. `done` 携带的完整响应与一次性 `/api/ask` **同形** —— 前端不必写两套渲染（这是"终态覆盖"能成立的前提）。
4. 前端收到 `done` 后用 `done.response` **终态覆盖**已渲染内容，**流式丢字以终态为准**。

### `/api/compare`：多公司同指标对比

`GET /api/compare?indicator=营业总收入&codes=600519,000858[&period=2024-12-31]` 与 `POST /api/compare`。

返回键固定为 `{ok, indicator, unit, rows, periods_consistent, chart, note}`；
`rows[i]` 含 `code/name/period/value/unit/source`；`chart.series[i].points` 按期次**升序**、
`chart.periods` 为**全局有序**期次（同一张图里各家的横轴不能各排各的）。

错误也结构化，不靠前端猜：指标名不认识 → `ok=false` + `error="unknown_indicator"`；
公司数 < 2 → `error="need_at_least_two_companies"`。
`src/compare.py` **复用已注册工具**（`compare_companies` / `get_financial_indicator`），**不另写取数逻辑** ——
否则同一指标会出现两套口径，而口径分裂是静默错误。

### 多轮指代消解（`src/retrieve/filters.py::resolve_entities`）

| # | 规则 | 边界（为什么这么定） |
|---|---|---|
| 1 | 问句里抽不到公司、且 history 最近一轮有**唯一** `code` → 沿用该 code | 并在 `note` 写明「沿用上一轮的公司：XXX」，来源要可核验 |
| 2 | 问句里抽到了公司 → **绝不**用 history 覆盖 | **本轮显式实体优先**，历史只补空 |
| 3 | history 最近一轮的公司不唯一或为空 → **不补** | **宁愿不猜**：猜错会答成别家公司，且看起来完全正确 |
| 4 | 年份同理，但**不能跨公司沿用** | 公司来自本轮、年份来自上一轮时，必须在 `note` 里**都标明来源**；跨公司沿用等于答别人家的年份 |
| 5 | 上限裁剪：只保留最近 `FA_HISTORY_MAX` 轮（默认 5） | 同时控住上下文预算与歧义面 |

### 单文件前端 `web/index.html`：五项能力 + 零 CDN 的取舍

单文件、内联 CSS/JS、**零外部 CDN、零构建步骤**，五项能力：

1. **对话**：SSE 流式逐字渲染；收到 `done` 后用 `done.response` **终态覆盖**渲染，防止流式丢字。
2. **引用卡片**：展示后端返回的 `citation` 字段**原样**，不在前端重拼格式；展开调 `/api/citations`；
   「回源核对」按钮用 `window.open` 打开 `pdf_url` / `adjunct_url`，**没有该字段时按钮置灰**
   并提示「本条引用未收录原文链接」——**可点的按钮必须真能点**，点了没反应比置灰更糟。
3. **对比**：对比表 + **手绘内联 SVG 折线图**（`document.createElementNS`）。
4. **越界问题**：显示拒答文案且**无引用**（拒答就不该给出处）。
5. **HITL 挂起确认面板**：展示 `hitl.reason` + `verify` 明细 + 已落盘候选答案，两个按钮调 `/confirm`；
   409 时给明确提示而不是静默失败，措辞必须是「**这是待确认、不是错误**」。

取舍的理由（都写在这里，避免以后有人"顺手引个库"）：

- **零构建步骤**：一次 `git clone` 就能离线打开，没有 npm / 打包链，也没有"CDN 挂了页面就白屏"。
  演示场景下"打开就能用"比"工程化"更值钱。
- **图表手绘 SVG 而不引图表库**：本需求只有一条折线 + 若干点，引 ECharts / Chart.js 会带来几百 KB 外部依赖与构建配置；
  `createElementNS` 几十行就够，且完全离线。**不是为了省，而是为了不引入一个"必须联网才能渲染"的依赖。**
- 代价如实说：样式与交互"够用就好"，不做动效/主题；要复杂可视化再引库，那时再引入构建步骤也不迟。

### 容器化：一条命令起 app + MySQL

```bash
docker compose up -d --build     # 或：python launcher.py --docker
```

两个坑是这一节的重点（都是实测踩出来的）：

- **语料 seed 放 `/app/seed`，不放 `/app/data`**：compose 给 `/app/data` 挂卷时，
  **卷会遮住镜像里同一路径的内容**（姊妹项目踩过）。所以镜像里的语料放 `/app/seed`，
  由 `entrypoint.sh` 启动时 `cp -rn /app/seed/data/. /app/data/` —— 用 `-n` **不覆盖**：
  用户在卷里放了真语料就以卷为准，没放才用镜像内的副本。
- **MySQL 卷只在首次创建时初始化**：改了 `MYSQL_*` 必须 `docker compose down -v` 才会重新初始化，
  否则会误判成"改了没生效"。

`entrypoint.sh` 的顺序（每一步失败都要**响亮地失败**）：
复制 seed → 等 MySQL 就绪（socket 循环，最多 60s，超时**明确报错退出 1**、不无声继续）→
`python scripts/init_db.py` → `python scripts/load_seed.py` → `exec "$@"`。

> ⚠️ **容器链路只在 VM 上实测**：VM 是 `192.168.57.128`（Ubuntu 22.04.4，Docker 29.1.3）；
> **本机 Windows 侧没有 Docker**（`docker` CLI 不存在），所以容器相关的数字与断言都来自 VM。
> 宿主端口用 `APP_PORT` 覆盖（VM 的 8000 被姊妹项目 workflow-agent 的容器占着 → 实测用 **8001**）；
> **容器内始终监听 8000，只有宿主映射变**。

### Step 6 容器实测（VM `192.168.57.128` / Ubuntu 22.04.4 / Docker 29.1.3）

- **构建耗时**：`docker compose up -d --build` 冷构建 ≈ **1478s**（其中 `pip install -r requirements.txt` **1268.4s**、
  导出镜像层 212.2s）；改 env 后热重建导出只 **9.4s**（依赖层全 `CACHED`）。
- **镜像大小（两个口径都记）**：权威口径 `docker image inspect --format '{{.Size}}'` = **176,676,355 B ≈ 168.5 MiB**；
  `docker images` 表里 CONTENT SIZE **177 MB** / DISK USAGE **710 MB**（Docker 29 新口径，含 attestation / 未压缩层摊算）。
  两个口径差得多，只写一个都会让人对不上账。
- **闸门**：`python scripts/deploy_vm.py --verify-only` 退出码 **0**。该脚本断言 `/api/health` 的
  `index/vector/regulation/checkpointer` 全 `ok` 且 `checkpointer.backend="mysql"`、`/api/compare` 返回 **2 行**、
  `/api/ask/stream` 首帧为 `meta`。
  **v0.8.0 起断言扩到 6 条**（容器**不该以免鉴权形态**通过验收）：另加 `auth.enabled` /
  `auth.jwt_secret_configured` / `auth.seed_admin_present` 与 `web.enabled` / `web.provider_available` 均为真、
  **无票访问 `/api/compare` 必须 401 + `WWW-Authenticate`**、容器内真实出网跑一次
  `python -m src.search.provider` 退出码 0。业务端点探针（compare / stream）先 `/api/auth/login` 换票；
  `FA_JWT_SECRET` 与演示口令由脚本**本机现生成**写入 VM 侧 `.env`（权限 600，值不回显）——
  不注入密钥容器起不来（刻意的安全默认）。
  2026-09-23 实测：`docker compose up -d --build` 完整重建（pip 日志含 `PyJWT-2.14.0` /
  `duckduckgo-search-8.1.1`）、容器 `(healthy)`、六条断言全过（容器内联网实测 `通道=bing 命中=5 耗时=4.68s`）。
- **VM 上 `/api/health` 实测（节选）**：`index.ok=true, chunks=3161, companies=5, company_years=6`；
  `vector.available=true`（3161 向量 × dim 1024，`text-embedding-v4`）；`regulation.available=true`
  （132 条 = 182 号 65 + 226 号 67）；`checkpointer.backend="mysql"`、`exists=true`；`frontend.available=true`；
  `retrieve_mode_default="hybrid"`。
- **容器占用**：app 222MiB / 1.172GiB（18.50%）、web-db 71MiB / 900MiB（7.91%）；
  宿主内存 available **1079M**、swap 空闲 2967M、磁盘可用 **16G**。
- **上传 117 个文件 / 29.6 MB**：`seed/` 与 `data/regulation/` **必须上传**，否则法规与向量通道起不来。
- **容器内实测载入**：`companies 5 / reports 6 / financial_indicators 651`。

#### 容器化的两个必要条件（少一个就建不出表）

- **MySQL Checkpointer**：`autocommit=True` + **显式调 `saver.setup()`**，缺一个表就建不出来。
- **`PyMySQL[rsa]`**：认证插件要 rsa，不带 extra 会连不上。
- **`--default-time-zone=+08:00` 写进 mysqld 的 command 参数，不要用 `TZ` 环境变量**：
  后者会让 MySQL 初始化慢十几倍。
- **slim 镜像里没有 curl**：`HEALTHCHECK` 用 `python -c` 打 `/api/health`，不为此装 apt 包。

### 答案级评估（`eval/report_answer.md`）：从"检索命中"到"答案对不对"

- 评测集 `eval/golden_qa.jsonl` 共 **55 题**：数值题（`indicator`）**31** / 引用题（`literal`）**10** /
  法规题（`article`）**4** / 拒答题（`expect_refuse`）**10**；其中 `answer_expect.judge=true` **45 题**
  （`judge=false` 10 题，均为拒答题、无答案可判）。
- 四个数字：faithfulness **20.3%**、answer_relevancy **83.6%**（均分母 45 题）、数值准确率 **100.0%**（31 题）、
  引用命中率 **20.0%**（40 题）；辅助：证据完整性 63.3%（30 题）、路由命中率 91.1%（45 题）。
- **分桶读法（读 faithfulness 头条数字的关键）**：有检索上下文（literal/article）14 题 → faithfulness 65.4% /
  answer_relevancy 47.5%；无检索上下文（indicator，走工具层）31 题 → faithfulness 0.0% / answer_relevancy 99.8%。
  头条 20.3% 主要反映"有多少题没有可判的上下文"，**不是"答案在编造"**。
- **口径**：判分失败的题计为「未判」并**单列计数、不进分母**（本次 45 题全部判成，未判 0 题）——
  把未判当 0 分会凭空压低指标，直接丢掉又不留痕。
- **必须与「判分模型名 + 生成时间」一起标注**：`deepseek-flash` / **2026-09-23 08:34:28**（本次耗时 823s）。
  faithfulness / answer_relevancy 由 LLM 判分，**同模型重跑有 1~3pp 抖动**
  （实测同一 55 题两轮：23.7%/82.3% → 25.1%/79.8%），**换模型或重入库后不可直接比**。
- 样本量提醒：数值题 1 题 = **3.2pp**；两个 LLM 指标 1 题 = **2.2pp**。
- 复现：`python scripts/eval_rag.py --judge`（判分走 `src/llm.py` 的当前通道）。

### 检索级评测（55 题）：新值 + 0.5.0 旧值并列

| 配置 | page_hit@5 | 年份精度 | 公司精度 | 拒答正确率 | 空召回 |
|---|---|---|---|---|---|
| ① bm25（无过滤，Step 3 原样） | 32.4% | 84.4% | 55.1% | 40.0% | 0 |
| ② bm25 + 自动过滤 ← **最佳配置** | **54.0%** | **100.0%** | **100.0%** | **80.0%** | 0 |
| ③ bm25 + 过滤 + 每页配额 | 54.0% | 100.0% | 100.0% | 80.0% | 0 |
| ④ hybrid（双路 + RRF）+ 过滤 + 配额 | 54.0% | 100.0% | 100.0% | 80.0% | 0 |
| ⑤ hybrid + rerank + 过滤 + 配额 | 43.2% | 100.0% | 100.0% | 80.0% | 0 |

- 归因：①→② 只加**自动元数据过滤** → page_hit@5 **+21.6pp**、年份精度 +15.6pp、公司精度 +44.9pp；
  ④→⑤ 只加**重排** → **−10.8pp**（重排确实生效：**51/55** 题 top5 顺序被改变；丢 7 题、救回 3 题）。
- **与 0.5.0 旧值并列（旧值 34 题，同一脚本）**：① 24.0% ② 40.0% ③ 40.0% ④ 48.0% ⑤ 40.0%；
  年份精度 84.1%→100%；公司精度 52.4%→100%；拒答正确率 40%→80%。
  **分母从 34 题变成 55 题，所以新旧数字不可直接比** —— 评测集扩容本身就会让同一配置整体位移。
- 检索级口径：页级金标准 **37** / 宽泛题 **3** / 章节级 **1** / 拒答题 **10**（另有 **4 道法规题**
  `gt.type==article` 无年报页码概念、**不进页级分母** —— 所以 37+3+1+10=51，加 4 道法规题才是 55 题）。
  页级题 37 道 → **1 题 = 2.7pp**。
- 复现：`python scripts/eval_retrieval.py`（详见 `eval/report.md`）。

## 溯源是怎么保证的

三件事必须同时做对，缺一个引用就会指向错误位置（而错误引用**看起来和对的一样**）：

1. **页码与文本逐页绑定**：`parse_pdf` 逐页提取、逐页记录，绝不做"全文拼起来再切"。
   产出里每页都带 `page_no` / `section` / `text` / `chars`。
2. **章节识别只认可靠信号**：报告带「第X节」锚点时只认锚点；港式体例（无锚点）退回
   "页首标题"。目录页一律丢弃（否则第 1 页就会被判成「财务报告」，后面全错）。
   详见 `EXTENSION.md` §2 解析与切分类 —— 那里的每条坑都配了确定性用例。
3. **章节精确到"页内位置"，不是"整页一个标签"**：`parse_pdf` 为每页记录
   `section_marks = [{offset, section}, …]`，`chunk.py` 按块在页内的**字符偏移**判定归属。
   茅台把「第三节」与标题分成两行写在页中段（实测 600519/2024 P7 第 32 行），
   整页打标签会把该页上半（其实是第二节「非经常性损益」表格的续页）算进第三节 ——
   引用出处直接错一节。改判后修正了 **19 个 chunk**，且**切分正文逐字节未变**
   （只改标签、不改块，`index/bm25.pkl` 的召回集合同一批）。
4. **每个 chunk 自带完整元数据**：`{code, company, year, report_type, section, page_no,
   part, parts_total, kind}`，由 `citation.format_citation()` 统一拼装，
   少一个字段就拼不出引用。

当前支持的引用形态：

```
[1] 贵州茅台2024年年报 P87 管理层讨论与分析           # 单段页
[2] 比亚迪2024年年报 P112 重要事项 (第4/4段)          # 多段页带段号
[3] 3007502024年年报 P3                                # 缺章节/公司名时不崩、不拼空引用
```

## 手动修改接口约定

| 要改什么 | 改哪里 | 改完要做什么 |
|---|---|---|
| **目标公司清单** | `config/watchlist.yaml` | 重跑 `scripts/ingest_all.py` |
| **加年份 / 只跑某家** | `ingest_all.py --code 600519 --years 2024,2025` | — |
| 章节名与别名（换新体例） | `src/config.py` 的 `REPORT_SECTIONS` / `SECTION_ALIASES` | 重跑解析（`--skip-fetch --force`） |
| 章节识别行数与目录判定 | `src/config.py` 的 `HEADING_TOP_LINES` | 同上 |
| 切分块大小 / 碎块下限 / 页眉清理比例 | `src/config.py` 的 `CHUNK_*` / `MIN_CHUNK_CHARS` / `RUNNING_HEADER_RATIO` | 重跑切分与建索引 |
| 检索 topk / 召回数 / 短语加成 | `src/config.py` 的 `RETRIEVE_TOPK` / `RECALL_TOPN` / `BM25_PHRASE_BONUS` | 无需重启（每次查询读取） |
| **检索模式（bm25 / hybrid）** | `src/config.py` 的 `RETRIEVE_MODE`（或 `--mode` / `POST /api/ask` 的 `mode`） | 无需重启；默认 `bm25`（Step 3 基线**原样可复现**），`hybrid` = 双路 + RRF + 重排 |
| 单路召回候选数 / RRF 常数 `k` | `src/config.py` 的 `RECALL_TOPN` / `RRF_K` | 无需重启 |
| **两路口权重**（默认等权） | `src/retrieve/fusion.py` 的 `DEFAULT_WEIGHTS` | 无需重启；改完跑 `pytest tests/test_fusion.py` |
| **向量化通道**（api / local / none） | `src/config.py` 的 `EMBEDDING_BACKEND` / `EMBEDDING_PROVIDER` / `EMBEDDING_API_MODEL` / `EMBEDDING_BATCH_SIZE` | **换模型名必须重建向量库**（`index_vector.py --force`）—— 否则 manifest 指纹校验会直接拒绝加载 |
| **重排通道**（passthrough / api / local） | `src/config.py` 的 `RERANK_BACKEND` / `RERANK_API_MODEL` / `RERANK_TOP_N` | 无需重启；任何失败自动降级为原序直通并在 `note` 说明 |
| 向量最低相似度门槛 | `src/config.py` 的 `VECTOR_MIN_SCORE` | 无需重启（默认 `0.0` = 不设门槛，相关性交给 RRF 排名与拒答闸门判） |
| **自动元数据过滤开关** | `src/retrieve/pipeline.py` 的 `retrieve(auto_filter=…)` | 无需重启；评测里 `bm25 无过滤` 那一行就是把它关掉 |
| **多轮上下文保留轮数** | 环境变量 `FA_HISTORY_MAX`（或 `src/config.py` 的 `HISTORY_MAX`，默认 5） | 无需重启；只影响指代消解沿用的轮次数 |
| **问答参数**（候选数 / 引用上限 / 上下文预算 / 覆盖度门槛 / 停用词） | `src/config.py` 的 `ANSWER_CANDIDATE_TOPK` / `ANSWER_MAX_CITATIONS` / `ANSWER_CONTEXT_CHARS` / `ANSWER_MIN_COVERAGE` / `COVERAGE_STOPWORDS` | 无需重启 |
| **免责声明文案** | `src/config.py` 的 `ANSWER_DISCLAIMER` | 无需重启（随每次响应返回，不写在前端） |
| 服务端口 / 启动方式 | `launcher.py --port/--host/--mode/--check/--no-auth/--docker`，或 `uvicorn src.server:app --port 8000` | 前端走 `GET /`；SSE / compare 接口见上面 Step 6 章节 |
| **compose 宿主端口** | 环境变量 `APP_PORT`（默认 8000，`docker-compose.yml` 里 `${APP_PORT:-8000}:8000`） | 重建容器生效；**容器内始终监听 8000，只有宿主映射变** |
| **容器语料 seed** | `scripts/export_seed.py --out` 导出、`scripts/load_seed.py --seed` 载入；容器内由 `entrypoint.sh` 从 `/app/seed` 复制（规划中的 `FA_SEED_*` 环境变量**未落地**，用脚本参数代替） | 改语料后重新导出 `seed/` 并重建镜像 |
| **答案级判分模型** | `scripts/eval_rag.py --judge`（判分走 `src/llm.py` 的**当前通道**） | 指标随判分模型变；报告须标注「模型名 + 生成时间」 |
| **引用格式** | `src/citation.py` | 无需重启 |
| **财务指标口径与别名** | `src/config.py` 的 `INDICATORS` | 改完重跑 `scripts/init_db.py --fetch`（口径表是取数的唯一事实来源） |
| **派生比率口径** | `src/config.py` 的 `RATIOS` | 无需重启（每次调用读取）；改完跑 `pytest tests/test_tools_ratios.py` |
| 公司简称/全称与查不到 | `companies` 表（来自 `watchlist.yaml`） | 改 `watchlist.yaml` 后重跑 `init_db.py` |
| LLM 供应商 / Key / 模型 | `src/config.py` 的 `LLM_PROVIDERS`；Key 走环境变量或 `data/llm_keys.local.json` | 无需重启 |
| 业务库后端（SQLite/MySQL） | 环境变量 `FA_DB_BACKEND=mysql`，或 `data/db_keys.local.json` 的 `backend` | 重启进程生效 |
| MySQL 连接信息 | 环境变量 `MYSQL_*`，或 `data/db_keys.local.json` | 重启进程生效 |
| 取数期数（每公司几期） | `src/config.py` 的 `EM_PERIODS`（或环境变量 `EM_PERIODS`） | 重跑 `init_db.py --fetch` |
| **数据源注册表**（哪个源、哪张报表） | `src/config.py` 的 `DATA_SOURCES` | 无需重启；改完跑 `pytest tests/test_collect_company.py` |
| **指标口径与替代项** | `src/config.py` 的 `INDICATORS`（含 `sources` 优先级与 `counterpart`） | 改完**必须重跑取数**（`init_db.py --fetch`）；先用 `scripts/probe_app_sources.py` 核对字段名 |
| 取数节流 / UA / 超时重试 | `src/config.py` 的 `FETCH_*` / `CNINFO_UA` | 无需重启 |

> `data/llm_keys.local.json`、`data/db_keys.local.json` 已在 `.gitignore` 内；
> `.example` 模板保留在仓库里，**不要在源码里写兜底 Key**（仓库一公开就泄露，
> 而且会让"未配置 Key"的界面提示永久失效）。

## 测试

```bash
# 448 例确定性测试（v0.7.0 基线 369 例全部保持通过，语义未改）：解析（页码/章节/页内偏移/页眉，含"纯数字行绝不判页眉""页首页码不单独成区间"两条回归）、
# 切分（不跨页/元数据/按偏移判章节）、检索（过滤/引用/分词两套模式）、
# 业务库（幂等/占位符/时间表达式）、工具层（归一/选期/缺项/注册表契约）、
# 新浪源（表代号/期次筛选/中文项目名/失败暴露）、期间轴与缺失分档、
# 问答（覆盖度单段判定/引用编号校验/两条拒答闸门/降级路径）、
# RRF 性质（共识优先/缺席不罚/稳定排序）、重排降级路径、过滤纪律（抽不到不加、歧义放弃）、
# 向量错配不变量（行数/维度/指纹不符必须拒绝加载）、
# Step 5：路由（三意图优先级/反例）、数字（单位换算/容差/无出处检测）、
# 分析子图（零幻觉/不猜边界）、verify+HITL（优先级/证据分流/确定性拒答不挂起）、
# 主图（挂起与跨进程恢复）、HTTP（/api/ask 与 /api/hitl 契约）、法规检索、
# Step 6：SSE 契约（事件序/必带键/失败降级）、compare 返回结构、多轮指代消解（沿用与不猜的边界）、
# 答案级判定（数值命中/口径）、seed 载入
# v0.8.0 新增：鉴权契约（口令哈希/三种 token 异常/认证不区分用户名枚举/seed 幂等）、接口层鉴权
# （无 token → 401、公开路径恒公开）、网络搜索（provider 解析/交叉验证判定/网络语料入库与命中）、
# 图上的 websearch 兜底节点与 SSE web 事件、前端契约（登录门面/网络卡片/两套引用口径不混排）
# 全部纯函数，不联网、不落盘、不依赖外部服务
./.venv/Scripts/python.exe -m pytest -q

# 只看 Step 6 服务化与评测的部分（纯内存，不联网）
./.venv/Scripts/python.exe -m pytest tests/test_streaming.py tests/test_compare.py \
    tests/test_history.py tests/test_eval_metrics.py tests/test_seed_db.py -q

# 只看 Step 5 编排部分（纯内存，不联网）
./.venv/Scripts/python.exe -m pytest tests/test_router.py tests/test_numeric.py \
    tests/test_analysis_subgraph.py tests/test_verify_hitl.py \
    tests/test_agent_graph.py tests/test_regulation.py tests/test_server.py -q

# 只看 Step 4 混合检索的部分（纯内存，不联网、不读向量库）
./.venv/Scripts/python.exe -m pytest tests/test_fusion.py tests/test_rerank.py \
    tests/test_query_filter.py tests/test_vector.py -q

# 只看 Step 3 问答与引用的部分
./.venv/Scripts/python.exe -m pytest tests/test_answer_citation.py \
    tests/test_parse_page_meta.py tests/test_chunk_meta.py -q

# 只看 Step 2 + 第二数据源的部分
./.venv/Scripts/python.exe -m pytest tests/test_db.py tests/test_tools_indicators.py \
    tests/test_tools_ratios.py tests/test_tools_registry.py \
    tests/test_fetch_sina.py tests/test_collect_company.py -q

# 只看 v0.8.0 新增（鉴权 / 网络搜索 / 前端契约；纯内存，不联网）
./.venv/Scripts/python.exe -m pytest tests/test_auth.py tests/test_server_auth.py \
    tests/test_web_search.py tests/test_frontend.py -q
```

> `tests/conftest.py` 会把后端钉在 SQLite、取数节流置 0，避免单测意外打到真实库或网络。
>
> Step 2 的工具层测试跑在**合成数据**上（`synth_db` 夹具：可手算的整十亿数 + 刻意构造的
> 缺分项/期次不一致/简称歧义），这样每条断言都能人工复现；
> 真实取数的数值正确性由 `scripts/verify_step2_db.py` 负责核对（依赖网络，不进 CI）。

## 里程碑状态

- [x] **Step 0** 骨架 + 四类数据源可用性实测（巨潮 / 东财 / 新浪 / 东财行情）
- [x] **Step 1** 年报 PDF 入库 → 按页解析 → 切分 → BM25 索引 → 引用格式（v0.1，27 例测试全过）
- [x] **Step 2** 结构化财务库（长表 + 双后端）+ 只读工具层 4 个工具（v0.2，90 例测试全过）
  - 保险股（中国平安）F10 资产负债表/现金流量表整表为空 → 多源优先级回退解决
  - 会计恒等式五家 0.0000% 误差；毛利率派生值与官方口径差 `-0.00pp`
- [x] **Step 2.5** 第二数据源（新浪）+ 口径替代机制（v0.3，115 例测试全过）
  - 东财全系无保险股「归母净资产」→ 接入新浪 `fzb` 补齐（9,286.00 亿，恒等式零误差）
  - 「营业成本」对保险不存在 → 单列「营业支出」+ 工具层 `counterpart` 给出替代科目与值
  - 「毛利率」对保险不存在 → 单列「毛利率(保险口径)」并公开算式，不拿替代口径冒充原名
  - 修掉期间轴被补充源污染导致的**缺失误报**（把"某期没覆盖"说成"全源缺失"）
- [x] **Step 3** RAG 问答子图 + 引用生成与校验 + 双闸门拒答 + 最小 HTTP 服务（v0.4，149 例测试全过）
  - 引用编号只能从"给过模型的编号"里选：编造编号被丢弃并记进 `notes`（机制保证，不靠提示词）
  - 引用校验上界 = **实际送进上下文几段**（不是 `len(hits)`），否则等于我们替模型伪造出处
  - 证据覆盖度改**单段**判定：越界问题从"拼凑放行"变成"拒答且不调模型"（正常 0.67~1.00 / 越界 0.25~0.33）
  - 节名下沉到 chunk 级：修正 19 个边界块的章节出处，切分正文逐字节未变
  - 冒烟带**源 PDF 页核验**（独立证据链）：调模型 8/8、离线 25/25 命中，对不上 0
  - 实跑暴露并修掉 `_pack()` 形参名不一致的 `TypeError`（只在真调到模型的分支触发）
- [x] **Step 4** 向量召回 + RRF 融合 + 重排（混合检索）（v0.5，205 例测试全过）
  - 消融评测（34 题，`eval/report.md`，**当时口径**）：page_hit@5 **14.3% → 52.4%**，年份精度 **100%**，
    公司精度 **100%**，**0 空召回**；只加自动过滤就值 +23.8pp，加向量+RRF 再 +14.3pp
  - 向量库 2650 × 1024（`text-embedding-v4`，建库 298.9s）；`batch` 上限**实测是 10**（文档写 25，照抄会炸）
  - 自动过滤直击 Step 3 的硬伤（离线 6/20 条引用落在题面年份外）→ 混入率归零
  - 重排通道接了但默认关（`passthrough`），任何失败降级原序 + `note` 说明，链路不断
  - **默认 `RETRIEVE_MODE=bm25`**：Step 3 的基线原样可复现，混合检索是"可选启用"
  - 已知不足：2 道越界题确定性闸门失效（靠第二道模型自评拒答）；同页多块占 top5 名额
- [x] **Step 5** 完整 LangGraph 编排：规则路由 + 三子图 + 引用校验 + HITL + 审计（v0.6，307 例测试全过）
  - 路由**纯规则**（`compliance` > `numeric_cue+indicator` > 兜底 `rag`），可解释、可回归；
    反例「食堂菜谱」有疑问形但无库内指标 → 不判数值题
  - 数值题的数字**只来自工具层**、组句**不经模型**、`verify` 再回归检查一遍 → 模型没有编数字的机会
  - `verify` 证据**按意图分流**（analysis→工具结果 / compliance→法规条文 / rag→引用片段），
    HITL 优先级 `numeric_mismatch > citation_unsupported > low_confidence`；
    **确定性拒答与检索故障不挂起**（那是结论，不是待确认）
  - HITL 用 SQLite Checkpointer，**挂起可跨进程恢复**（演练：进程 A 挂起 → 清缓存 → 进程 B 恢复确认）
  - 合规独立索引 **132 条**（226 号 67 + 182 号 65），只给条文原文 + 版本适用期，**不下合规结论**
  - **顺手修掉一个数据层真 bug**：报表主表金额被当成"跨页页眉"整行删掉（"某科目金额是多少"答不出）
     → 加"纯数字行不参与重复判定 + 永不删"；重入库后 chunks **2650 → 3161**，配回归用例
- [x] **Step 6** 服务化补全（SSE 流式 / 多公司对比 / 多轮指代）+ 单文件前端 + 答案级评估 + 容器化交付（v0.7.0，369 例测试全过）
  - **SSE 事件协议固定**（`meta` → `token`* → `citations` → `verify` →（挂起）`hitl` → `done`，异常另有 `error`）；
    数值/合规两条意图**不流式**（答案本就是确定性拼装、无 token 可流）；**流式失败回落一次性输出 + `degraded`**，绝不静默中断
  - `done` 与一次性响应**同形** → 前端收 `done` 后**终态覆盖**，流式丢字以终态为准
  - `/api/compare` 返回键固定 `{ok, indicator, unit, rows, periods_consistent, chart, note}`；`src/compare.py` **复用已注册工具**不另写取数
  - 多轮指代消解 5 条规则：**有唯一历史才沿用、本轮显式实体优先、不唯一就不猜、年份不跨公司沿用**、只保留最近 `FA_HISTORY_MAX` 轮
  - 单文件前端 `web/index.html` 五项能力，**零 CDN、零构建**；图表手绘内联 SVG（`createElementNS`），不引图表库
  - 答案级评测（`eval/report_answer.md`）：faithfulness **20.3%** / answer_relevancy **83.6%** / 数值准确率 **100.0%** / 引用命中率 **20.0%**；
    faithfulness 头条数字按**有无检索上下文**分桶读（31 道数值题走工具层、判成 0 分是结构性的）
  - 检索级评测扩到 **55 题**：最佳配置 page_hit@5 **54.0%**（② bm25 + 自动过滤）；**分母从 34 变 55，与 0.5.0 不可直接比**
  - 容器化：`Dockerfile` + `docker compose up -d`（app + MySQL 双容器）；**容器链路在 VM `192.168.57.128` 上实测，本机 Windows 无 Docker**
  - 已知不足：**4 道法规题被路由成 `rag`**（`_COMPLIANCE_CUES` 未覆盖）；引用命中率分母含 30 道数值题（结构上不可能命中页码）；SSE 未覆盖 analysis/compliance 的流式
- [x] **v0.8.0** 登录鉴权 + JWT、库外问题联网兜底、前端工作台级改版（448 例测试全过，基线 369 例语义未改）
  - **鉴权**：`users` 表（pbkdf2 加盐慢哈希 + 恒时比较，不引 passlib/bcrypt）+ PyJWT（HS256）；
    业务端点走 Bearer **依赖注入**保护，`actor` 取自 token 的 `sub`（不再采信自报字段）；
    `FA_JWT_SECRET` 为空且鉴权开启时**启动即失败**；`register` 不接受 `role` 入参（防自助提权）→ 修不足 **G-11 的鉴权半边**
  - **联网兜底**：拒答原因 ∈ `{no_evidence, out_of_corpus, low_coverage}` 时触发 `verify` 之后的 `websearch` 节点
    （**不新增 intent**，保住全部路由用例）；provider 可插拔，**默认通道 `bing`（免 Key，国内可达）**；
    **≥2 个独立域名交叉验证一致才展示并入库**，入库走**独立索引**（`bm25_web.pkl`），不污染年报索引与 `absent_terms` 判据
  - **实测（本机）**：`贵州茅台 2024 年换手率是多少` → `consistent` + `ingested=true` + 4 个域名带 `url`/`fetched_at`（20.1s）；
    二次提问 → `source=corpus_cache`、`duration_ms=12`、`fetched_pages=0`、配额不再消耗；
    `公司食堂的菜谱是什么` → `refused=true` 保持、`insufficient_sources`、`ingested=false`（**联网不放行编造**）
  - **前端**：仍是 `web/index.html` **单文件零 CDN 零构建**；新增登录页 / 401 自动刷新跳登录 / 会话侧栏 / 骨架屏 / 深浅色 / 网络结果卡片
  - 已知不足（本轮新增，见不足清单）：无 Key 通道依赖对方页面结构（G-21）、网络语料无 TTL（G-22）、
    交叉验证是确定性启发式不做语义核验（G-23）、`conflict` 难自然出现（G-24）、缓存命中面过宽（G-25）；
    **Docker 构建未在本机实测**（本机无 Docker，G-20）
