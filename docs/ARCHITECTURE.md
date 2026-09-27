# 架构设计文档 —— fin-research-agent v2.0（已部署）

> **状态**：已部署 ✅（Docker Compose，VM Ubuntu 22.04，2026-09-27 上线）
> **定位**：面向券商研究所的可核验财报问答 Agent
> **核心纪律**：防编造靠数据流设计，不靠模型自觉

---

## 一、系统拓扑

### 容器拓扑（Docker Compose）

```
VM 192.168.57.128
┌─────────────────────────────────────────────────────────┐
│                                                         │
│  ┌──────────────┐    8001→8000    ┌──────────────────┐ │
│  │   Nginx     │─────────────────▶│   App (FastAPI)  │ │
│  │  :8081      │  resolver 127.0.0.11               │ │
│  │  /grafana/  │─────────────────────────────┐      │ │
│  └──────────────┘                             │      │ │
│                                               │      │ │
│  ┌──────────────────────┐   ┌────────────────┐│      │ │
│  │   MySQL 8           │   │   Qdrant       ││      │ │
│  │   :3306 (internal)  │   │   :6333        ││      │ │
│  │   wait_timeout=24h  │   │   3161 pts     ││      │ │
│  │   业务库+checkpoint │   │   1024 dim     ││      │ │
│  └──────────────────────┘   └────────────────┘│      │ │
│                                               │      │ │
│  ┌──────────────────────┐                     │      │ │
│  │   Prometheus +       │◀────────────────────┘      │ │
│  │   Grafana            │   (metrics scrape)          │ │
│  └──────────────────────┘                             │ │
└─────────────────────────────────────────────────────────┘
```

### 端口与依赖

| 容器 | 内部端口 | 宿主机端口 | 内存 | 依赖 |
|---|---|---|---|---|
| App | 8000 | **8001** | 1200m | MySQL, Qdrant |
| Nginx | 80 | **8081** | — | App, Grafana |
| MySQL 8 | 3306 | — (internal) | 900m | — |
| Qdrant | 6333 | 6333 | — | — |

> VM 已有 Dify 占用 80/443/8000，故 App 用 8001、Nginx 用 8081 避免冲突。

### ENV 配置（docker-compose.yml + .env）

```yaml
APP_PORT=8001
FA_AUTH_ENABLED=0          # 单机部署免登录
FA_VECTOR_BACKEND=qdrant
FA_QDRANT_URL=http://qdrant:6333
LLM_ACTIVE=deepseek        # deepseek-flash（成本低速度快）
RETRIEVE_MODE=hybrid       # BM25 + 向量 RRF 融合
NGINX_PORT=8081
# MySQL wait_timeout=86400 (24h)，防止 server has gone away
```

**不入库**的 secrets：`.env` 里的 `DEEPSEEK_API_KEY` / `QWEN_API_KEY`（gitignored）。

---

## 二、请求流程（`POST /api/ask`）

```
客户端
  │
  ▼
① Router (src/graph/router.py)
  ├─ 规则分诊（不用模型！关键词 + 形态规则）
  ├─ intent ∈ {rag, analysis, compliance}
  └─ force_intent 可覆盖（测试用）
  │
  ├─ intent=analysis ──▶ 子图 analysis
  │   ├─ tools/indicators.py → 结构化财务库取数
  │   ├─ tools/ratios.py     → 算比率（LLM 只算算术）
  │   └─ 代码组句（数字全来自库）
  │
  ├─ intent=rag ───────▶ 子图 rag
  │   ├─ retrieve/pipeline.py
  │   │   ├─ bm25.py        → BM25 精确词召回
  │   │   ├─ vector.py      → Qdrant 向量召回
  │   │   ├─ fusion.py      → RRF 融合（共识优先、缺席不罚）
  │   │   └─ rerank.py      → 重排（passthrough 降级安全）
  │   ├─ LLM 生成答案（只用召回 chunk 的原文）
  │   ├─ citation.py        → 组装引用 [1] P87
  │   └─ verify.py          → 答案数字逐一与证据核对
  │
  └─ intent=compliance ─▶ 子图 compliance
      └─ 法规独立索引检索 → 只给条文原文，不下结论
  │
  ▼
② verify 校验层（必过）
  ├─ "无出处数字"是可数的、有颜色标记的
  ├─ 证据覆盖度不达标 → 拒答 + 说明原因
  └─ 拒答也是结论，而且是诚实的结论
  │
  ▼
③ 响应组装
  ├─ degraded=false → LLM 正常回复
  ├─ degraded=true  → BM25 原文摘录 + "未能生成综合答案"
  ├─ citations[]    → 每个引用的 page_no/section/code/year
  └─ retrieval      → {mode, returned, max_score}
```

