#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping, Sequence


SCRIPT_ROOT = Path(__file__).resolve().parent
ANALYZE_A_SHARE = SCRIPT_ROOT / "analyze_a_share.py"
ANALYZE_SECTOR = SCRIPT_ROOT / "analyze_sector.py"

LOGIC_READY_PHASES = {"logic_validated", "evidence"}
NEXT_ACTIONS = {
    "needs_logic": "write_logic_case",
    "needs_evidence": "targeted_evidence",
    "needs_candidate_selection": "select_candidates",
    "needs_deep_research": "deep_research",
    "needs_judgment": "write_judgment_v4",
    "ready": "render_report",
    "failed": "manual_review",
}


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules():
    return (
        _load_module(ANALYZE_A_SHARE, "moda_logic_a_share"),
        _load_module(ANALYZE_SECTOR, "moda_logic_sector"),
    )


def _engine(root: Path):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools import logic_engine

    return logic_engine


def _auto_kind(root: Path, query: str, requested: str, a_share: Any) -> str:
    if requested in {"stock", "sector", "concept"}:
        return requested
    if re.fullmatch(r"\d{6}", query.strip()):
        return "stock"
    try:
        a_share._resolve_stock(root, query)
        return "stock"
    except (ValueError, RuntimeError, LookupError, OSError):
        from tools.sector_screening import infer_query_kind

        return infer_query_kind(query)


