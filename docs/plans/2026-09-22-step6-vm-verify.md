# Step 6 D8：在 VM 192.168.57.128 上的容器化实测验收

- 日期：2026-09-23
- 对应步骤：`2026-09-22-step6-service-eval-container-workflow.md` 的 **Step D8**（gate: human）
- 执行者：控制器（TraeCode 主会话）＋ 一次 `--with-env-keys` 决策由用户裁定
- 原始记录：`2026-09-22-step6-metrics-raw.md` §15

---

## 0. 一句话结论

`docker compose up -d --build` 在 VM 上**完整跑通**：app + MySQL 双容器均 healthy，
`/api/health` 的 `index` / `vector` / `regulation` / `checkpointer` 四项全可用且
`checkpointer.backend="mysql"`，`/api/compare` 返回 2 行、`/api/ask/stream` 首帧为 `meta`。
闸门命令 `python scripts/deploy_vm.py --verify-only` **退出码 0**。

> ⚠️ 本机**没有 Docker**（Windows 侧无 docker CLI），所以容器链路**只能**在 VM 上实测 ——
> 这一点与 README 里"容器链路在 VM 上实测"的表述一致。

---

## 1. 环境与前置约束（都是实测出来的，不是假设）

| 项目 | 实测值 |
|---|---|
| 主机 | `192.168.57.128`，Ubuntu 22.04.4，`vcvvcv@22` |
| Docker | 29.1.3（`buildx` v0.26.2，BuildKit 默认开启） |
| 内存 | 共 3.8 Gi，其中被 Dify 整套（13 容器）+ 姊妹项目 workflow-agent 占着；可用约 1.1~1.6 Gi |
| Swap | 3.9 Gi（实测空闲约 3.0 Gi）—— 这是本项目能在内存紧张的 VM 上活下来的关键 |
| 磁盘 | 49 Gi，实测从 17 Gi 可用降到 **16 Gi 可用**（构建后） |
| 既有负载 | 整个 Dify 栈 + `workflow-agent`（app + mysql，均 healthy） |

### 1.1 三个必须绕开的既有事实

1. **宿主 8000 端口已被占用**：`workflow-agent` 映射了 `0.0.0.0:8000->8000/tcp`。
   本项目 compose 若照默认 `8000:8000` 起会 `port is already allocated`。
2. **内存余量很小**：可用内存长期在 1.1~1.6 Gi 之间。
3. **Docker 镜像加速器全部失效**（见 §2.1）：`python:3.13-slim` 在 VM 上**拉不下来**。

---

## 2. 为解决环境阻断所做的处置（3 项，均已留痕）

### 2.1 基础镜像走 daocloud（按用户裁定「注入/绕过」思路执行）

`/etc/docker/daemon.json` 里配的两个加速器**域名都已不存在**：

```
$ getent hosts mirror.baidubce.com   → NXDOMAIN
$ getent hosts hub-mirror.c.163.com  → NXDOMAIN
$ curl https://mirror.baidubce.com/v2/        → 000（8.3s 超时）
$ curl https://docker.m.daocloud.io/v2/       → 401（2.4s，正常：未带 token）
$ curl https://registry-1.docker.io/v2/       → 000（5.2s；只解析出 IPv6，无路由）
```

第一次 `docker compose up -d --build` 因此直接失败：

```
#3 ERROR: failed to do request: Head "https://mirror.baidubce.com/v2/library/python/manifests/3.13-slim":
   dial tcp: lookup mirror.baidubce.com on 127.0.0.53:53: no such host
```

**处置（最小侵入）**：**不动 VM 的 `daemon.json`、不重启 docker**（那样会把 Dify 与
workflow-agent 全部重启，属于动别人的栈），改为**显式从可达的 daocloud 拉下来再打回本地 tag**：

```
$ docker pull docker.m.daocloud.io/library/python:3.13-slim
$ docker tag  docker.m.daocloud.io/library/python:3.13-slim python:3.13-slim
→ python:3.13-slim 178MB（本地已有），mysql:8.0 1.1GB（本地早已有，无需拉）
```

之后 `docker compose up -d --build` 的 `#6 FROM docker.io/library/python:3.13-slim@sha256:8d9d0b8b…`
直接命中本地镜像，**不再出网**。

