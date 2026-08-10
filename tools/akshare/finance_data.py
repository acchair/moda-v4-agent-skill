"""
个股基本面 + 行情数据模块
========================
集成 easy_tdx、Sina、efinance 和 AKShare 获取 A 股行情与基本面数据，
输出结构化 Markdown 报告供莫大 persona 参考。

数据源优先级: easy_tdx/TDX/Sina → efinance/AKShare
用法:
    python3 tools/akshare/finance_data.py --stock 603290 --name 斯达半导
    python3 tools/akshare/finance_data.py --stock 603290,600460
"""
import time, sys, os, argparse, json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/akshare"))

# ══ 反限流: 必须在 import akshare 之前 ══
from anti_rate_limit import apply_patch
apply_patch()

import akshare as ak
import pandas as pd
import numpy as np
from tools.data_call import dataframe_empty, run_fallback_chain
OUTPUT_BASE = ROOT / "knowledge/research/finance_data"

# 东方财富列名 → 统一列名映射
EM_COL_MAP_DAILY = {"日期": "date", "开盘": "open", "收盘": "close",
                     "最高": "high", "最低": "low", "成交量": "volume",
                     "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover"}
SINA_COL_MAP = {"date": "date", "open": "open", "high": "high", "low": "low",
                "close": "close", "volume": "volume", "amount": "amount",
                "outstanding_share": "outstanding", "turnover": "turnover"}

OUTPUT_BASE.mkdir(parents=True, exist_ok=True)


def _with_frame_meta(frame: pd.DataFrame, *, fetch_state: str, source_chain: list[dict] | None = None, error: str = "") -> pd.DataFrame:
    frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    frame.attrs["fetch_state"] = fetch_state
    frame.attrs["source_chain"] = source_chain or []
    frame.attrs["fetch_error"] = error or None
    return frame


def _normalize_daily(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame = frame.rename(columns=EM_COL_MAP_DAILY | SINA_COL_MAP).copy()
    required = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]
    for column in required:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in required[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def _financial_aliases(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    aliases = {
        "报告日期": "报告期", "报告期": "报告期", "日期": "报告期",
        "营业收入": "营业收入", "营业收入同比": "营业收入_同比", "营业收入_同比": "营业收入_同比",
        "归属于母公司所有者的净利润": "归属于母公司所有者的净利润",
        "归属于母公司所有者的净利润同比": "归属于母公司所有者的净利润_同比",
        "归属于母公司的净利润": "归属于母公司的净利润",
        "归属于母公司的净利润同比": "归属于母公司的净利润_同比",
        "经营活动产生的现金流量净额": "经营活动产生的现金流量净额",
    }
    renamed = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns}).copy()
    if "报告期" in renamed.columns:
        renamed["报告期"] = renamed["报告期"].astype(str)
    return renamed


THS_FINANCIAL_METRICS = {
    "lrb": {
        "operating_income_total": "营业总收入",
        "operating_income": "营业收入",
        "parent_holder_net_profit": "归属于母公司所有者的净利润",
        "net_profit": "净利润",
        "basic_eps": "基本每股收益",
    },
    "llb": {
        "act_cash_flow_net": "经营活动产生的现金流量净额",
        "invest_cash_flow_net": "投资活动产生的现金流量净额",
        "financing_cash_flow_net": "筹资活动产生的现金流量净额",
    },
    "fzb": {
        "cash": "货币资金",
        "accounts_receivable": "应收账款",
        "inventory": "存货",
        "short_term_loans": "短期借款",
        "year_non_current_debt": "一年内到期的非流动负债",
        "long_term_loan": "长期借款",
        "bonds_payable": "应付债券",
        "lease_debt": "租赁负债",
        "construction_process_total": "在建工程合计",
        "construction_in_process": "在建工程",
        "goodwill": "商誉",
        "assets_total": "资产总计",
        "total_debt": "负债合计",
        "parent_holder_equity_total": "归属于母公司股东权益合计",
    },
}


