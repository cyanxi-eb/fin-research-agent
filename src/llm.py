"""LLM 多供应商管理 —— ♻️ 搬自 workflow-agent/src/llm.py（同一套骨架）。

职责：
- 供应商注册表读取（`config.LLM_PROVIDERS`：DeepSeek / 通义千问 / 本地 Ollama）
- 当前激活「供应商 + 模型」状态管理，持久化到 `data/llm_state.json`
  （切换即写盘，无需改代码、无需重启）
- 余额查询（目前仅 DeepSeek 提供 `/user/balance`）

状态优先级：`llm_state.json` > 环境变量 `LLM_ACTIVE` > 供应商表第一个。

本项目的额外要求（金融场景）：**宁可拒答，不可瞎答**。所以：
- 供应商的 `temperature` 默认 0.0（不是通用的 0.7）；
- 没有 API Key 时不做"假装能答"的兜底 —— 由 `src/answer.py` 走**检索摘录降级路径**，
  在答案里明确标注"未调用大模型"，而不是编一段像模像样的话。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from src import config

STATE_PATH: Path = config.DATA_DIR / "llm_state.json"
BALANCE_TIMEOUT = 8  # 余额查询超时（秒）


# ---------------- 状态管理 ----------------

def _first_provider() -> str:
    return next(iter(config.LLM_PROVIDERS))


def get_state() -> dict:
    """当前激活状态：`{"provider": ..., "model": ...}`。"""
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            provider = state.get("provider")
            if provider in config.LLM_PROVIDERS:
                models = config.LLM_PROVIDERS[provider]["models"]
                model = state.get("model") or models[0]
                # 模型可能在某次配置调整后被删掉，此时回落到该供应商的第一个模型，
                # 而不是把非法模型名原样返回（否则调用方拿到一个必然 404 的名字）
                if model not in models:
                    model = models[0]
                return {"provider": provider, "model": model}
        except (json.JSONDecodeError, OSError):
            pass  # 状态文件损坏则回落默认
    provider = config.LLM_ACTIVE if config.LLM_ACTIVE in config.LLM_PROVIDERS \
        else _first_provider()
    return {"provider": provider, "model": config.LLM_PROVIDERS[provider]["models"][0]}


def set_state(provider: str, model: str) -> dict:
    """切换供应商/模型并持久化。非法值抛 ValueError。"""
    if provider not in config.LLM_PROVIDERS:
        raise ValueError(f"未知供应商: {provider}，可选: {list(config.LLM_PROVIDERS)}")
    models = config.LLM_PROVIDERS[provider]["models"]
    if model not in models:
        raise ValueError(f"供应商 {provider} 不支持模型 {model}，可选: {models}")
    state = {"provider": provider, "model": model}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def get_active_llm() -> dict:
    """当前激活通道的完整配置（含 base_url / api_key，供 LangChain 初始化用）。"""
    state = get_state()
    profile = config.LLM_PROVIDERS[state["provider"]]
    return {
        "provider": state["provider"],
        "model": state["model"],
        "base_url": profile["base_url"],
        "api_key": profile["api_key"],
        "temperature": profile.get("temperature", 0.0),
    }


def is_ready() -> tuple[bool, str]:
    """能不能真的调大模型（有没有 Key）。给 `src/answer.py` 决定是否走降级路径。

    返回 `(就绪, 原因)`。原因字符串是给用户看的，所以要说清"缺什么、怎么补"。
    """
    cfg = get_active_llm()
    if not str(cfg.get("api_key") or "").strip():
        return False, (f"当前通道 {cfg['provider']} 未配置 API Key"
                       f"（可设环境变量或在 data/llm_keys.local.json 填写）")
    return True, "ok"


def list_providers() -> list[dict]:
    """供应商列表（界面下拉框数据源），**脱敏**返回 —— 不返回 api_key 本身。"""
    active = get_state()
    result = []
    for key, p in config.LLM_PROVIDERS.items():
        result.append({
            "provider": key,
            "label": p.get("label", key),
            "models": p["models"],
            "supports_balance": p.get("supports_balance", False),
            # 用真值判断而不是"有兜底占位串"：源码里不留兜底 Key 后，
            # 旧写法会让界面把"未配置"误报成"已配置"
            "key_configured": bool(str(p["api_key"]).strip()),
            "active": key == active["provider"],
            "active_model": active["model"] if key == active["provider"] else None,
        })
    return result


# ---------------- 余额查询 ----------------

def get_balance(provider: str | None = None) -> dict:
    """查询账户余额。

    DeepSeek: `GET {base_url 去掉 /v1}/user/balance`，返回 `balance_infos`。
    不支持的供应商返回 `{"supported": False, "reason": ...}`，不抛异常。
    """
    state = get_state()
    provider = provider or state["provider"]
    if provider not in config.LLM_PROVIDERS:
        raise ValueError(f"未知供应商: {provider}")
    profile = config.LLM_PROVIDERS[provider]

    if not profile.get("supports_balance"):
        return {
            "supported": False,
            "provider": provider,
            "reason": f"{profile.get('label', provider)} 未提供余额查询接口，请到对应控制台查看",
        }

    if provider == "deepseek":
        base = profile["base_url"].rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        req = urllib.request.Request(
            f"{base}/user/balance",
            headers={"Authorization": f"Bearer {profile['api_key']}",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=BALANCE_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return {
                "supported": True,
                "provider": provider,
                "is_available": data.get("is_available"),
                "balances": [
                    {
                        "currency": b.get("currency"),
                        "total": b.get("total_balance"),
                        "granted": b.get("granted_balance"),
                        "topped_up": b.get("topped_up_balance"),
                    }
                    for b in data.get("balance_infos", [])
                ],
            }
        except urllib.error.HTTPError as e:
            return {"supported": True, "provider": provider,
                    "error": f"HTTP {e.code}（检查 API Key 是否有效）"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            return {"supported": True, "provider": provider, "error": f"查询失败: {e}"}

    return {"supported": False, "provider": provider, "reason": "该供应商暂未实现余额查询"}


if __name__ == "__main__":
    # 自检：python -m src.llm
    print("state    :", get_state())
    print("ready    :", is_ready())
    for p in list_providers():
        print(f"  {p['provider']:<10} {p['label']:<22} models={p['models']} "
              f"balance={p['supports_balance']} key_ok={p['key_configured']}")