> 这解释了为什么姊妹项目 `workflow-agent` 能用 `python:3.12-slim`（本地已缓存）；
> 本项目原本在 Dockerfile 里写的是 `python:3.13-slim`（与开发机 `.venv` 的 3.13.14 对齐），
> 该 tag 在 VM 上从未缓存过，才暴露出加速器失效的问题。

### 2.2 宿主端口改 8001（按用户裁定）

`docker-compose.yml` 的端口映射改为可覆盖形式：

```yaml
ports:
  - "${APP_PORT:-8000}:8000"
```

**容器内始终监听 8000 不变**（Dockerfile `EXPOSE 8000`、`CMD --port 8000`、镜像内 `HEALTHCHECK`
都打 `127.0.0.1:8000`），所以：

- 容器内健康检查与所有探针**不受影响**；
- `scripts/deploy_vm.py` 的 HTTP 探针全部走 `docker compose exec -T app python -c`
  **在容器内**打 `127.0.0.1:8000`，所以脚本侧不需要跟着改成 8001；
- 只有**宿主侧映射**需要避开 workflow-agent 占的 8000。

`scripts/deploy_vm.py` 新增模块常量 `VM_APP_PORT`（默认 `8001`，可用同名环境变量覆盖），
`docker compose up` 时以前缀 `APP_PORT=<VM_APP_PORT>` 注入。

### 2.3 内存护栏（按用户裁定「先 prune 再限内存」）

- 先 `docker system prune -f` → `Total reclaimed space: 265MB`。
- `docker-compose.yml` 加护栏：app `mem_limit: 1200m`、web-db `mem_limit: 900m`；
  MySQL 另加 `--innodb-buffer-pool-size=128M` 与 `--performance-schema=OFF`（省 100~200MB）。
- 护栏刻意给得**比实际需要宽**：它的目的是防"某进程失控拖死宿主机"，不是挤压正常运行 ——
  Docker 的 `mem_limit` 顶到会**直接 OOM-kill 容器**，而本 VM 有约 3 Gi swap，
  真紧张时宁可换页慢一点也不要被杀。

### 2.4 注入 embedding / LLM Key（**由用户明确裁定：注入真实 Key**）

容器裸起时环境里没有任何 Key，向量通道会**按设计降级**为纯 BM25 ——
第一次完整跑的实测就是如此（唯一未过的断言）：

```
[deploy] ❌ /api/health 断言未过：
   - vector.available != true（实际 False，reason='向量通道 qwen 未配置 API Key（可设环境变量或在 data/llm_keys.local.json 填写）'）
```

用户裁定「注入真实 Key」，于是给 `scripts/deploy_vm.py` 加了**显式 opt-in 开关**
`--with-env-keys`（默认关闭）：

- 读本机 `data/llm_keys.local.json` 的非空 Key，写成 VM 侧 `<REMOTE_DIR>/.env`（权限 `600`）；
- 只打印**键名 + 条数**，**绝不回显 Key 值**；
- 该 `.env` 既在 `deploy_vm.py` 的上传排除表里，也在本机 `.gitignore` 中 —— **不会进仓库**。

实测输出：

```
[deploy] 已把 2 个 Key 写入 VM 侧 /home/vcvvcv/fin-research-agent/.env（权限 600）：
         QWEN_API_KEY, DEEPSEEK_API_KEY —— 值不回显
```

**为什么注 Key 之后 vector 就可用**（读源码确认，不是试出来的）：
`src/retrieve/vector.py` 的 `status()` 三层检查 ——
`embedding_ready()`（backend=api 时**只要求 Key 非空**，见 `src/embedding.py:114-118`）
→ `index_vector.verify_against(spec)`（只比 **model + dim**，见 `src/ingest/index_vector.py:260-264`）
→ 加载。`data/vector/manifest.json` 记的是 `backend=api / provider=qwen / model=text-embedding-v4 / dim=1024`，
与容器内 API 通道算出的 spec 同源，故直接通过。

---

## 3. 真实执行时序与耗时

上传：**117 个文件 / 29.6 MB**（按前缀裁掉 `data/raw|parsed|index|vector|db`、`eval`、`docs`、`tests`，
但**保留 `seed/` 与 `data/regulation`**）。

### 3.1 第一次构建（冷，含全部依赖安装）