def _normalize_ths_financial_report(frame: pd.DataFrame, report_type: str) -> pd.DataFrame:
    """Convert AKShare's THS long-form statements to the existing wide contract."""
    metrics = THS_FINANCIAL_METRICS.get(report_type, {})
    required = {"report_date", "metric_name", "value"}
    if frame is None or frame.empty or not required.issubset(frame.columns) or not metrics:
        return pd.DataFrame()
    selected = frame[frame["metric_name"].isin(metrics)].copy()
    if selected.empty:
        return pd.DataFrame()
    selected["report_date"] = pd.to_datetime(selected["report_date"], errors="coerce")
    selected = selected.dropna(subset=["report_date"])
    if selected.empty:
        return pd.DataFrame()

    result = pd.DataFrame(index=sorted(selected["report_date"].unique(), reverse=True))
    result.index.name = "报告期"
    for metric, title in metrics.items():
        rows = selected[selected["metric_name"].eq(metric)].drop_duplicates("report_date", keep="first")
        if rows.empty:
            continue
        values = pd.to_numeric(rows.set_index("report_date")["value"], errors="coerce")
        result[title] = values.reindex(result.index)
        if "yoy" in rows.columns:
            yoy = pd.to_numeric(rows.set_index("report_date")["yoy"], errors="coerce")
            if yoy.notna().any():
                result[f"{title}_同比"] = yoy.reindex(result.index)
    result = result.reset_index()
    result["报告期"] = result["报告期"].dt.strftime("%Y-%m-%d")
    if "归属于母公司所有者的净利润" in result.columns:
        result["归属于母公司的净利润"] = result["归属于母公司所有者的净利润"]
        yoy_column = "归属于母公司所有者的净利润_同比"
        if yoy_column in result.columns:
            result["归属于母公司的净利润_同比"] = result[yoy_column]
    return result


# ══════════════════════════════════════════════════════
#  Data Fetchers (each with fallback)
# ══════════════════════════════════════════════════════

def fetch_kline_daily(code: str, kline_file: Path | None = None) -> pd.DataFrame:
    """日K线: 本次共享缓存 → easy_tdx → 东财 → 新浪。"""
    if kline_file and kline_file.stem == code and kline_file.exists():
        df = pd.read_csv(kline_file, parse_dates=["date"])
        print(f"  [日K] 共享缓存 → {len(df)} 条")
        return _with_frame_meta(df, fetch_state="ok", source_chain=[{"source": "shared-cache", "status": "ok", "error": ""}])

    def easy_tdx() -> pd.DataFrame:
        from tools.providers.easy_tdx_provider import fetch_kline_daily as fetch_easy_tdx_kline
        return _normalize_daily(fetch_easy_tdx_kline(code))

    def eastmoney() -> pd.DataFrame:
        return _normalize_daily(ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq"))

    def sina() -> pd.DataFrame:
        pfx = "sh" if code[0] == "6" else "sz"
        frame = _normalize_daily(ak.stock_zh_a_daily(symbol=f"{pfx}{code}", adjust="qfq"))
        if "pct_chg" in frame.columns and frame["pct_chg"].isna().all() and "close" in frame.columns:
            frame["pct_chg"] = frame["close"].pct_change() * 100
        return frame

    result = run_fallback_chain(
        "日K线",
        [("easy_tdx", easy_tdx), ("AKShare/东方财富", eastmoney), ("AKShare/Sina", sina)],
        seconds=12,
        empty=dataframe_empty,
    )
    frame = result.value if isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    if result.ok:
        print(f"  [日K] {result.source} → {len(frame)} 条")
    return _with_frame_meta(frame, fetch_state=result.fetch_state, source_chain=result.source_chain, error=result.error)


def fetch_kline_quarterly(code: str, daily: pd.DataFrame | None = None) -> pd.DataFrame:
    """季K线: 从已取得的日K本地聚合。"""
    print("  [季K] 从日K降采样 ...", end=" ")
    df = daily.copy() if daily is not None else fetch_kline_daily(code)
    if df.empty:
        print("失败: 日K为空")
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])

    # 降采样到季度
    df = df.set_index("date")
    q = df.resample("QE").agg({
        "open": "first", "close": "last",
        "high": "max", "low": "min",
        "volume": "sum", "amount": "sum",
    })
    q["pct_chg"] = (q["close"].pct_change() * 100).round(2)
    q = q.dropna(subset=["open"]).reset_index()
    q["date"] = q["date"].apply(lambda dt: f"{dt.year}-Q{(dt.month-1)//3+1}")
    print(f"{len(q)} 条")
    return q


