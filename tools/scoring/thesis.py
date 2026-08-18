"""Evidence packet and executable Agent Judgment V4 contract.

The six-factor engine remains authoritative for facts, evidence coverage and
Hard Caps. This module turns those facts into an auditable investment thesis
without mapping research scores to buy/sell labels.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping


DECISION_STATES = {"观察", "等待", "试错", "买入", "退出"}
JUDGMENT_SCHEMA_VERSION = 4
BUSINESS_QUALITIES = {"优秀", "合格", "一般", "较弱", "需人工确认"}
INDUSTRY_TIMINGS = {"正在改善", "等待验证", "已经透支", "正在走弱", "需人工确认"}
PROFIT_STATES = {"已兑现", "开始兑现", "只有线索", "未受益", "需人工确认"}
EXPECTATION_STATES = {"预期偏低", "预期合理", "预期偏高", "需人工确认"}
DRIVER_STAGES = {
    "纯题材", "预期", "订单", "业绩", "估值修复", "周期反转",
    "产业趋势", "资金抱团", "混合", "需人工确认",
}
CHAIN_STATUSES = {"已验证", "部分验证", "缺失", "矛盾"}
PEER_VERDICTS = {"最优候选", "可比候选", "无法证明优于同行", "非优选", "需人工确认"}
UNKNOWN_MARKERS = ("需人工确认", "搜索失败", "已搜索未命中", "网络命中（未核验）")
RAW_SEARCH_ERROR_MARKERS = (
    "connectionerror",
    "duckduckgo:",
    "model_search:",
    "not_configured",
    "traceback",
    "search_budget_exhausted",
    "target_budget_exhausted",
    "target_limit_exceeded",
    "搜索失败",
    "已搜索未命中",
)
SCORECARD_FRONT_MARKERS = (
    "action_rating", "行动评级", "research_score", "研究评分", "证据覆盖率", "覆盖率",
    "Hard Cap", "hard cap", "F1", "F2", "F3", "F4", "F5", "F6",
    "评分", "得分", "分数", "量化",
)


class ThesisValidationError(ValueError):
    """Raised when an Agent response violates the investment-judgment contract."""


@dataclass(frozen=True)
class ThesisContext:
    company: dict[str, Any]
    industry: dict[str, Any]
    realization: dict[str, Any]
    peers: tuple[dict[str, Any], ...]
    valuation: dict[str, Any]
    valuation_scenarios: dict[str, Any]
    system_change: dict[str, Any]
    bottleneck: dict[str, Any]
    survival: dict[str, Any]
    market_stage: dict[str, Any]
    a_share_signals: dict[str, Any]
    decision_gates: dict[str, Any]
    research: dict[str, Any]
    data_quality: dict[str, Any]
    prior_judgment: dict[str, Any]
    evidence_gaps: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 4,
            "packet_role": "collector_only",
            "company": self.company,
            "industry": self.industry,
            "realization": self.realization,
            "peers": list(self.peers),
            "valuation": self.valuation,
            "valuation_scenarios": self.valuation_scenarios,
            "system_change": self.system_change,
            "bottleneck": self.bottleneck,
            "survival": self.survival,
            "market_stage": self.market_stage,
            "a_share_signals": self.a_share_signals,
            "decision_gates": self.decision_gates,
            "research": self.research,
            "data_quality": self.data_quality,
            "prior_judgment": self.prior_judgment,
            "evidence_gaps": list(self.evidence_gaps),
            "decision_rules": {
                "states": ["观察", "等待", "试错", "买入", "退出"],
                "order": "为什么现在进入视野 -> 过去为何受压 -> 边际变化 -> 产业链与利润池 -> 公司受益 -> 同行选择 -> 市场交易阶段 -> 基本面兑现阶段 -> 赔率 -> 决策 -> 验证",
                "observe": "覆盖率低于60%，或核心投资链尚未形成",
                "wait": "逻辑可研究，但利润、预期差、同行优势或赔率不足",
                "trial": "核心逻辑成立、赔率至少2:1，但仍有关键条件待确认",
                "buy": "覆盖率至少60%、利润开始兑现、两家同行比较、预期差验证、无关键断点、赔率至少3:1",
                "exit": "核心链被证伪、生意较弱、确认未受益，或触发强制退出 Hard Cap",
            },
        }


@dataclass(frozen=True)
class ThesisOutput:
    schema_version: int
    one_sentence: str
    core_contradiction: dict[str, Any]
    industry_positioning: dict[str, Any]
    thesis: dict[str, Any]
    why_watch: dict[str, Any]
    reversal_judgment: dict[str, Any]
    business_judgment: dict[str, Any]
    industry_judgment: dict[str, Any]
    profit_judgment: dict[str, Any]
    causal_chain: tuple[dict[str, Any], ...]
    causal_breakpoint: dict[str, Any]
    why_this_company: dict[str, Any]
    market_expectation: dict[str, Any]
    driver_judgment: dict[str, Any]
    bull_case: dict[str, Any]
    base_case: dict[str, Any]
    bear_case: dict[str, Any]
    valuation_interpretation: dict[str, Any]
    decision: dict[str, Any]
    verification: dict[str, Any]
    state_transition: dict[str, Any]
    confidence: str
    expression_status: str = "agent_generated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "one_sentence": self.one_sentence,
            "core_contradiction": self.core_contradiction,
            "industry_positioning": self.industry_positioning,
            "thesis": self.thesis,
            "why_watch": self.why_watch,
            "reversal_judgment": self.reversal_judgment,
            "business_judgment": self.business_judgment,
            "industry_judgment": self.industry_judgment,
            "profit_judgment": self.profit_judgment,
            "causal_chain": list(self.causal_chain),
            "causal_breakpoint": self.causal_breakpoint,
            "why_this_company": self.why_this_company,
            "market_expectation": self.market_expectation,
            "driver_judgment": self.driver_judgment,
            "bull_case": self.bull_case,
            "base_case": self.base_case,
            "bear_case": self.bear_case,
            "valuation_interpretation": self.valuation_interpretation,
            "decision": self.decision,
            "verification": self.verification,
            "state_transition": self.state_transition,
            "confidence": self.confidence,
            "expression_status": self.expression_status,
        }


def _value(evidence: Mapping[str, Any], key: str, default: Any = "需人工确认") -> Any:
    value = evidence.get(key)
    return default if value is None or value == "" else value


def _status(evidence: Mapping[str, Any], key: str) -> str:
    sources = evidence.get("metric_sources")
    return "已验证" if isinstance(sources, Mapping) and sources.get(key) else "需人工确认"


def _fact(evidence: Mapping[str, Any], key: str) -> dict[str, Any]:
    sources = evidence.get("metric_sources")
    source_values = sources.get(key, []) if isinstance(sources, Mapping) else []
    return {
        "value": _value(evidence, key),
        "status": _status(evidence, key),
        "sources": list(source_values) if isinstance(source_values, (list, tuple)) else [str(source_values)],
    }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quantiles(value: Any, minimum: int) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    result = {key: _number(value.get(key)) for key in ("q20", "q50", "q80")}
    samples = _number(value.get("samples"))
    if samples is None or samples < minimum or any(item is None or item <= 0 for item in result.values()):
        return None
    return {**{key: float(item) for key, item in result.items()}, "samples": int(samples)}


def build_valuation_scenarios(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Build deterministic historical-valuation scenarios from verified inputs."""
    price = _number(evidence.get("latest_price"))
    pe = _number(evidence.get("pe_ttm"))
    pb = _number(evidence.get("pb"))
    profit = _number(evidence.get("net_profit"))
    pe_history = _quantiles(evidence.get("pe_history_quantiles_5y"), 250)
    pb_history = _quantiles(evidence.get("pb_history_quantiles_5y"), 250)
    price_history = _quantiles(evidence.get("price_history_quantiles_3y"), 720)

    method = "unavailable"
    basis = None
    quantiles = None
    unit_label = ""
    if price and price > 0 and profit is not None and profit > 0 and pe and pe > 0 and pe_history:
        method, basis, quantiles, unit_label = "pe_history_5y", price / pe, pe_history, "每股盈利"
    elif price and price > 0 and pb and pb > 0 and pb_history:
        method, basis, quantiles, unit_label = "pb_history_5y", price / pb, pb_history, "每股净资产"
    elif price and price > 0 and pe and pe > 0 and pe_history:
        method, basis, quantiles, unit_label = "pe_history_5y", price / pe, pe_history, "每股盈利"
    elif price and price > 0 and price_history:
        method, basis, quantiles, unit_label = "price_history_3y_fallback", 1.0, price_history, "历史价格"

    if price is None or price <= 0 or basis is None or quantiles is None:
        return {
            "status": "insufficient_evidence",
            "method": method,
            "model_label": "历史估值情景不可用",
            "reason": "缺少有效当前价格、估值历史或足够样本",
            "risk_reward": None,
            "risk_reward_status": "不可计算",
        }

    scenario_prices = {
        "bear": basis * quantiles["q20"],
        "base": basis * quantiles["q50"],
        "bull": basis * quantiles["q80"],
    }
    if method == "price_history_3y_fallback":
        scenario_prices = {key: quantiles[q] for key, q in (("bear", "q20"), ("base", "q50"), ("bull", "q80"))}
    scenarios = {
        key: {
            "price": round(value, 2),
            "return": round(value / price - 1.0, 4),
            "multiple": round(quantiles[q], 4),
        }
        for key, value, q in (
            ("bear", scenario_prices["bear"], "q20"),
            ("base", scenario_prices["base"], "q50"),
            ("bull", scenario_prices["bull"], "q80"),
        )
    }
    downside = max(0.0, -scenarios["bear"]["return"])
    upside = max(0.0, scenarios["bull"]["return"])
    ratio = round(upside / downside, 2) if downside > 0 else None
    return {
        "status": "ready",
        "method": method,
        "model_label": "五年历史PE估值回归" if method.startswith("pe_") else "五年历史PB估值回归" if method.startswith("pb_") else "三年价格分位降级模型",
        "current_price": round(price, 2),
        "fundamental_basis": round(basis, 6),
        "fundamental_label": unit_label,
        "samples": quantiles["samples"],
        "scenarios": scenarios,
        "risk_reward": ratio,
        "risk_reward_status": "可计算" if ratio is not None else "悲观情景未覆盖当前价以下的风险",
        "formula": "当前每股基本面保持不变 x 历史估值20/50/80分位；该结果不是目标价或盈利预测",
    }