| 阶段 | 耗时 |
|---|---|
| `pip install -r requirements.txt`（阿里云源） | **1268.4 s（≈21.1 min）** |
| 导出镜像层 `exporting layers` | **141.2 s** |
| 解包 `unpacking` | 69.1 s |
| 导出总计（含 provenance） | **212.2 s（≈3.5 min）** |
| **构建合计** | **≈1478 s（≈24.6 min）** |

- pip 源延迟实测：阿里云 `200 / 5.2s`、腾讯云 `200 / 4.1s`、清华 `403`（印证 Dockerfile 注释里"清华源会 403"）。
- 容器创建/启动阶段：`web-db Creating → Created → Starting → Healthy`（MySQL 首次初始化约 1 min），
  随后 app `Starting → Started`，entrypoint 里等库 + 建表 + 灌 seed 完成。

### 3.2 第二次构建（热，注入 `.env` 后重建 app 容器）

除 `requirements.txt` 之后的 `COPY` 层外**全部 CACHED**（`pip install` 层命中缓存）：
`exporting layers 7.2s` / `#20 DONE 9.4s`；`Container fin-research-agent Recreate → Recreated → Started`。
**这就是"依赖层放前面"的收益：改代码/配置不再重装依赖。**

### 3.3 镜像大小（两个口径都给，避免误读）

| 口径 | 值 |
|---|---|
| `docker image inspect … --format '{{.Size}}'`（权威，内容大小） | **176,676,355 B ≈ 168.5 MiB** |
| `docker images` 表里的 `CONTENT SIZE` | **177 MB** |
| `docker images` 表里的 `DISK USAGE`（Docker 29 新口径，含 attestation/未压缩层摊算） | 710 MB |

### 3.4 entrypoint 全链路实测输出（app 容器日志原文节选）

```
[entrypoint] 启动：FA_DB_BACKEND=mysql
[entrypoint] 已同步 seed 语料 → /app/data（cp -rn，不覆盖既有文件）
[entrypoint] 等待 MySQL web-db:3306 就绪（最多 60s）…
[entrypoint] MySQL web-db:3306 已就绪
[entrypoint] 建表 + 同步清单：python scripts/init_db.py
后端 mysql  库 {'host': 'web-db', 'port': 3306, 'user': 'agent', 'password': 'finagent', 'database': 'fin_research'}
建表完成（幂等）
同步 companies 5 家、reports 6 份（来自 config/watchlist.yaml 与 data/parsed）
[entrypoint] 载入 seed 业务数据：python scripts/load_seed.py
已从 /app/seed/business.json 灌入后端 mysql
=== 库内各表行数（载入后）===
  companies                   5 行
  reports                     6 行
  financial_indicators      651 行
  golden_qa                   0 行
[entrypoint] 启动服务：uvicorn src.server:app --host 0.0.0.0 --port 8000
```

---

## 4. 闸门命令与实测输出

**闸门命令（计划 D8 的 verify 原文）**：

```
scripts/deploy_vm.py --verify-only
```

**退出码：`D8_GATE_EXIT=0`**。

### 4.1 `/api/health` 原文（`--verify-only` 实测，逐字）

```json
{
  "ok": true,
  "app": "Fin Research Agent",
  "version": "0.6.0",
  "pid": 1,
  "index": { "ok": true, "chunks": 3161, "companies": 5, "company_years": 6, "avg_tokens": 174.9 },
  "regulation": {
    "available": true,
    "path": "/app/data/index/bm25_regulation.pkl",
    "articles": 132,
    "by_doc": { "xinpi-226": 67, "xinpi-182": 65 },
    "titles": [
      "上市公司信息披露管理办法(中国证监会令第182号)",
      "上市公司信息披露管理办法(中国证监会令第226号)"
    ]
  },
  "llm": { "provider": "deepseek", "model": "deepseek-flash", "ready": true, "reason": null },
  "vector": {
    "available": true, "reason": "ok", "stage": "ok",
    "vectors": 3161, "dim": 1024, "model": "text-embedding-v4", "backend": "api",
    "companies": 5, "company_years": 6, "built_at": "2026-09-22 19:34:36"
  },
  "rerank": {
    "available": false, "backend": "passthrough",
    "reason": "未启用（RERANK_BACKEND=passthrough，候选按融合序直通）"
  },
  "checkpointer": { "backend": "mysql", "path": "web-db:3306/fin_research", "exists": true, "threads": 2 },
  "hitl": { "enabled": true, "min_confidence": 0.4,
            "triggers": ["citation_unsupported", "low_confidence", "numeric_mismatch"] },
  "audit": { "last_error": null, "rows": 2, "by_action": { "ask": 2 } },
  "frontend": { "available": true, "path": "/app/web/index.html" },
  "retrieve_modes": ["bm25", "hybrid"],
  "retrieve_mode_default": "hybrid",
  "intents": ["analysis", "compliance", "rag"]
}
```

