#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def candidate_roots(script_path: Path | None = None) -> list[Path]:
    script_path = (script_path or Path(__file__)).resolve()
    candidates: list[Path] = []
    runtime_config = script_path.parents[1] / ".moda-companion-runtime.json"
    if runtime_config.is_file():
        try:
            configured_root = json.loads(runtime_config.read_text(encoding="utf-8")).get("moda_root")
            if configured_root:
                candidates.append(Path(configured_root).expanduser())
        except (OSError, ValueError, TypeError):
            pass
    explicit = os.environ.get("MODA_V4_ROOT", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(
        [
            script_path.parents[2],
            Path.cwd(),
            Path("/var/minis/skills/moda-v4"),
            script_path.parents[1] / "_runtime" / "moda-v4",
            Path.home() / ".agents" / "skills" / "moda-v4",
            Path.home() / ".codex" / "skills" / "moda-v4",
            Path.home() / ".claude" / "skills" / "moda-v4",
        ]
    )
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def find_moda_root(explicit: str | Path | None = None) -> Path:
    candidates = [Path(explicit).expanduser().resolve()] if explicit else candidate_roots()
    for root in candidates:
        if (root / "tools" / "run_pipeline.py").is_file() and (root / "SKILL.md").is_file():
            return root
    locations = "\n".join(f"- {path}" for path in candidates)
    raise FileNotFoundError(
        "未找到 moda-v4。请设置 MODA_V4_ROOT，或将 moda-v4 安装到标准 Skills 目录。\n"
        f"已检查：\n{locations}"
    )


def _resolve_stock(root: Path, query: str) -> tuple[str, str]:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools.stock_resolver import resolve_stock_input

    return resolve_stock_input(query)


def _pending_judgment_report(name: str, code: str, expression_status: str) -> str:
    if expression_status == "stale_schema":
        reason = "当前事实包版本已过期，需先重新采集，不能沿用旧报告。"
    elif expression_status == "stale_expression":
        reason = "当前事实包仍可复用，但旧版判断卡不能作为当前结论；需按 V4 重建判断层。"
    else:
        reason = "事实包已准备完成，需先生成 Agent Judgment V4，不能把量化审计当作投资结论。"
    return "\n".join(
        [
            f"# {name}（{code}）",
            "",
            "## 莫大判断",
            "",
            f"> {reason}",
            "",
        ]
    )


def load_analysis(root: Path, code: str, name: str = "") -> dict[str, Any]:
    scorecard_path = root / "knowledge" / "research" / "scorecards" / f"{code}.json"
    report_path = root / "knowledge" / "research" / "scoring" / f"{code}.md"
    pipeline_path = root / "knowledge" / "research" / "pipeline" / f"{code}.json"
    if not scorecard_path.is_file() or not report_path.is_file() or not pipeline_path.is_file():
        raise FileNotFoundError(f"moda-v4 未生成完整结果：{code}")

    payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    card = payload.get("scorecard") or {}
    evidence = payload.get("evidence") or {}
    thesis = payload.get("thesis") or {}
    research_packet = thesis.get("research_packet") or thesis.get("thesis_context") or {}
    schema_current = research_packet.get("schema_version") == 4
    saved_expression = thesis.get("thesis_output") if isinstance(thesis.get("thesis_output"), dict) else {}
    saved_expression_status = str(thesis.get("expression_status") or "collector_only")
    expression_current = (
        saved_expression_status == "agent_generated"
        and saved_expression.get("schema_version") == 4
        and saved_expression.get("expression_status") == "agent_generated"
    )
    if not schema_current:
        expression_status = "stale_schema"
    elif saved_expression_status != "agent_generated":
        expression_status = saved_expression_status
    else:
        expression_status = "agent_generated" if expression_current else "stale_expression"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    resolved_name = name or evidence.get("name") or code
    report_ready = expression_status == "agent_generated"
    return {
        "code": code,
        "name": resolved_name,
        "research_score": card.get("research_score"),
        "coverage": card.get("coverage"),
        "unknown_maximum": card.get("unknown_maximum"),
        "signal": card.get("signal"),
        "hard_caps": card.get("hard_caps") or [],
        "pipeline": pipeline,
        "research_packet": research_packet,
        "thesis_context": research_packet,
        "collector_status": "ready" if schema_current else "stale",
        "expression_status": expression_status,
        "decision_state": ((saved_expression.get("decision") or {}).get("state")) if expression_current else None,
        "formal_report": report_path.read_text(encoding="utf-8") if report_ready else _pending_judgment_report(resolved_name, code, expression_status),
        "formal_report_status": "ready" if report_ready else "judgment_rebuild_required",
        "next_action": None if report_ready else (
            "rerun_collector" if expression_status == "stale_schema" else "generate_agent_judgment_v4"
        ),
        "report_path": str(report_path),
        "scorecard_path": str(scorecard_path),
        "pipeline_path": str(pipeline_path),
    }


def _scorecard_from_dict(root: Path, payload: dict[str, Any]):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools.scoring.model import AdjustmentResult, FactorResult, Scorecard, SubfactorResult

    factors = tuple(
        FactorResult(
            key=item["key"],
            label=item["label"],
            score=item["score"],
            maximum=item["maximum"],
            subfactors=tuple(
                SubfactorResult(
                    key=subitem["key"],
                    label=subitem["label"],
                    score=subitem["score"],
                    maximum=subitem["maximum"],
                    status=subitem["status"],
                    reason=subitem["reason"],
                    sources=tuple(subitem.get("sources") or ()),
                    verified_points=subitem.get("verified_points", 0.0),
                    provisional_points=subitem.get("provisional_points", 0.0),
                    unknown_maximum=subitem.get("unknown_maximum", 0.0),
                    coverage=subitem.get("coverage", 0.0),
                )
                for subitem in item.get("subfactors") or ()
            ),
            verified_points=item.get("verified_points", 0.0),
            provisional_points=item.get("provisional_points", 0.0),
            unknown_maximum=item.get("unknown_maximum", 0.0),
            coverage=item.get("coverage", 0.0),
        )
        for item in payload.get("factors") or ()
    )
    adjustments = tuple(
        AdjustmentResult(
            key=item["key"],
            label=item["label"],
            score=item["score"],
            minimum=item["minimum"],
            maximum=item["maximum"],
            status=item["status"],
            reason=item["reason"],
            sources=tuple(item.get("sources") or ()),
            verified_points=item.get("verified_points", 0.0),
            provisional_points=item.get("provisional_points", 0.0),
            unknown_maximum=item.get("unknown_maximum", 0.0),
            coverage=item.get("coverage", 0.0),
        )
        for item in payload.get("adjustments") or ()
    )
    return Scorecard(
        factors=factors,
        adjustments=adjustments,
        base_score=payload["base_score"],
        adjustment_score=payload["adjustment_score"],
        final_score=payload["final_score"],
        signal=payload["signal"],
        hard_caps=tuple(payload.get("hard_caps") or ()),
        verified_points=payload.get("verified_points", 0.0),
        provisional_points=payload.get("provisional_points", 0.0),
        unknown_maximum=payload.get("unknown_maximum", 0.0),
        coverage=payload.get("coverage", 0.0),
        research_score=payload.get("research_score", 0.0),
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _append_judgment_snapshot(root: Path, code: str, name: str, output: dict[str, Any], context: dict[str, Any]) -> Path:
    path = root / "knowledge" / "research" / "judgments" / f"{code}.json"
    history_payload: dict[str, Any] = {"schema_version": 1, "code": code, "name": name, "history": []}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("history"), list):
                history_payload = existing
        except (OSError, ValueError):
            pass
    snapshot = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "state": output["decision"]["state"],
        "one_sentence": output["one_sentence"],
        "thesis": output["thesis"]["statement"],
        "verification": output["verification"],
        "valuation_scenarios": context.get("valuation_scenarios") or {},
        "confidence": output.get("confidence", "低"),
    }
    history = history_payload.setdefault("history", [])
    history.append(snapshot)
    history_payload["latest"] = snapshot
    _atomic_write(path, json.dumps(history_payload, ensure_ascii=False, indent=2))
    return path


