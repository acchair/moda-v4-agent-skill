from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.daily_cache import load_daily_json, shanghai_now
from tools.industry_aliases import aliases_for, normalize_industry

OUTPUT_BASE = ROOT / "knowledge" / "research" / "congestion"
CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "sw_congestion_daily.json"
SW_SNAPSHOT_CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "sw_second_snapshot_daily.json"
SW_PROXY_CACHE_DIR = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "sw_congestion_proxy"
LEGULEGU_PAGE = "https://www.legulegu.com/stockdata/sw-congestion/sec-level"
LEGULEGU_API = "https://www.legulegu.com/api/stockdata/sw-congestion"
UA = "moda-v4-congestion/1.0"


def _token(now: datetime) -> str:
    return hashlib.md5(now.strftime("%Y-%m-%d").encode()).hexdigest()


def _normalize(value: Any) -> str:
    return normalize_industry(str(value or ""))


def _strength(value: float) -> str:
    if value >= 80:
        return "极热"
    if value >= 65:
        return "偏热"
    if value >= 45:
        return "中性"
    if value >= 30:
        return "偏冷"
    return "冰点"


def _fetch_latest(now: datetime, trade_days: int = 30) -> dict[str, Any]:
    session = requests.Session()
    headers = {
        "User-Agent": UA,
        "Referer": LEGULEGU_PAGE,
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    }
    session.get(LEGULEGU_PAGE, headers=headers, timeout=20).raise_for_status()
    response = session.get(
        LEGULEGU_API,
        params={"level": 2, "severalTradeDays": trade_days, "token": _token(now)},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    raw = response.json()
    dates = [str(item) for item in raw.get("dates") or []]
    names = {str(row.get("indexCode")): row for row in raw.get("swCodeNames") or []}
    congestions = raw.get("congestions") or {}
    if not dates or not names or not congestions:
        raise ValueError("乐咕申万二级拥挤度返回为空")
    latest_index = len(dates) - 1
    source_date = dates[latest_index]
    rows: list[dict[str, Any]] = []
    for code, values in congestions.items():
        if not isinstance(values, list) or latest_index >= len(values):
            continue
        item = values[latest_index] or {}
        turnover = _number(item.get("turnoverRateFQuantile"))
        amount = _number(item.get("amountCongestionQuantile"))
        if turnover is None and amount is None:
            continue
        combined = (turnover + amount) / 2 if turnover is not None and amount is not None else turnover or amount
        meta = names.get(str(code), {})
        rows.append({
            "sw_second_code": str(code),
            "sw_second_name": str(meta.get("indexName") or ""),
            "sw_first_code": str(meta.get("parentIndustryCode") or ""),
            "turnover_percentile": turnover,
            "amount_ratio_percentile": amount,
            "congestion": round(combined / 100, 4),
            "strength_score": round(combined, 2),
            "strength": _strength(combined),
        })
    if not rows:
        raise ValueError("乐咕申万二级拥挤度没有可用行业行")
    return {
        "source": "乐咕乐股/申万二级行业拥挤度",
        "source_url": LEGULEGU_PAGE,
        "source_date": source_date,
        "rows": rows,
        "trade_days": len(dates),
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _fetch_sw_second_snapshot() -> dict[str, Any]:
    import akshare as ak

    frame = ak.index_realtime_sw(symbol="二级行业")
    if frame is None or frame.empty:
        raise ValueError("申万二级行业快照为空")
    rows = []
    for _, row in frame.iterrows():
        code = str(row.get("指数代码") or "").replace(".SI", "")
        name = str(row.get("指数名称") or "").strip()
        if code and name:
            rows.append({"sw_second_code": code, "sw_second_name": name, "sw_first_code": ""})
    if not rows:
        raise ValueError("申万二级行业快照无有效代码")
    return {"source": "申万宏源研究/二级行业指数", "source_date": shanghai_now().date().isoformat(), "rows": rows}


def _rolling_percentile(values: pd.Series, window: int = 20, history: int = 120) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    rolling = numeric.rolling(window).mean().dropna().tail(history)
    if len(rolling) < 40:
        return None
    return float((rolling <= rolling.iloc[-1]).mean())


def _fetch_sw_activity_proxy(code: str, name: str) -> dict[str, Any]:
    import akshare as ak

    frame = ak.index_hist_sw(symbol=code.replace(".SI", ""), period="day")
    if frame is None or frame.empty or "日期" not in frame.columns:
        raise ValueError("申万行业指数历史为空")
    frame = frame.sort_values("日期")
    amount = _rolling_percentile(frame.get("成交额", pd.Series(dtype=float)))
    volume = _rolling_percentile(frame.get("成交量", pd.Series(dtype=float)))
    known = [value for value in (amount, volume) if value is not None]
    if not known:
        raise ValueError("申万行业指数成交活跃度样本不足")
    combined = sum(known) / len(known)
    source_date = str(pd.to_datetime(frame["日期"], errors="coerce").dropna().iloc[-1].date())
    return {
        "source": "申万宏源研究/二级行业指数成交活跃度代理",
        "source_url": "https://www.swsresearch.com/institute_sw/allIndex/releasedIndex",
        "source_date": source_date,
        "sw_second_code": code,
        "sw_second_name": name,
        "amount_percentile": amount,
        "volume_percentile": volume,
        "congestion": round(combined, 4),
        "strength_score": round(combined * 100, 2),
        "strength": _strength(combined * 100),
        "proxy": True,
    }


def _map_industry(industry: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = aliases_for(industry)
    value = values[0] if values else _normalize(industry)
    if not value:
        return {"status": "不可用", "input": industry}
    candidates = []
    for row in rows:
        name = _normalize(row.get("sw_second_name"))
        if name and any(alias == name or alias in name or name in alias for alias in values):
            matched = max((alias for alias in values if alias == name or alias in name or name in alias), key=len)
            candidates.append((min(len(matched), len(name)), row))
    if not candidates:
        return {"status": "不可用", "input": industry}
    row = max(candidates, key=lambda item: item[0])[1]
    return {
        "status": "已验证",
        "input": industry,
        "sw_second_name": row.get("sw_second_name"),
        "sw_second_code": row.get("sw_second_code"),
        "sw_first_code": row.get("sw_first_code"),
    }


def collect(
    industry: str = "",
    max_age_days: int = 10,
    *,
    refresh: bool = False,
    cache_path: Path = CACHE_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = shanghai_now(now)
    record = load_daily_json(
        cache_path,
        lambda: _fetch_latest(checked_at),
        force_refresh=refresh,
        now=checked_at,
    )
    payload = dict(record.get("payload") or {})
    source_date = payload.get("source_date")
    try:
        age_days = (checked_at.date() - datetime.fromisoformat(str(source_date)).date()).days
    except (TypeError, ValueError):
        age_days = None
    mapping = _map_industry(industry, payload.get("rows") or [])
    matched = next((row for row in payload.get("rows") or [] if row.get("sw_second_code") == mapping.get("sw_second_code")), None)
    fresh = bool(record.get("usable") and age_days is not None and 0 <= age_days <= max_age_days)
    proxy: dict[str, Any] = {}
    proxy_record: dict[str, Any] = {}
    if not (matched and fresh):
        try:
            snapshot_record = load_daily_json(
                SW_SNAPSHOT_CACHE_PATH,
                _fetch_sw_second_snapshot,
                force_refresh=refresh,
                now=checked_at,
            )
            snapshot = dict(snapshot_record.get("payload") or {})
            proxy_mapping = _map_industry(industry, snapshot.get("rows") or [])
            if proxy_mapping.get("sw_second_code"):
                if not matched:
                    mapping = proxy_mapping
                proxy_code = str(proxy_mapping["sw_second_code"]).replace(".SI", "")
                proxy_path = SW_PROXY_CACHE_DIR / f"{proxy_code}.json"
                proxy_record = load_daily_json(
                    proxy_path,
                    lambda: _fetch_sw_activity_proxy(proxy_code, str(proxy_mapping.get("sw_second_name") or "")),
                    force_refresh=refresh,
                    now=checked_at,
                )
                proxy = dict(proxy_record.get("payload") or {}) if proxy_record.get("usable") else {}
        except Exception as exc:
            proxy_record = {"fetch_state": "failed", "error": f"{type(exc).__name__}: {exc}"}

    use_proxy = not (matched and fresh) and bool(proxy)
    if use_proxy:
        matched = {
            "congestion": proxy.get("congestion"),
            "strength": proxy.get("strength"),
            "strength_score": proxy.get("strength_score"),
            "turnover_percentile": proxy.get("volume_percentile") * 100 if proxy.get("volume_percentile") is not None else None,
            "amount_ratio_percentile": proxy.get("amount_percentile") * 100 if proxy.get("amount_percentile") is not None else None,
            "sw_second_name": proxy.get("sw_second_name"),
            "sw_second_code": proxy.get("sw_second_code"),
            "sw_first_code": "",
        }
        source_date = proxy.get("source_date")
        try:
            age_days = (checked_at.date() - datetime.fromisoformat(str(source_date)).date()).days
        except (TypeError, ValueError):
            age_days = None
        fresh = bool(age_days is not None and 0 <= age_days <= max_age_days)

    primary_source = proxy.get("source") if use_proxy else payload.get("source") or "乐咕乐股/申万二级行业拥挤度"
    source_chain = list(payload.get("source_chain") or [{
        "source": payload.get("source") or "乐咕乐股/申万二级行业拥挤度",
        "status": "ok" if record.get("usable") else "failed",
        "error": record.get("error") or "",
    }])
    if proxy:
        source_chain.append({"source": proxy.get("source"), "status": "ok", "error": ""})
    result = {
        "source": primary_source,
        "source_url": proxy.get("source_url") if use_proxy else payload.get("source_url") or LEGULEGU_PAGE,
        "source_date": source_date,
        "market_congestion": matched.get("congestion") if matched else None,
        "market_congestion_date": source_date,
        "market_congestion_age_days": age_days,
        "market_congestion_fresh": fresh,
        "market_congestion_max_age_days": max_age_days,
        "market_congestion_strength": matched.get("strength") if matched else None,
        "market_congestion_strength_score": matched.get("strength_score") if matched else None,
        "market_congestion_turnover_percentile": matched.get("turnover_percentile") if matched else None,
        "market_congestion_amount_percentile": matched.get("amount_ratio_percentile") if matched else None,
        "market_congestion_industry": matched.get("sw_second_name") if matched else mapping.get("sw_second_name"),
        "market_congestion_industry_code": matched.get("sw_second_code") if matched else mapping.get("sw_second_code"),
        "market_congestion_parent_code": matched.get("sw_first_code") if matched else mapping.get("sw_first_code"),
        "market_congestion_mapping": mapping,
        "market_congestion_checked_date": record.get("checked_date"),
        "market_congestion_checked_at": record.get("checked_at"),
        "market_congestion_cache_hit": record.get("cache_hit", False),
        "market_congestion_cache_status": record.get("status"),
        "market_congestion_proxy": use_proxy,
        "market_congestion_crosscheck": proxy.get("congestion") if not use_proxy else None,
        "market_congestion_crosscheck_consistent": (
            abs(float(matched.get("congestion")) - float(proxy.get("congestion"))) <= 0.25
            if matched and proxy and not use_proxy and matched.get("congestion") is not None and proxy.get("congestion") is not None
            else None
        ),
        "fetch_state": "fallback_ok" if use_proxy and fresh else "stale" if not fresh else "ok" if matched else "empty",
        "source_chain": source_chain,
        "market_congestion_error": record.get("error") or proxy_record.get("error"),
        "market_congestion_rows": len(payload.get("rows") or []),
    }
    return result


def build_report(data: dict[str, Any], name: str = "") -> str:
    fresh = "有效" if data.get("market_congestion_fresh") else "过期或不可用，仅展示不计分"
    def value(key: str, suffix: str = "") -> str:
        number = data.get(key)
        return f"{number:.2f}{suffix}" if isinstance(number, (int, float)) else "需人工确认"
    industry = data.get("market_congestion_industry") or "需人工确认"
    code = data.get("market_congestion_industry_code") or "需人工确认"
    return "\n".join([
        f"# 申万二级行业拥挤度：{name}" if name else "# 申万二级行业拥挤度",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  数据源：{data.get('source') or '需人工确认'}",
        "",
        f"<!-- moda_congestion: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 所属申万二级：{industry}（{code}）",
        f"- 行业强度：{data.get('market_congestion_strength') or '需人工确认'}（综合分位 {value('market_congestion_strength_score')}）",
        f"- 等权换手率分位数：{value('market_congestion_turnover_percentile', '%')}",
        f"- 成交额拥挤度分位数：{value('market_congestion_amount_percentile', '%')}",
        f"- 综合拥挤度：{value('market_congestion')}",
        f"- 数据日期：{data.get('market_congestion_date') or '需人工确认'}；今日检查：{data.get('market_congestion_checked_date') or '需人工确认'}",
        f"- 新鲜度：{fresh}；共享行业行数：{data.get('market_congestion_rows', 0)}",
        f"- 缓存状态：{data.get('market_congestion_cache_status') or '需人工确认'}" + (f"；失败原因：{data['market_congestion_error']}" if data.get("market_congestion_error") else ""),
        f"- 口径：{'申万指数成交额/成交量滚动分位代理' if data.get('market_congestion_proxy') else '乐咕换手率/成交额拥挤度'}",
        "",
        "说明：同一交易日全量申万二级行业数据只采集一次，其他股票按申万二级映射共享；过期数据只展示，不参与情绪修正或 Hard Cap。",
        "",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Shenwan level-2 industry congestion")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--industry", default="")
    parser.add_argument("--max-age-days", type=int, default=10)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    data = collect(args.industry, args.max_age_days, refresh=args.refresh)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{args.stock.strip()}.md"
    path.write_text(build_report(data, args.name or args.stock.strip()), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
