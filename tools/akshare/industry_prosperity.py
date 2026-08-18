from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import akshare as ak
from bs4 import BeautifulSoup
import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.daily_cache import load_daily_json, shanghai_now
from tools.industry_aliases import aliases_for, normalize_industry


OUTPUT_BASE = ROOT / "knowledge" / "research" / "industry_prosperity"
CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "industry_prosperity_daily.json"
LEGULEGU_URL = "https://www.legulegu.com/stockdata/middle-avg-indicator"
METRICS = {
    "orYoy": "营业收入增长率",
    "trYoy": "营业总收入增长率",
    "opYoy": "营业利润增长率",
    "ebtYoy": "利润总额增长率",
    "roeYoy": "ROE增长率",
    "netProfitYoy": "净利润增长率",
}
INDUSTRY_WEB_SITE_GROUP = "(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn OR site:bse.cn OR site:eastmoney.com OR site:10jqka.com.cn OR site:stcn.com OR site:cs.com.cn OR site:cnstock.com OR site:yicai.com OR site:cls.cn OR site:jrj.com.cn)"
INDUSTRY_WEB_LAYERS = {
    "financial": {
        "label": "财务确认",
        "terms": ("营收增长", "利润增长", "盈利改善", "ROE提升", "业绩改善", "景气改善", "业绩拐点", "复苏", "营收下滑", "利润下滑", "亏损扩大", "ROE下降", "景气下行", "需求疲软"),
        "positive": ("营收增长", "利润增长", "盈利改善", "ROE提升", "业绩改善", "景气改善", "业绩拐点", "复苏"),
        "negative": ("营收下滑", "利润下滑", "亏损扩大", "ROE下降", "景气下行", "需求疲软"),
    },
    "supply": {
        "label": "供需先行",
        "terms": ("订单增长", "价格上涨", "库存下降", "供不应求", "产能利用率提升", "排产饱满", "涨价", "订单下降", "价格下跌", "库存上升", "供过于求", "产能过剩", "需求下滑"),
        "positive": ("订单增长", "价格上涨", "库存下降", "供不应求", "产能利用率提升", "排产饱满", "涨价"),
        "negative": ("订单下降", "价格下跌", "库存上升", "供过于求", "产能过剩", "需求下滑"),
    },
    "market": {
        "label": "市场验证",
        "terms": ("行业指数上涨", "跑赢", "资金流入", "成交放量", "强势", "反弹", "估值修复", "行业指数下跌", "跑输", "资金流出", "成交萎缩", "弱势", "破位", "估值压缩"),
        "positive": ("行业指数上涨", "跑赢", "资金流入", "成交放量", "强势", "反弹", "估值修复"),
        "negative": ("行业指数下跌", "跑输", "资金流出", "成交萎缩", "弱势", "破位", "估值压缩"),
    },
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _industry_web_queries(name: str, industry: str) -> dict[str, str]:
    subject = " ".join(item for item in (name, industry) if item).strip()
    return {
        key: f"{subject} {layer['label']} {' '.join(layer['terms'][:7])} {INDUSTRY_WEB_SITE_GROUP}"
        for key, layer in INDUSTRY_WEB_LAYERS.items()
    }


def _web_layer_status(rows: list[dict[str, Any]], layer: dict[str, Any]) -> dict[str, Any]:
    positive = sum(1 for row in rows if row.get("positive_hits"))
    negative = sum(1 for row in rows if row.get("negative_hits"))
    domains = {str(row.get("domain") or "") for row in rows if row.get("domain")}
    if len(rows) < 2 or len(domains) < 2:
        status = "需人工确认"
    elif positive >= 2 and positive > negative:
        status = "上行"
    elif negative >= 2 and negative > positive:
        status = "走弱"
    else:
        status = "中性"
    return {
        "status": status,
        "evidence_count": len(rows),
        "domain_count": len(domains),
        "positive_count": positive,
        "negative_count": negative,
        "evidence": rows[:6],
        "reason": f"网络旁证正向 {positive} 条、负向 {negative} 条、独立域名 {len(domains)} 个；仅作未核验交叉验证",
    }


def collect_web_signal(name: str, industry: str, timeout: float = 10) -> dict[str, Any]:
    """Collect unverified industry-side corroboration without changing the structured score."""
    from tools.scoring import web_research

    queries = _industry_web_queries(name, industry)
    layers: dict[str, Any] = {}
    errors: list[str] = []
    provider = os.getenv("MODA_SEARCH_PROVIDER", "auto").strip().lower()
    if provider not in {"auto", "searxng", "duckduckgo", "so360", "model"}:
        provider = "auto"
    for key, query in queries.items():
        layer = INDUSTRY_WEB_LAYERS[key]
        used, rows, query_errors = web_research._search(provider, query, timeout)
        errors.extend(f"{key}:{item}" for item in query_errors)
        relevant: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rank, row in enumerate(rows, 1):
            url = str(row.get("url") or "")
            if not url or url in seen:
                continue
            text = " ".join(str(row.get(field) or "") for field in ("title", "snippet"))
            positive_hits = [term for term in layer["positive"] if term in text]
            negative_hits = [term for term in layer["negative"] if term in text]
            if not positive_hits and not negative_hits:
                continue
            seen.add(url)
            fetch_status, content = web_research._fetch_page(url, min(timeout, 5)) if rank <= 3 else ("not_fetched", "")
            full_text = f"{text} {content}"
            positive_hits = [term for term in layer["positive"] if term in full_text]
            negative_hits = [term for term in layer["negative"] if term in full_text]
            domain = web_research._domain(url)
            role, tier = web_research._source_role(domain)
            relevant.append({
                "title": row.get("title", ""),
                "url": url,
                "domain": domain,
                "provider": used,
                "query": query,
                "fetch_status": fetch_status,
                "source_role": role,
                "source_tier": tier,
                "positive_hits": positive_hits,
                "negative_hits": negative_hits,
            })
        layers[key] = {"label": layer["label"], **_web_layer_status(relevant, layer)}
    statuses = [item.get("status") for item in layers.values() if item.get("status") not in {"不可用", "需人工确认"}]
    positive = sum(item in {"上行", "改善"} for item in statuses)
    negative = sum(item == "走弱" for item in statuses)
    if positive >= 2 and layers.get("financial", {}).get("status") == "上行":
        overall = "上行"
    elif positive >= 2:
        overall = "改善"
    elif negative >= 2:
        overall = "走弱"
    elif statuses:
        overall = "中性"
    else:
        overall = "不可用"
    return {
        "status": overall,
        "coverage": "完整" if len(layers) == 3 and all(item.get("status") not in {"不可用", "需人工确认"} for item in layers.values()) else "部分" if any(item.get("evidence_count") for item in layers.values()) else "不可用",
        "layers": layers,
        "queries": queries,
        "errors": errors,
        "provider": ",".join(sorted({row.get("provider") for item in layers.values() for row in item.get("evidence", []) if row.get("provider")})) or provider,
        "conflicts": [f"网络旁证{key}层出现正负信号并存" for key, item in layers.items() if item.get("positive_count", 0) and item.get("negative_count", 0)],
    }


def parse_legulegu_metric(html: str, metric_code: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise ValueError(f"{metric_code}: no table found")
    table = max(tables, key=lambda item: len(item.find_all("tr")))
    header_text = " ".join(cell.get_text(" ", strip=True) for cell in table.find_all("th"))
    periods = list(dict.fromkeys(re.findall(r"数值\((\d{4}-\d{2}-\d{2})\)", header_text)))
    if len(periods) < 2:
        raise ValueError(f"{metric_code}: report periods missing")

    rows: dict[str, dict[str, float]] = {}
    for row in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        if len(cells) < 5 or not cells[0].strip().isdigit():
            continue
        industry = cells[1].replace("[查看成分股]", "").strip()
        current, previous, delta = (_number(cells[index]) for index in (2, 3, 4))
        if not industry or current is None or previous is None or delta is None:
            continue
        rows[industry] = {"current": current, "previous": previous, "delta": delta}
    if not rows:
        raise ValueError(f"{metric_code}: industry rows missing")

    page_text = soup.get_text(" ", strip=True)
    pending = re.search(r"当前报告期[：:]\s*(\d{8}).{0,120}?已更新财报家数分别为[：:]\s*(\d+)", page_text)
    return {
        "code": metric_code,
        "label": METRICS[metric_code],
        "current_period": periods[0],
        "previous_period": periods[1],
        "pending_period": pending.group(1) if pending else None,
        "pending_announced_count": int(pending.group(2)) if pending else None,
        "rows": rows,
    }


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def fetch_legulegu_tables(timeout: float = 20) -> dict[str, Any]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    metrics: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for code in METRICS:
        try:
            response = session.get(LEGULEGU_URL, params={"indicatorCode": code}, timeout=timeout)
            response.raise_for_status()
            metrics[code] = parse_legulegu_metric(response.text, code)
        except Exception as exc:
            errors.append(f"{code}:{type(exc).__name__}")
    if not metrics:
        raise ValueError("all legulegu prosperity metrics failed")

    try:
        first_level = _frame_records(ak.sw_index_first_info())
    except Exception as exc:
        first_level = []
        errors.append(f"sw1:{type(exc).__name__}")
    try:
        second_level = _frame_records(ak.sw_index_second_info())
    except Exception as exc:
        second_level = []
        errors.append(f"sw2:{type(exc).__name__}")

    periods = [item["current_period"] for item in metrics.values() if item.get("current_period")]
    return {
        "source": "乐咕乐股/申万行业中位数",
        "source_date": max(periods) if periods else None,
        "metrics": metrics,
        "sw_first": first_level,
        "sw_second": second_level,
        "errors": errors,
    }


def _normalize_industry(value: str) -> str:
    return normalize_industry(value)


def map_industry(industry: str, raw: dict[str, Any]) -> dict[str, Any]:
    tokens = [item for item in re.split(r"[\s,/，、;；]+", industry) if len(item) >= 2]
    normalized_tokens = [alias for item in tokens for alias in aliases_for(item)]
    second = raw.get("sw_second") or []
    first = raw.get("sw_first") or []

    candidates: list[tuple[int, dict[str, Any], str]] = []
    for row in second:
        name = str(row.get("行业名称") or "")
        normalized = _normalize_industry(name)
        for source_token in tokens:
            for token in aliases_for(source_token):
                if token and normalized and (token == normalized or token in normalized or normalized in token):
                    candidates.append((min(len(token), len(normalized)), row, source_token))
    if candidates:
        _, row, source_token = max(candidates, key=lambda item: item[0])
        parent = str(row.get("上级行业") or "")
        parent_row = next((item for item in first if str(item.get("行业名称")) == parent), {})
        return {
            "input": industry,
            "matched_token": source_token,
            "sw_second_name": str(row.get("行业名称") or ""),
            "sw_second_code": str(row.get("行业代码") or ""),
            "sw_first_name": parent,
            "sw_first_code": str(parent_row.get("行业代码") or ""),
            "status": "已验证" if parent else "部分覆盖",
        }

    for row in first:
        name = str(row.get("行业名称") or "")
        normalized = _normalize_industry(name)
        if any(token and (token == normalized or token in normalized or normalized in token) for token in normalized_tokens):
            return {
                "input": industry,
                "matched_token": name,
                "sw_second_name": None,
                "sw_second_code": None,
                "sw_first_name": name,
                "sw_first_code": str(row.get("行业代码") or ""),
                "status": "部分覆盖",
            }
    return {"input": industry, "status": "不可用"}


def _return(frame: pd.DataFrame, periods: int) -> float | None:
    if frame is None or frame.empty or "收盘" not in frame.columns or len(frame) <= periods:
        return None
    values = pd.to_numeric(frame["收盘"], errors="coerce").dropna()
    if len(values) <= periods or values.iloc[-periods - 1] == 0:
        return None
    return float(values.iloc[-1] / values.iloc[-periods - 1] - 1)


def collect_market_signal(mapping: dict[str, Any]) -> dict[str, Any]:
    code = str(mapping.get("sw_first_code") or "").replace(".SI", "")
    name = str(mapping.get("sw_first_name") or "")
    if not code:
        return {"status": "不可用", "errors": ["industry_mapping_missing"]}
    errors: list[str] = []
    industry_frame = pd.DataFrame()
    market_frame = pd.DataFrame()
    fund_frame = pd.DataFrame()
    try:
        industry_frame = ak.index_hist_sw(symbol=code, period="day")
    except Exception as exc:
        errors.append(f"industry_index:{type(exc).__name__}")
    try:
        market_frame = ak.stock_zh_index_daily(symbol="sh000300")
    except Exception as exc:
        errors.append(f"market_index:{type(exc).__name__}")
    try:
        fund_frame = ak.stock_sector_fund_flow_rank(indicator="5日", sector_type="行业资金流")
    except Exception as exc:
        errors.append(f"fund_flow:{type(exc).__name__}")

    industry_20 = _return(industry_frame, 20)
    industry_60 = _return(industry_frame, 60)
    market_20 = None
    if not market_frame.empty and "close" in market_frame.columns and len(market_frame) > 20:
        values = pd.to_numeric(market_frame["close"], errors="coerce").dropna()
        if len(values) > 20 and values.iloc[-21] != 0:
            market_20 = float(values.iloc[-1] / values.iloc[-21] - 1)
    relative_20 = industry_20 - market_20 if industry_20 is not None and market_20 is not None else None

    activity_ratio = None
    if not industry_frame.empty and "成交额" in industry_frame.columns:
        amount = pd.to_numeric(industry_frame["成交额"], errors="coerce").dropna()
        if len(amount) >= 40 and amount.iloc[-40:-20].mean() > 0:
            activity_ratio = float(amount.iloc[-20:].mean() / amount.iloc[-40:-20].mean())

    fund_flow = None
    if not fund_frame.empty:
        name_column = next((column for column in ("名称", "行业", "板块名称") if column in fund_frame.columns), None)
        flow_column = next((column for column in fund_frame.columns if "净流入" in str(column) or "主力净流入" in str(column)), None)
        if name_column and flow_column:
            matched = fund_frame[fund_frame[name_column].astype(str).str.contains(name, regex=False, na=False)]
            if not matched.empty:
                fund_flow = _number(matched.iloc[0][flow_column])

    checks = [
        relative_20 > 0 if relative_20 is not None else None,
        industry_60 > 0 if industry_60 is not None else None,
        activity_ratio >= 1 if activity_ratio is not None else None,
        fund_flow > 0 if fund_flow is not None else None,
    ]
    known = [item for item in checks if item is not None]
    positives = sum(item is True for item in known)
    if len(known) < 2:
        status = "不可用"
    elif positives >= 2:
        status = "上行"
    elif positives == 0:
        status = "走弱"
    else:
        status = "中性"
    return {
        "status": status,
        "industry_return_20d": industry_20,
        "industry_return_60d": industry_60,
        "relative_to_csi300_20d": relative_20,
        "turnover_activity_ratio": activity_ratio,
        "fund_flow_5d": fund_flow,
        "available_checks": len(known),
        "errors": errors,
    }


def _read_payload(directory: str, code: str, marker: str) -> dict[str, Any]:
    path = ROOT / "knowledge" / "research" / directory / f"{code}.md"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(rf"<!--\s*{re.escape(marker)}:\s*(\{{.*?\}})\s*-->", text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def collect_supply_signal(code: str) -> dict[str, Any]:
    supply = _read_payload("supply_demand", code, "moda_supply_demand")
    macro = _read_payload("macro_policy", code, "moda_macro_policy")
    tightening = supply.get("supply_tightening")
    if tightening is True:
        status = "上行"
    elif tightening is False:
        status = "走弱"
    elif supply.get("supply_evidence_count"):
        status = "中性"
    else:
        status = "不可用"
    return {
        "status": status,
        "commodity": supply.get("supply_commodity"),
        "evidence_count": supply.get("supply_evidence_count"),
        "evidence": supply.get("supply_evidence") or [],
        "manufacturing_pmi": macro.get("manufacturing_pmi"),
        "pmi_date": macro.get("pmi_date"),
        "ppi_yoy": macro.get("ppi_yoy"),
        "ppi_yoy_change": macro.get("ppi_yoy_change"),
    }


def financial_signal(raw: dict[str, Any], industry: str) -> dict[str, Any]:
    values: dict[str, dict[str, Any]] = {}
    for code, item in (raw.get("metrics") or {}).items():
        row = (item.get("rows") or {}).get(industry)
        if row:
            values[code] = {**row, "label": item.get("label"), "period": item.get("current_period")}
    if not values:
        return {"status": "不可用", "values": {}, "available_metrics": 0}
    current_positive = sum(item["current"] > 0 for item in values.values())
    delta_positive = sum(item["delta"] > 0 for item in values.values())
    delta_negative = sum(item["delta"] < 0 for item in values.values())
    count = len(values)
    if current_positive / count >= 0.6 and delta_positive / count >= 0.6:
        status = "上行"
    elif delta_positive / count >= 0.6:
        status = "改善"
    elif delta_negative / count >= 0.6:
        status = "走弱"
    else:
        status = "中性"
    return {
        "status": status,
        "values": values,
        "available_metrics": count,
        "current_positive": current_positive,
        "delta_positive": delta_positive,
        "delta_negative": delta_negative,
    }


def _conflicts(financial: dict[str, Any], supply: dict[str, Any]) -> list[str]:
    values = financial.get("values") or {}
    revenue = [values[key] for key in ("orYoy", "trYoy") if key in values]
    profit = [values[key] for key in ("opYoy", "ebtYoy", "netProfitYoy") if key in values]
    conflicts: list[str] = []
    if revenue and profit and max(item["delta"] for item in profit) > 0 and all(item["delta"] < 0 for item in revenue):
        conflicts.append("利润改善但营收边际下降")
    if supply.get("status") == "走弱" and financial.get("status") in {"上行", "改善"}:
        conflicts.append("财务改善但价格/库存供需信号走弱")
    return conflicts


def _overall(financial: dict[str, Any], supply: dict[str, Any], market: dict[str, Any]) -> str:
    available = [item.get("status") for item in (financial, supply, market) if item.get("status") != "不可用"]
    if not available:
        return "不可用"
    positive = sum(item in {"上行", "改善"} for item in available)
    negative = sum(item == "走弱" for item in available)
    if financial.get("status") == "上行" and positive >= 2:
        return "上行"
    if positive >= 2 or financial.get("status") == "改善":
        return "改善"
    if negative >= 2 or financial.get("status") == "走弱":
        return "走弱"
    return "中性"


def collect(
    code: str,
    industry: str,
    *,
    name: str = "",
    refresh: bool = False,
    cache_path: Path = CACHE_PATH,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = shanghai_now(now)
    record = load_daily_json(cache_path, fetch_legulegu_tables, force_refresh=refresh, now=checked_at)
    raw = dict(record.get("payload") or {})
    mapping = map_industry(industry, raw)
    parent = str(mapping.get("sw_first_name") or "")
    financial = financial_signal(raw, parent) if parent else {"status": "不可用", "values": {}, "available_metrics": 0}
    supply = collect_supply_signal(code)
    market = collect_market_signal(mapping)
    conflicts = _conflicts(financial, supply)
    usable = bool(record.get("usable"))
    overall = _overall(financial, supply, market) if usable else "不可用"
    service_like = parent in {"银行", "非银金融", "社会服务", "计算机", "传媒"}
    financial_complete = financial.get("available_metrics", 0) >= 4
    market_complete = market.get("status") != "不可用"
    supply_complete = supply.get("status") != "不可用"
    secondary_complete = market_complete if service_like else market_complete and supply_complete
    coverage = "完整" if usable and mapping.get("status") == "已验证" and financial_complete and secondary_complete else "部分" if usable and parent else "不可用"
    periods = [item.get("period") for item in financial.get("values", {}).values() if item.get("period")]
    web_signal = collect_web_signal(name or code, industry)
    all_conflicts = [*conflicts, *web_signal.get("conflicts", [])]
    fetch_state = "stale" if not usable and raw else "failed" if not raw else "empty" if mapping.get("status") != "已验证" else "ok"
    prosperity_chain = [{
        "source": raw.get("source") or "乐咕乐股/申万行业中位数",
        "status": "ok" if usable else "stale" if raw else "failed",
        "error": record.get("error") or "",
    }]
    return {
        "industry_mapping": mapping,
        "industry_prosperity_status": overall,
        "industry_prosperity_coverage": coverage,
        "industry_prosperity_period": max(periods) if periods else raw.get("source_date"),
        "industry_financial_signal": financial,
        "industry_supply_signal": supply,
        "industry_market_signal": market,
        "industry_capex_signal": "不可用",
        "industry_prosperity_conflicts": all_conflicts,
        "industry_prosperity_sources": ["乐咕乐股/申万行业中位数(B级)", "AKShare/申万行业指数", "AKShare/商品供需与宏观", "网络检索中国金融网站（未核验旁证）"],
        "industry_web_signal": web_signal,
        "industry_cycle_cold": overall == "走弱" if coverage == "完整" else None,
        "industry_prosperity_checked_date": record.get("checked_date"),
        "industry_prosperity_cache_hit": record.get("cache_hit", False),
        "industry_prosperity_cache_status": record.get("status"),
        "industry_prosperity_error": record.get("error"),
        "fetch_state": fetch_state,
        "source_chain": prosperity_chain,
        "industry_required_factors": {
            "industry_growth": financial.get("status", "不可用"),
            "penetration_rate": "需人工确认",
            "supply_concentration_cr3": "需人工确认",
            "capacity_expansion_cycle": "需人工确认",
            "new_orders": "需人工确认",
            "capacity_utilization": "需人工确认",
            "product_price_inventory": supply.get("status", "不可用"),
        },
    }


def build_report(code: str, name: str, data: dict[str, Any]) -> str:
    mapping = data.get("industry_mapping") or {}
    financial = data.get("industry_financial_signal") or {}
    supply = data.get("industry_supply_signal") or {}
    market = data.get("industry_market_signal") or {}

    def pct(value: Any) -> str:
        number = _number(value)
        return f"{number:.2%}" if number is not None else "需人工确认"

    def ratio(value: Any) -> str:
        number = _number(value)
        return f"{number:.2f}x" if number is not None else "需人工确认"
    lines = [
        f"# 行业景气度交叉验证：{name or code}（{code}）",
        "",
        f"> 检查时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  乐咕为 B 级聚合数据，雪球文章仅作 C 级方法线索",
        "",
        f"<!-- moda_industry_prosperity: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 行业映射：{mapping.get('matched_token', '需人工确认')} → {mapping.get('sw_second_name', '需人工确认')} → {mapping.get('sw_first_name', '需人工确认')}（{mapping.get('status', '不可用')}）",
        f"- 景气判断：{data.get('industry_prosperity_status', '不可用')}；覆盖状态：{data.get('industry_prosperity_coverage', '不可用')}；报告期：{data.get('industry_prosperity_period', '需人工确认')}",
        f"- 每日缓存：{data.get('industry_prosperity_checked_date', '需人工确认')}（{'命中' if data.get('industry_prosperity_cache_hit') else '本次刷新'}；{data.get('industry_prosperity_cache_status', '不可用')}）",
        "",
        "## 三层验证",
        "",
        "| 层面 | 状态 | 核心证据 |",
        "|---|---|---|",
        f"| 财务确认 | {financial.get('status', '不可用')} | 可用 {financial.get('available_metrics', 0)}/6；正向当期 {financial.get('current_positive', 0)}；正向边际 {financial.get('delta_positive', 0)} |",
        f"| 供需先行 | {supply.get('status', '不可用')} | 商品 {supply.get('commodity') or '未匹配'}；证据 {supply.get('evidence_count') or 0} 类；PMI {supply.get('manufacturing_pmi', '需人工确认')} |",
        f"| 市场验证 | {market.get('status', '不可用')} | 20日相对沪深300 {pct(market.get('relative_to_csi300_20d'))}；成交活跃比 {ratio(market.get('turnover_activity_ratio'))}；5日资金流 {market.get('fund_flow_5d') if market.get('fund_flow_5d') is not None else '需人工确认'} |",
        f"| 网络旁证 | {(data.get('industry_web_signal') or {}).get('status', '不可用')} | 覆盖 {(data.get('industry_web_signal') or {}).get('coverage', '不可用')}；后端 {(data.get('industry_web_signal') or {}).get('provider', 'none')}；仅作未核验交叉验证 |",
        "",
        "## 行业财务中位数",
        "",
        "| 指标 | 当期 | 上期 | 边际变化 |",
        "|---|---:|---:|---:|",
    ]
    for item in financial.get("values", {}).values():
        lines.append(f"| {item.get('label')} | {item.get('current'):.2f}% | {item.get('previous'):.2f}% | {item.get('delta'):.2f}% |")
    if not financial.get("values"):
        lines.append("| 六项财务指标 | - | - | 需人工确认 |")
    lines += ["", "## 所需因子覆盖", "", "| 因子 | 状态 |", "|---|---|"]
    labels = {
        "industry_growth": "行业增长",
        "penetration_rate": "产业渗透率",
        "supply_concentration_cr3": "供给集中度 CR3",
        "capacity_expansion_cycle": "扩产周期",
        "new_orders": "新订单",
        "capacity_utilization": "产能利用率",
        "product_price_inventory": "产品价格与库存",
    }
    for key, status in (data.get("industry_required_factors") or {}).items():
        lines.append(f"| {labels.get(key, key)} | {status} |")
    lines += [
        "",
        "## 冲突与边界",
        "",
        "- " + ("；".join(data.get("industry_prosperity_conflicts") or []) or "未发现已覆盖指标之间的明确冲突。"),
        "- 行业景气仅交叉验证 F1、F4、F5 的确信度，不独立加分，不替代公司公告和财报。",
        "- 网络旁证按财务确认、供需先行、市场验证三层搜索；至少两个独立域名且同向才显示层面判断，不能替代结构化数据。",
        "- 网络三层明细：" + ("；".join(
            f"{item.get('label', key)}={item.get('status', '需人工确认')}（正向{item.get('positive_count', 0)}/负向{item.get('negative_count', 0)}，域名{item.get('domain_count', 0)}）"
            for key, item in (data.get('industry_web_signal') or {}).get('layers', {}).items()
        ) if (data.get('industry_web_signal') or {}).get('layers') else "需人工确认"),
        "- 服务业或导入期产业缺少库存、价格和产能数据时，不按负面处理。",
        "",
    ]
    if data.get("industry_prosperity_error"):
        lines.append(f"- 缓存刷新失败：{data['industry_prosperity_error']}。旧数据仅展示，不参与确认。")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect industry prosperity cross-check evidence")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--industry", default="")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    code = args.stock.strip()
    if len(code) != 6 or not code.isdigit():
        parser.error("--stock must be a 6-digit A-share code")
    data = collect(code, args.industry, name=args.name, refresh=args.refresh)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