def _packet_from_reports(
    root: Path,
    code: str,
    name: str,
    directories: Sequence[str],
    since: float = 0,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools.scoring.evidence import build_evidence, read_reports
    from tools.scoring.model import score_evidence
    from tools.scoring.thesis import build_thesis_context

    reports = read_reports(code, tuple(dict.fromkeys(directories)), since)
    evidence = build_evidence(code, name, reports)
    card = score_evidence(evidence)
    packet = build_thesis_context(card, evidence).to_dict()
    return packet, card.to_dict(), evidence


def _require_logic_stage(
    case: Mapping[str, Any],
    action: str,
    *,
    allow_needs_candidate_selection: bool = False,
) -> None:
    """Prevent evidence/deep collection from bypassing the Logic Case gate."""
    phase = str(case.get("phase") or "")
    status = str(case.get("status") or "")
    if phase not in LOGIC_READY_PHASES:
        raise ValueError(
            f"{action} 前必须先提交并校验 Logic Case；当前 phase={phase or 'unknown'}。"
        )
    if status == "needs_logic":
        raise ValueError(f"{action} 前必须先完成投资命题和产业链 Logic Map。")
    if action == "deep" and status == "needs_evidence":
        raise ValueError("完整深研前必须先完成定向补证，并用新证据更新 Logic Case。")
    if action == "deep" and status == "needs_candidate_selection" and not allow_needs_candidate_selection:
        raise ValueError("完整深研前必须先明确选择候选公司。")


def _next_action(case: Mapping[str, Any]) -> str:
    return NEXT_ACTIONS.get(str(case.get("status") or ""), "manual_review")


def _collect_stock_baseline(root: Path, code: str, name: str, refresh: bool) -> dict[str, Any]:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools import run_pipeline

    started = time.time()
    common = ["--stock", code, "--name", name or code]
    kline = run_pipeline.prepare_kline(code, refresh=refresh)
    kline_args = ["--kline-file", str(kline)] if kline else []
    modules = [
        ("finance_data", "tools/akshare/finance_data.py", [*common, *kline_args], 180),
        ("business_data", "tools/akshare/business_data.py", common, 60),
        ("announcements", "tools/akshare/announcements.py", [*common, "--days", "180"], 120),
    ]
    results = run_pipeline.run_collectors(modules)
    sources = [
        str(item.get("label"))
        for item in results
        if item.get("ok") or item.get("report_fresh")
    ]
    packet, scorecard, evidence = _packet_from_reports(root, code, name, sources, started)
    return {
        "stage": "baseline",
        "started_at": started,
        "modules": results,
        "sources": sources,
        "research_packet": packet,
        "scorecard": scorecard,
        "evidence": evidence,
    }


def _packet_context(packet: Mapping[str, Any]) -> tuple[str, str]:
    company = packet.get("company") if isinstance(packet.get("company"), Mapping) else {}
    industry = packet.get("industry") if isinstance(packet.get("industry"), Mapping) else {}
    industry_name = str(
        industry.get("chain_name")
        or (industry.get("industry_mapping") or {}).get("sw_secondary")
        or (industry.get("industry_mapping") or {}).get("sw_primary")
        or "综合"
    )
    context = " ".join(
        str(value).strip()
        for value in (
            industry_name,
            company.get("main_business"),
            " ".join(str(item) for item in company.get("business_items") or []),
        )
        if value
    )
    return industry_name, context


def _request_kinds(case: Mapping[str, Any], requested: Sequence[str] | None) -> set[str]:
    if requested:
        return {str(item).strip() for item in requested if str(item).strip()}
    return {
        str(item.get("kind") or "").strip()
        for item in case.get("evidence_requests") or []
        if isinstance(item, Mapping) and str(item.get("status") or "pending") != "completed"
    }


def _collect_targeted_stock(
    root: Path,
    code: str,
    name: str,
    case: Mapping[str, Any],
    *,
    refresh: bool,
    evidence_kinds: Sequence[str] | None,
) -> dict[str, Any]:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools import run_pipeline
    from tools.logic_engine import evidence_requests_to_targets

    packet = dict(((case.get("context") or {}).get("research_packet") or {}))
    if packet.get("schema_version") != 4:
        baseline = _collect_stock_baseline(root, code, name, refresh)
        packet = baseline["research_packet"]
    industry, context = _packet_context(packet)
    common = ["--stock", code, "--name", name or code]
    refresh_args = ["--refresh"] if refresh else []
    kinds = _request_kinds(case, evidence_kinds)
    modules: list[tuple] = []

    if kinds.intersection({"system_change", "capex", "industry"}):
        modules.extend([
            ("macro_policy", "tools/akshare/macro_policy.py", [*common, "--industry", industry], 150),
            ("industry_prosperity", "tools/akshare/industry_prosperity.py", [*common, "--industry", industry, *refresh_args], 180),
        ])
    if kinds.intersection({"supply", "bottleneck", "capex"}):
        modules.append(("supply_demand", "tools/scoring/supply_demand.py", [*common, "--context", context], 150))
    if kinds.intersection({"company_position", "profit", "peers", "risk"}):
        modules.append(("market_events", "tools/akshare/market_events.py", common, 90))
    if kinds.intersection({"expectation", "valuation", "market", "timing"}):
        kline = run_pipeline.prepare_kline(code, refresh=refresh)
        kline_args = ["--kline-file", str(kline)] if kline else []
        modules.extend([
            ("congestion", "tools/akshare/congestion.py", [*common, "--industry", industry, *refresh_args], 90),
            ("popularity", "tools/akshare/popularity.py", common, 30),
            ("social_sentiment", "tools/akshare/social_sentiment.py", common, 90),
            ("tdx_analysis", "tools/tdx/analyzer.py", [*common, *kline_args], 120),
        ])

    targets = evidence_requests_to_targets(case, sorted(kinds))
    if targets:
        modules.append((
            "web_research",
            "tools/scoring/web_research.py",
            [*common, "--context", context, "--targets-json", json.dumps(targets, ensure_ascii=False), *refresh_args],
            120,
        ))

    deduped: list[tuple] = []
    seen: set[str] = set()
    for module in modules:
        if module[0] not in seen:
            seen.add(module[0])
            deduped.append(module)
    started = time.time()
    results = run_pipeline.run_collectors(deduped) if deduped else []
    successful = [
        str(item.get("label"))
        for item in results
        if item.get("ok") or item.get("report_fresh")
    ]
    baseline_sources = ["finance_data", "business_data", "announcements"]
    packet, scorecard, evidence = _packet_from_reports(
        root,
        code,
        name,
        [*baseline_sources, *successful],
        0,
    )
    return {
        "stage": "targeted_evidence",
        "started_at": started,
        "request_kinds": sorted(kinds),
        "modules": results,
        "sources": successful,
        "research_packet": packet,
        "scorecard": scorecard,
        "evidence": evidence,
        "web_data": {"web_gap_results": evidence.get("web_gap_results") or []},
    }


def _result(case: Mapping[str, Any], paths: Mapping[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "case_id": case.get("case_id"),
        "kind": case.get("kind"),
        "phase": case.get("phase"),
        "status": case.get("status"),
        "next_action": _next_action(case),
        "logic_case": dict(case),
        "logic_report": extra.pop("logic_report", ""),
        **dict(paths),
        **extra,
    }


def analyze_logic(
    query: str,
    kind: str = "auto",
    phase: str = "baseline",
    case_id: str = "",
    refresh: bool = False,
    candidates: Sequence[str] | None = None,
    evidence_kinds: Sequence[str] | None = None,
    save: bool = True,
    *,
    moda_root: str | Path | None = None,
) -> dict[str, Any]:
    a_share, sector = _modules()
    root = a_share.find_moda_root(moda_root)
    engine = _engine(root)
    resolved_kind = _auto_kind(root, query, kind, a_share)
    resolved_id = case_id.strip() or engine.make_case_id(query, resolved_kind)
    previous = engine.load_logic_case(root, resolved_id)

    if phase == "show":
        if previous is None:
            raise FileNotFoundError(f"未找到逻辑主档：{resolved_id}")
        paths = engine.save_logic_case(root, previous) if save else {}
        return _result(previous, paths, logic_report=engine.render_logic_case(previous))

    if resolved_kind in {"sector", "concept"}:
        if phase == "baseline":
            screening = sector.screen_sector(
                query,
                candidates,
                save=False,
                use_live_universe=True,
                fetch_business=True,
                shortlist_limit=6,
                query_kind=resolved_kind,
                moda_root=root,
            )
            case = previous or engine.new_logic_case(query, resolved_kind, screening=screening)
            context = dict(case.get("context") or {})
            context["screening"] = screening
            case["context"] = context
            case["phase"] = "logic_draft"
            case["status"] = "needs_logic"
        elif phase == "evidence":
            if previous is None:
                raise ValueError("板块或概念广搜前必须先建立并保存 logic_case")
            _require_logic_stage(previous, "evidence")
            from tools.scoring.sector_broad_research import collect_sector_broad_evidence

            previous_context = previous.get("context") if isinstance(previous.get("context"), Mapping) else {}
            screening = previous_context.get("screening") if isinstance(previous_context.get("screening"), Mapping) else None
            broad = collect_sector_broad_evidence(query, context=query, screening=screening)
            case = dict(previous)
            context = dict(case.get("context") or {})
            context["sector_broad_research"] = broad
            case["context"] = context
            case["phase"] = "evidence"
            # Broad search only supplies auditable material.  The user still
            # needs to state the sector thesis before any deep research.
            case["status"] = engine.derive_status(case)
        elif phase == "deep":
            if previous is None:
                raise ValueError("板块或概念深研前必须先建立并保存 logic_case")
            _require_logic_stage(previous, "deep", allow_needs_candidate_selection=True)
            selected = [str(item).strip() for item in (candidates or []) if str(item).strip()]
            if not selected:
                selected = [
                    str(item.get("code") or item.get("name") or "").strip()
                    for item in previous.get("company_branches") or []
                    if isinstance(item, Mapping) and item.get("selected_for_deep_research")
                ]
            selected = list(dict.fromkeys(item for item in selected if item))[:3]
            if not selected:
                raise ValueError("请先从 Top 6 中明确选择最多三家公司再深研")
            deep = sector.analyze_sector(
                query,
                selected,
                refresh=refresh,
                save=False,
                collect=True,
                max_candidates=3,
                moda_root=root,
            )
            case = dict(previous)
            branches = []
            for item in case.get("company_branches") or []:
                row = dict(item) if isinstance(item, Mapping) else {}
                token = str(row.get("code") or row.get("name") or "")
                row["selected_for_deep_research"] = token in selected
                branches.append(row)
            case["company_branches"] = branches
            context = dict(case.get("context") or {})
            context["sector_research"] = deep
            case["context"] = context
            case["phase"] = "deep_research"
            case["status"] = "ready"
        else:
            raise ValueError("板块或概念仅支持 baseline、evidence、deep 或 show 阶段")
    else:
        code, name = a_share._resolve_stock(root, query)
        if phase == "baseline":
            baseline = _collect_stock_baseline(root, code, name, refresh)
            case = previous or engine.new_logic_case(query, "stock", research_packet=baseline["research_packet"])
            case = engine.attach_research_packet(case, baseline["research_packet"], {
                "stage": "baseline",
                "modules": baseline["modules"],
            })
            case["phase"] = "logic_draft"
            case["status"] = "needs_logic" if previous is None else case["status"]
        elif phase == "evidence":
            if previous is None:
                raise ValueError("补证前必须先建立并保存 logic_case")
            _require_logic_stage(previous, "evidence")
            evidence_run = _collect_targeted_stock(
                root,
                code,
                name,
                previous,
                refresh=refresh,
                evidence_kinds=evidence_kinds,
            )
            case = engine.attach_research_packet(previous, evidence_run["research_packet"], {
                "stage": "targeted_evidence",
                "request_kinds": evidence_run["request_kinds"],
                "modules": evidence_run["modules"],
            })
            case = engine.merge_web_evidence(case, evidence_run["web_data"])
            case["phase"] = "evidence"
            case["status"] = engine.derive_status(case)
        elif phase == "deep":
            if previous is None:
                raise ValueError("深研前必须先建立并保存 logic_case")
            _require_logic_stage(previous, "deep")
            deep = a_share.analyze_a_share(query, refresh=refresh, save=False, moda_root=root, run_pipeline=True)
            case = engine.attach_research_packet(previous, deep["research_packet"], {
                "stage": "deep_research",
                "pipeline_path": deep.get("pipeline_path"),
            })
            case["phase"] = "deep_research"
            case["status"] = engine.derive_status(case)
        else:
            raise ValueError("个股仅支持 baseline、evidence、deep 或 show 阶段")

    report = engine.render_logic_case(case)
    paths = engine.save_logic_case(root, case) if save else {}
    return _result(
        case,
        paths,
        logic_report=report,
        research_packet=(case.get("context") or {}).get("research_packet") or {},
    )


def finalize_logic_case(
    query: str,
    logic_payload: Mapping[str, Any],
    *,
    moda_root: str | Path | None = None,
    save: bool = True,
) -> dict[str, Any]:
    a_share, _ = _modules()
    root = a_share.find_moda_root(moda_root)
    engine = _engine(root)
    kind = str(logic_payload.get("kind") or "stock")
    case_id = engine.make_case_id(query, kind)
    previous = engine.load_logic_case(root, case_id)
    if previous is None:
        raise ValueError("请先运行 analyze_logic baseline 建立事实包和逻辑主档")
    payload = dict(logic_payload)
    payload["case_id"] = case_id
    payload["query"] = query
    payload["kind"] = kind
    case = engine.validate_logic_case(payload, previous=previous)
    case["phase"] = "logic_validated"
    case["status"] = engine.derive_status(case)
    paths = engine.save_logic_case(root, case) if save else {}
    return _result(case, paths, logic_report=engine.render_logic_case(case))


def finalize_logic_report(
    query: str,
    logic_payload: Mapping[str, Any],
    judgment_payload: Mapping[str, Any],
    *,
    moda_root: str | Path | None = None,
    save: bool = False,
) -> dict[str, Any]:
    a_share, _ = _modules()
    root = a_share.find_moda_root(moda_root)
    engine = _engine(root)
    case_id = engine.make_case_id(query, "stock")
    previous = engine.load_logic_case(root, case_id)
    if previous is None:
        raise ValueError("请先运行 analyze_logic baseline 建立逻辑主档")
    logic = dict(logic_payload)
    logic.update({"schema_version": 1, "case_id": case_id, "query": query, "kind": "stock"})
    case = engine.validate_logic_case(logic, previous=previous)

    if engine.derive_status(case) in {"needs_logic", "needs_evidence"}:
        raise ValueError("正式判断前必须先闭合 Logic Case 的关键证据缺口。")

    deep = a_share.analyze_a_share(query, refresh=False, save=False, moda_root=root, run_pipeline=True)
    case = engine.attach_research_packet(case, deep["research_packet"], {
        "stage": "deep_research",
        "pipeline_path": deep.get("pipeline_path"),
    })
    final = a_share.finalize_a_share(query, dict(judgment_payload), moda_root=root, save=save)
    case = engine.apply_judgment(case, dict(judgment_payload))
    case = engine.validate_logic_case(case, previous=previous, require_decision=True)
    paths = engine.save_logic_case(root, case)
    return _result(
        case,
        paths,
        logic_report=engine.render_logic_case(case),
        formal_report=final["formal_report"],
        formal_report_status=final["formal_report_status"],
        scorecard_path=final.get("scorecard_path"),
        pipeline_path=final.get("pipeline_path"),
        saved_path=final.get("saved_path"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Logic-first Moda investment research entry")
    parser.add_argument("query")
    parser.add_argument("--kind", choices=("auto", "stock", "sector", "concept"), default="auto")
    parser.add_argument("--phase", choices=("baseline", "evidence", "deep", "show"), default="baseline")
    parser.add_argument("--case-id", default="")
    parser.add_argument("--candidate", action="append", default=[])
    parser.add_argument("--evidence-kind", action="append", default=[])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--moda-root", type=Path)
    parser.add_argument("--logic-json", type=Path)
    parser.add_argument("--judgment-json", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args(argv)

    if args.logic_json:
        logic = json.loads(args.logic_json.read_text(encoding="utf-8"))
        if not isinstance(logic, dict):
            raise ValueError("--logic-json 必须包含 JSON 对象")
        if args.judgment_json:
            judgment = json.loads(args.judgment_json.read_text(encoding="utf-8"))
            if not isinstance(judgment, dict):
                raise ValueError("--judgment-json 必须包含 JSON 对象")
            result = finalize_logic_report(args.query, logic, judgment, moda_root=args.moda_root)
        else:
            result = finalize_logic_case(args.query, logic, moda_root=args.moda_root, save=not args.no_save)
    else:
        result = analyze_logic(
            args.query,
            args.kind,
            args.phase,
            args.case_id,
            args.refresh,
            args.candidate,
            args.evidence_kind,
            not args.no_save,
            moda_root=args.moda_root,
        )
    print(result.get("logic_report") if args.format == "markdown" else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
