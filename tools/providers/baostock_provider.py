from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, timedelta
import importlib
import io
import threading
import time
from typing import Iterator

import pandas as pd


_SESSION_LOCK = threading.Lock()
_KLINE_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,"
    "tradestatus,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST"
)
_ADJUST_FLAGS = {"hfq": "1", "qfq": "2", "none": "3", "": "3", None: "3"}
_SUMMARY_QUERIES = (
    "query_profit_data",
    "query_balance_data",
    "query_cash_flow_data",
)
_HEALTH_CACHE: tuple[float, dict] = (0.0, {})


def _baostock_module():
    try:
        return importlib.import_module("baostock")
    except ImportError as exc:
        raise RuntimeError("BaoStock is not installed; install baostock==0.9.3") from exc


def security_code(code: str) -> str:
    """Convert a six-digit A-share code to BaoStock's exchange-prefixed form."""
    value = str(code).strip().lower()
    if value.startswith(("sh.", "sz.")):
        return value
    value = value.zfill(6)
    if not value.isdigit() or len(value) != 6:
        raise ValueError(f"invalid A-share code: {code}")
    if value[0] in {"4", "8", "9"}:
        raise ValueError(f"BaoStock does not support Beijing Stock Exchange code {value}")
    exchange = "sh" if value[0] in {"5", "6"} else "sz"
    return f"{exchange}.{value}"


def _check_response(response, label: str) -> None:
    error_code = str(getattr(response, "error_code", ""))
    if error_code != "0":
        error_message = str(getattr(response, "error_msg", "unknown error"))
        raise RuntimeError(f"BaoStock {label} failed [{error_code}]: {error_message}")


@contextmanager
def _session() -> Iterator[object]:
    """Serialize access because the BaoStock client stores one global socket session."""
    with _SESSION_LOCK:
        client = _baostock_module()
        sink = io.StringIO()
        with redirect_stdout(sink), redirect_stderr(sink):
            login = client.login()
        _check_response(login, "login")
        try:
            yield client
        finally:
            with redirect_stdout(sink), redirect_stderr(sink):
                client.logout()


def _response_frame(response, label: str) -> pd.DataFrame:
    _check_response(response, label)
    fields = list(getattr(response, "fields", []) or [])
    rows: list[list[str]] = []
    while response.next():
        rows.append(response.get_row_data())
    return pd.DataFrame(rows, columns=fields)


def fetch_kline_daily(
    code: str,
    count: int = 800,
    *,
    adjust: str | None = "qfq",
    end_date: date | str | None = None,
) -> pd.DataFrame:
    """Fetch normalized daily A-share K lines from BaoStock."""
    symbol = security_code(code)
    normalized_adjust = adjust.lower() if isinstance(adjust, str) else adjust
    if normalized_adjust not in _ADJUST_FLAGS:
        raise ValueError(f"unsupported BaoStock adjustment: {adjust}")
    end = pd.Timestamp(end_date or date.today()).date()
    calendar_days = max(730, int(max(1, count) * 1.8))
    start = end - timedelta(days=calendar_days)

    with _session() as client:
        response = client.query_history_k_data_plus(
            symbol,
            _KLINE_FIELDS,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag=_ADJUST_FLAGS[normalized_adjust],
        )
        frame = _response_frame(response, "daily K line")

    if frame.empty:
        return frame
    frame = frame.rename(columns={
        "pctChg": "pct_chg",
        "turn": "turnover",
        "tradestatus": "trade_status",
        "isST": "is_st",
    })
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    text_columns = {"code", "adjustflag"}
    for column in frame.columns:
        if column != "date" and column not in text_columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date", keep="last")
    frame = frame.tail(max(1, int(count))).reset_index(drop=True)
    frame.attrs.update({"source": "BaoStock", "source_tier": "B"})
    return frame


def _recent_quarters(limit: int) -> Iterator[tuple[int, int]]:
    today = date.today()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    for _ in range(max(1, limit)):
        yield year, quarter
        quarter -= 1
        if quarter == 0:
            year -= 1
            quarter = 4


def fetch_financial_summary(code: str, periods: int = 8) -> pd.DataFrame:
    """Fetch BaoStock's ratio-level summary; this is not a full statutory statement."""
    symbol = security_code(code)
    rows: list[pd.DataFrame] = []
    with _session() as client:
        for year, quarter in _recent_quarters(max(1, periods) + 2):
            parts: list[pd.DataFrame] = []
            for method_name in _SUMMARY_QUERIES:
                method = getattr(client, method_name)
                part = _response_frame(
                    method(code=symbol, year=year, quarter=quarter),
                    f"{method_name}/{year}Q{quarter}",
                )
                if not part.empty:
                    parts.append(part)
            if not parts:
                continue
            merged = parts[0]
            keys = [column for column in ("code", "statDate") if column in merged.columns]
            for index, part in enumerate(parts[1:], start=1):
                merge_keys = [column for column in keys if column in part.columns]
                if "pubDate" in merged.columns and "pubDate" in part.columns:
                    part = part.rename(columns={"pubDate": f"pubDate_{index}"})
                merged = merged.merge(part, on=merge_keys, how="outer") if merge_keys else merged
            pub_date_columns = [column for column in merged.columns if column.startswith("pubDate")]
            if pub_date_columns:
                published = merged[pub_date_columns].apply(pd.to_datetime, errors="coerce")
                merged["pubDate"] = published.max(axis=1)
                merged = merged.drop(columns=[column for column in pub_date_columns if column != "pubDate"])
            rows.append(merged)
            if len(rows) >= max(1, periods):
                break

    if not rows:
        return pd.DataFrame()
    frame = pd.concat(rows, ignore_index=True, sort=False)
    for column in ("pubDate", "statDate"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
    for column in frame.columns:
        if column not in {"code", "pubDate", "statDate"}:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["statDate"]).sort_values("statDate", ascending=False).drop_duplicates("statDate")
    frame = frame.reset_index(drop=True)
    frame.attrs.update({
        "source": "BaoStock",
        "source_tier": "B",
        "evidence_role": "cross_check_and_partial_fallback",
    })
    return frame


def health_check(cache_seconds: int = 60) -> dict:
    global _HEALTH_CACHE
    now = time.time()
    checked_at, cached = _HEALTH_CACHE
    if cached and now - checked_at < max(0, cache_seconds):
        return dict(cached)
    started = time.perf_counter()
    try:
        frame = fetch_kline_daily("000001", count=5)
        result = {
            "ok": not frame.empty,
            "status": "ok" if not frame.empty else "empty",
            "latest_date": str(frame.iloc[-1]["date"])[:10] if not frame.empty else None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        result = {
            "ok": False,
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {str(exc)[:120]}",
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    _HEALTH_CACHE = (now, result)
    return dict(result)
