# VM 部署时间线

> VM: Ubuntu 22.04, 192.168.57.128, user=vcvvcv, pass=123456
> 已有服务：Dify (80/443/8000), workflow-agent (MySQL 13306), weaviate, postgres
> 部署日期：2026-09-21 起，2026-09-27 完成

## 环境侦察（Day 1）

- VM 2.3Gi 可用内存 → compose mem_limit 保守设 App=1200m, MySQL=900m
- Dify 占用 80/443/8000 → 我们用 **App:8001, Nginx:8081**
- Paramiko SSH 连通 → 写 `scripts/vm_ssh.py` 做远程执行
- Docker + Docker Compose v2 已装好

## 构建 Bug 三连击

| 时间 | 事件 | 修复 Commit |
|---|---|---|
| Day 1 | `.gitignore` 的 `*.pkl` + `seed/data/index/` glob 把 BM25 索引排除 | `b0236ca`（unignore 规则） |
| Day 2 | `.dockerignore` 路径段匹配，`data/index/` 误命中 `seed/data/index/` | `638d5ab`（根锚 `/data/index/`） |
| Day 2 | Nginx 启动时 Grafana 未就绪，`proxy_pass grafana:3000` 预解析失败 | `c7234d4`（resolver + 变量 proxy_pass） |

## MySQL Checkpointer 幽灵

| 时间 | 事件 | 修复 |
|---|---|---|
| Day 2 | `POST /api/ask` LLM ON → `InterfaceError: (0, '')` | 裸 pymysql + db.get_conn 正常 → 锁定 Checkpointer |
| Day 2 | 加 `ping(reconnect=True)` | ❌ PyMySQLSaver 内部缓存旧 cursor |
| Day 3 | 三次迭代后放弃 conn cache，每次新建 | ✅ 稳定 |
| Day 3 | MySQL wait_timeout=86400 双保险 | ✅ |

## 真实 LLM 激活

- VM 上写 `~/fin-research-agent/.env`（gitignored）
- `DEEPSEEK_API_KEY` + `QWEN_API_KEY` 写入
- rebuild + up -d
- `run_agent(use_llm=True)` 容器内实测：✅ `degraded=False`，88s 返回
- `POST /api/ask` 实测：✅ 连续 2 次正常，无 InterfaceError

## 踩坑总结

1. **先 `.docker build -t test . && docker run -it test ls /app/seed/`** 验证 context，别等容器起不来才排查
2. **PyMySQLSaver 内部状态重建需要全新 conn**，ping 不够
3. **Docker Compose port mapping 是 host:container**，VM 端口冲突先侦察
4. **`.env` 只写 VM，git commit 前跑 `git check-ignore -v .env` 验证被忽略**
5. **docker restart 不重新读 env**，改 env 必须 `docker compose up -d`