def _peer_rows(evidence: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    rows = evidence.get("peer_comparison")
    if not isinstance(rows, list):
        rows = evidence.get("peer_candidates")
    return tuple(row for row in (rows or []) if isinstance(row, dict))


def _gap_rows(card: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    gaps: list[dict[str, Any]] = []
    for factor in card.get("factors", []) if isinstance(card.get("factors"), list) else []:
        for item in factor.get("subfactors", []) if isinstance(factor, Mapping) else []:
            status = str(item.get("status") or "") if isinstance(item, Mapping) else ""
            if status and status != "已验证":
                gaps.append({
                    "key": item.get("key", ""),
                    "label": item.get("label", ""),
                    "status": status,
                    "reason": item.get("reason", "需人工确认"),
                })
    return tuple(gaps)


def build_thesis_context_from_dict(card: Mapping[str, Any], evidence: Mapping[str, Any]) -> ThesisContext:
    subfactors = {
        item.get("key"): item
        for factor in card.get("factors", []) if isinstance(factor, Mapping)
        for item in factor.get("subfactors", []) if isinstance(item, Mapping) and item.get("key")
    }

    def factor_status(key: str) -> str:
        item = subfactors.get(key)
        return str(item.get("status") or "需人工确认") if isinstance(item, Mapping) else "需人工确认"

    def factor_reason(key: str) -> str:
        item = subfactors.get(key)
        return str(item.get("reason") or "需人工确认") if isinstance(item, Mapping) else "需人工确认"

    peers = _peer_rows(evidence)
    verified_peers = [item for item in peers if item.get("status") == "已验证"]
    hard_caps = list(card.get("hard_caps") or [])
    return ThesisContext(
        company={
            "code": _value(evidence, "code"),
            "name": _value(evidence, "name"),
            "main_business": _value(evidence, "main_business"),
            "business_items": list(evidence.get("business_items") or [])[:8],
            "business_breakdown": list(evidence.get("business_breakdown") or [])[:8],
            "company_profile": dict(evidence.get("company_profile") or {}),
            "business_intro_ths": dict(evidence.get("business_intro_ths") or {}),
            "business_crosscheck": dict(evidence.get("business_crosscheck") or {}),
            "peer_snapshot": dict(evidence.get("industry_peer_snapshot") or {}),
            "chain_match_type": _value(evidence, "chain_match_type"),
            "annual_report": evidence.get("annual_report") or {},
        },
        industry={
            "chain_name": _value(evidence, "chain_name", "产业链待确认"),
            "chain_stage": _value(evidence, "chain_stage", "位置待确认"),
            "prosperity_status": _value(evidence, "industry_prosperity_status"),
            "industry_mapping": evidence.get("industry_mapping") or {},
        },
        realization={
            "business_match_status": factor_status("business_match"),
            "business_match_reason": factor_reason("business_match"),
            "realization_status": factor_status("realization"),
            "realization_reason": factor_reason("realization"),
            "revenue_yoy": _fact(evidence, "revenue_yoy"),
            "profit_yoy": _fact(evidence, "profit_yoy"),
            "net_profit": _fact(evidence, "net_profit"),
            "order_growth": _fact(evidence, "order_growth"),
            "operating_cashflow": _fact(evidence, "operating_cashflow"),
        },
        peers=peers,
        valuation={
            key: _fact(evidence, key)
            for key in (
                "latest_price", "pe_ttm", "pb", "pe_percentile_5y", "pb_percentile_5y",
                "peer_pe_ttm_median", "price_percentile_3y",
            )
        } | {
            "valuation_status": factor_status("valuation"),
            "valuation_reason": factor_reason("valuation"),
        },
        valuation_scenarios=build_valuation_scenarios(evidence),
        system_change={
            "era_track": {"status": factor_status("era_track"), "reason": factor_reason("era_track")},
            "capital_expenditure": {"status": factor_status("capex_wave"), "reason": factor_reason("capex_wave")},
            "supply_demand": {"status": factor_status("supply_gap"), "reason": factor_reason("supply_gap")},
        },
        bottleneck={
            "upstream_position": {"status": factor_status("upstream"), "reason": factor_reason("upstream")},
            "chokepoint": {"status": factor_status("chokepoint"), "reason": factor_reason("chokepoint")},
            "leadership": {"status": factor_status("leadership"), "reason": factor_reason("leadership")},
        },
        survival={
            key: {"status": factor_status(key), "reason": factor_reason(key)}
            for key in ("background", "financial_safety", "survival_risk")
        },
        market_stage={
            "expectation_gap_status": factor_status("expectation_gap"),
            "expectation_gap_reason": factor_reason("expectation_gap"),
            "price_position_status": factor_status("price_position"),
            "price_position_reason": factor_reason("price_position"),
        },
        a_share_signals={
            "technical_signal": _value(evidence, "technical_signal"),
            "attention_heat": _fact(evidence, "attention_heat"),
            "market_congestion": _fact(evidence, "market_congestion"),
            "verified_catalyst_count": _fact(evidence, "verified_catalyst_count"),
            "order_growth": _fact(evidence, "order_growth"),
            "revenue_yoy": _fact(evidence, "revenue_yoy"),
            "profit_yoy": _fact(evidence, "profit_yoy"),
        },
        decision_gates={
            "coverage": card.get("coverage"),
            "peer_count_verified": len(verified_peers),
            "expectation_gap_status": factor_status("expectation_gap"),
            "realization_status": factor_status("realization"),
            "hard_caps": hard_caps,
        },
        research={
            "research_score": card.get("research_score"),
            "coverage": card.get("coverage"),
            "unknown_maximum": card.get("unknown_maximum"),
            "hard_caps": hard_caps,
            "signal": card.get("signal"),
        },
        data_quality={
            "coverage": card.get("coverage"),
            "web_research_status": _value(evidence, "web_research_status"),
            "web_research_provider": _value(evidence, "web_research_provider"),
            "peer_count_verified": len(verified_peers),
            "industry_peer_snapshot_fetch_state": _value(
                evidence.get("industry_peer_snapshot") or {},
                "fetch_state",
            ),
        },
        prior_judgment=dict(evidence.get("prior_judgment") or {}),
        evidence_gaps=_gap_rows(card),
    )


def build_thesis_context(card: Any, evidence: Mapping[str, Any]) -> ThesisContext:
    card_dict = card.to_dict() if hasattr(card, "to_dict") else dict(card or {})
    return build_thesis_context_from_dict(card_dict, evidence)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThesisValidationError(f"缺少 {label}")
    return value.strip()


def _text_list(value: Any, label: str, minimum: int = 1, maximum: int = 8) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ThesisValidationError(f"{label} 必须包含 {minimum}-{maximum} 项")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ThesisValidationError(f"{label} 包含空项")
    return [item.strip() for item in value]


def _reference_value(context: Mapping[str, Any], reference: str) -> Any:
    current: Any = context
    for part in reference.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _is_confirmed(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not any(marker in value for marker in UNKNOWN_MARKERS)
    if isinstance(value, Mapping):
        if value.get("status") in {"需人工确认", "insufficient_evidence"}:
            return False
        return any(_is_confirmed(item) for key, item in value.items() if key != "sources")
    if isinstance(value, (list, tuple)):
        return any(_is_confirmed(item) for item in value)
    return True


def _refs(raw: Mapping[str, Any], label: str, context: Mapping[str, Any]) -> list[str]:
    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item.strip() for item in refs):
        raise ThesisValidationError(f"{label}.evidence_refs 必须包含字段路径")
    refs = [item.strip() for item in refs]
    missing = [item for item in refs if _reference_value(context, item) is None]
    if missing:
        raise ThesisValidationError(f"{label}.evidence_refs 不存在：{'、'.join(missing)}")
    return refs


def _block(payload: Mapping[str, Any], key: str, fields: tuple[str, ...], context: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get(key)
    if not isinstance(raw, Mapping):
        raise ThesisValidationError(f"缺少 {key}")
    result = {field: _text(raw.get(field), f"{key}.{field}") for field in fields}
    result["evidence_refs"] = _refs(raw, key, context)
    return result


def _contains_unknown_text(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value for marker in UNKNOWN_MARKERS)
    if isinstance(value, Mapping):
        return any(_contains_unknown_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unknown_text(item) for item in value)
    return False


def _contains_decision_state(value: str) -> bool:
    return any(state in value for state in DECISION_STATES)


def _reject_raw_search_errors(value: Any, label: str = "Agent Judgment") -> None:
    """Keep operational search failures in diagnostics, never in user-facing prose."""
    if isinstance(value, str):
        lower = value.lower()
        if any(marker in lower for marker in RAW_SEARCH_ERROR_MARKERS):
            raise ThesisValidationError(f"{label} 不得包含原始网络搜索错误")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_raw_search_errors(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_raw_search_errors(item, f"{label}.{index}")


def _reject_scorecard_front_terms(value: Any, label: str = "Agent Judgment") -> None:
    if isinstance(value, str):
        if any(marker in value for marker in SCORECARD_FRONT_MARKERS):
            raise ThesisValidationError(f"{label} 不得把六层评分或旧行动评级写入判断卡片")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_scorecard_front_terms(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_scorecard_front_terms(item, f"{label}.{index}")


def _judgment_block(
    payload: Mapping[str, Any],
    key: str,
    context: Mapping[str, Any],
    *,
    fields: tuple[str, ...] = (),
    allow_unknown: bool = False,
) -> dict[str, Any]:
    """Validate a visible judgment block with the fact -> meaning -> decision discipline."""
    raw = payload.get(key)
    if not isinstance(raw, Mapping):
        raise ThesisValidationError(f"缺少 {key}")
    result = {
        "fact": _text(raw.get("fact"), f"{key}.fact"),
        "meaning": _text(raw.get("meaning"), f"{key}.meaning"),
        "decision_impact": _text(raw.get("decision_impact"), f"{key}.decision_impact"),
    }
    for field in fields:
        result[field] = _text(raw.get(field), f"{key}.{field}")
    result["evidence_refs"] = _refs(raw, key, context)
    _require_confirmed(
        result,
        key,
        context,
        allow_unknown or _contains_unknown_text(result),
    )
    return result


def _refs_union(*blocks: Any) -> list[str]:
    refs: list[str] = []
    for block in blocks:
        if isinstance(block, Mapping):
            candidates = block.get("evidence_refs")
        else:
            candidates = block
        if isinstance(candidates, (list, tuple)):
            for item in candidates:
                if isinstance(item, str) and item not in refs:
                    refs.append(item)
    return refs


def _first_text(values: Any, fallback: str) -> str:
    if isinstance(values, list):
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return fallback


def _v3_block(fact: str, meaning: str, decision_impact: str, evidence_refs: list[str], **extra: Any) -> dict[str, Any]:
    return {
        **extra,
        "fact": fact,
        "meaning": meaning,
        "decision_impact": decision_impact,
        "evidence_refs": evidence_refs,
    }


def _matching_verified_peers(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    peers = context.get("peers") if isinstance(context.get("peers"), list) else []
    return [item for item in peers if isinstance(item, Mapping) and item.get("status") == "已验证"]


def _upgrade_v2_payload(payload: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Convert historical V2 output to the V3 storage and rendering contract.

    The conversion deliberately does not infer a stronger conclusion. Missing
    peer evidence stays a non-selection conclusion and old prose becomes the
    evidence-backed factual layer of a V3 block.
    """
    upgraded = deepcopy(dict(payload))
    upgraded["schema_version"] = JUDGMENT_SCHEMA_VERSION

    thesis = upgraded.get("thesis") if isinstance(upgraded.get("thesis"), Mapping) else {}
    business = upgraded.get("business_judgment") if isinstance(upgraded.get("business_judgment"), Mapping) else {}
    industry = upgraded.get("industry_judgment") if isinstance(upgraded.get("industry_judgment"), Mapping) else {}
    profit = upgraded.get("profit_judgment") if isinstance(upgraded.get("profit_judgment"), Mapping) else {}
    why_company = upgraded.get("why_this_company") if isinstance(upgraded.get("why_this_company"), Mapping) else {}
    expectation = upgraded.get("market_expectation") if isinstance(upgraded.get("market_expectation"), Mapping) else {}
    decision = upgraded.get("decision") if isinstance(upgraded.get("decision"), Mapping) else {}
    verification = upgraded.get("verification") if isinstance(upgraded.get("verification"), Mapping) else {}
    chain = upgraded.get("causal_chain") if isinstance(upgraded.get("causal_chain"), list) else []

    thesis_refs = _refs_union(thesis, business, industry, profit)
    why_refs = _refs_union(why_company, business, profit)
    decision_refs = _refs_union(decision, verification, thesis)
    contradiction_fact = str(profit.get("summary") or business.get("summary") or thesis.get("statement") or "核心事实待确认")
    if _contains_decision_state(str(upgraded.get("one_sentence") or "")):
        upgraded["one_sentence"] = str(thesis.get("statement") or contradiction_fact)
    contradiction_meaning = str(upgraded.get("one_sentence") or thesis.get("statement") or "当前主要矛盾待确认")
    decision_rationale = str(decision.get("rationale") or "等待关键证据进一步确认。")
    upgraded.setdefault("core_contradiction", _v3_block(
        contradiction_fact,
        contradiction_meaning,
        decision_rationale,
        thesis_refs or ["company.main_business"],
        statement=contradiction_meaning,
    ))

    upgraded.setdefault("why_watch", _v3_block(
        f"{industry.get('summary') or '产业变化待确认'} {business.get('summary') or '公司产业位置待确认'}",
        str(thesis.get("statement") or "产业位置能否转成利润仍待验证。"),
        decision_rationale,
        thesis_refs or ["company.main_business"],
        industry_change=str(industry.get("summary") or "产业变化待确认"),
        company_position=str(business.get("summary") or "公司位置待确认"),
        scarcity=_first_text(why_company.get("advantages"), "稀缺性待确认"),
        profit_pool=str(why_company.get("profit_pool") or "利润池待确认"),
    ))

    for key, block in (("thesis", thesis), ("business_judgment", business), ("industry_judgment", industry), ("profit_judgment", profit)):
        if isinstance(block, dict):
            refs = _refs_union(block) or thesis_refs or ["company.main_business"]
            block.setdefault("fact", str(block.get("summary") or block.get("statement") or "事实待确认"))
            block.setdefault("meaning", str(thesis.get("statement") or block.get("summary") or "含义待确认"))
            block.setdefault("decision_impact", decision_rationale)
            block.setdefault("evidence_refs", refs)
            upgraded[key] = block

    if isinstance(expectation, dict):
        expectation.setdefault("known", str(expectation.get("priced_in") or "市场已知信息待确认"))
        expectation.setdefault("unpriced", str(expectation.get("underappreciated") or "未充分反映部分待确认"))
        expectation.setdefault("mispriced", "当前估值与基本面匹配度仍需由后续验证。")
        expectation.setdefault("fact", expectation["known"])
        expectation.setdefault("meaning", expectation["unpriced"])
        expectation.setdefault("decision_impact", decision_rationale)
        expectation.setdefault("evidence_refs", _refs_union(expectation) or ["market_stage.expectation_gap_reason"])
        upgraded["market_expectation"] = expectation

    driver = upgraded.get("driver_judgment")
    if isinstance(driver, dict):
        driver.setdefault("fact", str(driver.get("summary") or "当前市场驱动待确认"))
        driver.setdefault("meaning", "市场驱动需要由基本面和预期差共同验证。")
        driver.setdefault("decision_impact", decision_rationale)
        driver.setdefault("evidence_refs", _refs_union(driver) or ["a_share_signals.profit_yoy"])
        upgraded["driver_judgment"] = driver

    verified_peers = _matching_verified_peers(context)
    if isinstance(why_company, dict):
        peer_comparison = why_company.get("peer_comparison")
        if not isinstance(peer_comparison, list):
            peer_comparison = [
                {
                    "company": str(row.get("name") or row.get("code") or "已验证同行"),
                    "industry_position": "已验证直接同行",
                    "core_barrier": "详细壁垒以同行比较原始证据为准",
                    "profit_realization": "经营兑现细项待进一步比较",
                    "valuation_and_odds": "估值与赔率需结合同口径数据复核",
                    "current_choice": "不单独形成优选",
                    "evidence_refs": ["peers"],
                }
                for row in verified_peers[:3]
            ]
        why_company.setdefault("peer_comparison", peer_comparison)
        why_company.setdefault(
            "selection_conclusion",
            "行业值得研究，但目前没有一家形成明显优选。"
            if why_company.get("peer_verdict") != "最优候选"
            else "若只选一家，仍需以两家已验证直接同行的完整对比证明优选。",
        )
        why_company.setdefault("fact", str(why_company.get("profit_pool") or "利润池待确认"))
        why_company.setdefault("meaning", _first_text(why_company.get("advantages"), "公司相对优势待确认"))
        why_company.setdefault("decision_impact", decision_rationale)
        why_company.setdefault("evidence_refs", why_refs or ["peers"])
        upgraded["why_this_company"] = why_company

    critical = next((item for item in chain if isinstance(item, Mapping) and item.get("status") != "已验证"), None)
    if isinstance(critical, Mapping):
        link = f"{critical.get('from', '上游')} -> {critical.get('to', '下游')}"
        reason = str(critical.get("claim") or "该因果箭头尚未完成验证")
        critical_refs = _refs_union(critical) or thesis_refs or ["realization.order_growth"]
        closure = [_first_text(thesis.get("required_conditions"), "补充该箭头的正式披露或连续经营数据")]
    else:
        link = "当前无关键断点"
        reason = "已列出的核心箭头均有验证，仍需按后续报告持续复核。"
        critical_refs = thesis_refs or ["realization.order_growth"]
        closure = ["后续报告继续验证订单、利润和现金流的一致性"]
    upgraded.setdefault("causal_breakpoint", _v3_block(
        reason,
        "该箭头决定产业逻辑能否转成投资回报。",
        decision_rationale,
        critical_refs,
        key_link=link,
        reason=reason,
        closure_conditions=closure,
    ))

    for key, default_summary in (
        ("bull_case", "订单、份额和利润率同步改善时，产业位置可能转成更强利润兑现。"),
        ("bear_case", "订单、利润或现金流转弱时，当前投资逻辑需要被下调或证伪。"),
    ):
        case = upgraded.get(key)
        if isinstance(case, dict):
            case.setdefault("summary", default_summary)
            case.setdefault("conditions", ["关键经营变量按预期发展"])
            case.setdefault("fact", str(case["summary"]))
            case.setdefault("meaning", "基本面情景需要与估值情景分开理解。")
            case.setdefault("decision_impact", decision_rationale)
            case.setdefault("evidence_refs", _refs_union(case) or thesis_refs or ["realization.order_growth"])
            upgraded[key] = case
    upgraded.setdefault("base_case", _v3_block(
        str(thesis.get("statement") or "产业与利润按正常节奏传导。"),
        "行业正常发展并不自动等于估值继续扩张。",
        decision_rationale,
        thesis_refs or ["realization.revenue_yoy"],
        summary="产业、订单和利润按当前已验证节奏传导，不额外假设份额或估值跃升。",
        conditions=list(thesis.get("required_conditions") or ["订单与利润维持正常传导"])[:5],
    ))

    valuation = upgraded.get("valuation_interpretation")
    if isinstance(valuation, dict):
        valuation.setdefault("fact", str(valuation.get("conclusion") or "历史估值情景待确认"))
        valuation.setdefault("meaning", "估值情景只是历史参考，不是目标价。")
        valuation.setdefault("decision_impact", decision_rationale)
        valuation.setdefault("evidence_refs", ["valuation_scenarios"])
        upgraded["valuation_interpretation"] = valuation

    if isinstance(decision, dict):
        state = str(decision.get("state") or "等待")
        decision.setdefault("fact", decision_rationale)
        decision.setdefault("meaning", str(upgraded.get("one_sentence") or "当前结论待确认"))
        decision.setdefault("decision_impact", f"当前维持{state}，直到关键条件发生变化。")
        decision.setdefault("why_not_higher_state", decision_rationale)
        decision.setdefault("evidence_refs", decision_refs or ["decision_gates"])
        upgraded["decision"] = decision

    if isinstance(verification, dict):
        verification.setdefault("fact", str(verification.get("next_event") or "下一验证事件待确认"))
        verification.setdefault("meaning", "关键变量将决定当前判断能否升级或降级。")
        verification.setdefault("decision_impact", decision_rationale)
        verification.setdefault("evidence_refs", _refs_union(verification) or ["realization.order_growth"])
        verification.setdefault("top_variables", [
            {
                "variable": "订单持续性",
                "why": "订单决定产业逻辑能否先转成收入。",
                "window": str(verification.get("window") or "未来两个报告期"),
                "upgrade_signal": str(verification.get("upgrade_if") or "订单继续改善"),
                "downgrade_signal": str(verification.get("downgrade_if") or "订单连续转弱"),
                "evidence_refs": ["realization.order_growth"],
            },
            {
                "variable": "利润与现金流",
                "why": "利润和现金流共同验证收入质量。",
                "window": str(verification.get("window") or "未来两个报告期"),
                "upgrade_signal": str(verification.get("upgrade_if") or "利润和现金流同步改善"),
                "downgrade_signal": str(verification.get("downgrade_if") or "利润或现金流转弱"),
                "evidence_refs": ["realization.profit_yoy", "realization.operating_cashflow"],
            },
            {
                "variable": "同行优势与预期差",
                "why": "相对优势和市场预期决定是否能从研究转为更高状态。",
                "window": str(verification.get("window") or "未来两个报告期"),
                "upgrade_signal": "两家直接同行比较和预期差获得验证。",
                "downgrade_signal": "同行持续领先或市场预期继续抬升。",
                "evidence_refs": ["peers", "market_stage.expectation_gap_reason"],
            },
        ])
        upgraded["verification"] = verification
    return upgraded


def _require_confirmed(block: Mapping[str, Any], label: str, context: Mapping[str, Any], allow_unknown: bool = False) -> None:
    if allow_unknown:
        return
    if not any(_is_confirmed(_reference_value(context, path)) for path in block["evidence_refs"]):
        raise ThesisValidationError(f"{label}.evidence_refs 只有待确认信息，不能支持肯定判断")


def _validate_state(output: dict[str, Any], context: Mapping[str, Any]) -> None:
    state = output["decision"]["state"]
    gates = context.get("decision_gates") if isinstance(context.get("decision_gates"), Mapping) else {}
    coverage = _number(gates.get("coverage")) or 0.0
    hard_caps = gates.get("hard_caps") if isinstance(gates.get("hard_caps"), list) else []
    force_exit = any(item.get("result") == "已触发" and item.get("decision_effect") == "强制退出" for item in hard_caps if isinstance(item, Mapping))
    wait_ceiling = any(item.get("result") == "已触发" and item.get("decision_effect") == "最高等待" for item in hard_caps if isinstance(item, Mapping))
    statuses = [item["status"] for item in output["causal_chain"]]
    scenarios = context.get("valuation_scenarios") if isinstance(context.get("valuation_scenarios"), Mapping) else {}
    ratio = _number(scenarios.get("risk_reward"))

    if force_exit and state != "退出":
        raise ThesisValidationError("强制退出 Hard Cap 已触发，decision.state 只能为退出")
    if "矛盾" in statuses and state != "退出":
        raise ThesisValidationError("核心因果链存在矛盾，decision.state 只能为退出")
    if output["business_judgment"]["quality"] == "较弱" and state != "退出":
        raise ThesisValidationError("生意质量已判断为较弱，decision.state 只能为退出")
    if output["profit_judgment"]["state"] == "未受益" and state != "退出":
        raise ThesisValidationError("已确认公司未受益，decision.state 只能为退出")
    if wait_ceiling and state in {"试错", "买入"}:
        raise ThesisValidationError("Hard Cap 限制 decision.state 最高为等待")
    if coverage < 0.60 and state not in {"观察", "退出"}:
        raise ThesisValidationError("覆盖率低于60%，decision.state 只能为观察或退出")
    if "缺失" in statuses and state not in {"观察", "退出"}:
        raise ThesisValidationError("核心因果链仍有断点，decision.state 只能为观察或退出")

    if state in {"试错", "买入"}:
        failures: list[str] = []
        if output["business_judgment"]["quality"] not in {"优秀", "合格"}:
            failures.append("生意质量未达到合格")
        if output["industry_judgment"]["timing"] in {"正在走弱", "需人工确认"}:
            failures.append("行业时点未成立")
        if output["profit_judgment"]["state"] not in {"已兑现", "开始兑现"}:
            failures.append("利润尚未开始兑现")
        if any(status in {"缺失", "矛盾"} for status in statuses):
            failures.append("核心因果链仍有断点")
        required_ratio = 3.0 if state == "买入" else 2.0
        if ratio is None or ratio < required_ratio:
            failures.append(f"赔率低于{required_ratio:g}:1或不可计算")
        if failures:
            raise ThesisValidationError(f"decision.state={state} 未通过门槛：" + "；".join(failures))

    if state == "试错" and "部分验证" not in statuses:
        raise ThesisValidationError("decision.state=试错 必须仍有至少一个部分验证的关键条件")

    if state == "买入":
        failures = []
        if any(status != "已验证" for status in statuses):
            failures.append("因果链未全部验证")
        if int(gates.get("peer_count_verified") or 0) < 2:
            failures.append("已验证直接同行少于两家")
        if gates.get("expectation_gap_status") != "已验证" or output["market_expectation"]["gap_state"] != "预期偏低":
            failures.append("预期差未验证为偏低")
        if failures:
            raise ThesisValidationError("decision.state=买入 未通过门槛：" + "；".join(failures))


def _validate_thesis_output_v2_legacy(payload: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> ThesisOutput:
    if not isinstance(payload, Mapping) or not isinstance(context, Mapping):
        raise ThesisValidationError("Agent Judgment V2 需要对象和 schema_version=4 的 research_packet")
    if context.get("schema_version") != 4:
        raise ThesisValidationError("旧 research_packet 已过期，请重新运行采集器")

    one_sentence = _text(payload.get("one_sentence"), "one_sentence")
    if any(token in one_sentence for token in ("action_rating", "行动评级", "F1", "F2", "F3", "F4", "F5", "F6")):
        raise ThesisValidationError("one_sentence 不得使用旧评级或堆放六层分数")

    thesis_raw = payload.get("thesis")
    if not isinstance(thesis_raw, Mapping):
        raise ThesisValidationError("缺少 thesis")
    thesis = {
        "statement": _text(thesis_raw.get("statement"), "thesis.statement"),
        "time_horizon": _text(thesis_raw.get("time_horizon"), "thesis.time_horizon"),
        "key_drivers": _text_list(thesis_raw.get("key_drivers"), "thesis.key_drivers", 1, 5),
        "required_conditions": _text_list(thesis_raw.get("required_conditions"), "thesis.required_conditions", 1, 6),
        "invalidation_conditions": _text_list(thesis_raw.get("invalidation_conditions"), "thesis.invalidation_conditions", 1, 6),
        "evidence_refs": _refs(thesis_raw, "thesis", context),
    }
    _require_confirmed(thesis, "thesis", context)

    business = _block(payload, "business_judgment", ("quality", "summary"), context)
    industry = _block(payload, "industry_judgment", ("timing", "summary"), context)
    profit = _block(payload, "profit_judgment", ("state", "summary"), context)
    if business["quality"] not in BUSINESS_QUALITIES:
        raise ThesisValidationError("business_judgment.quality 不受支持")
    if industry["timing"] not in INDUSTRY_TIMINGS:
        raise ThesisValidationError("industry_judgment.timing 不受支持")
    if profit["state"] not in PROFIT_STATES:
        raise ThesisValidationError("profit_judgment.state 不受支持")
    _require_confirmed(business, "business_judgment", context, business["quality"] == "需人工确认")
    _require_confirmed(industry, "industry_judgment", context, industry["timing"] == "需人工确认")
    _require_confirmed(profit, "profit_judgment", context, profit["state"] == "需人工确认")

    chain_raw = payload.get("causal_chain")
    if not isinstance(chain_raw, list) or not 2 <= len(chain_raw) <= 8:
        raise ThesisValidationError("causal_chain 必须包含2-8个核心箭头")
    chain: list[dict[str, Any]] = []
    for index, item in enumerate(chain_raw):
        if not isinstance(item, Mapping):
            raise ThesisValidationError(f"causal_chain.{index} 必须是对象")
        row = {
            "from": _text(item.get("from"), f"causal_chain.{index}.from"),
            "to": _text(item.get("to"), f"causal_chain.{index}.to"),
            "claim": _text(item.get("claim"), f"causal_chain.{index}.claim"),
            "status": _text(item.get("status"), f"causal_chain.{index}.status"),
            "evidence_refs": _refs(item, f"causal_chain.{index}", context),
        }
        if row["status"] not in CHAIN_STATUSES:
            raise ThesisValidationError(f"causal_chain.{index}.status 不受支持")
        if row["status"] == "已验证":
            _require_confirmed(row, f"causal_chain.{index}", context)
        chain.append(row)

    why_raw = payload.get("why_this_company")
    if not isinstance(why_raw, Mapping):
        raise ThesisValidationError("缺少 why_this_company")
    why_company = {
        "profit_pool": _text(why_raw.get("profit_pool"), "why_this_company.profit_pool"),
        "advantages": _text_list(why_raw.get("advantages"), "why_this_company.advantages", 1, 5),
        "weaknesses": _text_list(why_raw.get("weaknesses"), "why_this_company.weaknesses", 1, 5),
        "peer_verdict": _text(why_raw.get("peer_verdict"), "why_this_company.peer_verdict"),
        "evidence_refs": _refs(why_raw, "why_this_company", context),
    }
    if why_company["peer_verdict"] not in PEER_VERDICTS:
        raise ThesisValidationError("why_this_company.peer_verdict 不受支持")
    peer_count = int((context.get("decision_gates") or {}).get("peer_count_verified") or 0)
    if why_company["peer_verdict"] == "最优候选" and peer_count < 2:
        raise ThesisValidationError("少于两家已验证直接同行，不得输出最优候选")
    _require_confirmed(
        why_company,
        "why_this_company",
        context,
        why_company["peer_verdict"] == "需人工确认",
    )

    expectation = _block(payload, "market_expectation", ("gap_state", "priced_in", "underappreciated"), context)
    if expectation["gap_state"] not in EXPECTATION_STATES:
        raise ThesisValidationError("market_expectation.gap_state 不受支持")
    _require_confirmed(expectation, "market_expectation", context, expectation["gap_state"] == "需人工确认")
    driver = _block(payload, "driver_judgment", ("stage", "summary"), context)
    if driver["stage"] not in DRIVER_STAGES:
        raise ThesisValidationError("driver_judgment.stage 不受支持")
    _require_confirmed(driver, "driver_judgment", context, driver["stage"] == "需人工确认")

    def case_block(key: str) -> dict[str, Any]:
        raw = payload.get(key)
        if not isinstance(raw, Mapping):
            raise ThesisValidationError(f"缺少 {key}")
        result = {
            "summary": _text(raw.get("summary"), f"{key}.summary"),
            "conditions": _text_list(raw.get("conditions"), f"{key}.conditions", 1, 5),
            "evidence_refs": _refs(raw, key, context),
        }
        _require_confirmed(result, key, context)
        return result

    bull, bear = case_block("bull_case"), case_block("bear_case")
    valuation = _block(payload, "valuation_interpretation", ("conclusion",), context)
    if "valuation_scenarios" not in valuation["evidence_refs"]:
        raise ThesisValidationError("valuation_interpretation 必须引用 valuation_scenarios")

    decision = _block(payload, "decision", ("state", "rationale"), context)
    if decision["state"] not in DECISION_STATES:
        raise ThesisValidationError("decision.state 必须为观察、等待、试错、买入或退出")
    _require_confirmed(decision, "decision", context)
    verification = _block(payload, "verification", ("next_event", "window", "upgrade_if", "downgrade_if"), context)
    _require_confirmed(verification, "verification", context)
    transition_raw = payload.get("state_transition")
    if not isinstance(transition_raw, Mapping):
        raise ThesisValidationError("缺少 state_transition")
    transition = {
        "previous_state": _text(transition_raw.get("previous_state"), "state_transition.previous_state"),
        "current_state": _text(transition_raw.get("current_state"), "state_transition.current_state"),
        "reason": _text(transition_raw.get("reason"), "state_transition.reason"),
    }
    if transition["current_state"] != decision["state"]:
        raise ThesisValidationError("state_transition.current_state 必须等于 decision.state")
    prior = context.get("prior_judgment") if isinstance(context.get("prior_judgment"), Mapping) else {}
    expected_previous = str(prior.get("state") or "首次判断")
    if transition["previous_state"] != expected_previous:
        raise ThesisValidationError(f"state_transition.previous_state 必须为 {expected_previous}")

    confidence = _text(payload.get("confidence"), "confidence")
    if confidence not in {"高", "中", "低"}:
        raise ThesisValidationError("confidence 必须为高、中或低")
    if payload.get("expression_status", "agent_generated") != "agent_generated":
        raise ThesisValidationError("expression_status 不受支持")

    normalized = {
        "business_judgment": business,
        "industry_judgment": industry,
        "profit_judgment": profit,
        "causal_chain": chain,
        "market_expectation": expectation,
        "decision": decision,
    }
    _validate_state(normalized, context)
    return ThesisOutput(
        one_sentence=one_sentence,
        thesis=thesis,
        business_judgment=business,
        industry_judgment=industry,
        profit_judgment=profit,
        causal_chain=tuple(chain),
        why_this_company=why_company,
        market_expectation=expectation,
        driver_judgment=driver,
        bull_case=bull,
        bear_case=bear,
        valuation_interpretation=valuation,
        decision=decision,
        verification=verification,
        state_transition=transition,
        confidence=confidence,
    )


def _pct(value: Any) -> str:
    number = _number(value)
    return "需人工确认" if number is None else f"{number:.1%}"


def _render_thesis_output_v2_legacy(output: ThesisOutput, context: Mapping[str, Any] | None = None) -> list[str]:
    scenarios = (context or {}).get("valuation_scenarios") if isinstance((context or {}).get("valuation_scenarios"), Mapping) else {}
    lines = [
        f"## 当前状态：{output.decision['state']}", "", f"> {output.one_sentence}", "",
        "### 投资假设", "", output.thesis["statement"], "",
        f"- 时间范围：{output.thesis['time_horizon']}",
        f"- 成立条件：{'；'.join(output.thesis['required_conditions'])}",
        f"- 失效条件：{'；'.join(output.thesis['invalidation_conditions'])}", "",
        "### 核心因果链", "", "| 从 | 到 | 判断 | 状态 |", "|---|---|---|---|",
    ]
    lines.extend(f"| {item['from']} | {item['to']} | {item['claim']} | {item['status']} |" for item in output.causal_chain)
    broken = [item for item in output.causal_chain if item["status"] in {"缺失", "矛盾"}]
    lines += ["", "- 当前断点：" + ("；".join(f"{item['from']} -> {item['to']}（{item['status']}）" for item in broken) if broken else "核心箭头无缺失或矛盾。"), ""]
    lines += [
        "### 为什么是它", "", f"- 利润池：{output.why_this_company['profit_pool']}",
        f"- 同行结论：{output.why_this_company['peer_verdict']}",
        f"- 优势：{'；'.join(output.why_this_company['advantages'])}",
        f"- 弱点：{'；'.join(output.why_this_company['weaknesses'])}", "",
        "### 市场预期", "", f"- 当前定价：{output.market_expectation['priced_in']}",
        f"- 可能忽略：{output.market_expectation['underappreciated']}",
        f"- 预期差：{output.market_expectation['gap_state']}；A股驱动：{output.driver_judgment['stage']}。{output.driver_judgment['summary']}", "",
        "### Bull / Bear", "", f"- **Bull：** {output.bull_case['summary']}", f"- **Bear：** {output.bear_case['summary']}", "",
        "### 三情景赔率", "",
    ]
    if scenarios.get("status") == "ready":
        values = scenarios["scenarios"]
        lines += [
            f"> {scenarios['model_label']}；{scenarios['formula']}。", "",
            "| 情景 | 估算价格 | 相对当前价 |", "|---|---:|---:|",
            f"| 悲观 | {values['bear']['price']:.2f} | {_pct(values['bear']['return'])} |",
            f"| 基准 | {values['base']['price']:.2f} | {_pct(values['base']['return'])} |",
            f"| 乐观 | {values['bull']['price']:.2f} | {_pct(values['bull']['return'])} |", "",
            f"- 风险收益比：{scenarios['risk_reward']}:1" if scenarios.get("risk_reward") is not None else f"- 风险收益比：{scenarios.get('risk_reward_status', '不可计算')}",
        ]
    else:
        lines += [f"> {scenarios.get('reason', '估值证据不足，无法计算')}。", ""]
    lines += [
        "", output.valuation_interpretation["conclusion"], "", "### 决策与验证", "",
        f"- 当前状态：**{output.decision['state']}**。{output.decision['rationale']}",
        f"- 下一事件：{output.verification['next_event']}（{output.verification['window']}）",
        f"- 升级条件：{output.verification['upgrade_if']}", f"- 降级条件：{output.verification['downgrade_if']}",
        f"- 状态迁移：{output.state_transition['previous_state']} -> {output.state_transition['current_state']}。{output.state_transition['reason']}",
        f"- 判断信心：{output.confidence}", "",
    ]
    return lines


def _is_v4_payload(payload: Mapping[str, Any]) -> bool:
    return payload.get("schema_version") in {JUDGMENT_SCHEMA_VERSION, "4", "v4", "V4"}


def _normalize_judgment_payload(payload: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    version = payload.get("schema_version")
    if _is_v4_payload(payload):
        normalized = deepcopy(dict(payload))
        normalized["schema_version"] = JUDGMENT_SCHEMA_VERSION
        return normalized
    if version in {None, 2, "2", "v2", "V2", 3, "3", "v3", "V3"}:
        raise ThesisValidationError(
            "Agent Judgment V3 及更早版本不满足假设优先的 V4 合同；请基于当前事实包重新生成。"
        )
    raise ThesisValidationError("Agent Judgment 版本不受支持；请提交 V4 判断")


def _peer_matches_context(company: str, context: Mapping[str, Any]) -> str | None:
    target = context.get("company") if isinstance(context.get("company"), Mapping) else {}
    target_tokens = {
        str(target.get("name") or "").strip(),
        str(target.get("code") or "").strip(),
    }
    if any(token and token != "需人工确认" and token in company for token in target_tokens):
        return "target"
    for row in _matching_verified_peers(context):
        for token in (str(row.get("name") or "").strip(), str(row.get("code") or "").strip()):
            if token and token in company:
                return token
    return None


def _validate_peer_comparison(raw: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    rows = raw.get("peer_comparison")
    if not isinstance(rows, list) or len(rows) > 5:
        raise ThesisValidationError("why_this_company.peer_comparison 必须为不超过5项的列表")
    result: list[dict[str, Any]] = []
    direct_peer_tokens: set[str] = set()
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise ThesisValidationError(f"why_this_company.peer_comparison.{index} 必须是对象")
        row = {
            field: _text(item.get(field), f"why_this_company.peer_comparison.{index}.{field}")
            for field in (
                "company",
                "trend_exposure",
                "business_purity",
                "industry_position",
                "core_barrier",
                "profit_realization",
                "market_cap_elasticity",
                "overseas_risk",
                "crowding_and_expectation",
                "largest_flaw",
                "valuation_and_odds",
                "current_choice",
            )
        }
        row["evidence_refs"] = _refs(item, f"why_this_company.peer_comparison.{index}", context)
        if not any(reference == "peers" or reference.startswith("peers.") for reference in row["evidence_refs"]):
            raise ThesisValidationError("同行比较必须引用 peers 中的已验证直接同行")
        matched = _peer_matches_context(row["company"], context)
        if matched is None:
            raise ThesisValidationError("同行比较中的公司未在已验证直接同行或当前公司中找到")
        if matched != "target":
            direct_peer_tokens.add(matched)
        _require_confirmed(row, f"why_this_company.peer_comparison.{index}", context)
        result.append(row)
    return result, len(direct_peer_tokens)


def _validate_causal_breakpoint(
    payload: Mapping[str, Any],
    chain: list[dict[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    critical = next((item for item in chain if item["status"] != "已验证"), None)
    block = _judgment_block(
        payload,
        "causal_breakpoint",
        context,
        fields=("key_link", "reason"),
        allow_unknown=critical is not None and critical["status"] == "缺失",
    )
    raw = payload.get("causal_breakpoint")
    assert isinstance(raw, Mapping)
    block["closure_conditions"] = _text_list(
        raw.get("closure_conditions"),
        "causal_breakpoint.closure_conditions",
        1,
        4,
    )
    if critical is None:
        if "无关键断点" not in block["key_link"]:
            raise ThesisValidationError("因果链全部验证时 causal_breakpoint.key_link 必须明确无关键断点")
        return block

    if critical["from"] not in block["key_link"] or critical["to"] not in block["key_link"]:
        raise ThesisValidationError("causal_breakpoint.key_link 必须指向当前最关键的未验证箭头")
    chain_refs = set(critical["evidence_refs"])
    if not chain_refs.intersection(block["evidence_refs"]):
        raise ThesisValidationError("causal_breakpoint 必须引用关键断点对应的证据")
    return block


def _validate_case_block(payload: Mapping[str, Any], key: str, context: Mapping[str, Any]) -> dict[str, Any]:
    block = _judgment_block(payload, key, context, fields=("summary",))
    raw = payload.get(key)
    assert isinstance(raw, Mapping)
    block["conditions"] = _text_list(raw.get("conditions"), f"{key}.conditions", 1, 5)
    return block


def _validate_top_variables(raw: Mapping[str, Any], context: Mapping[str, Any]) -> list[dict[str, Any]]:
    variables = raw.get("top_variables")
    if not isinstance(variables, list) or len(variables) != 3:
        raise ThesisValidationError("verification.top_variables 必须恰好包含3个验证变量")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(variables):
        if not isinstance(item, Mapping):
            raise ThesisValidationError(f"verification.top_variables.{index} 必须是对象")
        variable = {
            field: _text(item.get(field), f"verification.top_variables.{index}.{field}")
            for field in ("variable", "why", "window", "upgrade_signal", "downgrade_signal")
        }
        variable["evidence_refs"] = _refs(item, f"verification.top_variables.{index}", context)
        _require_confirmed(
            variable,
            f"verification.top_variables.{index}",
            context,
            _contains_unknown_text(variable),
        )
        result.append(variable)
    return result


def validate_thesis_output(payload: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> ThesisOutput:
    """Validate the hypothesis-first V4 judgment without changing evidence semantics."""
    if not isinstance(payload, Mapping) or not isinstance(context, Mapping):
        raise ThesisValidationError("Agent Judgment V4 需要对象和 schema_version=4 的 research_packet")
    if context.get("schema_version") != 4:
        raise ThesisValidationError("旧 research_packet 已过期，请重新运行采集器")

    normalized = _normalize_judgment_payload(payload, context)
    _reject_raw_search_errors(normalized)
    _reject_scorecard_front_terms(normalized)
    if "core_contradictions" in normalized:
        raise ThesisValidationError("Agent Judgment V4 只能有一个 core_contradiction")

    one_sentence = _text(normalized.get("one_sentence"), "one_sentence")
    if any(token in one_sentence for token in ("action_rating", "行动评级", "F1", "F2", "F3", "F4", "F5", "F6")):
        raise ThesisValidationError("one_sentence 不得使用旧评级或堆放六层分数")
    if _contains_decision_state(one_sentence):
        raise ThesisValidationError("one_sentence 必须只说明核心矛盾；五态只能在最后的决策模块出现")

    core_contradiction = _judgment_block(
        normalized,
        "core_contradiction",
        context,
        fields=("statement", "market_stage", "fundamental_stage"),
    )
    industry_positioning = _judgment_block(
        normalized,
        "industry_positioning",
        context,
        fields=("industry_chain", "demand_driver", "company_link", "profit_path"),
        allow_unknown=True,
    )
    if "company.main_business" not in industry_positioning["evidence_refs"]:
        raise ThesisValidationError("industry_positioning 必须引用 company.main_business，不能只凭泛行业或概念词定位产业链")
    thesis = _judgment_block(normalized, "thesis", context, fields=("statement", "time_horizon"))
    thesis_raw = normalized.get("thesis")
    assert isinstance(thesis_raw, Mapping)
    thesis["key_drivers"] = _text_list(thesis_raw.get("key_drivers"), "thesis.key_drivers", 1, 5)
    thesis["required_conditions"] = _text_list(thesis_raw.get("required_conditions"), "thesis.required_conditions", 1, 6)
    thesis["invalidation_conditions"] = _text_list(thesis_raw.get("invalidation_conditions"), "thesis.invalidation_conditions", 1, 6)

    why_watch = _judgment_block(
        normalized,
        "why_watch",
        context,
        fields=("why_now", "industry_change", "company_position", "scarcity", "profit_pool"),
    )
    reversal = _judgment_block(
        normalized,
        "reversal_judgment",
        context,
        fields=("past_pressure", "marginal_change", "reversal_stage", "remaining_gap"),
        allow_unknown=True,
    )

    business = _judgment_block(normalized, "business_judgment", context, fields=("quality", "summary"))
    industry = _judgment_block(normalized, "industry_judgment", context, fields=("timing", "summary"))
    profit = _judgment_block(normalized, "profit_judgment", context, fields=("state", "summary"))
    if business["quality"] not in BUSINESS_QUALITIES:
        raise ThesisValidationError("business_judgment.quality 不受支持")
    if industry["timing"] not in INDUSTRY_TIMINGS:
        raise ThesisValidationError("industry_judgment.timing 不受支持")
    if profit["state"] not in PROFIT_STATES:
        raise ThesisValidationError("profit_judgment.state 不受支持")
    _require_confirmed(business, "business_judgment", context, business["quality"] == "需人工确认")
    _require_confirmed(industry, "industry_judgment", context, industry["timing"] == "需人工确认")
    _require_confirmed(profit, "profit_judgment", context, profit["state"] == "需人工确认")

    chain_raw = normalized.get("causal_chain")
    if not isinstance(chain_raw, list) or not 2 <= len(chain_raw) <= 8:
        raise ThesisValidationError("causal_chain 必须包含2-8个核心箭头")
    chain: list[dict[str, Any]] = []
    for index, item in enumerate(chain_raw):
        if not isinstance(item, Mapping):
            raise ThesisValidationError(f"causal_chain.{index} 必须是对象")
        row = {
            "from": _text(item.get("from"), f"causal_chain.{index}.from"),
            "to": _text(item.get("to"), f"causal_chain.{index}.to"),
            "claim": _text(item.get("claim"), f"causal_chain.{index}.claim"),
            "status": _text(item.get("status"), f"causal_chain.{index}.status"),
            "evidence_refs": _refs(item, f"causal_chain.{index}", context),
        }
        if row["status"] not in CHAIN_STATUSES:
            raise ThesisValidationError(f"causal_chain.{index}.status 不受支持")
        if row["status"] == "已验证":
            _require_confirmed(row, f"causal_chain.{index}", context)
        chain.append(row)
    causal_breakpoint = _validate_causal_breakpoint(normalized, chain, context)

    why_company = _judgment_block(
        normalized,
        "why_this_company",
        context,
        fields=("profit_pool", "peer_verdict", "selection_conclusion"),
    )
    why_raw = normalized.get("why_this_company")
    assert isinstance(why_raw, Mapping)
    why_company["advantages"] = _text_list(why_raw.get("advantages"), "why_this_company.advantages", 1, 5)
    why_company["weaknesses"] = _text_list(why_raw.get("weaknesses"), "why_this_company.weaknesses", 1, 5)
    if why_company["peer_verdict"] not in PEER_VERDICTS:
        raise ThesisValidationError("why_this_company.peer_verdict 不受支持")
    peer_rows, compared_direct_peers = _validate_peer_comparison(why_raw, context)
    why_company["peer_comparison"] = peer_rows
    verified_peer_count = int((context.get("decision_gates") or {}).get("peer_count_verified") or 0)
    if why_company["peer_verdict"] == "最优候选":
        if verified_peer_count < 2 or compared_direct_peers < 2:
            raise ThesisValidationError("至少两家已验证直接同行完成比较后，才可输出最优候选")

    expectation = _judgment_block(
        normalized,
        "market_expectation",
        context,
        fields=(
            "gap_state", "market_narrative", "market_stage", "fundamental_stage",
            "market_vs_fundamentals", "known", "priced_in", "unpriced", "mispriced",
        ),
    )
    if expectation["gap_state"] not in EXPECTATION_STATES:
        raise ThesisValidationError("market_expectation.gap_state 不受支持")
    expectation["underappreciated"] = expectation["unpriced"]
    driver = _judgment_block(normalized, "driver_judgment", context, fields=("stage", "summary"))
    if driver["stage"] not in DRIVER_STAGES:
        raise ThesisValidationError("driver_judgment.stage 不受支持")

    bull = _validate_case_block(normalized, "bull_case", context)
    base = _validate_case_block(normalized, "base_case", context)
    bear = _validate_case_block(normalized, "bear_case", context)
    valuation = _judgment_block(
        normalized,
        "valuation_interpretation",
        context,
        fields=("conclusion",),
        allow_unknown=True,
    )
    if "valuation_scenarios" not in valuation["evidence_refs"]:
        raise ThesisValidationError("valuation_interpretation 必须引用 valuation_scenarios")

    decision = _judgment_block(
        normalized,
        "decision",
        context,
        fields=("state", "rationale", "why_not_higher_state"),
    )
    if decision["state"] not in DECISION_STATES:
        raise ThesisValidationError("decision.state 必须为观察、等待、试错、买入或退出")
    verification = _judgment_block(
        normalized,
        "verification",
        context,
        fields=("next_event", "window", "upgrade_if", "downgrade_if"),
    )
    verification_raw = normalized.get("verification")
    assert isinstance(verification_raw, Mapping)
    verification["top_variables"] = _validate_top_variables(verification_raw, context)

    transition_raw = normalized.get("state_transition")
    if not isinstance(transition_raw, Mapping):
        raise ThesisValidationError("缺少 state_transition")
    transition = {
        "previous_state": _text(transition_raw.get("previous_state"), "state_transition.previous_state"),
        "current_state": _text(transition_raw.get("current_state"), "state_transition.current_state"),
        "reason": _text(transition_raw.get("reason"), "state_transition.reason"),
    }
    if transition["current_state"] != decision["state"]:
        raise ThesisValidationError("state_transition.current_state 必须等于 decision.state")
    prior = context.get("prior_judgment") if isinstance(context.get("prior_judgment"), Mapping) else {}
    expected_previous = str(prior.get("state") or "首次判断")
    if transition["previous_state"] != expected_previous:
        raise ThesisValidationError(f"state_transition.previous_state 必须为 {expected_previous}")

    confidence = _text(normalized.get("confidence"), "confidence")
    if confidence not in {"高", "中", "低"}:
        raise ThesisValidationError("confidence 必须为高、中或低")
    if normalized.get("expression_status", "agent_generated") != "agent_generated":
        raise ThesisValidationError("expression_status 不受支持")

    _validate_state(
        {
            "business_judgment": business,
            "industry_judgment": industry,
            "profit_judgment": profit,
            "causal_chain": chain,
            "market_expectation": expectation,
            "decision": decision,
        },
        context,
    )
    return ThesisOutput(
        schema_version=JUDGMENT_SCHEMA_VERSION,
        one_sentence=one_sentence,
        core_contradiction=core_contradiction,
        industry_positioning=industry_positioning,
        thesis=thesis,
        why_watch=why_watch,
        reversal_judgment=reversal,
        business_judgment=business,
        industry_judgment=industry,
        profit_judgment=profit,
        causal_chain=tuple(chain),
        causal_breakpoint=causal_breakpoint,
        why_this_company=why_company,
        market_expectation=expectation,
        driver_judgment=driver,
        bull_case=bull,
        base_case=base,
        bear_case=bear,
        valuation_interpretation=valuation,
        decision=decision,
        verification=verification,
        state_transition=transition,
        confidence=confidence,
    )


def _sentence(value: str) -> str:
    return value.strip().rstrip("。！？!?")


def _render_judgment_paragraph(block: Mapping[str, Any]) -> str:
    return (
        f"{_sentence(block['fact'])}；{_sentence(block['meaning'])}。"
        f"{_sentence(block['decision_impact'])}。"
    )


def _render_evidence_refs(block: Mapping[str, Any]) -> str:
    """Keep traceability in the file without exposing implementation paths to readers."""
    refs = [str(item) for item in block.get("evidence_refs", ()) if isinstance(item, str) and item]
    return "<!-- evidence_refs: " + json.dumps(refs, ensure_ascii=False) + " -->"


def render_thesis_output(output: ThesisOutput, context: Mapping[str, Any] | None = None) -> list[str]:
    """Render the V4 front layer as a hypothesis-first judgment, not a score recap."""
    scenarios = (context or {}).get("valuation_scenarios") if isinstance((context or {}).get("valuation_scenarios"), Mapping) else {}
    lines = [
        "## 莫大判断", "", "### 一句话结论", "",
        f"> {output.one_sentence}",
        _render_evidence_refs(output.core_contradiction), "",
        "### 核心矛盾", "", output.core_contradiction["statement"], "",
        f"- 市场走到：{output.core_contradiction['market_stage']}",
        f"- 基本面走到：{output.core_contradiction['fundamental_stage']}",
        _render_judgment_paragraph(output.core_contradiction), _render_evidence_refs(output.core_contradiction), "",
        "### 产业链定位", "", _render_judgment_paragraph(output.industry_positioning), _render_evidence_refs(output.industry_positioning), "",
        f"- 产业链：{output.industry_positioning['industry_chain']}",
        f"- 需求从哪里来：{output.industry_positioning['demand_driver']}",
        f"- 公司吃哪一段：{output.industry_positioning['company_link']}",
        f"- 利润怎样传导：{output.industry_positioning['profit_path']}", "",
        "### 为什么现在进入视野", "", _render_judgment_paragraph(output.why_watch), _render_evidence_refs(output.why_watch), "",
        f"- 进入视野的触发：{output.why_watch['why_now']}",
        f"公司处在{_sentence(output.why_watch['company_position'])}；产业变化是{_sentence(output.why_watch['industry_change'])}。",
        f"真正需要验证的稀缺性是{_sentence(output.why_watch['scarcity'])}，利润池在{_sentence(output.why_watch['profit_pool'])}。", "",
        "### 过去为什么不行，现在改变了什么", "", _render_judgment_paragraph(output.reversal_judgment), _render_evidence_refs(output.reversal_judgment), "",
        f"- 过去的压制：{output.reversal_judgment['past_pressure']}",
        f"- 现在的边际变化：{output.reversal_judgment['marginal_change']}",
        f"- 反转走到哪一步：{output.reversal_judgment['reversal_stage']}",
        f"- 还没有跨过的坎：{output.reversal_judgment['remaining_gap']}", "",
        "### 投资假设", "", output.thesis["statement"], "",
        _render_judgment_paragraph(output.thesis), _render_evidence_refs(output.thesis), "",
        f"- 生意：**{output.business_judgment['quality']}**。{output.business_judgment['summary']}",
        f"  {_render_judgment_paragraph(output.business_judgment)}",
        f"  {_render_evidence_refs(output.business_judgment)}",
        f"- 行业：**{output.industry_judgment['timing']}**。{output.industry_judgment['summary']}",
        f"  {_render_judgment_paragraph(output.industry_judgment)}",
        f"  {_render_evidence_refs(output.industry_judgment)}",
        f"- 利润：**{output.profit_judgment['state']}**。{output.profit_judgment['summary']}",
        f"  {_render_judgment_paragraph(output.profit_judgment)}",
        f"  {_render_evidence_refs(output.profit_judgment)}", "",
        "### 市场现在在交易什么", "",
        f"- 市场已经知道：{output.market_expectation['known']}",
        f"- 股价已经反映：{output.market_expectation['priced_in']}",
        f"- 市场可能尚未反映：{output.market_expectation['unpriced']}",
        f"- 市场可能错误定价：{output.market_expectation['mispriced']}",
        f"- 市场叙事：{output.market_expectation['market_narrative']}",
        f"- 市场交易阶段：{output.market_expectation['market_stage']}",
        f"- 公司基本面阶段：{output.market_expectation['fundamental_stage']}",
        f"- 两者的错位：{output.market_expectation['market_vs_fundamentals']}",
        f"- 当前预期判断：{output.market_expectation['gap_state']}。{_render_judgment_paragraph(output.market_expectation)}",
        f"  {_render_evidence_refs(output.market_expectation)}",
        f"- 当前市场驱动：{output.driver_judgment['stage']}。{output.driver_judgment['summary']}",
        f"- 驱动对决策的影响：{_render_judgment_paragraph(output.driver_judgment)}",
        f"  {_render_evidence_refs(output.driver_judgment)}", "",
        "### 为什么是它", "", _render_judgment_paragraph(output.why_this_company), _render_evidence_refs(output.why_this_company), "",
        f"公司优势是{'；'.join(output.why_this_company['advantages'])}。需要正视的问题是{'；'.join(output.why_this_company['weaknesses'])}。",
        "", "| 公司 | 吃哪段趋势 / 业务纯度 | 地位 / 壁垒 | 利润 / 市值弹性 | 海外风险 / 拥挤与预期 | 最大缺陷 | 当前选择 |", "|---|---|---|---|---|---|---|",
    ]
    peer_rows = output.why_this_company["peer_comparison"]
    if peer_rows:
        lines.extend(
            f"| {row['company']} | {row['trend_exposure']} / {row['business_purity']} | {row['industry_position']} / {row['core_barrier']} | {row['profit_realization']} / {row['market_cap_elasticity']} | {row['overseas_risk']} / {row['crowding_and_expectation']} | {row['largest_flaw']} | {row['current_choice']} |"
            for row in peer_rows
        )
    else:
        lines.append("| 需补证 | 已验证直接同行不足 | 需补证 | 需补证 | 需补证 | 需补证 | 暂不形成优选 |")
    lines.append("<!-- peer_evidence_refs: " + json.dumps(
        [row.get("evidence_refs", []) for row in peer_rows], ensure_ascii=False
    ) + " -->")
    lines += [
        "", f"- 同行结论：{output.why_this_company['peer_verdict']}。{output.why_this_company['selection_conclusion']}", "",
        "### 核心因果链", "", "| 从 | 到 | 判断 | 状态 |", "|---|---|---|---|",
    ]
    lines.extend(
        f"| {item['from']} | {item['to']} | {item['claim']} | {item['status']} |"
        for item in output.causal_chain
    )
    lines.append("<!-- causal_chain_evidence_refs: " + json.dumps(
        [item.get("evidence_refs", []) for item in output.causal_chain], ensure_ascii=False
    ) + " -->")
    lines += [
        "", f"- 关键断点：{output.causal_breakpoint['key_link']}。{output.causal_breakpoint['reason']}",
        f"- 为什么这会改变判断：{_render_judgment_paragraph(output.causal_breakpoint)}",
        f"  {_render_evidence_refs(output.causal_breakpoint)}",
        f"- 闭环需要：{'；'.join(output.causal_breakpoint['closure_conditions'])}", "",
        "### Bull / Base / Bear 情景", "",
        f"- **Bull：** {output.bull_case['summary']} 成立条件：{'；'.join(output.bull_case['conditions'])}。",
        f"  判断：{_render_judgment_paragraph(output.bull_case)}",
        f"  {_render_evidence_refs(output.bull_case)}",
        f"- **Base：** {output.base_case['summary']} 成立条件：{'；'.join(output.base_case['conditions'])}。",
        f"  判断：{_render_judgment_paragraph(output.base_case)}",
        f"  {_render_evidence_refs(output.base_case)}",
        f"- **Bear：** {output.bear_case['summary']} 成立条件：{'；'.join(output.bear_case['conditions'])}。",
        f"  判断：{_render_judgment_paragraph(output.bear_case)}",
        f"  {_render_evidence_refs(output.bear_case)}", "",
        "### 估值与赔率", "",
    ]
    if scenarios.get("status") == "ready":
        values = scenarios["scenarios"]
        lines += [
            f"> {scenarios['model_label']}；{scenarios['formula']}。", "",
            "| 情景 | 估算价格 | 相对当前价 |", "|---|---:|---:|",
            f"| 悲观 | {values['bear']['price']:.2f} | {_pct(values['bear']['return'])} |",
            f"| 基准 | {values['base']['price']:.2f} | {_pct(values['base']['return'])} |",
            f"| 乐观 | {values['bull']['price']:.2f} | {_pct(values['bull']['return'])} |", "",
            f"- 风险收益比：{scenarios['risk_reward']}:1" if scenarios.get("risk_reward") is not None else f"- 风险收益比：{scenarios.get('risk_reward_status', '不可计算')}",
        ]
    else:
        lines += [f"> {scenarios.get('reason', '估值证据不足，无法计算')}。"]
    lines += [
        "", _render_judgment_paragraph(output.valuation_interpretation), _render_evidence_refs(output.valuation_interpretation), "",
        "### 我的决策与下一步验证", "",
        f"- 当前状态：**{output.decision['state']}**。{output.decision['rationale']}",
        f"- 为什么不是更高状态：{output.decision['why_not_higher_state']}",
        f"- 判断依据：{_render_judgment_paragraph(output.decision)}",
        f"  {_render_evidence_refs(output.decision)}",
        f"- 下一事件：{output.verification['next_event']}（{output.verification['window']}）",
        f"- 升级条件：{output.verification['upgrade_if']}",
        f"- 降级或退出条件：{output.verification['downgrade_if']}", "",
        f"- 验证逻辑：{_render_judgment_paragraph(output.verification)}",
        f"  {_render_evidence_refs(output.verification)}", "",
        "| 最重要验证变量 | 为什么看它 | 时间窗口 | 升级信号 | 降级信号 |",
        "|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {item['variable']} | {item['why']} | {item['window']} | {item['upgrade_signal']} | {item['downgrade_signal']} |"
        for item in output.verification["top_variables"]
    )
    lines.append("<!-- verification_evidence_refs: " + json.dumps(
        [item.get("evidence_refs", []) for item in output.verification["top_variables"]], ensure_ascii=False
    ) + " -->")
    lines += [
        "", f"- 状态迁移：{output.state_transition['previous_state']} -> {output.state_transition['current_state']}。{output.state_transition['reason']}",
        f"- 判断信心：{output.confidence}", "",
    ]
    return lines