### 4.2 断言逐条结果

| # | 断言 | 实测 | 结果 |
|---|---|---|---|
| 1 | `/api/health` 的 `ok=true` | `true` | ✅ |
| 2 | `index.ok=true` | `true`（3161 chunks / 5 家 / 6 个公司-年份） | ✅ |
| 3 | `vector.available=true` | `true`（3161 向量 / dim 1024 / text-embedding-v4） | ✅ |
| 4 | `regulation.available=true` | `true`（132 条，182 号 65 + 226 号 67） | ✅ |
| 5 | `checkpointer.backend="mysql"` 且 `exists=true` | `mysql` / `true` / `path=web-db:3306/fin_research` | ✅ |
| 6 | `/api/compare` `ok=true` 且 `rows` 长度 2 | `ok=true`，`rows=2`（indicator=营业总收入） | ✅ |
| 7 | `/api/ask/stream` 首个 SSE 事件为 `meta` | `first_event="meta"` | ✅ |

**完整流程那次（`--keep --with-env-keys`）的末尾也逐字打印了同样的三行 ✅**，退出码 0。

---

## 5. 资源占用实测

```
fin-research-agent         222MiB / 1.172GiB (18.50%)   CPU 0.16%
fin-research-agent-web-db   71MiB /   900MiB ( 7.91%)   CPU 1.04%
宿主：内存 available 1079M，swap 空闲 2967M；磁盘 49G 已用 31G / 可用 16G
```

- 两个容器的 `mem_limit`（1200m / 900m）**远未被顶到**（实际用量分别约 19% / 8%），
  说明护栏没有挤压正常运行 —— 它只在失控时兜底。
- `docker system prune -f` 在本轮开始前回收 **265MB**。

---

## 6. 与计划 / 预期的偏离（如实记录，不改口径）

| # | 计划/预期 | 实测 | 处置与理由 |
|---|---|---|---|
| 1 | 宿主端口 `8000:8000` | `0.0.0.0:8001->8000` | 8000 被姊妹项目 workflow-agent 占用；改为可覆盖的 `${APP_PORT:-8000}`，VM 侧用 `VM_APP_PORT=8001`。**容器内仍是 8000**，探针与健康检查不受影响 |
| 2 | `python:3.13-slim` 直接可拉 | 拉不动（加速器域名 NXDOMAIN） | 从 daocloud 拉下并打本地 tag；**未改 `daemon.json`、未重启 docker**（避免动 Dify/workflow-agent） |
| 3 | 容器环境自带 Key（`.env.example` 有键位） | 裸起时**无 Key** → `vector.available=false` | 用户裁定注入真实 Key；新增 `--with-env-keys`（默认关闭），Key 只落 VM 侧 `.env`（600）且不回显 |
| 4 | 计划 D7 的 CLI 只有 `--verify-only / --down / --keep` | 现多一个 `--with-env-keys` | 本轮为解决 #3 而加，已在 `deploy_vm.py` 的 docstring 与 CLI help 里写明 |
| 5 | 期望 `rerank` 也可用 | `rerank.available=false`（`RERANK_BACKEND=passthrough`） | **不在 D8 的验收判据内**（判据只要求 index/vector/regulation/checkpointer）。是否为容器显式配置重排 Key 留给部署方决定 |
| 6 | `docker image` 大小"一个数" | Docker 29 表里同时有 DISK USAGE 710MB 与 CONTENT SIZE 177MB | 两个口径都写进 §3.3，以 `inspect.Size`（176,676,355 B）为准 |
| 7 | 首次构建"几分钟" | 实测 ≈24.6 min（pip 就占 21.1 min） | 阿里云源延迟高（5.2s/请求），非代码问题；第 2 次构建因依赖层缓存只用 9.4s |

---

## 7. 未验证 / 存疑（不许在 E5 写成"已完成"）

