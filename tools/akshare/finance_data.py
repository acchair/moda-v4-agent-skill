"""
个股基本面 + 行情数据模块
========================
集成 easy_tdx、BaoStock、Sina、efinance 和 AKShare 获取 A 股行情与基本面数据，
输出结构化 Markdown 报告供莫大 persona 参考。

数据源优先级: easy_tdx/TDX/Sina → BaoStock → efinance/AKShare
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
from tools.data_call import dataframe_empty, run_fallback_chain, run_with_timeout
from tools.akshare.business_data import build_structured as build_business_structured
from tools.akshare.business_data import fetch_business_data
from tools.providers.kline_quality import validate_kline_frame
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
        "pay_fixed_assets_etc_cash": "购建固定资产、无形资产和其他长期资产支付的现金",
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
    """日K线: 本次共享缓存 → easy_tdx → BaoStock → 东财 → 新浪 → 腾讯。"""
    if kline_file and kline_file.stem == code and kline_file.exists():
        df, issues = validate_kline_frame(pd.read_csv(kline_file), minimum_rows=60, max_age_days=14)
        meta_path = kline_file.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                meta = {}
        print(f"  [日K] 共享缓存 → {len(df)} 条")
        df = _with_frame_meta(
            df,
            fetch_state=str(meta.get("fetch_state") or "ok"),
            source_chain=list(meta.get("source_chain") or [{"source": "shared-cache", "status": "ok", "error": ""}]),
            error=str(meta.get("fetch_error") or ""),
        )
        df.attrs["quality_issues"] = list(dict.fromkeys([*(meta.get("quality_issues") or []), *issues]))
        return df

    def easy_tdx() -> pd.DataFrame:
        from tools.providers.easy_tdx_provider import fetch_kline_daily as fetch_easy_tdx_kline
        return _normalize_daily(fetch_easy_tdx_kline(code))

    def baostock() -> pd.DataFrame:
        from tools.providers.baostock_provider import fetch_kline_daily as fetch_baostock_kline
        return _normalize_daily(fetch_baostock_kline(code))

    def eastmoney() -> pd.DataFrame:
        return _normalize_daily(ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq"))

    def sina() -> pd.DataFrame:
        pfx = "sh" if code[0] == "6" else "sz"
        frame = _normalize_daily(ak.stock_zh_a_daily(symbol=f"{pfx}{code}", adjust="qfq"))
        if "pct_chg" in frame.columns and frame["pct_chg"].isna().all() and "close" in frame.columns:
            frame["pct_chg"] = frame["close"].pct_change() * 100
        return frame

    def tencent() -> pd.DataFrame:
        from tools.providers.tencent_provider import fetch_kline_daily as fetch_tencent_kline
        return _normalize_daily(fetch_tencent_kline(code))

    result = run_fallback_chain(
        "日K线",
        [
            ("easy_tdx", easy_tdx),
            ("BaoStock", baostock),
            ("AKShare/东方财富", eastmoney),
            ("AKShare/Sina", sina),
            ("Tencent/ifzq", tencent),
        ],
        seconds=25,
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
    """实时行情: easy_tdx 单股查询，回退 efinance 和腾讯单股查询。"""
    def easy_tdx() -> dict:
        from tools.providers.easy_tdx_provider import fetch_realtime_quote
        return fetch_realtime_quote(code)

    def efinance() -> dict:
        from tools.efinance.provider import fetch_realtime_quotes
        return fetch_realtime_quotes(code)

    def tencent() -> dict:
        from tools.providers.tencent_provider import fetch_realtime_quote
        quote = fetch_realtime_quote(code)
        if quote.get("quote_stale_suspect"):
            raise ValueError("腾讯行情疑似陈旧：零成交且最新价等于昨收")
        return quote

    result = run_fallback_chain(
        "实时行情",
        [("easy_tdx", easy_tdx), ("efinance", efinance), ("Tencent/qt.gtimg.cn", tencent)],
        seconds=8,
        empty=lambda value: not bool(value),
    )
    payload = dict(result.value or {})
    if result.ok:
        print(f"  [行情] {result.source} → {payload.get('最新价', 'N/A')}")
    payload.update({"_fetch_state": result.fetch_state, "_source_chain": result.source_chain or [], "_fetch_error": result.error or None})
    return payload


def fetch_company_and_peers(code: str) -> tuple[dict, pd.DataFrame]:
    """Use the TDX snapshot for valuation, scoped to a verified SW level-2 industry."""
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
    peers.attrs["peer_scope_status"] = "需人工确认"
    peers.attrs["peer_scope_source"] = "通达信行业板块（尚未确认申万二级）"
    try:
        from tools.akshare.industry_prosperity import map_industry

        sw_second = ak.sw_index_second_info()
        mapping = map_industry(info["行业"], {"sw_second": sw_second.to_dict("records"), "sw_first": []})
        sw_code = str(mapping.get("sw_second_code") or "").replace(".SI", "")
        if mapping.get("status") == "已验证" and sw_code:
            components = ak.index_component_sw(symbol=sw_code)
            component_column = next((item for item in ("证券代码", "成分券代码", "股票代码") if item in components.columns), None)
            component_codes = set(components[component_column].astype(str).str.zfill(6)) if component_column else set()
            if code in component_codes:
                peers = peers[peers["代码"].isin(component_codes)].copy()
                peers.attrs["peer_scope_status"] = "已验证"
                peers.attrs["peer_scope_source"] = "申万宏源研究/申万二级成分股"
                peers.attrs["sw_second_name"] = mapping.get("sw_second_name")
                peers.attrs["sw_second_code"] = mapping.get("sw_second_code")
                info.update({
                    "申万二级": mapping.get("sw_second_name"),
                    "申万二级代码": mapping.get("sw_second_code"),
                    "同行候选口径": "同一申万二级行业",
                })
    except Exception as exc:
        print(f"  [同行/申万二级] 确认失败: {type(exc).__name__}: {exc}")
    print(f"  [同行] {info.get('申万二级') or board['board_name']} → {len(peers)} 家")
    return info, peers


def _akshare_market_symbol(code: str) -> str:
    """Return the market-prefixed code required by EastMoney peer endpoints."""
    normalized = str(code).zfill(6)
    market = "SH" if normalized.startswith(("6", "9")) else "BJ" if normalized.startswith(("4", "8")) else "SZ"
    return f"{market}{normalized}"


def _comparison_code(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _comparison_value(value: object) -> object:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.notna(number):
        return float(number)
    text = str(value).strip()
    return text or None


PEER_COMPARISON_FIELDS = {
    "scale": {
        "总市值排名": "market_cap_rank",
        "流通市值排名": "float_market_cap_rank",
        "营业收入排名": "revenue_rank",
        "净利润排名": "net_profit_rank",
    },
    "growth": {
        "基本每股收益增长率-3年复合": "eps_growth_3y",
        "基本每股收益增长率-TTM": "eps_growth_ttm",
        "基本每股收益增长率-3年复合排名": "eps_growth_3y_rank",
        "营业收入增长率-3年复合": "revenue_growth_3y",
        "营业收入增长率-TTM": "revenue_growth_ttm",
        "净利润增长率-3年复合": "net_profit_growth_3y",
        "净利润增长率-TTM": "net_profit_growth_ttm",
    },
    "dupont": {
        "ROE-3年平均": "roe_3y_avg",
        "ROE-3年平均排名": "roe_3y_rank",
        "净利率-3年平均": "net_margin_3y_avg",
        "总资产周转率-3年平均": "asset_turnover_3y_avg",
        "权益乘数-3年平均": "equity_multiplier_3y_avg",
    },
    "valuation": {
        "排名": "valuation_rank",
        "市盈率-TTM": "pe_ttm",
        "市净率-MRQ": "pb_mrq",
        "PEG": "peg",
        "市销率-TTM": "ps_ttm",
    },
}


def _comparison_metric_subset(row: pd.Series, comparison: str) -> dict[str, object]:
    fields = PEER_COMPARISON_FIELDS.get(comparison, {})
    result = {
        target: value
        for source, target in fields.items()
        if (value := _comparison_value(row.get(source))) is not None
    }
    return result


def _comparison_status(results: dict[str, object]) -> str:
    states = [str(getattr(result, "fetch_state", "failed")) for result in results.values()]
    if any(state == "ok" for state in states):
        return "ok"
    if states and all(state == "empty" for state in states):
        return "empty"
    return "failed"


def fetch_industry_peer_snapshot(
    code: str,
    timeout: int = 8,
    comparisons: tuple[str, ...] = ("scale", "growth", "valuation", "dupont"),
) -> dict:
    """Fetch B-tier EastMoney peer snapshots without redefining direct peers.

    The scale endpoint intentionally returns only the target row and its
    industry ranks.  The growth, valuation and DuPont endpoints return a small
    ranked sample plus industry average/median; those rows are useful for
    screening but are *not* a verified peer universe.
    """
    symbol = _akshare_market_symbol(code)
    available_endpoints = {
        "scale": ak.stock_zh_scale_comparison_em,
        "growth": ak.stock_zh_growth_comparison_em,
        "valuation": ak.stock_zh_valuation_comparison_em,
        "dupont": ak.stock_zh_dupont_comparison_em,
    }
    requested = tuple(dict.fromkeys(comparisons))
    invalid = [item for item in requested if item not in available_endpoints]
    if invalid:
        raise ValueError(f"未知同行比较类型：{'、'.join(invalid)}")
    endpoints = {kind: available_endpoints[kind] for kind in requested}
    if not endpoints:
        raise ValueError("至少需要一个同行比较类型")

    def fetch(kind: str, function) -> object:
        return run_with_timeout(
            f"AKShare同行比较/{kind}",
            lambda: function(symbol=symbol),
            seconds=timeout,
            source="AKShare/东方财富同行比较",
            empty=dataframe_empty,
        )

    with ThreadPoolExecutor(max_workers=len(endpoints)) as executor:
        futures = {kind: executor.submit(fetch, kind, function) for kind, function in endpoints.items()}
        results = {kind: future.result() for kind, future in futures.items()}

    frames = {
        kind: result.value if result.ok and isinstance(result.value, pd.DataFrame) else pd.DataFrame()
        for kind, result in results.items()
    }
    target: dict[str, object] = {"code": str(code).zfill(6)}
    benchmarks: dict[str, dict[str, dict[str, object]]] = {}
    samples: dict[str, dict[str, object]] = {}
    for kind, frame in frames.items():
        if frame.empty or "代码" not in frame.columns:
            continue
        for _, row in frame.iterrows():
            raw_code = str(row.get("代码") or "").strip()
            normalized_code = _comparison_code(raw_code)
            metrics = _comparison_metric_subset(row, kind)
            if normalized_code == str(code).zfill(6):
                target.update(metrics)
                target.setdefault("name", str(row.get("简称") or ""))
                continue
            if raw_code in {"行业平均", "行业中值"}:
                benchmarks.setdefault(kind, {})[raw_code] = metrics
                continue
            if not normalized_code:
                continue
            sample = samples.setdefault(
                normalized_code,
                {
                    "code": normalized_code,
                    "name": str(row.get("简称") or normalized_code),
                    "status": "行业指标样本，需主营与产业链位置确认",
                    "source": "AKShare/东方财富同行比较（B级；非完整同行池）",
                },
            )
            sample.update(metrics)

    status = _comparison_status(results)
    source_chain = {
        kind: {
            "fetch_state": result.fetch_state,
            "source_chain": result.source_chain or [],
            "error": result.error or None,
        }
        for kind, result in results.items()
    }
    return {
        "status": "已获取" if status == "ok" and len(target) > 1 else "需人工确认",
        "fetch_state": status,
        "source": "AKShare/东方财富同行比较",
        "source_tier": "B",
        "scope": "行业横截面排名、均值/中值和少量排序样本；不等于申万完整成分或已验证直接同行",
        "symbol": symbol,
        "target": target,
        "industry_benchmarks": benchmarks,
        "peer_samples": list(samples.values()),
        "source_chain": source_chain,
    }


def _enrich_direct_peers_with_snapshot(rows: list[dict], snapshot: dict | None) -> list[dict]:
    if not rows or not isinstance(snapshot, dict) or snapshot.get("fetch_state") != "ok":
        return rows
    sample_map = {
        str(item.get("code") or "").zfill(6): item
        for item in snapshot.get("peer_samples", [])
        if isinstance(item, dict)
    }
    for row in rows:
        metrics = sample_map.get(str(row.get("code") or "").zfill(6))
        if not metrics:
            continue
        row["industry_snapshot_metrics"] = {
            key: value
            for key, value in metrics.items()
            if key not in {"code", "name", "status", "source"}
        }
        row["industry_snapshot_source"] = metrics.get("source")
    return rows


def fetch_financial_report(code: str, report_type: str) -> pd.DataFrame:
    """财报: easy_tdx/Sina → AKShare/Sina → AKShare/同花顺。"""
    indicator = {"lrb": "利润表", "fzb": "资产负债表", "llb": "现金流量表"}.get(report_type, report_type)

    def easy_sina() -> pd.DataFrame:
        from tools.providers.easy_tdx_provider import fetch_financial_report as fetch_sina_report
        return _financial_aliases(fetch_sina_report(code, report_type, num=20))

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
        return _normalize_ths_financial_report(function(symbol=code, indicator="按报告期"), report_type).head(20)

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


def fetch_baostock_financial_summary(code: str) -> pd.DataFrame:
    """Fetch B-tier summary metrics for cross-checking and limited last-resort filling."""
    def baostock() -> pd.DataFrame:
        from tools.providers.baostock_provider import fetch_financial_summary

        return fetch_financial_summary(code)

    result = run_fallback_chain(
        "BaoStock财务摘要",
        [("BaoStock/B级财务摘要", baostock)],
        seconds=15,
        empty=dataframe_empty,
    )
    frame = result.value if isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    frame = _with_frame_meta(
        frame,
        fetch_state=result.fetch_state,
        source_chain=result.source_chain,
        error=result.error,
    )
    frame.attrs.update({
        "source_tier": "B",
        "evidence_role": "cross_check_and_partial_fallback",
    })
    return frame


def _normalize_valuation_history(frame: pd.DataFrame, column: str, source: str) -> pd.DataFrame:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.DataFrame(columns=["date", "value"])
    date_column = next((item for item in ("数据日期", "date", "日期") if item in frame.columns), None)
    if date_column is None:
        return pd.DataFrame(columns=["date", "value"])
    result = frame[[date_column, column]].rename(columns={date_column: "date", column: "value"}).copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["value"] = pd.to_numeric(result["value"], errors="coerce")
    result = result.dropna(subset=["date", "value"]).sort_values("date").drop_duplicates("date", keep="last")
    result.attrs["source"] = source
    return result.reset_index(drop=True)


def fetch_historical_valuation(code: str) -> dict[str, pd.DataFrame]:
    """Multi-source valuation history plus share-capital details."""
    results: dict[str, pd.DataFrame] = {}
    eastmoney = pd.DataFrame()
    eastmoney_error = ""
    try:
        eastmoney = ak.stock_value_em(symbol=code)
        if eastmoney is not None and not eastmoney.empty:
            cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(years=5)
            dates = pd.to_datetime(eastmoney.get("数据日期"), errors="coerce")
            eastmoney = eastmoney.loc[dates.ge(cutoff)].copy()
            eastmoney.attrs["source"] = "AKShare/东方财富估值分析"
            results["capital"] = eastmoney
    except Exception as exc:
        eastmoney_error = f"{type(exc).__name__}: {exc}"

    for key, indicator, em_column in (("pe", "市盈率(TTM)", "PE(TTM)"), ("pb", "市净率", "市净率")):
        primary = _normalize_valuation_history(eastmoney, em_column, "AKShare/东方财富估值分析")
        chain = [{"source": "AKShare/东方财富估值分析", "status": "ok" if not primary.empty else "failed", "error": eastmoney_error}]
        frame = primary
        if frame.empty:
            try:
                fallback = ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近五年")
                frame = _normalize_valuation_history(fallback, "value", "AKShare/百度估值")
                chain.append({"source": "AKShare/百度估值", "status": "ok" if not frame.empty else "empty", "error": ""})
            except Exception as exc:
                chain.append({"source": "AKShare/百度估值", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        if not frame.empty:
            frame.attrs["source_chain"] = chain
            results[key] = frame
        else:
            print(f"  [历史估值/{indicator}] 失败: {chain[-1]['error'] or 'empty'}")
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


def _history_quantiles(frame: pd.DataFrame, minimum: int = 250) -> dict[str, float] | None:
    if frame is None or frame.empty or "value" not in frame.columns:
        return None
    values = pd.to_numeric(frame["value"], errors="coerce").dropna()
    values = values[values > 0]
    if len(values) < minimum:
        return None
    return {
        "q20": float(values.quantile(0.20)),
        "q50": float(values.quantile(0.50)),
        "q80": float(values.quantile(0.80)),
        "samples": int(len(values)),
    }


def _business_signature(structured: dict) -> set[str]:
    text = "".join(str(item) for item in structured.get("business_items") or [])
    text = "".join(character for character in text if "\u4e00" <= character <= "\u9fff")
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


def _business_similarity(left: dict, right: dict) -> float:
    left_signature, right_signature = _business_signature(left), _business_signature(right)
    if not left_signature or not right_signature:
        return 0.0
    return len(left_signature & right_signature) / len(left_signature | right_signature)


CHAIN_POSITION_KEYWORDS = {
    "设备": ("设备", "仪器", "机器", "系统", "平台", "产线"),
    "零部件/材料": ("零部件", "组件", "部件", "材料", "芯片", "模组", "器件"),
    "耗材/试剂": ("耗材", "试剂", "试剂盒", "药盒"),
    "软件/服务": ("软件", "服务", "检测", "诊断", "运营", "维护"),
    "产品": ("药品", "制剂", "食品", "整车", "电池", "产品"),
}


def _chain_positions(structured: dict) -> set[str]:
    text = "".join(str(item) for item in structured.get("business_items") or [])
    return {
        position
        for position, keywords in CHAIN_POSITION_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    }


def _latest_peer_metric(frame: pd.DataFrame, *columns: str) -> float | None:
    column = next((item for item in columns if item in frame.columns), None)
    if frame.empty or column is None:
        return None
    value = pd.to_numeric(pd.Series([frame.iloc[0][column]]), errors="coerce").iloc[0]
    return None if pd.isna(value) else float(value)


def collect_direct_peers(code: str, valuation: pd.DataFrame, maximum: int = 3) -> list[dict]:
    """Confirm direct peers with product overlap, then collect comparable facts."""
    if valuation.empty or "代码" not in valuation.columns or valuation.attrs.get("peer_scope_status") != "已验证":
        return []
    normalized_codes = valuation["代码"].astype(str).str.zfill(6)
    candidate_rows = valuation.loc[~normalized_codes.eq(code)].head(8)
    candidate_codes = [str(item).zfill(6) for item in candidate_rows["代码"].tolist()]
    try:
        with ThreadPoolExecutor(max_workers=min(7, len(candidate_codes) + 1)) as executor:
            business_futures = {
                peer_code: executor.submit(fetch_business_data, peer_code, 10)
                for peer_code in [code, *candidate_codes]
            }
            business = {
                peer_code: build_business_structured(future.result())
                for peer_code, future in business_futures.items()
            }
    except Exception as exc:
        print(f"  [直接同行] 主营确认失败: {type(exc).__name__}: {exc}")
        return []

    target_positions = _chain_positions(business.get(code, {}))
    ranked = sorted(
        (
            (
                peer_code,
                _business_similarity(business.get(code, {}), business.get(peer_code, {})),
                _chain_positions(business.get(peer_code, {})),
            )
            for peer_code in candidate_codes
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    selected = [
        (peer_code, similarity, positions)
        for peer_code, similarity, positions in ranked
        if similarity >= 0.12 and target_positions and target_positions & positions
    ][:maximum]
    if not selected:
        return []

    financials: dict[str, dict[str, pd.DataFrame]] = {peer_code: {} for peer_code, _, _ in selected}
    try:
        with ThreadPoolExecutor(max_workers=len(selected) * 3) as executor:
            futures = {
                (peer_code, report_type): executor.submit(fetch_financial_report, peer_code, report_type)
                for peer_code, _, _ in selected for report_type in ("lrb", "fzb", "llb")
            }
            for (peer_code, report_type), future in futures.items():
                financials[peer_code][report_type] = future.result()
    except Exception as exc:
        print(f"  [直接同行] 财务确认部分失败: {type(exc).__name__}: {exc}")

    rows: list[dict] = []
    for peer_code, similarity, positions in selected:
        valuation_row = candidate_rows[candidate_rows["代码"].astype(str).str.zfill(6).eq(peer_code)]
        item = valuation_row.iloc[0] if not valuation_row.empty else pd.Series(dtype=object)
        peer_finance = financials.get(peer_code, {})
        income, cashflow = peer_finance.get("lrb", pd.DataFrame()), peer_finance.get("llb", pd.DataFrame())
        breakdown = business.get(peer_code, {}).get("business_breakdown") or []
        margins = [row.get("gross_margin") for row in breakdown if isinstance(row, dict) and row.get("gross_margin") is not None]
        verified = business.get(peer_code, {}).get("fetch_state") in {"ok", "fallback_ok"} and not income.empty
        rows.append({
            "code": peer_code,
            "name": str(item.get("简称") or peer_code),
            "business_similarity": round(similarity, 4),
            "main_business": business.get(peer_code, {}).get("main_business", "需人工确认"),
            "chain_positions": sorted(positions),
            "chain_position_match": sorted(target_positions & positions),
            "revenue": _latest_peer_metric(income, "营业收入"),
            "revenue_yoy": _latest_peer_metric(income, "营业收入_同比"),
            "net_profit": _latest_peer_metric(income, "归属于母公司的净利润", "归属于母公司所有者的净利润"),
            "profit_yoy": _latest_peer_metric(income, "归属于母公司的净利润_同比", "归属于母公司所有者的净利润_同比"),
            "operating_cashflow": _latest_peer_metric(cashflow, "经营活动产生的现金流量净额"),
            "gross_margin": max(margins) if margins else None,
            "pe_ttm": None if pd.isna(item.get("市盈率-TTM")) else float(item.get("市盈率-TTM")),
            "pb": None if pd.isna(item.get("市净率")) else float(item.get("市净率")),
            "status": "已验证" if verified else "需人工确认",
            "industry_scope": valuation.attrs.get("sw_second_name"),
            "source": "申万二级成分股 + 主营关键词 + 产业链位置 + 公开财报",
        })
    return rows


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


def _statement_period(frame: pd.DataFrame) -> pd.Timestamp | None:
    if frame is None or frame.empty or "报告期" not in frame.columns:
        return None
    value = pd.to_datetime(frame.iloc[0].get("报告期"), errors="coerce")
    return None if pd.isna(value) else pd.Timestamp(value).normalize()


def _summary_number(row: pd.Series, column: str) -> float | None:
    value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
    return None if pd.isna(value) else float(value)


def _apply_baostock_summary(
    metrics: dict,
    financials: dict[str, pd.DataFrame],
    summary: pd.DataFrame,
) -> None:
    """Cross-check matching periods and fill only fields absent from full statements."""
    if summary is None or summary.empty:
        return
    ordered = summary.copy()
    ordered["statDate"] = pd.to_datetime(ordered.get("statDate"), errors="coerce")
    ordered = ordered.dropna(subset=["statDate"]).sort_values("statDate", ascending=False)
    if ordered.empty:
        return
    latest_row = ordered.iloc[0]
    latest_date = pd.Timestamp(latest_row["statDate"]).normalize()
    source = "BaoStock/B级财务摘要"
    overrides = metrics.setdefault("metric_source_overrides", {})
    fallback_fields: list[str] = []
    for target, source_column, report_type in (
        ("net_profit", "netProfit", "lrb"),
        ("debt_ratio", "liabilityToAsset", "fzb"),
        ("operating_cashflow_to_net_profit", "CFOToNP", "llb"),
        ("total_shares", "totalShare", None),
        ("float_shares", "liqaShare", None),
    ):
        value = _summary_number(latest_row, source_column)
        primary_period = _statement_period(financials.get(report_type, pd.DataFrame())) if report_type else None
        period_compatible = primary_period is None or primary_period == latest_date
        if metrics.get(target) is None and value is not None and period_compatible:
            metrics[target] = value
            overrides[target] = f"{source}（末级部分补缺）"
            fallback_fields.append(target)

    comparisons: dict[str, dict] = {}
    specs = (
        ("net_profit", "netProfit", "lrb"),
        ("debt_ratio", "liabilityToAsset", "fzb"),
        ("operating_cashflow_to_net_profit", "CFOToNP", "llb"),
    )
    for metric_key, source_column, report_type in specs:
        primary_period = _statement_period(financials.get(report_type, pd.DataFrame()))
        if primary_period is None:
            continue
        if metric_key == "operating_cashflow_to_net_profit":
            income_period = _statement_period(financials.get("lrb", pd.DataFrame()))
            if income_period is not None and income_period != primary_period:
                comparisons[metric_key] = {
                    "status": "period_mismatch",
                    "income_period": income_period.date().isoformat(),
                    "cash_flow_period": primary_period.date().isoformat(),
                }
                continue
        match = ordered[ordered["statDate"].dt.normalize().eq(primary_period)]
        if match.empty:
            comparisons[metric_key] = {
                "status": "period_mismatch",
                "primary_period": primary_period.date().isoformat(),
                "baostock_latest_period": latest_date.date().isoformat(),
            }
            continue
        primary_value = metrics.get(metric_key)
        bao_value = _summary_number(match.iloc[0], source_column)
        if primary_value is None or bao_value is None:
            continue
        difference = abs(float(primary_value) - bao_value) / max(abs(float(primary_value)), 1e-12)
        comparisons[metric_key] = {
            "status": "matched" if difference <= 0.05 else "mismatch",
            "primary": float(primary_value),
            "baostock": bao_value,
            "relative_difference": difference,
            "stat_date": primary_period.date().isoformat(),
        }

    comparison_states = {item.get("status") for item in comparisons.values()}
    status = (
        "mismatch" if "mismatch" in comparison_states
        else "matched" if "matched" in comparison_states
        else "partial_fallback" if fallback_fields
        else "period_mismatch" if "period_mismatch" in comparison_states
        else "available"
    )
    pub_date = pd.to_datetime(latest_row.get("pubDate"), errors="coerce")
    metrics["baostock_financial_crosscheck"] = {
        "status": status,
        "source": source,
        "source_tier": "B",
        "role": "cross_check_and_partial_fallback",
        "latest_stat_date": latest_date.date().isoformat(),
        "latest_pub_date": "" if pd.isna(pub_date) else pd.Timestamp(pub_date).date().isoformat(),
        "fallback_fields": fallback_fields,
        "comparisons": comparisons,
    }


def _report_metrics(code: str, spot: dict, info: dict, kline_daily: pd.DataFrame,
                    valuation: pd.DataFrame, financials: dict[str, pd.DataFrame],
                    valuation_history: dict[str, pd.DataFrame] | None = None,
                    direct_peers: list[dict] | None = None,
                    baostock_summary: pd.DataFrame | None = None,
                    industry_peer_snapshot: dict | None = None) -> dict:
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
        "kline_quality_issues": kline_daily.attrs.get("quality_issues", []),
        "financial_fetch_state": {
            key: frame.attrs.get("fetch_state", "failed") for key, frame in financials.items()
        },
        "financial_source_chain": {
            key: frame.attrs.get("source_chain", []) for key, frame in financials.items()
        },
        "baostock_financial_fetch_state": (
            baostock_summary.attrs.get("fetch_state", "failed")
            if isinstance(baostock_summary, pd.DataFrame) else "failed"
        ),
        "baostock_financial_source_chain": (
            baostock_summary.attrs.get("source_chain", [])
            if isinstance(baostock_summary, pd.DataFrame) else []
        ),
    }
    if direct_peers:
        metrics["peer_comparison"] = direct_peers
    if isinstance(industry_peer_snapshot, dict):
        metrics["industry_peer_snapshot"] = industry_peer_snapshot
        metrics.setdefault("metric_source_overrides", {})["industry_peer_snapshot"] = (
            "AKShare/东方财富同行比较"
        )
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
    if cash is not None and interest_debt is not None:
        metrics["net_debt"] = interest_debt - cash
        if assets and assets > 0:
            metrics["net_cash_ratio"] = (cash - interest_debt) / assets
    if cash is not None and short_debt is not None:
        metrics["cash_to_short_debt"] = cash / short_debt if short_debt > 0 else 999.0
    operating_cashflow = metrics.get("operating_cashflow")
    net_profit = metrics.get("net_profit")
    capex_cash_paid = latest(
        "llb",
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "购建固定资产、无形资产和其他长期资产所支付的现金",
    )
    if capex_cash_paid is not None:
        metrics["capex_cash_paid"] = capex_cash_paid
    if operating_cashflow is not None and capex_cash_paid is not None:
        metrics["free_cash_flow"] = operating_cashflow - capex_cash_paid
    if operating_cashflow is not None and net_profit is not None and net_profit > 0:
        metrics["operating_cashflow_to_net_profit"] = operating_cashflow / net_profit
        if metrics.get("free_cash_flow") is not None:
            metrics["free_cash_flow_to_net_profit"] = metrics["free_cash_flow"] / net_profit
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
        peer_candidates = []
        for _, row in valuation.loc[~valuation["代码"].eq(code)].head(8).iterrows():
            peer_candidates.append({
                "code": str(row.get("代码") or "").zfill(6),
                "name": str(row.get("简称") or "同行候选"),
                "pe_ttm": None if pd.isna(row.get("市盈率-TTM")) else float(row.get("市盈率-TTM")),
                "pb": None if pd.isna(row.get("市净率")) else float(row.get("市净率")),
                "market_cap": None if pd.isna(row.get("总市值")) else float(row.get("总市值")),
                "eps": None if pd.isna(row.get("每股收益")) else float(row.get("每股收益")),
                "status": "行业候选，需主营与产业链位置确认",
                "source": "同一行业板块结构化行情",
            })
        if peer_candidates:
            metrics["peer_candidates"] = peer_candidates
    valuation_history = valuation_history or {}
    capital = valuation_history.get("capital", pd.DataFrame())
    if not capital.empty:
        latest_capital = capital.sort_values("数据日期").iloc[-1]
        for column, key in (
            ("总股本", "total_shares"),
            ("流通股本", "float_shares"),
            ("总市值", "market_cap"),
            ("流通市值", "float_market_cap"),
        ):
            value = pd.to_numeric(pd.Series([latest_capital.get(column)]), errors="coerce").iloc[0]
            if pd.notna(value):
                metrics[key] = float(value)
    if isinstance(baostock_summary, pd.DataFrame):
        _apply_baostock_summary(metrics, financials, baostock_summary)
    metrics["valuation_history_source_chain"] = {
        key: frame.attrs.get("source_chain", []) for key, frame in valuation_history.items() if key in {"pe", "pb"}
    }
    pe_percentile = _history_percentile(valuation_history.get("pe", pd.DataFrame()))
    pb_percentile = _history_percentile(valuation_history.get("pb", pd.DataFrame()))
    pb_median_ratio = _history_median_ratio(valuation_history.get("pb", pd.DataFrame()))
    pe_quantiles = _history_quantiles(valuation_history.get("pe", pd.DataFrame()))
    pb_quantiles = _history_quantiles(valuation_history.get("pb", pd.DataFrame()))
    if pe_percentile is not None:
        metrics["pe_percentile_5y"] = pe_percentile
    if pb_percentile is not None:
        metrics["pb_percentile_5y"] = pb_percentile
    if pb_median_ratio is not None:
        metrics["pb_to_5y_median"] = pb_median_ratio
    if pe_quantiles is not None:
        metrics["pe_history_quantiles_5y"] = pe_quantiles
    if pb_quantiles is not None:
        metrics["pb_history_quantiles_5y"] = pb_quantiles
    kline_consistent = True
    if not kline_daily.empty and "close" in kline_daily.columns:
        latest_kline = pd.to_numeric(pd.Series([kline_daily.iloc[-1].get("close")]), errors="coerce").iloc[0]
        latest_quote = pd.to_numeric(pd.Series([spot.get("最新价") if spot else None]), errors="coerce").iloc[0]
        if pd.notna(latest_kline):
            metrics["kline_latest_close"] = float(latest_kline)
        if pd.notna(latest_kline) and pd.notna(latest_quote) and latest_quote > 0:
            deviation = abs(float(latest_kline) / float(latest_quote) - 1)
            metrics["kline_quote_deviation"] = deviation
            if deviation > 0.10:
                kline_consistent = False
                metrics["kline_sanity_status"] = "warning"
                metrics["kline_sanity_reason"] = "日K末值与实时行情偏差超过10%，价格分位不参与判断"
            else:
                metrics["kline_sanity_status"] = "ok"
    if kline_consistent and not kline_daily.empty and "close" in kline_daily.columns:
        close = pd.to_numeric(kline_daily["close"], errors="coerce").dropna().tail(800)
        if len(close) >= 720:
            latest_close, low, high = float(close.iloc[-1]), float(close.min()), float(close.max())
            if high > low:
                metrics["price_percentile_3y"] = (latest_close - low) / (high - low)
                metrics["drawdown_from_3y_high"] = latest_close / high - 1
                metrics["price_history_quantiles_3y"] = {
                    "q20": float(close.quantile(0.20)),
                    "q50": float(close.quantile(0.50)),
                    "q80": float(close.quantile(0.80)),
                    "samples": int(len(close)),
                }
    kline_sources = {
        str(item.get("source") or "")
        for item in metrics.get("kline_source_chain", [])
        if item.get("status") == "ok"
    }
    if "BaoStock" in kline_sources:
        overrides = metrics.setdefault("metric_source_overrides", {})
        for key in (
            "kline_latest_close",
            "kline_quote_deviation",
            "price_percentile_3y",
            "drawdown_from_3y_high",
            "price_history_quantiles_3y",
        ):
            if key in metrics:
                overrides[key] = "BaoStock/B级行情"
    states = [metrics.get("quote_fetch_state"), metrics.get("kline_fetch_state"), *metrics.get("financial_fetch_state", {}).values()]
    metrics["fetch_state"] = "failed" if any(state == "failed" for state in states) else "fallback_ok" if any(state == "fallback_ok" for state in states) else "empty" if all(state in {"empty", "failed"} for state in states) else "ok"
    metrics["source_chain"] = {
        "quote": metrics.get("quote_source_chain", []),
        "kline": metrics.get("kline_source_chain", []),
        "financial": metrics.get("financial_source_chain", {}),
        "baostock_financial_summary": metrics.get("baostock_financial_source_chain", []),
        "industry_peer_snapshot": (
            metrics.get("industry_peer_snapshot", {}).get("source_chain", {})
            if isinstance(metrics.get("industry_peer_snapshot"), dict) else {}
        ),
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
                 valuation_history: dict[str, pd.DataFrame] | None = None,
                 direct_peers: list[dict] | None = None,
                 baostock_summary: pd.DataFrame | None = None,
                 industry_peer_snapshot: dict | None = None) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    metrics = _report_metrics(
        code,
        spot,
        info,
        kline_daily,
        valuation,
        financials,
        valuation_history,
        direct_peers,
        baostock_summary,
        industry_peer_snapshot,
    )
    L = [
        f"# 基本面+行情报告: {name}({code})",
        f"",
        f"> 采集时间: {ts}  |  数据源: easy_tdx/TDX/Sina + BaoStock + efinance/AKShare + Tencent",
        f"> 雪球: [个股页](https://xueqiu.com/S/{'SH' if code[0]=='6' else 'SZ'}{code})  "
        f"|  东财: [股吧](https://guba.eastmoney.com/list,{code},99,f.html)",
        f"",
        "---",
    ]
    L.append(f"<!-- moda_metrics: {json.dumps(metrics, ensure_ascii=False)} -->")

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

    L += ["### BaoStock 财务摘要交叉验证", ""]
    if isinstance(baostock_summary, pd.DataFrame) and not baostock_summary.empty:
        L += ["*来源: BaoStock（B级聚合；仅交叉验证和末级部分补缺，不替代完整三表）*  ", ""]
        summary_columns = [
            column for column in (
                "statDate", "pubDate", "netProfit", "roeAvg", "gpMargin",
                "liabilityToAsset", "CFOToNP", "totalShare", "liqaShare",
            ) if column in baostock_summary.columns
        ]
        L.append("| " + " | ".join(summary_columns) + " |")
        L.append("|" + "|".join(["------"] * len(summary_columns)) + "|")
        for _, row in baostock_summary.head(4).iterrows():
            L.append("| " + " | ".join(_safe_num(row.get(column)) for column in summary_columns) + " |")
        crosscheck = metrics.get("baostock_financial_crosscheck", {})
        L += ["", f"交叉验证状态：{crosscheck.get('status', 'available')}。", ""]
    else:
        L += ["⚠️ 无可用数据；不影响完整三表的既有采集状态。", ""]

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
    L += ["### 已确认直接同行", "", "| 公司 | 主营相似度 | 营收同比 | 利润同比 | 经营现金流 | 状态 |", "|---|---:|---:|---:|---:|---|"]
    for peer in direct_peers or []:
        L.append(f"| {peer.get('name')} | {_safe_num(peer.get('business_similarity'))} | {_safe_num(peer.get('revenue_yoy'))} | {_safe_num(peer.get('profit_yoy'))} | {_safe_num(peer.get('operating_cashflow'))} | {peer.get('status')} |")
    if not direct_peers:
        L.append("| 需人工确认 | - | - | - | - | 未取得主营与产业链位置均匹配的直接同行 |")
    L.append("")
    snapshot = metrics.get("industry_peer_snapshot") if isinstance(metrics.get("industry_peer_snapshot"), dict) else {}
    L += ["### AKShare/东方财富行业横截面快照", ""]
    if snapshot.get("fetch_state") == "ok" and isinstance(snapshot.get("target"), dict):
        target = snapshot["target"]
        ranks = [
            ("总市值行业排名", target.get("market_cap_rank")),
            ("流通市值行业排名", target.get("float_market_cap_rank")),
            ("营收行业排名", target.get("revenue_rank")),
            ("净利润行业排名", target.get("net_profit_rank")),
            ("ROE-3年平均排名", target.get("roe_3y_rank")),
            ("EPS增长3年复合排名", target.get("eps_growth_3y_rank")),
        ]
        L += [
            "*B级横截面数据，仅用于相对位置与轻量候选排序；不替代申万成分、主营和产业链位置的直接同行核验。*  ",
            "",
            "| 指标 | 行业排名 |",
            "|---|---:|",
        ]
        for label, value in ranks:
            if value is not None:
                L.append(f"| {label} | {_safe_num(value)} |")
        benchmarks = snapshot.get("industry_benchmarks") if isinstance(snapshot.get("industry_benchmarks"), dict) else {}
        growth_median = benchmarks.get("growth", {}).get("行业中值", {}) if isinstance(benchmarks.get("growth"), dict) else {}
        dupont_median = benchmarks.get("dupont", {}).get("行业中值", {}) if isinstance(benchmarks.get("dupont"), dict) else {}
        if growth_median or dupont_median:
            L += [
                "",
                "| 行业中值（可用字段） | 数值 |",
                "|---|---:|",
            ]
            for label, value in (
                ("营收增长TTM", growth_median.get("revenue_growth_ttm")),
                ("净利润增长TTM", growth_median.get("net_profit_growth_ttm")),
                ("ROE-3年平均", dupont_median.get("roe_3y_avg")),
            ):
                if value is not None:
                    L.append(f"| {label} | {_safe_num(value)} |")
    else:
        L.append("需人工确认：未取得可用的 AKShare/东方财富同行横截面快照。")
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
        "本报告基于 easy_tdx、BaoStock、Sina、efinance 和 AKShare 自动采集，仅供信息参考，不构成任何投资建议。",
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

    print("[1/3] 并行获取行情、行业同行、AKShare 横截面、三张财报和 BaoStock 摘要 ...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        spot_future = executor.submit(fetch_spot, code)
        company_future = executor.submit(fetch_company_and_peers, code)
        peer_snapshot_future = executor.submit(fetch_industry_peer_snapshot, code)
        financial_futures = {
            report_type: executor.submit(fetch_financial_report, code, report_type)
            for report_type in ("lrb", "fzb", "llb")
        }
        valuation_history_future = executor.submit(fetch_historical_valuation, code)
        baostock_summary_future = executor.submit(fetch_baostock_financial_summary, code)

        print("[2/3] 读取日K线 ...")
        kline_daily = fetch_kline_daily(code, kline_file)
        print("[3/3] 从日K生成季K ...")
        kline_quarterly = fetch_kline_quarterly(code, kline_daily)

        spot = spot_future.result()
        info, valuation = company_future.result()
        financials = {report_type: future.result() for report_type, future in financial_futures.items()}
        valuation_history = valuation_history_future.result()
        baostock_summary = baostock_summary_future.result()
        industry_peer_snapshot = peer_snapshot_future.result()

    direct_peers = collect_direct_peers(code, valuation)
    direct_peers = _enrich_direct_peers_with_snapshot(direct_peers, industry_peer_snapshot)
    report = build_report(code, name, spot, info,
                          kline_daily, kline_quarterly,
                          valuation, financials, valuation_history, direct_peers, baostock_summary,
                          industry_peer_snapshot)

    outpath = OUTPUT_BASE / f"{code}.md"
    outpath.write_text(report, encoding="utf-8")

    # 快速摘要
    ok = sum(1 for x in [spot, info, not kline_daily.empty, not kline_quarterly.empty,
                          not valuation.empty, any(not frame.empty for frame in financials.values()),
                          bool(valuation_history), not baostock_summary.empty,
                          industry_peer_snapshot.get("fetch_state") == "ok"] if x)
    print(f"\n  ✅ 报告 ({ok}/9 数据集可用) → {outpath}")
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
