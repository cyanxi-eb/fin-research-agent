"""Checkpointer 工厂 —— 让图能在**挂起后跨进程恢复**。

HITL 的验收标准是"进程 A 跑出挂起 → 进程 B 用同一 `thread_id` 恢复并确认"。
这条要求把 Checkpointer 从"可选优化"变成**必要条件**：状态只存在内存里的话，
进程一退，人还没确认的那条待办就蒸发了 —— 而"人工确认"天然是慢的（可能隔几小时），
中间进程重启、部署、换机器都是常态。

Step 6 起支持 MySQL（企业部署）：SQLite 用于单机演示与**全部确定性单测**，
MySQL 用于容器化交付。两者 saver 实现不同、建表时机也不同。

⚠️ 三个容易踩的点：
1. **连接必须 `check_same_thread=False`**：FastAPI 的同步端点跑在线程池里，
   而 `sqlite3` 默认禁止跨线程使用同一连接 → 单测（单线程）全绿、HTTP 面偶发
   `ProgrammingError`。这种"只在并发下出现"的 bug 最难查，所以在源头就放开。
2. **saver 必须缓存**：每次 `invoke` 都新建 saver 会让连接数随请求线性涨，
   最终撞 SQLite 的文件锁（MySQL 则是撞 max_connections）。同一个进程共用一个 saver。
3. **不要把 checkpointer 放进 `build_*_graph()` 的默认参数**：编译产物不可跨进程 pickle，
   缓存编译结果会在多 worker / 热重载时出怪问题。每次编译、共用 saver。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src import config

# 进程内单例：缓存键（sqlite 用文件路径，mysql 用 `mysql:<库名>`）-> (saver, conn)
_SAVERS: dict[str, tuple[object, object]] = {}


def _backend() -> str:
    """生效的 Checkpointer 后端（空值跟随业务库后端，见 config.CHECKPOINT_BACKEND）。"""
    return (config.CHECKPOINT_BACKEND or "sqlite").strip().lower()


def _make_sqlite(path: Path | None):
    p = str(path or config.CHECKPOINT_DB_PATH)
    cached = _SAVERS.get(p)
    if cached is not None:
        return cached[0]

    from langgraph.checkpoint.sqlite import SqliteSaver

    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    saver = SqliteSaver(conn)
    # 建表：SqliteSaver 只在 setup() 里建，直接给连接时必须显式调一次
    saver.setup()
    _SAVERS[p] = (saver, conn)
    return saver


def _make_mysql():
    """MySQL Checkpointer（每次新建连接，不缓存）。

    ⚠️ 实测结论（踩坑后最终方案）：**放弃进程内长连接缓存**。
    MySQL server has gone away (Bad file descriptor) 的根因是：pymysql
    的 ping(reconnect=True) 只重建底层 socket，但 LangGraph 的
    PyMySQLSaver 内部可能缓存了旧 cursor / session 状态，导致后续
    checkpoint 读写仍打到已关闭的 fd。每次新建 conn + saver 最稳。

    性能可接受：pymysql.connect + setup() ≈ 30-50ms，一次请求内
    checkpoint 调用 ≈ 2-3 次，额外开销 < 200ms。

    仍保留 autocommit=True（同原注释，setup() DDL 必须在 autocommit
    下才能正确提交）。
    """
    import pymysql
    from pymysql.cursors import DictCursor

    # 库不存在时兜底建库（同原逻辑）
    from src import db as _db
    _db.ensure_mysql_database()

    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    # 每次全新连接（connect_timeout=5s 防止 MySQL 不可达时卡死）
    conn = pymysql.connect(charset="utf8mb4", cursorclass=DictCursor,
                           autocommit=True, connect_timeout=5, **config.MYSQL)
    saver = PyMySQLSaver(conn)
    saver.setup()
    return saver, conn


def make_checkpointer(path: Path | None = None):
    """返回当前后端（sqlite / mysql）的 Checkpointer。

    MySQL 每次新建连接（见 `_make_mysql` 注释）；SQLite 仍进程内复用。
    未知后端**直接报错**，不静默退回 SQLite。
    """
    backend = _backend()
    if backend == "mysql":
        return _make_mysql()[0]
    if backend not in ("sqlite", ""):
        raise NotImplementedError(
            f"未知 Checkpointer 后端 {backend!r}；可选 sqlite / mysql。"
            f"设 FA_CHECKPOINT_BACKEND=sqlite|mysql（或清空跟随 FA_DB_BACKEND）。")
    return _make_sqlite(path)


def reset() -> None:
    """关掉并清空缓存（仅 SQLite 分支有缓存，MySQL 每次新建）。"""
    _SAVERS.clear()


def status() -> dict:
    """健康检查用：后端 + 落盘位置 + 已存会话数。

    sqlite 分支**不建表、不写盘**（库文件不存在就直接返回）；mysql 分支要能数出会话数，
    必须先 `setup()` 保证表存在（幂等），因此对 MySQL 是"只读查询 + 幂等建表"。
    """
    if _backend() == "mysql":
        out = {"backend": "mysql",
               "path": f"{config.MYSQL['host']}:{config.MYSQL['port']}/{config.MYSQL['database']}",
               "exists": None, "threads": None}
        conn = None
        try:
            saver, conn = _make_mysql()
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(DISTINCT thread_id) AS n FROM checkpoints")
                out["threads"] = int((cur.fetchone() or {}).get("n") or 0)
            out["exists"] = True
        except Exception as e:  # noqa: BLE001 —— 健康检查不能把服务搞挂
            out["exists"] = False
            out["error"] = f"{type(e).__name__}: {e}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return out

    p = config.CHECKPOINT_DB_PATH
    out = {"backend": config.CHECKPOINT_BACKEND or "sqlite", "path": str(p),
           "exists": p.exists(), "threads": None}
    if not p.exists():
        return out
    try:
        with sqlite3.connect(p) as conn:
            row = conn.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()
        out["threads"] = int(row[0]) if row else 0
    except sqlite3.Error as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
