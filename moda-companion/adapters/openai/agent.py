from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "SKILL.md"
ANALYZE_TOOL = ROOT / "scripts" / "analyze_a_share.py"
SECTOR_TOOL = ROOT / "scripts" / "analyze_sector.py"
LOGIC_TOOL = ROOT / "scripts" / "analyze_logic.py"
MEMORY_TOOL = ROOT / "scripts" / "memory.py"


def _load_analysis_module():
    spec = importlib.util.spec_from_file_location("moda_companion_analysis", ANALYZE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 moda-companion 分析工具")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_sector_module():
    spec = importlib.util.spec_from_file_location("moda_companion_sector", SECTOR_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 moda-companion 板块分析工具")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_logic_module():
    spec = importlib.util.spec_from_file_location("moda_companion_logic", LOGIC_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 moda-companion 逻辑优先入口")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_agent():
    try:
        from agents import Agent, function_tool
    except ImportError as exc:
        raise RuntimeError("请先安装 OpenAI Agents SDK：pip install openai-agents") from exc

    analysis_module = _load_analysis_module()
    sector_module = _load_sector_module()
    logic_module = _load_logic_module()
    memory_spec = importlib.util.spec_from_file_location("moda_companion_memory", MEMORY_TOOL)
    if memory_spec is None or memory_spec.loader is None:
        raise RuntimeError("无法加载 moda-companion 记忆工具")
    memory_module = importlib.util.module_from_spec(memory_spec)
    memory_spec.loader.exec_module(memory_module)

    @function_tool
    def analyze_logic(
        query: str,
        kind: str = "auto",
        phase: str = "baseline",
        candidates: list[str] | None = None,
        evidence_kinds: list[str] | None = None,
        refresh: bool = False,
        save: bool = True,
    ) -> dict[str, Any]:
        """Primary Logic-First entry for both stocks and sectors.

        Run baseline first, create and validate a falsifiable Logic Case, then
        follow the returned status/next_action state machine. Evidence or deep
        cannot bypass the Logic Case gate. This tool never presents a
        collector-only score report as an investment conclusion.
        """
        return logic_module.analyze_logic(
            query,
            kind=kind,
            phase=phase,
            refresh=refresh,
            candidates=candidates,
            evidence_kinds=evidence_kinds,
            save=save,
        )

    @function_tool
    def finalize_logic_case(query: str, logic_json: str, save: bool = True) -> dict[str, Any]:
        """Validate and persist the Logic Case before targeted evidence or deep research."""
        payload = json.loads(logic_json)
        if not isinstance(payload, dict):
            raise ValueError("logic_json must contain a JSON object")
        return logic_module.finalize_logic_case(query, payload, save=save)

    @function_tool
    def finalize_logic_report(
        query: str,
        logic_json: str,
        judgment_json: str,
        save: bool = False,
    ) -> dict[str, Any]:
        """Run fresh deep evidence, validate Judgment V4, and return the complete report."""
        logic_payload = json.loads(logic_json)
        judgment_payload = json.loads(judgment_json)
        if not isinstance(logic_payload, dict) or not isinstance(judgment_payload, dict):
            raise ValueError("logic_json and judgment_json must contain JSON objects")
        return logic_module.finalize_logic_report(
            query,
            logic_payload,
            judgment_payload,
            save=save,
        )

    @function_tool
    def analyze_a_share(query: str, refresh: bool = False, save: bool = False) -> dict[str, Any]:
        """Legacy evidence-only A-share entry; prefer analyze_logic.

        When formal_report_status is judgment_rebuild_required, create a V4
        judgment from research_packet and call finalize_a_share_report before
        showing a report to the user.
        """
        return analysis_module.analyze_a_share(query, refresh, save)

    @function_tool
    def finalize_a_share_report(query: str, thesis_json: str, save: bool = False) -> dict[str, Any]:
        """Validate Agent Judgment V4, embed it in the report, and return the complete combined report."""
        payload = json.loads(thesis_json)
        if not isinstance(payload, dict):
            raise ValueError("thesis_json must contain a JSON object")
        return analysis_module.finalize_a_share(query, payload, save=save)

    @function_tool
    def discover_sector_candidates(sector: str, limit: int = 3) -> dict[str, Any]:
        """Build a legacy bounded local research universe; use screen_sector for full-universe selection."""
        root = analysis_module.find_moda_root()
        return sector_module.discover_sector_candidates(root, sector, limit)

    @function_tool
    def screen_sector(
        sector: str,
        candidates: list[str] | None = None,
        screen_limit: int = 6,
        save: bool = False,
        use_live_universe: bool = True,
        fetch_business: bool = True,
    ) -> dict[str, Any]:
        """Run a full-universe Moda quick screen and return the first six research candidates.

        This tool only collects lightweight sector, market, and F10 business
        snapshots. It does not run full individual-stock pipelines, create a
        research score, or issue a five-state stock decision. Ask the user for
        confirmation before any full individual research.
        """
        return sector_module.screen_sector(
            sector,
            candidates,
            save=save,
            use_live_universe=use_live_universe,
            fetch_business=fetch_business,
            shortlist_limit=screen_limit,
        )

    @function_tool
    def analyze_sector(
        sector: str,
        candidates: list[str] | None = None,
        refresh: bool = False,
        save: bool = False,
        sector_evidence_json: str = "",
        collect_sector: bool = False,
        sector_context: str = "",
        sector_provider: str = "",
        sector_timeout: int = 12,
    ) -> dict[str, Any]:
        """Build a deep sector card from explicit candidate facts.

        This is not the default board-selection route. Use ``screen_sector``
        first, then call individual-stock research only after the user chooses
        which shortlist to investigate. ``collect_sector`` explicitly enables
        industry web evidence.
        """
        sector_evidence: dict[str, Any] | None = None
        if sector_evidence_json.strip():
            parsed = json.loads(sector_evidence_json)
            if not isinstance(parsed, dict):
                raise ValueError("sector_evidence_json must contain a JSON object")
            sector_evidence = parsed
        return sector_module.analyze_sector(
            sector,
            candidates,
            refresh=refresh,
            save=save,
            collect=False,
            max_candidates=3,
            sector_evidence=sector_evidence,
            collect_sector=collect_sector,
            sector_context=sector_context,
            sector_provider=sector_provider or None,
            sector_timeout=sector_timeout,
        )

    @function_tool
    def remember_preference(key: str, value: str) -> dict[str, Any]:
        """Store a reusable non-sensitive user preference or public research note."""
        return memory_module.remember(key, value)

    @function_tool
    def forget_preference(key: str) -> dict[str, bool]:
        """Delete a previously stored Moda Companion memory by key."""
        return {"removed": memory_module.forget(key)}

    instructions = SKILL.read_text(encoding="utf-8")
    return Agent(
        name="莫大 Agent",
        instructions=instructions,
        tools=[
            analyze_logic,
            finalize_logic_case,
            finalize_logic_report,
            remember_preference,
            forget_preference,
        ],
    )


agent = build_agent()
