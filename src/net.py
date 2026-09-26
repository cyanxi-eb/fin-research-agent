"""取数用的 HTTP 薄封装 —— 统一 UA / Referer / 重试 / 限速，所有 fetch_* 都走这里。

为什么自己写而不是上 akshare：
- 我们需要的是「可控 + 可测」，akshare 拖进 pandas 全家桶且版本漂移快；
- 实测三个接口（巨潮公告、东财数据中心、新浪行情）自写足够，且踩坑点能自己收口。

三个反爬要点（都实测过，写死在这里避免各 fetch 模块重复踩）：
1. UA 必带，否则部分接口直接拒；
2. **新浪 hq.sinajs.cn 必须带 Referer**，不带返回 403；
3. 请求间隔别太小，默认 1.5s，可在 .env 覆盖。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from src import config

# 全局会话复用连接；限速状态也放模块级（单进程脚本场景够用）
_SESSION = requests.Session()
_LAST_CALL: float = 0.0


def _throttle() -> None:
    """简单节流：保证两次请求间隔 >= FETCH_INTERVAL。"""
    global _LAST_CALL
    gap = time.monotonic() - _LAST_CALL
    if gap < config.FETCH_INTERVAL:
        time.sleep(config.FETCH_INTERVAL - gap)
    _LAST_CALL = time.monotonic()


def _headers(referer: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": config.FETCH_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    return h


def _request(method: str, url: str, *, referer: str | None = None,
             timeout: float | None = None, **kw) -> requests.Response:
    """带重试的请求。重试只针对网络异常与 5xx，4xx 直接抛（改参数才有用）。

    `timeout` 留空则用 `config.FETCH_TIMEOUT`（取数接口的默认值）；
    抓网页正文这类"慢一点也无所谓"的场景可以传更长的值。
    """
    last_err: Exception | None = None
    for attempt in range(1, config.FETCH_RETRY + 1):
        _throttle()
        try:
            resp = _SESSION.request(
                method, url, headers=_headers(referer),
                timeout=timeout or config.FETCH_TIMEOUT, **kw)
            if resp.status_code >= 500:
                last_err = requests.HTTPError(f"HTTP {resp.status_code} from {url}")
            else:
                resp.raise_for_status()
                return resp
        except requests.RequestException as e:  # 网络异常 / 4xx / 5xx
            last_err = e
            # 4xx 是参数问题，重试无意义，直接抛
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code is not None and 400 <= code < 500:
                raise
        if attempt < config.FETCH_RETRY:
            time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"请求失败（重试 {config.FETCH_RETRY} 次）：{url} -> {last_err}")


def get_json(url: str, params: dict | None = None, referer: str | None = None) -> Any:
    resp = _request("GET", url, params=params, referer=referer)
    return json.loads(resp.text)


def get_text(url: str, params: dict | None = None, referer: str | None = None,
             timeout: float | None = None) -> str:
    """GET 原始文本（HTML/XML 等）—— 给"直接解析页面"的通道用，不落盘。"""
    resp = _request("GET", url, params=params, referer=referer, timeout=timeout)
    return resp.text


def post_form_json(url: str, data: dict, referer: str | None = None) -> Any:
    """表单 POST 取 JSON（巨潮的查询接口都是这种形态）。"""
    resp = _request("POST", url, data=data, referer=referer)
    return json.loads(resp.text)


def post_json(url: str, payload: dict, referer: str | None = None,
              timeout: float | None = None) -> Any:
    """JSON POST 取 JSON（Tavily 等现代 API 要的是 JSON 体，不是表单）。"""
    resp = _request("POST", url, json=payload, referer=referer, timeout=timeout)
    return json.loads(resp.text)


def download_meta(url: str, dest: Path, referer: str | None = None) -> tuple[int, str]:
    """下载文件到 dest，**并回传响应头的 Content-Type**。

    为什么需要 Content-Type：抓网页正文时要先判"这是 HTML 还是 PDF"，
    URL 后缀在重定向/网关后面并不可靠。取数接口不需要它，故 `download()`
    只返回字节数（既有调用方一行都不用改）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    resp = _request("GET", url, referer=referer, stream=True)
    total = 0
    with tmp.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    tmp.replace(dest)
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return total, ctype


def download(url: str, dest: Path, referer: str | None = None) -> int:
    """下载文件到 dest（先写 .part 再改名，避免中断留下半个文件被当成已完成）。

    返回写入的字节数。
    """
    return download_meta(url, dest, referer=referer)[0]


if __name__ == "__main__":
    # 自检：python -m src.net  （验证限速+UA 生效，打一个最轻的接口）
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    t0 = time.monotonic()
    for _ in range(2):
        r = get_json("https://push2.eastmoney.com/api/qt/clist/get",
                     params={"pn": 1, "pz": 1, "fs": "m:0+t:6", "fields": "f12,f14"})
        diff = ((r.get("data") or {}) or {}).get("diff")
        print("got:", diff if isinstance(diff, list) else f"(非列表 {type(diff).__name__})")
    print(f"两次请求耗时 {time.monotonic() - t0:.2f}s（应 >= FETCH_INTERVAL={config.FETCH_INTERVAL}）")
