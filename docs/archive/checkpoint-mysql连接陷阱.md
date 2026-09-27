# MySQL Checkpointer 连接陷阱

> 时间：2026-09-27
> 修复 Commits：`f22d974`（Ping 不够）→ `cad3b06`（放弃缓存）+ `570fb2e`（wait_timeout 双保险）
> 涉及文件：`src/graph/checkpoint.py`、`docker-compose.yml`

## 现象

`POST /api/ask`（`use_llm=true`）偶发但稳定地报：
```
InterfaceError: (0, '')
OperationalError: (2006, "MySQL server has gone away (OSError(9, 'Bad file descriptor'))")
```

**关键观察**：
- 容器**全新启动**也报（排除了长时间 idle 问题）
- `use_llm=false`（纯 BM25）**不**报 —— 说明问题只在 Checkpointer 被调用时
- pymysql 裸连 `SELECT 1` 正常，`db.get_conn()` 正常 —— 问题只在 Checkpointer 的 PyMySQLSaver

## 三次迭代

### 迭代 1（commit f22d974）：加 `ping(reconnect=True)`

```python
# _make_mysql() 里的 cached 分支
if cached is not None:
    saver, conn = cached
    try:
        conn.ping(reconnect=True)   # ← 加了这个
    except Exception:
        _SAVERS.pop(key, None)
    else:
        return cached
```

**以为**：ping 能自动重连 stale socket。
**实际**：❌ 还是 `Bad file descriptor`。

根因：PyMySQL 的 `ping(reconnect=True)` 只重建底层 **TCP socket 文件描述符**，但 LangGraph `PyMySQLSaver` 内部缓存了 `cursor` 对象、会话状态等。这些 Python 对象仍指向**旧 fd 的整数**（旧 fd 已被 close() 回收，OS 可能把同一个整数分配给了完全不同的资源）。下次 checkpoint 读写用旧 cursor 执行 SQL → 打到无效 fd → `Bad file descriptor`。

### 迭代 2（commit cad3b06）：放弃进程内缓存

```python
def _make_mysql():
    """每次调用新建 conn + saver，不缓存。"""
    conn = pymysql.connect(charset="utf8mb4", cursorclass=DictCursor,
                           autocommit=True, connect_timeout=5, **config.MYSQL)
    saver = PyMySQLSaver(conn)
    saver.setup()
    return saver, conn   # 调用方用完即弃
```

**以为**：代价有点大（每次 ~50ms），但彻底消除 stale cursor。
**实际**：✅ 稳定了。连续 2 次 ask 请求都正常返回 `degraded=False`。

关键洞察：**当上层库（PyMySQLSaver）不尊重连接状态时，最土的"每次新建"反而最稳**。连接池不是银弹。

### 迭代 3（commit 570fb2e）：MySQL 服务端 wait_timeout=86400

```yaml
# docker-compose.yml web-db command
command:
  - mysqld
  - --wait-timeout=86400        # 24h（默认 28800s=8h）
  - --interactive-timeout=86400 # 24h
```

**双保险**：即使以后又有人把缓存加回去，24h 的 wait_timeout 也比默认 8h 宽松很多。

## 最终方案

| 方案 | 效果 |
|---|---|
| pymysql ping(reconnect=True) | ❌ 不够（PyMySQLSaver 内部缓存旧 cursor） |
| 每次调用新建 conn + saver | ✅ 稳定（代价 ~50ms/次） |
| MySQL wait_timeout=86400 | ✅ 双保险（即使有人又缓存回去） |

## 教训

1. **PyMySQLSaver 不是透明的** —— 它内部维护了 cursor 和 session 状态，连接重建≠内部状态重建。
2. **LangGraph Checkpointer 的连接管理是 caller 责任** —— 它只要求传一个 `conn` 对象，不管你怎么来。每次新建是最简单可靠的 caller 模式。
3. **autocommit=True 不能丢** —— saver 的 `setup()` 里 DDL 不会在隐式事务里提交，必须一开始就 autocommit。
4. **排查顺序**：先裸连 pymysql 验证 → 再 db.get_conn → 再 Checkpointer → 最后 PyMySQLSaver 内部。一层层剥离依赖，最快定位是哪一层炸了。
