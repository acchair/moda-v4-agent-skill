from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

import pandas as pd

from tools.providers.eastmoney_transport import get as eastmoney_get, wait_turn

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API = "https://reportapi.eastmoney.com/report/list"
PUSH2_URL = "https://push2.eastmoney.com/api/qt"
PUSH2HIS_URL = "https://push2his.eastmoney.com/api/qt"
SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Connection": "close",
}

def _throttle(interval: float = 0.35) -> None:
    """Compatibility shim for callers that used the old local throttle."""
    del interval
    wait_turn()


def _market_id(code: str) -> int:
    clean = clean_code(code)
    if clean.startswith(("6", "9")):
        return 1
    return 0


def clean_code(code: str) -> str:
    raw = str(code or "").strip().upper()
    raw = re.sub(r"^(?:SH|SZ|BJ)", "", raw)
    raw = re.sub(r"(?:\.SH|\.SZ|\.BJ)$", "", raw)
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError("东方财富接口仅接受六位 A 股代码")
    if raw.startswith(("43", "83", "87")):
        raise ValueError("北交所历史代码可能返回陈旧行情，请先使用当前 920xxx 代码")
    return raw


def secid(code: str) -> str:
    clean = clean_code(code)
    return f"{_market_id(clean)}.{clean}"


def _parse_jsonp(text: str) -> Any:
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    match = re.search(r"^[^(]*\((.*)\)\s*;?$", text, re.S)
    if not match:
        raise ValueError("invalid json/jsonp response")
    return json.loads(match.group(1))


def _get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    resp = eastmoney_get(url, params=params or {}, timeout=timeout, headers=HEADERS)
    resp.raise_for_status()
    return _parse_jsonp(resp.text)


def _records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("diff", "klines", "list", "items"):
            value = data.get(key)
            if isinstance(value, list):
                if key == "klines":
                    return [{"kline": item} for item in value]
                return [item for item in value if isinstance(item, dict)]
        if isinstance(data.get("data"), list):
            return [item for item in data["data"] if isinstance(item, dict)]
    result = payload.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("data"), list):
            return [item for item in result["data"] if isinstance(item, dict)]
        for value in result.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return []


def _df(payload: Any) -> pd.DataFrame:
    return pd.DataFrame(_records(payload))


def eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filters: str = "",
    sort_columns: str = "",
    sort_types: str = "-1",
    page_size: int = 50,
    page_number: int = 1,
) -> pd.DataFrame:
    params = {
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "pageSize": page_size,
        "pageNumber": page_number,
        "reportName": report_name,
        "columns": columns,
        "filter": filters,
        "source": "WEB",
        "client": "WEB",
    }
    payload = _get_json(DATACENTER_URL, params=params)
    return _df(payload)


def research_reports(code: str, max_pages: int = 2, look_back_days: int = 730) -> pd.DataFrame:
    clean = clean_code(code)
    begin = date.today() - timedelta(days=max(30, int(look_back_days)))
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = _get_json(
            REPORT_API,
            params={
                "pageNo": page,
                "pageSize": 20,
                "qType": 0,
                "code": clean,
                "beginTime": begin.isoformat(),
                "endTime": date.today().isoformat(),
            },
            timeout=12,
        )
        page_rows = _records(payload)
        if not page_rows:
            break
        for row in page_rows:
            info_code = row.get("infoCode") or row.get("INFO_CODE")
            if info_code and not row.get("pdf_url"):
                row["pdf_url"] = f"https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
        rows.extend(page_rows)
    return pd.DataFrame(rows)


def _eastmoney_stock_fund_flow_120d(code: str) -> pd.DataFrame:
    payload = _get_json(
        f"{PUSH2HIS_URL}/stock/fflow/daykline/get",
        params={
            "lmt": 120,
            "klt": 101,
            "secid": secid(code),
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62",
        },
    )
    rows = []
    for item in _records(payload):
        parts = str(item.get("kline", "")).split(",")
        if len(parts) >= 11:
            rows.append(
                {
                    "date": parts[0],
                    "main_net": parts[1],
                    "small_net": parts[2],
                    "medium_net": parts[3],
                    "large_net": parts[4],
                    "super_large_net": parts[5],
                    "main_net_pct": parts[6],
                    "small_net_pct": parts[7],
                    "medium_net_pct": parts[8],
                    "large_net_pct": parts[9],
                    "super_large_net_pct": parts[10],
                }
            )
    return pd.DataFrame(rows)


def stock_fund_flow_120d(code: str) -> pd.DataFrame:
    try:
        out = _eastmoney_stock_fund_flow_120d(code)
        if not out.empty:
            out["data_source"] = "eastmoney-120d"
            return out
    except Exception:
        pass
    from tools.providers.easy_tdx_provider import fetch_capital_flow

    out = fetch_capital_flow(code)
    if not out.empty:
        out = out.rename(columns={"mid_net": "medium_net"})
        out["data_source"] = "easy_tdx-current"
    return out


def fund_flow_minute(code: str) -> pd.DataFrame:
    payload = _get_json(
        f"{PUSH2_URL}/stock/fflow/kline/get",
        params={
            "secid": secid(code),
            "klt": 1,
            "lmt": 240,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62",
        },
    )
    rows = []
    for item in _records(payload):
        parts = str(item.get("kline", "")).split(",")
        if len(parts) < 6:
            continue
        rows.append({
            "time": parts[0],
            "main_net": parts[1],
            "small_net": parts[2],
            "medium_net": parts[3],
            "large_net": parts[4],
            "super_large_net": parts[5],
            "data_source": "eastmoney-minute",
        })
    return pd.DataFrame(rows, columns=(
        "time", "main_net", "small_net", "medium_net", "large_net", "super_large_net", "data_source",
    ))