def fetch_spot(code: str) -> dict:
    """实时行情: easy_tdx 单股查询，回退 efinance 单股查询。"""
    def easy_tdx() -> dict:
        from tools.providers.easy_tdx_provider import fetch_realtime_quote
        return fetch_realtime_quote(code)

    def efinance() -> dict:
        from tools.efinance.provider import fetch_realtime_quotes
        return fetch_realtime_quotes(code)

    result = run_fallback_chain("实时行情", [("easy_tdx", easy_tdx), ("efinance", efinance)], seconds=8, empty=lambda value: not bool(value))
    payload = dict(result.value or {})
    if result.ok:
        print(f"  [行情] {result.source} → {payload.get('最新价', 'N/A')}")
    payload.update({"_fetch_state": result.fetch_state, "_source_chain": result.source_chain or [], "_fetch_error": result.error or None})
    return payload


def fetch_company_and_peers(code: str) -> tuple[dict, pd.DataFrame]:
    """从所属行业板块一次取得行业标签和同行估值快照。"""
    from tools.providers.easy_tdx_provider import fetch_belong_boards, fetch_board_members

    try:
        boards = fetch_belong_boards(code)
    except Exception as e:
        print(f"  [行业] easy_tdx失败: {e}")
        return {}, pd.DataFrame()
    if boards is None or boards.empty:
        return {}, pd.DataFrame()

    industries = boards[pd.to_numeric(boards["board_type"], errors="coerce") == 12]
    if industries.empty:
        return {}, pd.DataFrame()
    names = list(dict.fromkeys(industries["board_name"].dropna().astype(str)))
    info = {"source": "easy_tdx/TDX", "行业": " / ".join(names)}
    board = industries.iloc[-1]

    try:
        members = fetch_board_members(str(board["board_code"]))
    except Exception as e:
        print(f"  [同行] easy_tdx失败: {e}")
        return info, pd.DataFrame()
    if members is None or members.empty:
        return info, pd.DataFrame()

    close = pd.to_numeric(members.get("close"), errors="coerce")
    net_assets = pd.to_numeric(members.get("net_assets"), errors="coerce")
    peers = pd.DataFrame({
        "代码": members["code"].astype(str).str.zfill(6),
        "简称": members["name"],
        "市盈率": pd.to_numeric(members.get("pe_dynamic"), errors="coerce"),
        "市盈率-TTM": pd.to_numeric(members.get("pe_ttm"), errors="coerce"),
        "市净率": close.div(net_assets.where(net_assets > 0)),
        "总市值": pd.to_numeric(members.get("total_market_cap_ab"), errors="coerce"),
        "每股收益": pd.to_numeric(members.get("eps"), errors="coerce"),
    })
    peers = peers[close > 0].copy()
    peers["_target"] = peers["代码"].eq(code)
    peers = peers.sort_values(["_target", "总市值"], ascending=[False, False]).drop(columns="_target")
    print(f"  [同行] {board['board_name']} → {len(peers)} 家")
    return info, peers


def fetch_financial_report(code: str, report_type: str) -> pd.DataFrame:
    """财报: easy_tdx/Sina → AKShare/Sina → AKShare/同花顺。"""
    indicator = {"lrb": "利润表", "fzb": "资产负债表", "llb": "现金流量表"}.get(report_type, report_type)

    def easy_sina() -> pd.DataFrame:
        from tools.providers.easy_tdx_provider import fetch_financial_report as fetch_sina_report
        return _financial_aliases(fetch_sina_report(code, report_type, num=8))

    def ak_sina() -> pd.DataFrame:
        prefix = "sh" if code.startswith(("6", "9")) else "bj" if code.startswith(("4", "8")) else "sz"
        return _financial_aliases(ak.stock_financial_report_sina(stock=f"{prefix}{code}", symbol=indicator))

    def ak_ths() -> pd.DataFrame:
        functions = {
            "lrb": ak.stock_financial_benefit_new_ths,
            "llb": ak.stock_financial_cash_new_ths,
            "fzb": ak.stock_financial_debt_new_ths,
        }
        function = functions.get(report_type)
        if function is None:
            return pd.DataFrame()
        return _normalize_ths_financial_report(function(symbol=code, indicator="按报告期"), report_type).head(8)

    result = run_fallback_chain(
        f"财报/{report_type}",
        [("easy_tdx/Sina", easy_sina), ("AKShare/Sina", ak_sina), ("AKShare/同花顺", ak_ths)],
        seconds=15,
        empty=dataframe_empty,
    )
    frame = result.value if isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    if result.ok:
        print(f"  [财报/{report_type}] {result.source} → {len(frame)} 期")
    return _with_frame_meta(frame, fetch_state=result.fetch_state, source_chain=result.source_chain, error=result.error)