---

## 三、关键架构决策

### 决策 1：Checkpoint 不缓存 MySQL 连接

| 备选 | 结论 | 理由 |
|---|---|---|
| A. 进程内长连接 + `ping(reconnect=True)` | ❌ 不够 | ping 只重建底层 socket，但 LangGraph `PyMySQLSaver` 内部缓存了旧 cursor/session 状态，后续 checkpoint 读写仍打到已关闭 fd → 报 `Bad file descriptor` |
| B. 每次调用新建 conn + saver | ✅ 采用 | 性能损失可接受（pymysql.connect + setup ≈ 30-50ms），彻底消除 stale socket |
| C. MySQL wait_timeout=86400 | ✅ 同采 | 24h idle 双保险 |

### 决策 2：规则分诊不用模型

意图就三类（查年报 / 数值分析 / 查法规），关键词和形态规则就能判准，用模型反而引入不确定性 + 花钱花时间。**确定性强的环节坚决不用模型**。

### 决策 3：Hybrid 检索（BM25 + Qdrant）

- 精确词查询（"营业收入""审计机构"）→ BM25 碾压性准
- 语义变体（"赚了多少钱" → 归母净利润）→ 向量强
- RRF 融合：共识优先、缺席不罚；重排 passthrough 降级安全

### 决策 4：溯源在数据入口处绑定

PDF 解析时就把 `page_no` / `section` / `code` / `year` 焊死在每个 chunk 的元数据里。引用不是"找出来的"而是"继承下来的"—— 回答里的 `[1] 茅台2024年报 P87` 天然可翻页核对。

### 决策 5：双 SQL 后端（SQLite / MySQL）

同一套代码，本地演示用 SQLite，容器部署用 MySQL。Checkpointer 同样双后端，env `FA_CHECKPOINT_BACKEND` 可切。

### 决策 6：Qdrant 跳过 re-embedding

构建容器时直接 upsert 已有的 `seed/data/vector/vectors.npy`（3161×1024，text-embedding-v4），避免在线重算成本。Point ID 用 `abs(hash(chunk_id)) % (2^31-1)` 保证确定性。

---

## 四、已知限制（v2.0 现状）

| # | 限制 | 影响 | 处置 |
|---|---|---|---|
| L1 | Nginx healthcheck 偶发 unhealthy（内部端口映射差异） | 非关键，容器实际可用 | 接受，健康检查实际看 App endpoint |
| L2 | 单实例无水平扩展 | 高并发场景 | 内存限制保守（App 1200m） |
| L3 | 联网兜底是启发式交叉验证 | 不做语义级核验 | 边界已写入不足清单，知情接受 |
| L4 | `.env` 手动写 VM | 密钥轮换不便 | 下一步用 Docker secrets / Vault |
| L5 | 前端静态构建产物 `web/` 与源码分离 | 前端改动需重新 build | 临时目录挂载或 CI |
| L6 | 目前 6 家公司 seed 数据 | 覆盖有限 | 入库向导支持动态扩展 |

---

## 五、技术栈

| 层 | 技术 | 版本 |
|---|---|---|
| 语言 | Python | 3.13-slim |
| 编排 | LangGraph | 1.2.11 |
| Web | FastAPI + Uvicorn | 0.141.1 / 0.53.0 |
| 数据库 | MySQL | 8.0 |
| 向量库 | Qdrant | latest |
| 检索 | rank-bm25 + Qdrant Python | 0.2.2 / 1.19.1 |
| LLM | DeepSeek Flash (text) + text-embedding-v4 (embed) | — |
| 容器 | Docker Compose | v2 |
| 监控 | Prometheus + Grafana | — |
