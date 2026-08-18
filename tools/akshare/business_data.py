from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.data_call import dataframe_empty, run_fallback_chain, run_with_timeout
from tools.providers.eastmoney_transport import get as eastmoney_get
OUTPUT_BASE = ROOT / "knowledge" / "research" / "business_data"
TYPE_NAMES = {"1": "按行业分类", "2": "按产品分类", "3": "按地区分类"}
OVERSEAS_TERMS = ("国外", "境外", "海外", "外销", "国际")


def _security_code(code: str) -> str:
    return ("SH" if code.startswith(("6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ") + code


def _normalize_business_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    aliases = {
        "REPORT_DATE": "REPORT_DATE", "报告期": "REPORT_DATE", "报告日期": "REPORT_DATE",
        "MAINOP_TYPE": "MAINOP_TYPE", "分类类型": "MAINOP_TYPE", "分类": "MAINOP_TYPE",
        "ITEM_NAME": "ITEM_NAME", "项目": "ITEM_NAME", "主营构成": "ITEM_NAME", "主营业务": "ITEM_NAME",
        "MAIN_BUSINESS_INCOME": "MAIN_BUSINESS_INCOME", "主营收入": "MAIN_BUSINESS_INCOME", "主营业务收入": "MAIN_BUSINESS_INCOME",
        "MBI_RATIO": "MBI_RATIO", "收入比例": "MBI_RATIO", "收入占比": "MBI_RATIO",
        "GROSS_RPOFIT_RATIO": "GROSS_RPOFIT_RATIO", "毛利率": "GROSS_RPOFIT_RATIO", "主营利润率": "GROSS_RPOFIT_RATIO",
    }
    renamed = frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns}).copy()
    if "MAINOP_TYPE" in renamed.columns:
        type_map = {"按行业分类": "1", "按产品分类": "2", "按地区分类": "3", "行业": "1", "产品": "2", "地区": "3"}
        renamed["MAINOP_TYPE"] = renamed["MAINOP_TYPE"].astype(str).map(lambda value: type_map.get(value, value))
    for column in ("MAIN_BUSINESS_INCOME", "MBI_RATIO", "GROSS_RPOFIT_RATIO"):
        if column in renamed.columns:
            renamed[column] = pd.to_numeric(renamed[column], errors="coerce")
    if "REPORT_DATE" in renamed.columns:
        renamed["REPORT_DATE"] = pd.to_datetime(renamed["REPORT_DATE"], errors="coerce")
    wanted = ["REPORT_DATE", "MAINOP_TYPE", "ITEM_NAME", "MAIN_BUSINESS_INCOME", "MBI_RATIO", "GROSS_RPOFIT_RATIO"]
    for column in wanted:
        if column not in renamed.columns:
            renamed[column] = pd.NA
    return renamed[wanted].dropna(subset=["REPORT_DATE", "ITEM_NAME"], how="any")


