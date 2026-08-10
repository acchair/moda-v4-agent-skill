from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
SOCIAL_ENDPOINTS = {
    "微博热搜": "https://weibo.com/ajax/side/hotSearch",
    "知乎热榜": "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=5&desktop=true",
    "百度热搜": "https://top.baidu.com/api/board?platform=wise&tab=realtime",
    "抖音热点": "https://www.douyin.com/aweme/v1/web/hot/search/list/",
    "头条热榜": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
    "B站热搜": "https://s.search.bilibili.com/main/hotword?limit=5",
}
NEWS_ENDPOINTS = {
    "金十快讯": "https://www.jin10.com/flash_newest.js",
    "东方财富快讯": "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
    "同花顺快讯": "http://news.10jqka.com.cn/today_list/",
}


def _package(name: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        module = importlib.import_module(name)
        return {"ok": True, "status": "installed", "version": getattr(module, "__version__", ""), "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "status": "missing", "error": f"{type(exc).__name__}: {str(exc)[:120]}", "elapsed_ms": round((time.perf_counter() - started) * 1000)}


def _endpoint(name: str, url: str, timeout: float = 6) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    try:
        response = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=timeout)
        response.raise_for_status()
        size = len(response.content or b"")
        return name, {
            "ok": size > 0,
            "status": "ok" if size > 0 else "empty",
            "http_status": response.status_code,
            "bytes": size,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        return name, {"ok": False, "status": "unavailable", "error": f"{type(exc).__name__}: {str(exc)[:120]}", "elapsed_ms": round((time.perf_counter() - started) * 1000)}


def _endpoint_group(endpoints: dict[str, str]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = {executor.submit(_endpoint, name, url): name for name, url in endpoints.items()}
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result
    return results


def core_health() -> dict[str, Any]:
    from tools.providers.easy_tdx_provider import health_check as easy_tdx_health
    from tools.providers.tencent_provider import health_check as tencent_health

    return {
        "easy_tdx": easy_tdx_health(cache_seconds=60),
        "tencent": tencent_health(cache_seconds=60),
        "akshare": _package("akshare"),
        "efinance": _package("efinance"),
    }


def collect(group: str = "all") -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S %z")}
    if group in {"all", "core"}:
        result["core"] = core_health()
    if group in {"all", "social"}:
        result["social"] = _endpoint_group(SOCIAL_ENDPOINTS)
    if group in {"all", "news"}:
        result["news"] = _endpoint_group(NEWS_ENDPOINTS)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return result


def _print_table(payload: dict[str, Any]) -> None:
    print(f"检查时间: {payload['checked_at']}")
    for group in ("core", "social", "news"):
        if group not in payload:
            continue
        print(f"\n[{group}]")
        for name, status in payload[group].items():
            marker = "OK" if status.get("ok") else "--"
            detail = status.get("status", "unknown")
            error = status.get("error")
            print(f"{marker:>2}  {name:<16} {detail}" + (f"  {error}" if error else ""))
    print(f"\n总耗时: {payload['elapsed_ms']} ms")


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 moda-v4 核心、社交热榜和财经快讯数据源健康度")
    parser.add_argument("--group", choices=("all", "core", "social", "news"), default="all")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = collect(args.group)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(payload)


if __name__ == "__main__":
    main()
