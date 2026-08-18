from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any

import pandas as pd
import requests


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
LEGACY_BSE_PREFIXES = ("43", "83", "87")


def _clean_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    raw = re.sub(r"^(?:SH|SZ|BJ)", "", raw)
    raw = re.sub(r"(?:\.SH|\.SZ|\.BJ)$", "", raw)
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError("腾讯行情仅接受六位 A 股代码")
    if raw.startswith(LEGACY_BSE_PREFIXES):
        raise ValueError("北交所历史代码可能返回陈旧行情，请先使用当前 920xxx 代码")
    return raw


def _symbol(code: str) -> str:
    code = _clean_code(code)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def _number(value: Any) -> float | None:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(parsed) else float(parsed)


def fetch_realtime_quote(code: str, timeout: float = 5) -> dict[str, Any]:
    clean_code = _clean_code(code)
    symbol = _symbol(clean_code)
    response = requests.get(
        f"https://qt.gtimg.cn/q={symbol}",
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "gbk"
    raw = response.text.partition('="')[2].rpartition('"')[0]
    fields = raw.split("~") if raw else []
    if len(fields) < 6:
        return {}
    returned_code = str(fields[2] or "").strip()
    if returned_code and returned_code != clean_code:
        raise ValueError(f"腾讯行情代码不一致：请求 {clean_code}，返回 {returned_code}")
    latest = _number(fields[3])
    previous = _number(fields[4])
    volume = _number(fields[6]) if len(fields) > 6 else None
    change = latest - previous if latest is not None and previous not in {None, 0} else None
    pct = change / previous * 100 if change is not None and previous else None
    return {
        "source": "Tencent/qt.gtimg.cn",
        "股票代码": clean_code,
        "股票简称": fields[1],
        "最新价": latest,
        "涨跌幅": pct,
        "涨跌额": change,
        "今开": _number(fields[5]),
        "成交量": volume,
        "最高": _number(fields[33]) if len(fields) > 33 else None,
        "最低": _number(fields[34]) if len(fields) > 34 else None,
        "quote_time": str(fields[30] or "") if len(fields) > 30 else "",
        "quote_stale_suspect": bool(latest is not None and latest == previous and volume in {None, 0}),
    }


def fetch_kline_daily(code: str, count: int = 800, timeout: float = 8) -> pd.DataFrame:
    symbol = _symbol(_clean_code(code))
    params = {"param": f"{symbol},day,,,{max(1, int(count))},qfq"}
    response = requests.get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params=params,
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    node = ((payload.get("data") or {}).get(symbol) or {})
    rows = node.get("qfqday") or node.get("day") or []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        normalized.append({
            "date": row[0],
            "open": row[1],
            "close": row[2],
            "high": row[3],
            "low": row[4],
            "volume": row[5],
            "amount": row[6] if len(row) > 6 else None,
        })
    frame = pd.DataFrame(normalized)
    if frame.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "close", "high", "low", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame["pct_chg"] = frame["close"].pct_change() * 100
    return frame[["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]


@lru_cache(maxsize=4)
def _health_check_cached(bucket: int) -> dict[str, Any]:
    started = time.perf_counter()
    checks: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        checks["quote"] = bool(fetch_realtime_quote("000001", timeout=4))
    except Exception as exc:
        checks["quote"] = False
        errors["quote"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    try:
        checks["kline"] = not fetch_kline_daily("000001", count=5, timeout=5).empty
    except Exception as exc:
        checks["kline"] = False
        errors["kline"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    ok = any(checks.values())
    return {
        "ok": ok,
        "status": "ok" if ok else "unavailable",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "checks": checks,
        "errors": errors,
    }


def health_check(cache_seconds: int = 60) -> dict[str, Any]:
    return _health_check_cached(int(time.time() // max(1, cache_seconds)))


if __name__ == "__main__":
    print(json.dumps(health_check(cache_seconds=1), ensure_ascii=False, indent=2))