def fetch_historical_valuation(code: str) -> dict[str, pd.DataFrame]:
    """Five-year PE/PB history for individual valuation percentiles."""
    results: dict[str, pd.DataFrame] = {}
    for key, indicator in (("pe", "市盈率(TTM)"), ("pb", "市净率")):
        try:
            frame = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近五年")
            if frame is not None and not frame.empty:
                results[key] = frame
        except Exception as exc:
            print(f"  [历史估值/{indicator}] 失败: {exc}")
    return results


def _history_percentile(frame: pd.DataFrame) -> float | None:
    if frame is None or frame.empty or "value" not in frame.columns:
        return None
    values = pd.to_numeric(frame["value"], errors="coerce").dropna()
    if len(values) < 60:
        return None
    current = float(values.iloc[-1])
    return float((values <= current).mean())


def _history_median_ratio(frame: pd.DataFrame) -> float | None:
    if frame is None or frame.empty or "value" not in frame.columns:
        return None
    values = pd.to_numeric(frame["value"], errors="coerce").dropna()
    if len(values) < 60:
        return None
    median = float(values.median())
    return float(values.iloc[-1]) / median if median > 0 else None


# ══════════════════════════════════════════════════════
#  Report Generator
# ══════════════════════════════════════════════════════

def _safe_num(v, fmt=".2f"):
    """安全格式化数字"""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:{fmt}}"
    return str(v)