1. **`launcher.py --docker` 分支**：本机无 Docker，未在真机跑过 `docker compose up -d` 分支
   （`--check` 分支已在 D9 验过）。VM 上的部署是**直接跑 compose**，不是经 launcher。
2. **`launcher.py` 的 Ctrl+C 优雅回收**：`finally: _terminate()` 未做真机信号验证。
3. **本机容器形态**：本机无 Docker，`docker compose up -d` 的"本机一键起"只做了静态检查。
4. **`--down` 分支**：本轮未执行（保留栈运行，便于人工复核）。释放 VM 内存时可跑
   `python scripts/deploy_vm.py --down`。
5. **`.env` 里的 Key 会长期留在 VM** `/home/vcvvcv/fin-research-agent/.env`（600 权限）。
   若需清理：删该文件并 `APP_PORT=8001 docker compose up -d app` 重建容器即可回到"无 Key 降级"形态。
6. ~~**`version` 仍是 `0.6.0`**：`src/server.py` 的 `APP_VERSION` 待批次 E 统一抬到 0.7.0。~~
   **✅ E5 已抬到 `0.7.0`**（见 §9.4）。注意 VM 上**运行中的容器**仍是抬版本前构建的镜像、
   仍报 `0.6.0`（§4.1 原文即那次运行的逐字记录）—— 纯版本字符串差异，不影响功能判据。
7. **MySQL 卷已初始化**：若后续改 `MYSQL_*`，需 `docker compose down -v` 才会重新初始化
   （这是 compose 卷的固有语义，已在 `docker-compose.yml` 注释里写明）。

---

## 8. 与 5 条 success_criteria 的关系（D8 这一条）

| success_criteria | 状态 |
|---|---|
| ① `eval/report_answer.md` 四数字齐全 | 与 D8 无关（批次 C 已完成） |
| ② **VM 上 `docker compose up -d --build` 后 health 全 ok 且 backend=mysql** | **✅ 本文件即证据**（§4.1 原文 + `D8_GATE_EXIT=0`） |
| ③ `python launcher.py` 一条命令起服务 + 前端五项能力 | 已实测（见 `2026-09-22-step6-browser-notes.md`）；`--docker` 分支见 §7-1 |
| ④ pytest 全绿 ≥350 例 | 批次 E 的 E5 复核 |
| ⑤ 四份文档与实现一致 | 批次 E 的 E1–E4 处理，E5 逐条核对 |

**结论：success_criteria ② 已由本次实测闭环。**

---

## 9. E5 最终回归与验收核对（2026-09-23，`gate: human`）

> 本节是 `2026-09-22-step6-service-eval-container-workflow.md` **Step E5** 的核对结论，
> 由**控制器直跑**（未委派子代理）。§8 是 D8 当时的核对（只覆盖 ②），本节覆盖**全量五项**。

### 9.1 回归实测（四条命令，均为控制器亲手跑）

| 命令 | 结果 |
|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **369 passed, 1 warning**（41.70s；抬版本后复跑 38.85s，仍 369 passed） |
| `scripts/smoke_qa.py --no-llm` | **退出码 0** —— `✅ 没有完整性缺陷`；20 条引用回源 PDF 命中 20 / 对不上 0 |
| `scripts/smoke_step5.py` | **退出码 0** —— 跨进程「挂起 → 读回 → 确认」链路通过 |
| `scripts/smoke_step6.py` | **退出码 0** —— GET `/` 200 + `health.frontend.available=true` + `/api/compare` 2 行 + `/api/ask/stream` 首帧 `meta` |

那唯一 1 个 warning 是 starlette 的 `anyio.abc.BlockingPortal` 弃用告警，与项目代码无关（0.5.0 起就在）。

### 9.2 五项 success_criteria 的证据链（逐条）