def concept_blocks(code: str) -> pd.DataFrame:
    payload = _get_json(
        f"{PUSH2_URL}/slist/get",
        params={
            "secid": secid(code),
            "pi": 0,
            "pz": 80,
            "fields": "f12,f13,f14,f1,f2,f3,f4,f20,f104,f105,f128,f140,f141,f136,f152",
        },
    )
    out = _df(payload)
    if not out.empty:
        return out
    from tools.providers.easy_tdx_provider import fetch_belong_boards

    out = fetch_belong_boards(code)
    return out.rename(columns={"board_code": "f12", "board_name": "f14"})


def _date_filter(field: str, begin: date | None = None, end: date | None = None) -> str:
    begin = begin or (date.today() - timedelta(days=365))
    end = end or date.today()
    return f"({field}>='{begin.isoformat()}')({field}<='{end.isoformat()}')"


def dragon_tiger_board(code: str, look_back: int = 180) -> pd.DataFrame:
    begin = date.today() - timedelta(days=look_back)
    return eastmoney_datacenter(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        filters=f"(SECURITY_CODE=\"{clean_code(code)}\"){_date_filter('TRADE_DATE', begin)}",
        sort_columns="TRADE_DATE",
    )


def lockup_expiry(code: str, forward_days: int = 180) -> pd.DataFrame:
    today = date.today()
    end = today + timedelta(days=forward_days)
    return eastmoney_datacenter(
        "RPT_LIFT_STAGE",
        filters=f"(SECURITY_CODE=\"{clean_code(code)}\"){_date_filter('FREE_DATE', today, end)}",
        sort_columns="FREE_DATE",
        sort_types="1",
    )


def margin_trading(code: str) -> pd.DataFrame:
    clean = clean_code(code)
    return eastmoney_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filters=f"(SCODE=\"{clean}\")",
        sort_columns="DATE",
    )


def block_trade(code: str, look_back: int = 180) -> pd.DataFrame:
    begin = date.today() - timedelta(days=look_back)
    return eastmoney_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filters=f"(SECURITY_CODE=\"{clean_code(code)}\"){_date_filter('TRADE_DATE', begin)}",
        sort_columns="TRADE_DATE",
    )


def holder_num_change(code: str) -> pd.DataFrame:
    return eastmoney_datacenter(
        "RPT_HOLDERNUMLATEST",
        filters=f"(SECURITY_CODE=\"{clean_code(code)}\")",
        sort_columns="END_DATE",
    )


def dividend_history(code: str) -> pd.DataFrame:
    return eastmoney_datacenter(
        "RPT_SHAREBONUS_DET",
        filters=f"(SECURITY_CODE=\"{clean_code(code)}\")",
        sort_columns="EX_DIVIDEND_DATE",
    )


def stock_news(code: str, pages: int = 1) -> pd.DataFrame:
    try:
        import akshare as ak

        out = ak.stock_news_em(symbol=clean_code(code))
        if out is not None and not out.empty:
            return out.rename(columns={
                "新闻标题": "title",
                "发布时间": "showTime",
                "新闻链接": "url",
                "文章来源": "source",
                "新闻内容": "content",
            })
    except Exception:
        pass

    rows: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        payload = _get_json(
            SEARCH_API,
            params={
                "cb": "jQuery112300000000000000000_1",
                "keyword": clean_code(code),
                "type": "cmsArticleWebOld",
                "pageindex": page,
                "pagesize": 20,
                "client": "web",
            },
            timeout=12,
        )
        rows.extend(_records(payload))
    return pd.DataFrame(rows)


def hot_rank(top: int = 50) -> pd.DataFrame:
    payload = _get_json(
        f"{PUSH2_URL}/ulist.np/get",
        params={
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14,f3,f2,f15,f16,f17,f18,f20,f21,f24,f25",
            "secids": "",
            "pz": top,
            "po": 1,
            "np": 1,
        },
    )
    return _df(payload)


def collect_market_events(code: str) -> dict[str, pd.DataFrame]:
    return {
        "research_reports": research_reports(code),
        "concept_blocks": concept_blocks(code),
        "dragon_tiger_board": dragon_tiger_board(code),
        "lockup_expiry": lockup_expiry(code),
        "margin_trading": margin_trading(code),
        "block_trade": block_trade(code),
        "holder_num_change": holder_num_change(code),
        "dividend_history": dividend_history(code),
        "fund_flow_120d": stock_fund_flow_120d(code),
        "stock_news": stock_news(code),
    }


def health_check() -> dict[str, Any]:
    started = time.perf_counter()
    checks: dict[str, Any] = {}
    try:
        checks["margin"] = not margin_trading("000001").empty
    except Exception as exc:
        checks["margin"] = False
        checks["margin_error"] = f"{type(exc).__name__}: {exc}"
    try:
        checks["holders"] = not holder_num_change("000001").empty
    except Exception as exc:
        checks["holders"] = False
        checks["holders_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "ok": any(value is True for value in checks.values()),
        "status": "ok" if any(value is True for value in checks.values()) else "unavailable",
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "checks": checks,
    }
