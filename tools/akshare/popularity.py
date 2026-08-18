from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import requests


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.data_call import run_with_timeout
from tools.providers.eastmoney_transport import post as eastmoney_post
OUTPUT_BASE = ROOT / "knowledge" / "research" / "popularity"
API = "https://emappdata.eastmoney.com/stockrank/getCurrentLatest"


def _market_code(code: str) -> str:
    return ("SH" if code.startswith(("5", "6", "9")) else "SZ") + code


def _collect_primary(code: str, timeout: float = 12) -> dict:
    payload = {
        "appId": "appId01",
        "globalId": "786e4c21-70dc-435a-93bb-38",
        "marketType": "",
        "srcSecurityCode": _market_code(code),
    }
    response = eastmoney_post(API, json=payload, timeout=timeout)
    response.raise_for_status()
    data = response.json().get("data") or {}
    rank = data.get("rank")
    total = data.get("marketAllCount")
    if not isinstance(rank, (int, float)) or not isinstance(total, (int, float)) or total <= 1:
        raise ValueError("EastMoney popularity response does not contain a valid rank")
    heat = 1 - (float(rank) - 1) / (float(total) - 1)
    return {
        "attention_rank": int(rank),
        "attention_universe": int(total),
        "attention_heat": round(max(0.0, min(1.0, heat)), 4),
        "attention_rank_change": data.get("rankChange"),
        "attention_calc_time": data.get("calcTime"),
        "attention_partial": False,
    }


def collect(code: str, timeout: float = 12) -> dict:
    result = run_with_timeout("个股人气", lambda: _collect_primary(code, timeout), seconds=int(timeout), source="东方财富人气榜")
    if result.ok:
        data = dict(result.value or {})
        data.update({"fetch_state": "ok", "source_chain": result.source_chain or [], "error": None})
        return data
    return {
        "fetch_state": "failed",
        "source_chain": result.source_chain or [],
        "error": result.error,
        "attention_unavailable": True,
        "attention_reason": "东方财富个股人气榜失败；没有语义等价备用接口，不以社交热度替代",
    }


def build_report(code: str, name: str, data: dict) -> str:
    if data.get("fetch_state") == "failed":
        return "\n".join([
            f"# 个股人气：{name or code}（{code}）",
            "",
            f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  数据源：东方财富个股人气榜",
            "",
            f"<!-- moda_popularity: {json.dumps(data, ensure_ascii=False)} -->",
            "",
            f"- 接口状态：失败，{data.get('attention_reason', '需人工确认')}。",
            f"- 原始错误：{data.get('error') or '未返回错误信息'}",
            "",
            "人气数据不可用时，不将其他热度指标当作个股人气事实。",
            "",
        ])
    return "\n".join([
        f"# 个股人气：{name or code}（{code}）",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  数据源：EastMoney 个股人气榜",
        "",
        f"<!-- moda_popularity: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 当前排名：{data['attention_rank']} / {data['attention_universe']}",
        f"- 关注热度：{data['attention_heat']:.4f}（0=冷，1=热）",
        f"- 排名变化：{data.get('attention_rank_change', '需人工确认')}",
        f"- 榜单时间：{data.get('attention_calc_time', '需人工确认')}",
        "",
        "本报告仅反映关注度，不代表看多或看空。",
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect EastMoney stock popularity")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    code = args.stock.strip()
    if len(code) != 6 or not code.isdigit():
        parser.error("--stock must be a 6-digit A-share code")
    data = collect(code)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