def finalize_analysis(root: Path, code: str, thesis_payload: dict[str, Any], name: str = "") -> dict[str, Any]:
    scorecard_path = root / "knowledge" / "research" / "scorecards" / f"{code}.json"
    report_path = root / "knowledge" / "research" / "scoring" / f"{code}.md"
    pipeline_path = root / "knowledge" / "research" / "pipeline" / f"{code}.json"
    payload = json.loads(scorecard_path.read_text(encoding="utf-8"))
    evidence = payload.get("evidence") or {}
    card_dict = payload.get("scorecard") or {}
    thesis = payload.get("thesis") or {}
    context = thesis.get("research_packet") or thesis.get("thesis_context") or {}
    if context.get("schema_version") != 4:
        raise ValueError("旧 research_packet 已过期，请重新运行 moda-v4 采集器")

    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools.scoring import grader
    from tools.scoring.thesis import validate_thesis_output

    validated = validate_thesis_output(thesis_payload, context)
    card = _scorecard_from_dict(root, card_dict)
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    requested_modules = tuple(
        str(item.get("label"))
        for item in pipeline
        if isinstance(item, dict) and item.get("ok") and item.get("label") in grader.REPORTS
    )
    resolved_name = name or evidence.get("name") or code
    report = grader.render_report(
        code,
        resolved_name,
        evidence,
        card,
        requested_modules,
        validated,
    )
    thesis["expression_status"] = "agent_generated"
    thesis["thesis_output"] = validated.to_dict()
    thesis["research_packet"] = context
    thesis["thesis_context"] = context
    payload["thesis"] = thesis

    _atomic_write(report_path, report)
    _atomic_write(scorecard_path, json.dumps(payload, ensure_ascii=False, indent=2))
    judgment_path = _append_judgment_snapshot(root, code, resolved_name, validated.to_dict(), context)
    result = load_analysis(root, code, resolved_name)
    result["final_report_ready"] = True
    result["judgment_path"] = str(judgment_path)
    return result