def _report_metrics(code: str, spot: dict, info: dict, kline_daily: pd.DataFrame,
                    valuation: pd.DataFrame, financials: dict[str, pd.DataFrame],
                    valuation_history: dict[str, pd.DataFrame] | None = None) -> dict:
    def latest(report_type: str, *columns: str):
        frame = financials.get(report_type, pd.DataFrame())
        column = next((candidate for candidate in columns if candidate in frame.columns), None)
        if frame.empty or column is None:
            return None
        value = pd.to_numeric(pd.Series([frame.iloc[0][column]]), errors="coerce").iloc[0]
        return None if pd.isna(value) else float(value)

    def latest_sum(report_type: str, groups: tuple[tuple[str, ...], ...]) -> tuple[float | None, int]:
        values = []
        for aliases in groups:
            value = latest(report_type, *aliases)
            if value is not None:
                values.append(value)
        return (sum(values), len(values)) if values else (None, 0)

    metrics = {
        "industry": info.get("行业") if info else None,
        "latest_price": spot.get("最新价") if spot else None,
        "revenue_yoy": latest("lrb", "营业收入_同比"),
        "profit_yoy": latest("lrb", "归属于母公司的净利润_同比", "归属于母公司所有者的净利润_同比"),
        "operating_cashflow": latest("llb", "经营活动产生的现金流量净额"),
        "net_profit": latest("lrb", "归属于母公司的净利润", "归属于母公司所有者的净利润"),
        "quote_fetch_state": spot.get("_fetch_state") if spot else "failed",
        "quote_source_chain": spot.get("_source_chain", []) if spot else [],
        "kline_fetch_state": kline_daily.attrs.get("fetch_state", "failed"),
        "kline_source_chain": kline_daily.attrs.get("source_chain", []),
        "financial_fetch_state": {
            key: frame.attrs.get("fetch_state", "failed") for key, frame in financials.items()
        },
        "financial_source_chain": {
            key: frame.attrs.get("source_chain", []) for key, frame in financials.items()
        },
    }
    income = financials.get("lrb", pd.DataFrame())
    if len(income) >= 2:
        for columns, target in (
            (("营业收入_同比",), "revenue_yoy_delta"),
            (("归属于母公司的净利润_同比", "归属于母公司所有者的净利润_同比"), "profit_yoy_delta"),
        ):
            column = next((candidate for candidate in columns if candidate in income.columns), None)
            if column is not None:
                values = pd.to_numeric(income[column].head(2), errors="coerce")
                if len(values) == 2 and values.notna().all():
                    metrics[target] = float(values.iloc[0] - values.iloc[1])
    revenue = latest("lrb", "营业收入")
    assets, liabilities = latest("fzb", "资产总计"), latest("fzb", "负债合计")
    cash = latest("fzb", "货币资金")
    receivables = latest("fzb", "应收账款")
    short_debt, short_debt_fields = latest_sum("fzb", (
        ("短期借款",),
        ("一年内到期的非流动负债", "一年内到期的长期负债"),
    ))
    interest_debt, interest_debt_fields = latest_sum("fzb", (
        ("短期借款",),
        ("一年内到期的非流动负债", "一年内到期的长期负债"),
        ("长期借款",),
        ("应付债券",),
        ("租赁负债",),
    ))
    metrics.update({
        key: value for key, value in (
            ("total_revenue", revenue),
            ("total_assets", assets),
            ("total_liabilities", liabilities),
            ("monetary_cash", cash),
            ("accounts_receivable", receivables),
            ("short_term_interest_debt", short_debt),
            ("interest_bearing_debt", interest_debt),
        ) if value is not None
    })
    metrics["short_debt_fields_found"] = short_debt_fields
    metrics["interest_debt_fields_found"] = interest_debt_fields
    if assets and liabilities is not None:
        metrics["debt_ratio"] = liabilities / assets
    if cash is not None and liabilities and liabilities > 0:
        metrics["cash_to_debt"] = cash / liabilities
    if cash is not None and assets and assets > 0 and interest_debt is not None:
        metrics["net_cash_ratio"] = (cash - interest_debt) / assets
    if cash is not None and short_debt is not None:
        metrics["cash_to_short_debt"] = cash / short_debt if short_debt > 0 else 999.0
    operating_cashflow = metrics.get("operating_cashflow")
    net_profit = metrics.get("net_profit")
    if operating_cashflow is not None and net_profit is not None and net_profit > 0:
        metrics["operating_cashflow_to_net_profit"] = operating_cashflow / net_profit
    if receivables is not None and assets and assets > 0:
        metrics["receivables_to_assets"] = receivables / assets
    goodwill = latest("fzb", "商誉")
    if goodwill is not None:
        metrics["goodwill"] = goodwill
        if assets and assets > 0:
            goodwill_ratio = goodwill / assets
            metrics["goodwill_to_assets"] = goodwill_ratio
            if goodwill_ratio <= 0.10:
                metrics["goodwill_risk"] = False
            elif goodwill_ratio >= 0.20:
                metrics["goodwill_risk"] = True
    if not valuation.empty:
        target = valuation[valuation["代码"].eq(code)]
        if not target.empty:
            metrics["pe_ttm"] = float(target.iloc[0]["市盈率-TTM"])
            metrics["pb"] = float(target.iloc[0]["市净率"])
        peers = pd.to_numeric(valuation.loc[~valuation["代码"].eq(code), "市盈率-TTM"], errors="coerce")
        peers = peers[peers > 0]
        if not peers.empty:
            metrics["peer_pe_ttm_median"] = float(peers.median())
    valuation_history = valuation_history or {}
    pe_percentile = _history_percentile(valuation_history.get("pe", pd.DataFrame()))
    pb_percentile = _history_percentile(valuation_history.get("pb", pd.DataFrame()))
    pb_median_ratio = _history_median_ratio(valuation_history.get("pb", pd.DataFrame()))
    if pe_percentile is not None:
        metrics["pe_percentile_5y"] = pe_percentile
    if pb_percentile is not None:
        metrics["pb_percentile_5y"] = pb_percentile
    if pb_median_ratio is not None:
        metrics["pb_to_5y_median"] = pb_median_ratio
    if not kline_daily.empty and "close" in kline_daily.columns:
        close = pd.to_numeric(kline_daily["close"], errors="coerce").dropna().tail(800)
        if len(close) >= 720:
            latest_close, low, high = float(close.iloc[-1]), float(close.min()), float(close.max())
            if high > low:
                metrics["price_percentile_3y"] = (latest_close - low) / (high - low)
                metrics["drawdown_from_3y_high"] = latest_close / high - 1
    states = [metrics.get("quote_fetch_state"), metrics.get("kline_fetch_state"), *metrics.get("financial_fetch_state", {}).values()]
    metrics["fetch_state"] = "failed" if any(state == "failed" for state in states) else "fallback_ok" if any(state == "fallback_ok" for state in states) else "empty" if all(state in {"empty", "failed"} for state in states) else "ok"
    metrics["source_chain"] = {
        "quote": metrics.get("quote_source_chain", []),
        "kline": metrics.get("kline_source_chain", []),
        "financial": metrics.get("financial_source_chain", {}),
    }
    clean: dict = {}
    for key, value in metrics.items():
        if value is None:
            continue
        if isinstance(value, (bool, np.bool_)):
            clean[key] = bool(value)
        elif isinstance(value, (int, float, np.number)):
            if np.isfinite(value):
                clean[key] = float(value)
        else:
            clean[key] = value
    return clean