| # | success_criteria | 实测证据 | 判定 |
|---|---|---|---|
| ① | `eval/report_answer.md` 四数字齐全 + 模型名 + 时间戳 | 文件在：faithfulness **20.3%**(45) / answer_relevancy **83.6%**(45) / 数值准确率 **100.0%**(31) / 引用命中率 **20.0%**(40)；判分模型 **deepseek-flash**；生成时间 **2026-09-23 08:34:28** | ✅ |
| ② | VM 上 `docker compose up -d --build` 后 health 全 ok 且 backend=mysql | 本文件 §4.1 health 原文（`index`/`vector`/`regulation`/`checkpointer` 全 ok、`checkpointer.backend="mysql"`）+ `D8_GATE_EXIT=0` | ✅ |
| ③ | `python launcher.py` 一条命令起服务；前端五项能力 | **本轮补测真机启动**：`launcher.py --no-browser --port 8123` → 日志 `[启动] …uvicorn…` → `[就绪] http://127.0.0.1:8123/`，`/api/health` 返回 `ok=true`（`version=0.7.0`、`frontend.available=true`、`index.ok=true`）、`GET /` **200**；五项能力见 `2026-09-22-step6-browser-notes.md` ①~⑤ | ✅（`--docker` 分支与自动开浏览器见 9.3） |
| ④ | `pytest -q` 全绿、用例数 ≥ 350 | **369 passed**（新增用例覆盖 SSE 契约 / compare / 多轮指代 / 答案级判定 / seed 载入） | ✅ |
| ⑤ | 四份文档与实现一致、无过期数字 | E1–E4 四条 verify 全 `ok`；E5 复核又清掉 EXTENSION §3 第 12/14 条的过期表述（"34 题小样本"、"答案级仍留 Step 6 的 RAGAS"）；`APP_VERSION` 0.6.0 → **0.7.0** 对齐文档口径 | ✅ |

### 9.3 仍**没有证据**的项（如实标注，未写成"已完成"）

1. **`launcher.py --docker` 分支** —— 本机无 Docker，未真机跑过（`--check` 与默认本机分支已验）。
2. **`launcher.py` 的 `webbrowser.open`** —— 本轮以 `--no-browser` 补测真机启动，未单独验证"自动开浏览器"
   （一行 stdlib 调用）；页面本身已在 B6 浏览器实测中验证。
3. **`launcher.py` 的 Ctrl+C 优雅回收** —— 未做真机信号验证（本轮用 `taskkill /T` 收尾）。
4. **`deploy_vm.py --down`** —— 未执行（VM 上刻意保留运行中的栈便于人工复核）。
5. **本机容器形态**（`docker compose up -d` 在本机一键起）—— 本机无 Docker，只做了静态检查。

> 处置：以上 5 项已在 README / CHANGELOG 的「已知不足」里如实登记，**不在任何文档里写成已完成**。

### 9.4 版本一致性（E5 裁定并落地 —— 闭环 §7-6 的待办）

`src/server.py` 的 `APP_VERSION`：**`0.6.0` → `0.7.0`**，与 README / CHANGELOG 的 v0.7.0 口径对齐。
改后复跑 `pytest -q` 仍 **369 passed**；并实测 `/api/health` 返回 `"version":"0.7.0"`（§9.1 依据）。

> ⚠️ **VM 上运行中的两个容器仍报 `version="0.6.0"`** —— 那是**抬版本之前构建的镜像**。
> 要让容器也报 0.7.0，需在 VM 上重跑 `docker compose up -d --build`（依赖层有缓存，导出很快）。
> 这是**纯版本字符串**差异，不影响任何功能判据。

### 9.5 两处 E5 期间记录在案的偏离（不改口径）

1. **计划 E1 的 action 提到往接口约定表补 `FA_SEED_*`** —— 全仓（非 md 文件）Grep 该键
   **零匹配**：D2 实际把它实现成了命令行参数（`export_seed.py --out` / `load_seed.py --seed`）。
   README 按**实际**写入并注明「规划中的 `FA_SEED_*` 未落地」——**未编造不存在的键**。
2. **EXTENSION §3 第 12/14 条的同步** —— 原委派说明"只改指定小节"，故子代理未动这两条；
   E5 逐条核对 success_criteria ⑤ 时发现其与更新后的 E6 自相矛盾（仍称答案级"留 Step 6 的 RAGAS"、
   评测集"34 题"），**由控制器就地同步**。

**E5 结论：五项 success_criteria 全部有实测证据；③ 的 `--docker` 分支与自动开浏览器除外，已在 9.3 明示。**

### 9.6 human gate 放行

**2026-09-23：用户验收通过（`gate: human` 已放行），Step 6 收尾。**
VM 上的栈**刻意保留运行**（未执行 `--down`），便于人工复核 `/api/health`、前端页面与 `/api/compare`。