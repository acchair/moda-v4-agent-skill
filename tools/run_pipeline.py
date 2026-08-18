from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CACHE_ROOT = ROOT / "knowledge" / "research" / "pipeline" / "cache"
REPORT_DIRS = {
    "finance_data": "finance_data",
    "business_data": "business_data",
    "tdx_analysis": "tdx_analysis",
    "scoring": "scoring",
    "announcements": "announcements",
    "market_events": "market_events",
    "popularity": "popularity",
    "social_sentiment": "social_sentiment",
    "congestion": "congestion",
    "supply_demand": "supply_demand",
    "macro_policy": "macro_policy",
    "web_research": "web_research",
    "industry_prosperity": "industry_prosperity",
}

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def run_module(label: str, script: str, args: list[str], timeout: int = 180) -> dict:
    command = [sys.executable, str(ROOT / script), *args]
    print(f"\n[{label}] {' '.join(command)}")
    started = datetime.now()
    started_ts = time.time()
    try:
        result = subprocess.run(command, cwd=ROOT, timeout=timeout, check=False)
        code = args[args.index("--stock") + 1]
        report = ROOT / "knowledge/research" / REPORT_DIRS[label] / f"{code}.md"
        fresh = report.exists() and report.stat().st_mtime >= started_ts - 1
        return {"label": label, "ok": result.returncode == 0 and fresh, "returncode": result.returncode,
                "report_fresh": fresh,
                "coverage": _report_coverage(label, report) if fresh else 0,
                "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1)}
    except subprocess.TimeoutExpired:
        code = args[args.index("--stock") + 1]
        report = ROOT / "knowledge/research" / REPORT_DIRS[label] / f"{code}.md"
        fresh = report.exists() and report.stat().st_mtime >= started_ts - 1
        return {
            "label": label,
            "ok": False,
            "error": f"timeout after {timeout}s",
            "report_fresh": fresh,
            "coverage": _report_coverage(label, report) if fresh else 0,
            "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
        }


def _report_coverage(label: str, path: Path) -> int:
    """Report comparable coverage without making missing data a hard failure."""
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    if label == "finance_data":
        return sum(marker in text and "无数据" not in text[text.find(marker):text.find(marker) + 160]
                   for marker in ("实时行情", "公司信息", "财务摘要", "近期行情", "同行估值"))
    if label == "scoring":
        return sum(bool(line.strip()) and line.startswith("| F") for line in text.splitlines())
    if label == "announcements":
        return int("最新公告" in text) + int("投资者互动问答" in text)
    if label == "industry_prosperity":
        return int("moda_industry_prosperity" in text)
    return int("评分:" in text or "ALPHA-SOROS" in text)


def _kline_cache_paths(code: str) -> tuple[Path, Path]:
    return CACHE_ROOT / "kline" / f"{code}.csv", CACHE_ROOT / "kline" / f"{code}.meta.json"


def _read_kline_cache(code: str, *, require_today: bool) -> tuple[Path | None, dict]:
    from tools.daily_cache import shanghai_now
    from tools.providers.kline_quality import validate_kline_frame

    path, meta_path = _kline_cache_paths(code)
    if not path.exists() or not meta_path.exists():
        return None, {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if require_today and meta.get("checked_date") != shanghai_now().date().isoformat():
            return None, meta
        frame, _ = validate_kline_frame(pd.read_csv(path), minimum_rows=60, max_age_days=14)
        if frame.empty:
            return None, meta
        return path, meta
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None, {}


def _write_kline_cache(code: str, frame, meta: dict) -> Path:
    path, meta_path = _kline_cache_paths(code)
    path.parent.mkdir(parents=True, exist_ok=True)
    csv_tmp = path.with_suffix(".csv.tmp")
    meta_tmp = meta_path.with_suffix(".json.tmp")
    frame.to_csv(csv_tmp, index=False)
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(csv_tmp, path)
    os.replace(meta_tmp, meta_path)
    return path


def prepare_kline(code: str, *, refresh: bool = False) -> Path | None:
    from tools.data_call import dataframe_empty, run_fallback_chain
    from tools.daily_cache import shanghai_now
    from tools.providers.kline_quality import validate_kline_frame

    if not refresh:
        cached, meta = _read_kline_cache(code, require_today=True)
        if cached:
            print(f"[kline] persistent cache -> {meta.get('rows', '?')} rows")
            return cached
    result = None
    try:
        def easy_tdx() -> pd.DataFrame:
            from tools.providers.easy_tdx_provider import fetch_kline_daily

            frame, _ = validate_kline_frame(fetch_kline_daily(code), minimum_rows=60, max_age_days=14)
            return frame

        def baostock() -> pd.DataFrame:
            from tools.providers.baostock_provider import fetch_kline_daily

            frame, _ = validate_kline_frame(fetch_kline_daily(code), minimum_rows=60, max_age_days=14)
            return frame

        result = run_fallback_chain(
            "共享日K",
            [("easy_tdx/TDX", easy_tdx), ("BaoStock", baostock)],
            seconds=25,
            empty=dataframe_empty,
        )
        if not result.ok or not isinstance(result.value, pd.DataFrame):
            raise RuntimeError(result.error or "all shared K-line sources failed")
        frame = result.value
        issues = list(frame.attrs.get("quality_issues", []))
        now = shanghai_now()
        path = _write_kline_cache(code, frame, {
            "checked_date": now.date().isoformat(),
            "checked_at": now.isoformat(),
            "latest_date": frame.attrs.get("latest_date"),
            "rows": len(frame),
            "fetch_state": result.fetch_state,
            "quality_issues": issues,
            "source_chain": result.source_chain or [],
        })
        print(f"[kline] {result.source} -> {len(frame)} rows")
        return path
    except Exception as exc:
        print(f"[kline] shared cache unavailable: {type(exc).__name__}: {exc}")
        cached, meta = _read_kline_cache(code, require_today=False)
        if cached:
            now = shanghai_now()
            meta.update({
                "checked_date": now.date().isoformat(),
                "checked_at": now.isoformat(),
                "fetch_state": "stale",
                "fetch_error": f"{type(exc).__name__}: {exc}",
                "source_chain": [
                    *((result.source_chain or []) if result is not None else [
                        {"source": "easy_tdx/TDX", "status": "failed", "error": type(exc).__name__},
                        {"source": "BaoStock", "status": "failed", "error": "not attempted"},
                    ]),
                    {"source": "persistent-cache", "status": "stale", "error": ""},
                ],
            })
            meta_path = _kline_cache_paths(code)[1]
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[kline] stale persistent cache -> {meta.get('rows', '?')} rows")
            return cached
        return None


def run_collectors(collectors: list[tuple]) -> list[dict]:
    with ThreadPoolExecutor(max_workers=min(6, len(collectors))) as executor:
        return list(executor.map(lambda module: run_module(*module), collectors))


def update_report_snapshot(
    code: str,
    directories: tuple[str, ...],
    since: float,
    snapshot: dict[str, str] | None = None,
) -> dict[str, str]:
    from tools.scoring.evidence import read_reports

    reports = snapshot if snapshot is not None else {}
    missing = tuple(directory for directory in directories if directory not in reports)
    if missing:
        reports.update(read_reports(code, missing, since))
    return reports


def current_context(
    code: str,
    directories: tuple[str, ...],
    since: float,
    reports: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    from tools.scoring.evidence import build_evidence, read_reports

    source_reports = reports if reports is not None else read_reports(code, directories, since)
    evidence = build_evidence(code, code, source_reports)
    industry = str(evidence.get("industry") or "综合")
    values = [
        industry,
        str(evidence.get("main_business") or ""),
        " ".join(str(item) for item in evidence.get("business_items", [])),
        " ".join(str(item) for item in evidence.get("concepts", [])),
    ]
    security_name = str(evidence.get("security_name") or "").strip()
    return industry, " ".join(value for value in values if value).strip(), security_name


def unresolved_targets(
    code: str,
    name: str,
    directories: tuple[str, ...],
    since: float,
    reports: dict[str, str] | None = None,
) -> list[dict]:
    from tools.scoring.evidence import build_evidence, read_reports
    from tools.scoring.model import score_evidence

    source_reports = reports if reports is not None else read_reports(code, directories, since)
    evidence = build_evidence(code, name, source_reports)
    card = score_evidence(evidence)
    targets = [
        {
            "factor_key": factor.key,
            "subfactor_key": item.key,
            "label": item.label,
            "maximum": item.maximum,
            "original_status": item.status,
            "original_reason": item.reason,
        }
        for factor in card.factors if factor.key != "F6"
        for item in factor.subfactors if item.status in {
            "需人工确认",
            "部分覆盖",
            "已搜索未命中",
            "搜索失败，需人工确认",
            "搜索结果待正文核验，需人工确认",
        }
    ]
    if evidence.get("classification_db_specialized") is True:
        targets = [
            item for item in targets
            if not (item["factor_key"] == "F3" and item["subfactor_key"] == "specialized")
        ]
    if evidence.get("classification_db_leadership") is True or evidence.get("classification_db_core_supplier") is True:
        targets = [
            item for item in targets
            if not (item["factor_key"] == "F3" and item["subfactor_key"] == "leadership")
        ]
    return targets


def run_contextual_collectors(
    collectors: list[tuple],
    context_labels: set[str],
    followup_factory,
) -> tuple[list[dict], list[dict]]:
    context_collectors = [module for module in collectors if module[0] in context_labels]
    sidecar_collectors = [module for module in collectors if module[0] not in context_labels]
    context_workers = min(3, max(1, len(context_collectors)))
    sidecar_workers = min(4, max(1, len(sidecar_collectors)))
    with ThreadPoolExecutor(max_workers=context_workers) as context_executor, \
         ThreadPoolExecutor(max_workers=sidecar_workers) as sidecar_executor:
        futures = {
            id(module): (
                context_executor if module[0] in context_labels else sidecar_executor
            ).submit(run_module, *module)
            for module in collectors
        }
        context_results = [
            futures[id(module)].result()
            for module in context_collectors
        ]
        followup_collectors = list(followup_factory(context_results))
        followup_futures = [context_executor.submit(run_module, *module) for module in followup_collectors]
        first_results = [futures[id(module)].result() for module in collectors]
        followup_results = [future.result() for future in followup_futures]
    return first_results, followup_results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the moda-v4 structured A-share pipeline")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--refresh", action="store_true", help="Force shared daily data caches to refresh")
    args = parser.parse_args()
    from tools.stock_resolver import resolve_stock_input

    try:
        code, resolved_input_name = resolve_stock_input(args.stock, args.name)
    except ValueError as exc:
        parser.error(str(exc))
    requested_name = args.name.strip()
    if requested_name == code:
        requested_name = ""
    input_name = requested_name or resolved_input_name

    started_ts = time.time()
    common = ["--stock", code, "--name", input_name or code]
    refresh_args = ["--refresh"] if args.refresh else []
    kline_path = prepare_kline(code, refresh=args.refresh)
    kline_args = ["--kline-file", str(kline_path)] if kline_path else []
    first_wave = [
        ("finance_data", "tools/akshare/finance_data.py", [*common, *kline_args], 180),
        ("business_data", "tools/akshare/business_data.py", common, 60),
        ("tdx_analysis", "tools/tdx/analyzer.py", [*common, *kline_args], 120),
        ("announcements", "tools/akshare/announcements.py", [*common, "--days", "180"], 120),
        ("market_events", "tools/akshare/market_events.py", common, 90),
        ("popularity", "tools/akshare/popularity.py", common, 30),
        ("social_sentiment", "tools/akshare/social_sentiment.py", common, 90),
    ]

    report_snapshot: dict[str, str] = {}
    context_state: dict[str, object] = {}

    def followup_factory(context_results: list[dict]) -> list[tuple]:
        context_sources = tuple(
            result["label"] for result in context_results
            if result.get("ok") or result.get("report_fresh")
        )
        update_report_snapshot(code, context_sources, started_ts, report_snapshot)
        industry, context, discovered_name = current_context(
            code,
            context_sources,
            started_ts,
            reports=report_snapshot,
        )
        resolved_name = requested_name or discovered_name or resolved_input_name or code
        resolved_common = ["--stock", code, "--name", resolved_name]
        congestion = (
            "congestion",
            "tools/akshare/congestion.py",
            [*resolved_common, "--industry", industry, *refresh_args],
            90,
        )
        second_wave = [
            ("supply_demand", "tools/scoring/supply_demand.py", [*resolved_common, "--context", context], 150),
            ("macro_policy", "tools/akshare/macro_policy.py", [*resolved_common, "--industry", industry], 150),
        ]
        prosperity = (
            "industry_prosperity",
            "tools/akshare/industry_prosperity.py",
            [*resolved_common, "--industry", industry, *refresh_args],
            180,
        )
        followups = [congestion, *second_wave, prosperity]
        context_state.update({
            "context": context,
            "resolved_name": resolved_name,
            "common": resolved_common,
            "followups": followups,
        })
        return followups

    results, followup_results = run_contextual_collectors(
        first_wave,
        {"finance_data", "business_data", "market_events"},
        followup_factory,
    )
    results.extend(followup_results)
    resolved_name = str(context_state["resolved_name"])
    common = list(context_state["common"])
    context = str(context_state["context"])
    followups = list(context_state["followups"])
    # A fresh report is usable evidence even when the subprocess exit status
    # was affected by an outer timeout. Its embedded fetch_state still
    # preserves whether the module itself succeeded, fell back, or failed.
    structured_sources = tuple(
        result["label"] for result in results
        if result.get("ok") or result.get("report_fresh")
    )
    update_report_snapshot(code, structured_sources, started_ts, report_snapshot)
    targets = unresolved_targets(code, resolved_name, structured_sources, started_ts, reports=report_snapshot)
    web_args = [*common, "--context", context, "--targets-json", json.dumps(targets, ensure_ascii=False), *refresh_args]
    results.append(run_module("web_research", "tools/scoring/web_research.py", web_args, 120))
    successful_sources = ",".join(
        result["label"] for result in results
        if result.get("ok") or result.get("report_fresh")
    )
    requested_labels = [module[0] for module in first_wave + followups] + ["web_research"]
    requested_sources = ",".join(requested_labels)
    scoring_args = [*common, "--sources", successful_sources, "--requested-sources", requested_sources, "--since", str(started_ts)]
    results.append(run_module("scoring", "tools/scoring/grader.py", scoring_args))
    output = ROOT / "knowledge/research/pipeline"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{code}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(item["ok"] for item in results)
    print(f"\nPipeline: {passed}/{len(results)} modules succeeded -> {path}")
    if not results[-1].get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