def build_report(code: str, name: str,
                 spot: dict, info: dict,
                 kline_daily: pd.DataFrame,
                 kline_quarterly: pd.DataFrame,
                 valuation: pd.DataFrame,
                 financials: dict[str, pd.DataFrame],
                 valuation_history: dict[str, pd.DataFrame] | None = None) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    L = [
        f"# 基本面+行情报告: {name}({code})",
        f"",
        f"> 采集时间: {ts}  |  数据源: easy_tdx/TDX/Sina + efinance/AKShare",
        f"> 雪球: [个股页](https://xueqiu.com/S/{'SH' if code[0]=='6' else 'SZ'}{code})  "
        f"|  东财: [股吧](https://guba.eastmoney.com/list,{code},99,f.html)",
        f"",
        "---",
    ]
    L.append(f"<!-- moda_metrics: {json.dumps(_report_metrics(code, spot, info, kline_daily, valuation, financials, valuation_history), ensure_ascii=False)} -->")

    # ── 1. 实时行情 ──
    L += ["## 1. 实时行情", ""]
    if spot:
        src = spot.pop("source", "")
        L.append(f"*来源: {src}*  \n")
        L.append("| 指标 | 数值 |")
        L.append("|------|------|")
        for k in ["最新价", "涨跌幅", "涨跌额", "换手率", "量比",
                   "市盈率-动态", "市净率", "总市值", "流通市值",
                   "60日涨跌幅", "年初至今涨跌幅"]:
            v = spot.get(k)
            if v is not None:
                L.append(f"| {k} | {_safe_num(v)} |")
    else:
        L.append("⚠️ 无实时行情数据（可能非交易时间或网络问题）")
    L.append("")

    # ── 2. 公司信息 ──
    L += ["## 2. 公司信息", ""]
    if info:
        L.append(f"*来源: {info.get('source', '')}*  \n")
        keys = ["行业"]
        L.append("| 指标 | 数值 |")
        L.append("|------|------|")
        for k in keys:
            v = info.get(k)
            if v:
                L.append(f"| {k} | {v} |")
    else:
        L.append("⚠️ 无公司信息数据")
    L.append("")

    # ── 3. 财务摘要 ──
    financial_sources = []
    for frame in financials.values():
        for item in frame.attrs.get("source_chain", []):
            source = str(item.get("source") or "")
            if item.get("status") == "ok" and source and source not in financial_sources:
                financial_sources.append(source)
    source_text = " / ".join(financial_sources) if financial_sources else "需人工确认"
    L += ["## 3. 财务摘要", "", f"*来源: {source_text}*  ", ""]
    financial_columns = {
        "利润表": ("lrb", ["报告期", "营业收入", "营业收入_同比", "归属于母公司的净利润", "归属于母公司的净利润_同比", "归属于母公司所有者的净利润", "归属于母公司所有者的净利润_同比", "基本每股收益"]),
        "资产负债表": ("fzb", ["报告期", "货币资金", "应收账款", "存货", "短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债", "资产总计", "负债合计", "归属于母公司股东权益合计"]),
        "现金流量表": ("llb", ["报告期", "经营活动产生的现金流量净额", "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额"]),
    }
    for title, (report_type, wanted) in financial_columns.items():
        frame = financials.get(report_type, pd.DataFrame())
        L += [f"### {title}", ""]
        cols = [column for column in wanted if column in frame.columns]
        if frame.empty or not cols:
            L += ["⚠️ 无数据", ""]
            continue
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "|".join(["------"] * len(cols)) + "|")
        for _, row in frame.head(4).iterrows():
            values = []
            for column in cols:
                value = row.get(column, "")
                if column.endswith("_同比") and pd.notna(value):
                    values.append(f"{float(value) * 100:.2f}%")
                else:
                    values.append(_safe_num(value))
            L.append("| " + " | ".join(values) + " |")
        L.append("")

    # ── 4. 近期行情 ──
    L += ["## 4. 近期行情 (日K)", ""]
    if not kline_daily.empty:
        recent = kline_daily.tail(10).sort_values("date", ascending=False)
        L.append("| 日期 | 开盘 | 收盘 | 最高 | 最低 | 涨跌幅% | 成交量 |")
        L.append("|------|------|------|------|------|---------|--------|")
        for _, r in recent.iterrows():
            L.append(
                f"| {str(r['date'])[:10]} | {_safe_num(r.get('open'))} | {_safe_num(r.get('close'))} "
                f"| {_safe_num(r.get('high'))} | {_safe_num(r.get('low'))} "
                f"| {_safe_num(r.get('pct_chg'))} | {_safe_num(r.get('volume'), '.0f')} |"
            )

        tail60 = kline_daily.tail(60)
        if len(tail60) > 0:
            h, l = tail60["high"].max(), tail60["low"].min()
            avg_v = tail60["volume"].mean()
            close_now = tail60["close"].iloc[-1]
            chg_60 = ((close_now / tail60["close"].iloc[0] - 1) * 100) if len(tail60) > 1 else 0
            L.append(f"\n**60日统计**: 最高 {_safe_num(h)} / 最低 {_safe_num(l)} "
                     f"/ 涨跌 {_safe_num(chg_60)}% / 日均量 {avg_v:,.0f}")
    else:
        L.append("⚠️ 无日K数据")
    L.append("")

    # ── 5. 季K 趋势 ──
    L += ["## 5. 季K 趋势（莫大最重视）", ""]
    if not kline_quarterly.empty and len(kline_quarterly) >= 2:
        q_data = kline_quarterly.tail(8).sort_values("date", ascending=False)
        L.append("| 季度 | 开盘 | 收盘 | 最高 | 最低 | 涨跌幅% | 成交量 |")
        L.append("|------|------|------|------|------|---------|--------|")
        for _, r in q_data.iterrows():
            L.append(
                f"| {r['date']} | {_safe_num(r.get('open'))} | {_safe_num(r.get('close'))} "
                f"| {_safe_num(r.get('high'))} | {_safe_num(r.get('low'))} "
                f"| {_safe_num(r.get('pct_chg'))} | {_safe_num(r.get('volume'), '.0f')} |"
            )

        # 莫大信号检测
        tail8 = kline_quarterly.tail(8)
        if len(tail8) >= 4:
            vols = tail8["volume"]
            avg_vol = vols.mean()
            last_vol = vols.iloc[-1]
            if last_vol > avg_vol * 1.5:
                L.append(f"\n⚡ **底部放巨量**: 最近季度成交量 {last_vol:,.0f}，"
                         f"显著高于 8 季均值 {avg_vol:,.0f}（{last_vol/avg_vol:.1f}x）。"
                         '莫大常说"主力的鸡脚露出"，值得关注。')

            recent_low = tail8["low"].min()
            last_close = tail8["close"].iloc[-1]
            if last_close < recent_low * 1.15:
                L.append(f"\n📉 **接近 N 季低点**: 当前 {_safe_num(last_close)}，"
                         f"距 8 季最低 {_safe_num(recent_low)} 不到 15%。"
                         '如果基本面没变，可能是被市场嫌弃的窗口。')
    else:
        L.append("⚠️ 无季K数据")
    L.append("")

    # ── 6. 同行估值 ──
    L += ["## 6. 同行估值对比", ""]
    if not valuation.empty:
        v = valuation
        wanted = ["代码", "简称", "市盈率", "市盈率-TTM", "市净率", "总市值", "每股收益"]
        cols = [c for c in wanted if c in v.columns]
        if not cols:
            cols = list(v.columns[:6])
        L.append("| " + " | ".join(cols) + " |")
        L.append("|" + "|".join(["------"] * len(cols)) + "|")
        for _, r in v.head(10).iterrows():
            L.append("| " + " | ".join(_safe_num(r.get(c, "")) for c in cols) + " |")
    else:
        L.append("⚠️ 无同行估值数据")
    L.append("")

    # ── 7. 历史估值分位 ──
    valuation_history = valuation_history or {}
    L += ["## 7. 五年估值分位", "", "| 指标 | 当前历史分位 | 样本数 |", "|---|---:|---:|"]
    for key, label in (("pe", "PE-TTM"), ("pb", "PB")):
        frame = valuation_history.get(key, pd.DataFrame())
        percentile = _history_percentile(frame)
        if percentile is not None:
            L.append(f"| {label} | {percentile:.1%} | {len(frame)} |")
        else:
            L.append(f"| {label} | 需人工确认 | {len(frame)} |")
    L.append("")

    L += [
        "---",
        "",
        "## 免责声明",
        "",
        "本报告基于 easy_tdx、Sina、efinance 和 AKShare 自动采集，仅供信息参考，不构成任何投资建议。",
        "数据可能因网络延迟、交易所休市等原因不完整。",
        "请以交易所官网、券商正式公告为准。",
    ]

    return "\n".join(L)