def fetch_business_data(code: str, timeout: float = 15) -> pd.DataFrame:
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"

    def eastmoney() -> pd.DataFrame:
        response = eastmoney_get(url, params={"code": _security_code(code)}, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return _normalize_business_frame(pd.DataFrame(response.json().get("zygcfx", [])))

    def akshare() -> pd.DataFrame:
        import akshare as ak
        return _normalize_business_frame(ak.stock_zygc_em(symbol=_security_code(code)))

    result = run_fallback_chain("主营构成", [("东方财富/F10", eastmoney), ("AKShare/stock_zygc_em", akshare)], seconds=int(timeout), empty=dataframe_empty)
    frame = result.value if isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    frame.attrs["fetch_state"] = result.fetch_state
    frame.attrs["source_chain"] = result.source_chain or []
    frame.attrs["fetch_error"] = result.error or None
    return frame


def _text_value(value: object) -> str:
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _context_meta(result, *, source: str, source_tier: str, content: dict) -> dict:
    usable = result.ok and any(value for key, value in content.items() if key not in {"status", "source", "source_tier"})
    return {
        **content,
        "status": "已验证" if usable else "需人工确认",
        "fetch_state": result.fetch_state,
        "source": source,
        "source_tier": source_tier,
        "source_chain": result.source_chain or [],
        "error": result.error or None,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def fetch_company_profile_cninfo(code: str, timeout: int = 10) -> dict:
    """Fetch the CNINFO company profile as a company-identity fact."""
    import akshare as ak

    result = run_with_timeout(
        "公司概况/CNINFO",
        lambda: ak.stock_profile_cninfo(symbol=str(code).zfill(6)),
        seconds=timeout,
        source="AKShare/CNINFO公司概况",
        empty=dataframe_empty,
    )
    frame = result.value if result.ok and isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    row = frame.iloc[0] if not frame.empty else pd.Series(dtype=object)
    content = {
        "company_name": _text_value(row.get("公司名称")),
        "stock_name": _text_value(row.get("A股简称")),
        "industry": _text_value(row.get("所属行业")),
        "legal_representative": _text_value(row.get("法人代表")),
        "listing_date": _text_value(row.get("上市日期")),
        "main_business": _text_value(row.get("主营业务")),
        "business_scope": _text_value(row.get("经营范围")),
    }
    return _context_meta(
        result,
        source="AKShare/CNINFO公司概况",
        source_tier="A",
        content=content,
    )


def fetch_business_intro_ths(code: str, timeout: int = 10) -> dict:
    """Fetch the independent THS business description, not revenue composition."""
    import akshare as ak

    result = run_with_timeout(
        "主营介绍/同花顺",
        lambda: ak.stock_zyjs_ths(symbol=str(code).zfill(6)),
        seconds=timeout,
        source="AKShare/同花顺主营介绍",
        empty=dataframe_empty,
    )
    frame = result.value if result.ok and isinstance(result.value, pd.DataFrame) else pd.DataFrame()
    row = frame.iloc[0] if not frame.empty else pd.Series(dtype=object)
    content = {
        "main_business": _text_value(row.get("主营业务")),
        "product_types": _text_value(row.get("产品类型")),
        "product_names": _text_value(row.get("产品名称")),
        "business_scope": _text_value(row.get("经营范围")),
    }
    return _context_meta(
        result,
        source="AKShare/同花顺主营介绍",
        source_tier="B",
        content=content,
    )


def _ths_business_items(intro: dict) -> list[str]:
    values = [str(intro.get(key) or "") for key in ("main_business", "product_types", "product_names")]
    items: list[str] = []
    for value in values:
        for item in value.replace("；", "、").replace(";", "、").split("、"):
            text = item.strip()
            if text and text not in items:
                items.append(text)
    return items[:20]


def collect_business_context(code: str, timeout: int = 12) -> tuple[pd.DataFrame, dict]:
    """Collect the revenue table plus independent company/business context for V4."""
    with ThreadPoolExecutor(max_workers=3) as executor:
        composition_future = executor.submit(fetch_business_data, code, timeout)
        profile_future = executor.submit(fetch_company_profile_cninfo, code, timeout)
        ths_future = executor.submit(fetch_business_intro_ths, code, timeout)
        frame = composition_future.result()
        profile = profile_future.result()
        ths_intro = ths_future.result()

    structured = build_structured(frame)
    structured["company_profile"] = profile
    structured["business_intro_ths"] = ths_intro
    structured["business_crosscheck"] = {
        "status": (
            "双源可比对，需结合原文语义核验"
            if structured.get("main_business") and ths_intro.get("status") == "已验证"
            else "同花顺主营介绍补缺"
            if ths_intro.get("status") == "已验证"
            else "需人工确认"
        ),
        "eastmoney_main_business": structured.get("main_business") or "",
        "ths_main_business": ths_intro.get("main_business") or "",
        "source": "东方财富F10主营构成 + AKShare/同花顺主营介绍",
        "source_tier": "B",
    }
    overrides = dict(structured.get("metric_source_overrides") or {})
    overrides.update({
        "company_profile": "AKShare/CNINFO公司概况",
        "business_intro_ths": "AKShare/同花顺主营介绍",
        "business_crosscheck": "东方财富F10 + 同花顺主营介绍",
    })
    if not structured.get("main_business") and ths_intro.get("status") == "已验证":
        fallback_items = _ths_business_items(ths_intro)
        structured.update({
            "main_business": ths_intro.get("main_business") or "、".join(fallback_items[:8]),
            "business_items": fallback_items,
            "fetch_state": "fallback_ok",
            "business_fallback_reason": "东方财富主营构成不可用，已降级为同花顺主营介绍；无收入/毛利分部数据",
        })
        overrides.update({
            "main_business": "AKShare/同花顺主营介绍",
            "business_items": "AKShare/同花顺主营介绍",
        })
    structured["metric_source_overrides"] = overrides
    return frame, structured


def build_structured(frame: pd.DataFrame) -> dict:
    if frame.empty or frame["REPORT_DATE"].dropna().empty:
        return {
            "fetch_state": frame.attrs.get("fetch_state", "empty"),
            "source_chain": frame.attrs.get("source_chain", []),
            "fetch_error": frame.attrs.get("fetch_error"),
        }
    latest_date = frame["REPORT_DATE"].max()
    latest = frame[frame["REPORT_DATE"].eq(latest_date)].copy()
    latest = latest.sort_values("MBI_RATIO", ascending=False)
    business_rows = latest[latest["MAINOP_TYPE"].isin(("1", "2"))]
    business_items = list(dict.fromkeys(
        item.strip()
        for item in business_rows["ITEM_NAME"].dropna().astype(str)
        if item.strip()
    ))[:20]
    breakdown: list[dict] = []
    for _, row in business_rows.head(30).iterrows():
        ratio = row.get("MBI_RATIO")
        margin = row.get("GROSS_RPOFIT_RATIO")
        breakdown.append({
            "category": TYPE_NAMES.get(str(row.get("MAINOP_TYPE")), str(row.get("MAINOP_TYPE"))),
            "item": str(row.get("ITEM_NAME") or ""),
            "revenue_ratio": None if pd.isna(ratio) else round(float(ratio), 6),
            "gross_margin": None if pd.isna(margin) else round(float(margin), 6),
        })
    region_rows = latest[latest["MAINOP_TYPE"].eq("3")]
    result = {
        "fetch_state": frame.attrs.get("fetch_state", "ok"),
        "source_chain": frame.attrs.get("source_chain", []),
        "business_report_date": latest_date.strftime("%Y-%m-%d"),
        "business_items": business_items,
        "business_breakdown": breakdown,
        "main_business": "、".join(business_items[:8]),
    }
    if not region_rows.empty:
        valid_regions = region_rows[region_rows["MBI_RATIO"].notna()]
        if not valid_regions.empty:
            overseas_rows = valid_regions[valid_regions["ITEM_NAME"].astype(str).map(lambda value: any(term in value for term in OVERSEAS_TERMS))]
            result["overseas_revenue_ratio"] = round(float(overseas_rows["MBI_RATIO"].sum() * 100), 4)
    return result


def build_report(code: str, name: str, frame: pd.DataFrame, structured: dict | None = None) -> str:
    structured = structured or build_structured(frame)
    lines = [
        f"# 主营构成报告：{name or code}（{code}）",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  数据源：东方财富 F10 → AKShare stock_zygc_em",
        "",
        f"<!-- moda_business: {json.dumps(structured, ensure_ascii=False)} -->",
        "",
    ]
    if frame.empty:
        lines += ["需人工确认：未取得主营构成数据。", ""]
    else:
        latest_date = frame["REPORT_DATE"].max()
        latest = frame[frame["REPORT_DATE"].eq(latest_date)].sort_values(["MAINOP_TYPE", "MBI_RATIO"], ascending=[True, False])
        for type_code, title in TYPE_NAMES.items():
            rows = latest[latest["MAINOP_TYPE"].eq(type_code)]
            lines += [f"## {title}", ""]
            if rows.empty:
                lines += ["需人工确认：无数据。", ""]
                continue
            lines += ["| 项目 | 收入 | 收入占比 | 毛利率 |", "|---|---:|---:|---:|"]
            for _, row in rows.head(12).iterrows():
                income = row.get("MAIN_BUSINESS_INCOME")
                ratio = row.get("MBI_RATIO")
                margin = row.get("GROSS_RPOFIT_RATIO")
                def fmt(value: object, percentage: bool = False) -> str:
                    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
                    if pd.isna(number):
                        return "需人工确认"
                    return f"{float(number):.2%}" if percentage else f"{float(number):,.0f}"
                lines.append(
                    f"| {str(row.get('ITEM_NAME', '')).replace('|', '/')} | "
                    f"{fmt(income)} | {fmt(ratio, percentage=True)} | {fmt(margin, percentage=True)} |"
                )
            lines.append("")
    profile = structured.get("company_profile") if isinstance(structured.get("company_profile"), dict) else {}
    if profile.get("status") == "已验证":
        lines += [
            "## 公司概况（CNINFO）",
            "",
            "| 字段 | 内容 |",
            "|---|---|",
        ]
        for label, key in (
            ("所属行业", "industry"),
            ("法人代表", "legal_representative"),
            ("上市日期", "listing_date"),
            ("主营业务", "main_business"),
        ):
            value = str(profile.get(key) or "").replace("|", "/")
            if value:
                lines.append(f"| {label} | {value} |")
        lines.append("")
    ths_intro = structured.get("business_intro_ths") if isinstance(structured.get("business_intro_ths"), dict) else {}
    if ths_intro.get("status") == "已验证":
        lines += [
            "## 主营交叉核验（同花顺）",
            "",
            "同花顺主营介绍是 B 级异源描述，只用于与东财收入分部做语义核验，不替代收入/毛利结构。",
            "",
        ]
        for label, key in (("主营业务", "main_business"), ("产品类型", "product_types"), ("产品名称", "product_names")):
            value = str(ths_intro.get(key) or "").replace("|", "/")
            if value:
                lines.append(f"- {label}：{value}")
        lines.append("")
    lines += ["## 免责声明", "", "本报告基于公开主营构成数据，仅供研究参考，不构成投资建议。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect structured business composition")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    code = args.stock.strip()
    if len(code) != 6 or not code.isdigit():
        parser.error("--stock must be a 6-digit A-share code")
    frame, structured = collect_business_context(code)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, frame, structured), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
