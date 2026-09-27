# .dockerignore 三层 Bug 复盘

> 发现时间：2026-09-27
> 修复 Commits：`a7d472b`（错方向）→ `638d5ab`（根锚修复）+ `b0236ca`（gitignore negation）

## 问题现象

Docker Compose 起 App 容器后，`/api/health` 返回 `chunks=0`，BM25 加载失败，Qdrant 也无数据。容器里 seed 目录几乎是空的 —— 但本地 `seed/data/index/` 明明有 `bm25.pkl`、`chunks/`、`vector/vectors.npy`。

## 根因：三层机制叠加

### 第 1 层：`.gitignore` 排除了 pkl

```gitignore
# 原来的规则
*.pkl
seed/data/index/
seed/data/vector/
seed/data/parsed/
```

`*.pkl` glob 把 `bm25.pkl` 也排除了，`seed/data/index/` 把 `chunks/` 目录也排除了。

**修复**：加 negation rules
```gitignore
!seed/data/index/
!seed/data/index/**
!seed/data/index/*.pkl
!seed/data/index/chunks/**
!seed/data/vector/
!seed/data/vector/**
!seed/data/vector/*.npy
!seed/data/vector/*.json
!seed/data/vector/*.jsonl
!seed/data/parsed/
!seed/data/parsed/**
```

### 第 2 层：`.dockerignore` 路径段匹配

Docker 的 `.dockerignore` 用的是**路径段匹配**（path segment matching），不是标准 glob。写 `data/index/` 会同时匹配：
- `/data/index/` （项目根的 data/）
- `/seed/data/index/` （seed 下的 data/index/）

因为 Docker 不关心 `data/index` 前面的路径前缀，只看**是否有一段叫 `data` 下接一段叫 `index`**。

**修复**：根锚所有规则
```
/data/index/
/data/vector/
/data/parsed/
```

加了 `/` 前缀后，只有项目根的 `data/index/` 被排除，`seed/data/index/` 不会误命中。

### 第 3 层：Dockerfile COPY 顺序

```dockerfile
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY src/ ./src/
COPY seed/ /app/seed/    # ← 这条在最后
```

前两层修好后，这条 COPY 正常工作。但中间有一次我把 negation 写在 `.dockerignore` 里而不是 `.gitignore` 里 —— 忘了 `docker build` 只看 `.dockerignore`，不看 `.gitignore`。结果 git 状态里 seed 数据全在，但 `docker build` context 里没它。

## 教训

1. **两层 ignore 分开想**：`.gitignore` 控制 git 跟踪，`.dockerignore` 控制 build context。不要假设它们行为一致。
2. **Docker 路径段匹配要根锚**：`data/index/` → `/data/index/`。
3. **容器部署前先验证 context 内容**：`docker build -t test . && docker run -it test ls /app/seed/data/index/`。别等容器起不来才排查。
4. **排查顺序**：先查 `.git status`（排除 gitignore 问题）→ 再查 `.docker build` 时的 `Step 3 [internal] load build context` 输出大小 → 最后查容器内文件。