# ══════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════

def analyze_stock(code: str, name: str = None, kline_file: Path | None = None) -> str:
    if not name:
        name = code

    print(f"\n{'='*55}")
    print(f"  {name}({code})")
    print(f"{'='*55}")

    print("[1/3] 并行获取行情、行业同行和三张财报 ...")
    with ThreadPoolExecutor(max_workers=6) as executor:
        spot_future = executor.submit(fetch_spot, code)
        company_future = executor.submit(fetch_company_and_peers, code)
        financial_futures = {
            report_type: executor.submit(fetch_financial_report, code, report_type)
            for report_type in ("lrb", "fzb", "llb")
        }
        valuation_history_future = executor.submit(fetch_historical_valuation, code)

        print("[2/3] 读取日K线 ...")
        kline_daily = fetch_kline_daily(code, kline_file)
        print("[3/3] 从日K生成季K ...")
        kline_quarterly = fetch_kline_quarterly(code, kline_daily)

        spot = spot_future.result()
        info, valuation = company_future.result()
        financials = {report_type: future.result() for report_type, future in financial_futures.items()}
        valuation_history = valuation_history_future.result()

    report = build_report(code, name, spot, info,
                          kline_daily, kline_quarterly,
                          valuation, financials, valuation_history)

    outpath = OUTPUT_BASE / f"{code}.md"
    outpath.write_text(report, encoding="utf-8")

    # 快速摘要
    ok = sum(1 for x in [spot, info, not kline_daily.empty, not kline_quarterly.empty,
                          not valuation.empty, any(not frame.empty for frame in financials.values()),
                          bool(valuation_history)] if x)
    print(f"\n  ✅ 报告 ({ok}/7 数据集可用) → {outpath}")
    print(f"{'='*55}")
    return str(outpath)


def main():
    p = argparse.ArgumentParser(description="AKShare 个股基本面+行情数据采集")
    p.add_argument("--stock", required=True, help="股票代码 (如 603290)")
    p.add_argument("--name", help="股票名称 (选填)")
    p.add_argument("--kline-file", type=Path, help="本次流水线共享的日K文件")
    args = p.parse_args()

    codes = [c.strip() for c in args.stock.split(",")]
    for code in codes:
        try:
            analyze_stock(code, args.name, args.kline_file)
        except Exception as e:
            print(f"[Error] {code}: {e}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