def finalize_a_share(
    query: str,
    thesis_payload: dict[str, Any],
    *,
    moda_root: str | Path | None = None,
    save: bool = False,
) -> dict[str, Any]:
    root = find_moda_root(moda_root)
    code, name = _resolve_stock(root, query)
    result = finalize_analysis(root, code, thesis_payload, name)
    if save and result["formal_report_status"] == "ready":
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from tools.export_skill_output import export

        result["saved_path"] = str(export(result["formal_report"], code, name))
    elif save:
        result["save_deferred"] = True
    return result


def analyze_a_share(
    query: str,
    refresh: bool = False,
    save: bool = False,
    *,
    moda_root: str | Path | None = None,
    run_pipeline: bool = True,
) -> dict[str, Any]:
    root = find_moda_root(moda_root)
    code, name = _resolve_stock(root, query)
    if run_pipeline:
        command = [sys.executable, str(root / "tools" / "run_pipeline.py"), "--stock", code, "--name", name]
        if refresh:
            command.append("--refresh")
        completed = subprocess.run(command, cwd=root, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"moda-v4 流水线运行失败，退出码 {completed.returncode}")

    result = load_analysis(root, code, name)
    if save and result["formal_report_status"] == "ready":
        root_text = str(root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        from tools.export_skill_output import export

        result["saved_path"] = str(export(result["formal_report"], code, name))
    elif save:
        result["save_deferred"] = True
    return result


def _run_logic_baseline(
    query: str,
    *,
    refresh: bool = False,
    moda_root: str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility CLI route; user research starts at Logic Case baseline."""
    logic_path = Path(__file__).with_name("analyze_logic.py")
    spec = importlib.util.spec_from_file_location("moda_companion_logic_cli", logic_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载逻辑优先入口")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.analyze_logic(
        query,
        kind="stock",
        phase="baseline",
        refresh=refresh,
        save=True,
        moda_root=moda_root,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run moda-v4 and return the stable Agent tool result")
    parser.add_argument("query", help="A-share name or six-digit code")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--moda-root")
    parser.add_argument("--report", action="store_true", help="Print the formal Markdown report instead of JSON")
    parser.add_argument("--thesis-json", type=Path, help="Agent expression JSON used to finalize the combined report")
    args = parser.parse_args()
    if args.thesis_json:
        thesis_payload = json.loads(args.thesis_json.read_text(encoding="utf-8"))
        if not isinstance(thesis_payload, dict):
            raise ValueError("--thesis-json 必须包含 JSON 对象")
        result = finalize_a_share(args.query, thesis_payload, moda_root=args.moda_root, save=args.save)
    else:
        result = _run_logic_baseline(args.query, refresh=args.refresh, moda_root=args.moda_root)
    if args.report:
        print(result.get("formal_report") or result.get("logic_report", ""))
    elif args.thesis_json:
        summary = {key: value for key, value in result.items() if key != "formal_report"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
