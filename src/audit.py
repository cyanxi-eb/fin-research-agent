"""审计落库 —— 每次问答与人工确认都留痕。

## 为什么审计要单独一层，且**永不抛异常**

审计的价值在事后：出问题时能回答"当时问的是什么、走的哪条链路、答案里的数字来自哪、
谁确认放行的"。所以它的正确性要求是**不漏记**，而不是"失败要显式报错"——
如果审计写失败会让整个问答 500，运维的第一反应是关掉审计，于是**该留痕的地方全空了**
（比不装更糟：留着一张空表会让人以为"没发生过"）。

因此本模块的所有写入都吞异常，但把最后一次错误记在模块变量里，由 `/api/health` 暴露出来。
"静默但可查"是这里唯一合理的选择。
"""
from __future__ import annotations

import json

from src import db

# 最后一次写入失败的原因（健康检查会展示）。None = 正常。
last_error: str | None = None

_ACTIONS = ("ask", "hitl_confirm", "tool_call", "retrieve", "hitl_persist_failed",
            # 鉴权（本轮）：**失败也记**（auth_login + ok=false）—— 撞库排查只有这一条线索。
            # auth_logout 单独一个动作，是因为它**不代表令牌失效**（JWT 无状态），
            # 混进 auth_login 会让人误以为"登出过 = 那张票作废了"。
            "auth_register", "auth_login", "auth_refresh", "auth_logout",
            # 联网搜索（本轮）：web_search = 发起/命中缓存的一次检索，
            # web_ingest = 交叉验证通过后写入独立网络语料（两者分开，才能回答
            # "这条网络结论是谁在何时入库的"，而不仅是"搜过"）。
            "web_search", "web_ingest",
            # 数据入库向导：data_ingest = 管理员确认后的一次结构化入库。
            # batch id 只活在 detail_json 里（无独立批次表，D5）。
            "data_ingest")


def log(action: str, target: str | None = None, detail: dict | None = None,
        actor: str | None = None, latency_ms: int | None = None) -> int | None:
    """写一条审计。返回 log_id，失败返回 None（**不抛异常**）。"""
    global last_error
    if action not in _ACTIONS:
        # 不认识的 action 直接记一条提示性的，而不是拒绝写 —— 审计不该成为
        # "新调用点忘了登记"的失败点，最多是分类不准。
        detail = {"_unknown_action": action, **(detail or {})}
    try:
        payload = json.dumps(detail or {}, ensure_ascii=False, default=str)
        # 详情可能很大（证据片段），截断而不是拒写：截断的审计仍然可用，
        # 缺失的审计不可用。具体上限按"能一次插入"来定，避免 SQLite 参数过大。
        if len(payload) > 20000:
            payload = payload[:20000] + '…（已截断）"}'
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO audit_logs (actor, action, target, detail_json, latency_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor or "anonymous", action, (target or "")[:500], payload, latency_ms))
            log_id = cur.lastrowid
        last_error = None
        return log_id
    except Exception as e:                      # noqa: BLE001 —— 见模块说明
        last_error = f"{type(e).__name__}: {e}"
        return None


def _rows(cur) -> list[dict]:
    return [dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}
            for r in cur.fetchall()]


def recent(limit: int = 50) -> list[dict]:
    """最近的审计记录（倒序）。detail_json 解析成对象，方便直接看。"""
    try:
        with db.get_conn() as conn:
            rows = _rows(conn.execute(
                "SELECT log_id, created_at, actor, action, target, detail_json, latency_ms "
                "FROM audit_logs ORDER BY log_id DESC LIMIT ?", (int(limit),)))
    except Exception as e:                      # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {e}"}]
    for r in rows:
        try:
            r["detail"] = json.loads(r.pop("detail_json") or "{}")
        except json.JSONDecodeError:
            r["detail"] = {"_raw": r.pop("detail_json")}
    return rows


def stats() -> dict:
    out: dict = {"last_error": last_error}
    try:
        with db.get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM audit_logs").fetchone()
            out["rows"] = int(row["n"]) if row is not None else 0
            by = _rows(conn.execute(
                "SELECT action, COUNT(*) AS n FROM audit_logs GROUP BY action"))
            out["by_action"] = {b["action"]: n for b in by for n in [b["n"]]}
    except Exception as e:                      # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def record_ask(response: dict, *, thread_id: str | None = None,
               actor: str | None = None, latency_ms: int | None = None) -> int | None:
    """按统一口径记录一次问答（各入口都走这里，避免各处各写一份摘要字段）。"""
    hitl = response.get("hitl") or {}
    return log("ask", target=(response.get("question") or "")[:500], actor=actor,
               latency_ms=latency_ms,
               detail={
                   "thread_id": thread_id or response.get("thread_id"),
                   "intent": response.get("intent"),
                   "route": response.get("route"),
                   "refused": response.get("refused"),
                   "refusal_reason": response.get("refusal_reason"),
                   "degraded": response.get("degraded"),
                   "confidence": response.get("confidence"),
                   "hitl": {"pending": hitl.get("pending"), "reason": hitl.get("reason"),
                            "reviewed": hitl.get("reviewed"),
                            "decision": hitl.get("decision")},
                   "verify": {
                       "checked": (response.get("verify") or {}).get("checked"),
                       "supported": (response.get("verify") or {}).get("supported"),
                       "unsupported": (response.get("verify") or {}).get("unsupported"),
                   },
                   "tool_calls": response.get("tool_calls"),
                   "citations": [
                       {"index": c.get("index"), "citation": c.get("citation")}
                       for c in (response.get("citations") or [])],
                   "answer_chars": len(response.get("answer") or ""),
               })


if __name__ == "__main__":
    # 自检：python -m src.audit
    import json as _json

    print(_json.dumps(stats(), ensure_ascii=False, indent=2))
    print(_json.dumps(recent(5), ensure_ascii=False, indent=2))
