from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


COLUMN_ALIASES = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
}
REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume", "amount")


def validate_kline_frame(
    frame: pd.DataFrame,
    *,
    minimum_rows: int = 1,
    max_age_days: int | None = None,
    reference_date: date | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Normalize daily OHLCV data and reject rows that can corrupt indicators."""
    if frame is None or frame.empty:
        raise ValueError("kline frame is empty")
    attrs: dict[str, Any] = dict(getattr(frame, "attrs", {}))
    normalized = frame.rename(columns={key: value for key, value in COLUMN_ALIASES.items() if key in frame.columns}).copy()
    missing = [column for column in REQUIRED_COLUMNS if column not in normalized.columns]
    if missing:
        raise ValueError(f"kline columns missing: {','.join(missing)}")

    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in REQUIRED_COLUMNS[1:]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    before = len(normalized)
    valid = normalized["date"].notna()
    valid &= normalized[["open", "high", "low", "close"]].notna().all(axis=1)
    valid &= (normalized[["open", "high", "low", "close"]] > 0).all(axis=1)
    valid &= normalized["volume"].fillna(-1).ge(0) & normalized["amount"].fillna(-1).ge(0)
    valid &= normalized["high"].ge(normalized[["open", "close", "low"]].max(axis=1))
    valid &= normalized["low"].le(normalized[["open", "close", "high"]].min(axis=1))
    normalized = normalized.loc[valid].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    issues: list[str] = []
    dropped = before - len(normalized)
    if dropped:
        issues.append(f"dropped_invalid_rows:{dropped}")
    zero_activity = int(((normalized["volume"] == 0) | (normalized["amount"] == 0)).sum())
    if zero_activity:
        issues.append(f"zero_activity_rows:{zero_activity}")
    if len(normalized) < max(1, int(minimum_rows)):
        raise ValueError(f"kline rows insufficient: {len(normalized)} < {minimum_rows}")

    today = reference_date or date.today()
    latest = normalized["date"].iloc[-1].date()
    if latest > today:
        raise ValueError(f"kline latest date is in the future: {latest.isoformat()}")
    age_days = (today - latest).days
    if max_age_days is not None and age_days > max_age_days:
        raise ValueError(f"kline is stale: {age_days} days")

    returns = normalized["close"].pct_change().abs()
    extreme_count = int(returns.gt(0.25).sum())
    if extreme_count:
        issues.append(f"extreme_adjusted_returns:{extreme_count}")
    normalized.attrs.update(attrs)
    normalized.attrs["quality_issues"] = issues
    normalized.attrs["latest_date"] = latest.isoformat()
    return normalized, issues
