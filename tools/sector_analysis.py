"""Evidence-first sector research packets and judgment cards.

This module is deliberately separate from the six-factor scorer.  It consumes
existing scorecard artifacts, schema-v4 research packets, or raw evidence and
turns them into a sector-level research view:

    industry trend -> supply/demand -> profit pool -> scarcity -> realization
    -> concept versus priced-in -> candidate ordering.

Candidate ordering is an evidence order, not a new score, rating, or stock
trading decision.  ``sector_state`` therefore never reuses the stock-level
five-state decision contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


SECTOR_SCHEMA_VERSION = 1
SECTOR_STATES = {"值得研究", "等待验证", "暂不优先"}
SECTION_KEYS = (
    "industry_trend",
    "supply_demand",
    "profit_pool",
    "scarcity",
    "profit_realization",
    "market_pricing",
)
CONFIRMED_STATUSES = {"已验证", "已坐实", "通过", "有效"}
PARTIAL_STATUSES = {"部分验证", "部分覆盖"}
UNKNOWN_MARKERS = (
    "需人工确认",
    "搜索失败",
    "已搜索未命中",
    "网络命中（未核验）",
    "search_budget_exhausted",
    "target_budget_exhausted",
    "target_limit_exceeded",
    "duckduckgo:",
    "connectionerror",
    "traceback",
    "not_configured",
)
TECHNICAL_ERROR_MARKERS = (
    "search_budget_exhausted",
    "target_budget_exhausted",
    "target_limit_exceeded",
    "duckduckgo:",
    "connectionerror",
    "traceback",
    "not_configured",
)

# This is a sample normalized contract rather than a second scoring schema.
SECTOR_JUDGMENT_CONTRACT: dict[str, Any] = {
    "schema_version": SECTOR_SCHEMA_VERSION,
    "packet_type": "sector_judgment",
    "sector": "示例板块",
    "sector_state": "等待验证",
    "not_stock_decision": True,
    "one_sentence": "板块可以跟踪，但关键供需或利润闭环仍需验证。",
    "one_sentence_evidence_refs": ["sections.supply_demand"],
    "core_contradiction": {
        "status": "需人工确认",
        "summary": "产业主题存在，但行业级供需证据尚未闭环。",
        "evidence_refs": ["sections.supply_demand"],
        "unknowns": [],
    },
    "sections": {
        key: {
            "status": "需人工确认",
            "summary": f"示例中未提供可核验的{key}事实。",
            "evidence_refs": [f"sections.{key}"],
            "unknowns": [],
        }
        for key in SECTION_KEYS
    },
    "candidate_comparison": {
        "comparison_type": "evidence_ordering_not_score",
        "candidates": [],
    },
    "evidence_gaps": [],
}


class SectorValidationError(ValueError):
    """Raised when a sector judgment misses its evidence contract."""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and not any(marker.lower() in stripped.lower() for marker in UNKNOWN_MARKERS)
    if isinstance(value, Mapping):
        if _text(value.get("status")) in {"需人工确认", "网络命中（未核验）"}:
            return False
        return any(_known(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return bool(value) and any(_known(item) for item in value)
    return True


def _clean_summary(value: Any, fallback: str) -> str:
    text = _text(value)
    if not text:
        return fallback
    if any(marker.lower() in text.lower() for marker in TECHNICAL_ERROR_MARKERS):
        return "外部证据未完成核验，需人工确认。"
    return text


def _normalize_status(value: Any) -> str:
    status = _text(value)
    if status in CONFIRMED_STATUSES:
        return "已验证"
    if status in PARTIAL_STATUSES:
        return "部分验证"
    return "需人工确认"


def _combine_status(*statuses: Any) -> str:
    normalized = {_normalize_status(item) for item in statuses if _text(item)}
    if "已验证" in normalized:
        return "已验证"
    if "部分验证" in normalized:
        return "部分验证"
    return "需人工确认"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _field_value(value: Any) -> tuple[Any, list[str], str]:
    if isinstance(value, Mapping) and "value" in value:
        return value.get("value"), _string_list(value.get("sources") or value.get("source_refs")), _text(value.get("status"))
    return value, [], ""


def _packet_to_evidence(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only the fields this module needs from a schema-v4 packet."""
    evidence: dict[str, Any] = {"metric_sources": {}}
    sources = evidence["metric_sources"]

    def take(key: str, *path: str) -> None:
        raw = _nested(packet, *path)
        value, field_sources, _ = _field_value(raw)
        if not _known(value):
            return
        evidence[key] = value
        sources[key] = field_sources or ["research_packet." + ".".join(path)]

    for key, path in (
        ("code", ("company", "code")),
        ("name", ("company", "name")),
        ("main_business", ("company", "main_business")),
        ("business_items", ("company", "business_items")),
        ("chain_name", ("industry", "chain_name")),
        ("chain_stage", ("industry", "chain_stage")),
        ("revenue_yoy", ("realization", "revenue_yoy")),
        ("profit_yoy", ("realization", "profit_yoy")),
        ("order_growth", ("realization", "order_growth")),
        ("operating_cashflow", ("realization", "operating_cashflow")),
        ("net_profit", ("realization", "net_profit")),
        ("latest_price", ("valuation", "latest_price")),
        ("pe_ttm", ("valuation", "pe_ttm")),
        ("pb", ("valuation", "pb")),
        ("price_percentile_3y", ("valuation", "price_percentile_3y")),
        ("drawdown_from_3y_high", ("valuation", "drawdown_from_3y_high")),
        ("market_congestion", ("a_share_signals", "market_congestion")),
        ("coverage", ("research", "coverage")),
    ):
        take(key, *path)

    return evidence


def _packet_factor(packet: Mapping[str, Any], key: str) -> dict[str, Any]:
    paths = {
        "era_track": ("system_change", "era_track"),
        "capex_wave": ("system_change", "capital_expenditure"),
        "supply_gap": ("system_change", "supply_demand"),
        "upstream": ("bottleneck", "upstream_position"),
        "chokepoint": ("bottleneck", "chokepoint"),
        "leadership": ("bottleneck", "leadership"),
        "financial_safety": ("survival", "financial_safety"),
        "survival_risk": ("survival", "survival_risk"),
        "business_match": ("realization", "business_match_status"),
        "realization": ("realization", "realization_status"),
        "profit_position": ("industry", "chain_stage"),
        "expectation_gap": ("market_stage", "expectation_gap_status"),
        "price_position": ("market_stage", "price_position_status"),
    }
    reason_paths = {
        "business_match": ("realization", "business_match_reason"),
        "realization": ("realization", "realization_reason"),
        "expectation_gap": ("market_stage", "expectation_gap_reason"),
        "price_position": ("market_stage", "price_position_reason"),
    }
    path = paths.get(key)
    raw = _nested(packet, *path) if path else None
    if isinstance(raw, Mapping):
        return {
            "status": _normalize_status(raw.get("status")),
            "reason": _clean_summary(raw.get("reason"), "研究包未提供可展示的说明。"),
            "source_refs": ["research_packet." + ".".join(path)],
        }
    raw_status = _normalize_status(raw)
    if raw_status != "需人工确认":
        reason = _nested(packet, *reason_paths[key]) if key in reason_paths else None
        return {
            "status": raw_status,
            "reason": _clean_summary(reason, "研究包记录了该状态，但未提供可展示的说明。"),
            "source_refs": ["research_packet." + ".".join(path)],
        }
    if key in reason_paths:
        reason = _nested(packet, *reason_paths[key])
        if _known(raw) or _known(reason):
            return {
                "status": "部分验证",
                "reason": _clean_summary(reason, "研究包提供了相关线索，但状态仍需人工确认。"),
                "source_refs": ["research_packet." + ".".join(path)],
            }
    if key == "profit_position" and _known(raw):
        return {
            "status": "部分验证",
            "reason": f"产业链位置为 {raw}；这只是利润池位置线索，不等于利润份额已验证。",
            "source_refs": ["research_packet." + ".".join(path)],
        }
    return {"status": "需人工确认", "reason": "未提供可核验事实。", "source_refs": []}


def _subfactor(card: Mapping[str, Any], packet: Mapping[str, Any], key: str) -> dict[str, Any]:
    for factor in _list(card.get("factors")):
        if not isinstance(factor, Mapping):
            continue
        for item in _list(factor.get("subfactors")):
            if isinstance(item, Mapping) and item.get("key") == key:
                return {
                    "status": _normalize_status(item.get("status")),
                    "reason": _clean_summary(item.get("reason"), "未提供可展示的说明。"),
                    "source_refs": _string_list(item.get("sources")),
                }
    return _packet_factor(packet, key) if packet else {
        "status": "需人工确认",
        "reason": "未提供可核验事实。",
        "source_refs": [],
    }


def _field(evidence: Mapping[str, Any], packet: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = evidence.get(key)
    value, embedded_sources, declared_status = _field_value(raw)
    source_refs = _string_list(_mapping(evidence.get("metric_sources")).get(key)) + embedded_sources
    if not _known(value) and packet:
        packet_evidence = _packet_to_evidence(packet)
        raw = packet_evidence.get(key)
        value, embedded_sources, declared_status = _field_value(raw)
        source_refs = _string_list(_mapping(packet_evidence.get("metric_sources")).get(key)) + embedded_sources
    if _normalize_status(declared_status) != "需人工确认":
        status = _normalize_status(declared_status)
    elif _known(value) and source_refs:
        status = "已验证"
    elif _known(value):
        status = "部分验证"
    else:
        status = "需人工确认"
    return {"value": value, "status": status, "source_refs": _unique(source_refs)}


def _percent(value: Any) -> str:
    number = _number(value)
    return f"{number * 100:.2f}%" if number is not None else "需人工确认"


def _display_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return str(value)
    rendered = f"{number:,.2f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _metric_sentence(label: str, fact: Mapping[str, Any], *, percent: bool = False) -> str | None:
    if fact.get("status") == "需人工确认" or not _known(fact.get("value")):
        return None
    value = _percent(fact.get("value")) if percent else _display_number(fact.get("value"))
    return f"{label} {value}"


def _public_fact(fact: Mapping[str, Any], *, percent: bool = False) -> dict[str, Any]:
    value = fact.get("value") if _known(fact.get("value")) else None
    return {
        "value": value,
        "display": _percent(value) if percent and value is not None else _display_number(value) if value is not None else "需人工确认",
        "status": fact.get("status", "需人工确认"),
        "evidence_refs": _string_list(fact.get("source_refs")) or ["input:unspecified"],
    }


def _hard_cap_summary(card: Mapping[str, Any], packet: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]], list[str]]:
    raw_caps = _list(card.get("hard_caps"))
    source_ref = "scorecard.hard_caps"
    if not raw_caps and packet:
        raw_caps = _list(_nested(packet, "decision_gates", "hard_caps"))
        source_ref = "research_packet.decision_gates.hard_caps"
    caps: list[dict[str, Any]] = []
    for item in raw_caps:
        if not isinstance(item, Mapping):
            continue
        condition = _text(item.get("condition")) or "风险项"
        result = _text(item.get("result")) or "需人工确认"
        effect = _text(item.get("decision_effect") or item.get("cap")) or "需人工确认"
        caps.append({"condition": condition, "result": result, "decision_effect": effect, "evidence_refs": [source_ref]})
    if not caps:
        return "需人工确认", "未提供 Hard Cap 或风险触发状态，需人工确认。", [], [source_ref]
    triggered = [item for item in caps if "已触发" in item["result"]]
    unknown = [item for item in caps if item["result"] not in {"已触发", "未触发"}]
    if triggered:
        labels = "、".join(item["condition"] for item in triggered)
        return "风险已触发", f"已触发风险项：{labels}。", caps, [source_ref]
    if unknown:
        return "部分验证", "部分 Hard Cap 或风险状态仍需人工确认。", caps, [source_ref]
    return "已验证", "已核验的 Hard Cap 均未触发。", caps, [source_ref]


def _facts_block(
    *,
    status: str,
    summary: str,
    source_refs: Iterable[str],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    refs = _unique(source_refs)
    result: dict[str, Any] = {
        "status": _normalize_status(status),
        "summary": _clean_summary(summary, "需人工确认。"),
        "evidence_refs": refs or ["input:unspecified"],
    }
    if extra:
        result.update(dict(extra))
    return result


def _candidate_input(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    thesis = _mapping(raw.get("thesis"))
    packet = _mapping(raw.get("research_packet")) or _mapping(thesis.get("research_packet")) or _mapping(thesis.get("thesis_context"))
    if not packet and raw.get("schema_version") == 4:
        packet = dict(raw)
    evidence = _mapping(raw.get("evidence"))
    card = _mapping(raw.get("scorecard"))
    if not evidence:
        direct = {key: value for key, value in raw.items() if key not in {"scorecard", "thesis", "research_packet"}}
        evidence = direct if any(key in direct for key in ("code", "name", "main_business", "chain_name")) else {}
    if not evidence and packet:
        evidence = _packet_to_evidence(packet)
    kind = "scorecard_artifact" if _mapping(raw.get("scorecard")) else "research_packet" if packet else "raw_evidence"
    return evidence, card, packet, kind


def _candidate_unknowns(member_index: int, facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    unknowns: list[dict[str, Any]] = []
    labels = {
        "trend": "产业趋势",
        "supply_demand": "供需变化",
        "profit_pool": "利润池",
        "scarcity": "稀缺性",
        "profit_realization": "利润兑现",
        "market_pricing": "概念与已计价",
        "safety": "估值与安全边际",
    }
    for key, label in labels.items():
        fact = _mapping(facts.get(key))
        if fact.get("status") != "已验证":
            unknowns.append({
                "item": label,
                "reason": "该候选的证据不足以单独形成确定判断。" if fact.get("status") == "部分验证" else "缺少可核验事实，需人工确认。",
                "evidence_refs": [f"members.{member_index}.facts.{key}"],
            })
    return unknowns


def _normalize_candidate(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
    evidence, card, packet, input_kind = _candidate_input(raw)
    code = _text(_field(evidence, packet, "code").get("value")) or _text(raw.get("code")) or f"candidate-{index + 1}"
    name = _text(_field(evidence, packet, "name").get("value")) or _text(raw.get("name")) or code
    candidate_id = code

    chain_name = _field(evidence, packet, "chain_name")
    chain_stage = _field(evidence, packet, "chain_stage")
    main_business = _field(evidence, packet, "main_business")
    business_items = _field(evidence, packet, "business_items")
    business_match = _subfactor(card, packet, "business_match")
    industry_status = _combine_status(chain_name.get("status"), chain_stage.get("status"), business_match.get("status"))
    industry_parts = [
        f"产业链：{chain_name.get('value')}" if _known(chain_name.get("value")) else "产业链待确认",
        f"位置：{chain_stage.get('value')}" if _known(chain_stage.get("value")) else "位置待确认",
    ]
    if _known(main_business.get("value")):
        industry_parts.append(f"主营：{main_business.get('value')}")
    if business_match.get("status") != "需人工确认":
        industry_parts.append(business_match["reason"])
    industry = _facts_block(
        status=industry_status,
        summary="；".join(industry_parts) + "。",
        source_refs=chain_name["source_refs"] + chain_stage["source_refs"] + main_business["source_refs"] + business_items["source_refs"] + business_match["source_refs"],
        extra={
            "chain_name": chain_name.get("value") if _known(chain_name.get("value")) else "需人工确认",
            "chain_stage": chain_stage.get("value") if _known(chain_stage.get("value")) else "需人工确认",
            "business_match_status": business_match["status"],
        },
    )

    era_track = _subfactor(card, packet, "era_track")
    capex_wave = _subfactor(card, packet, "capex_wave")
    trend_status = _combine_status(era_track["status"], capex_wave["status"])
    trend_reasons = [item["reason"] for item in (era_track, capex_wave) if item["status"] != "需人工确认"]
    trend = _facts_block(
        status=trend_status,
        summary="；".join(trend_reasons) if trend_reasons else "未提供可核验的产业趋势或资本开支事实。",
        source_refs=era_track["source_refs"] + capex_wave["source_refs"],
        extra={"era_track_status": era_track["status"], "capex_status": capex_wave["status"]},
    )

    supply_gap = _subfactor(card, packet, "supply_gap")
    supply_fields = {
        key: _field(evidence, packet, key)
        for key in ("supply_evidence_count", "supply_category_count", "supply_tightening", "supply_cr3", "capacity_expansion_cycle_years")
    }
    supply_bits: list[str] = []
    if _known(supply_fields["supply_evidence_count"]["value"]):
        supply_bits.append(f"供需证据 {supply_fields['supply_evidence_count']['value']} 类")
    if _known(supply_fields["supply_category_count"]["value"]):
        supply_bits.append(f"独立类别 {supply_fields['supply_category_count']['value']}")
    if supply_fields["supply_tightening"]["status"] != "需人工确认":
        supply_bits.append(f"趋紧={supply_fields['supply_tightening']['value']}")
    if _known(supply_fields["supply_cr3"]["value"]):
        supply_bits.append(f"CR3 {supply_fields['supply_cr3']['value']}")
    if _known(supply_fields["capacity_expansion_cycle_years"]["value"]):
        supply_bits.append(f"扩产周期 {supply_fields['capacity_expansion_cycle_years']['value']} 年")
    if supply_gap["status"] != "需人工确认":
        supply_bits.append(supply_gap["reason"])
    supply_status = _combine_status(supply_gap["status"], *(item["status"] for item in supply_fields.values()))
    supply = _facts_block(
        status=supply_status,
        summary="；".join(supply_bits) if supply_bits else "缺少价格、库存、订单、CR3 或扩产周期的供需事实。",
        source_refs=supply_gap["source_refs"] + [ref for item in supply_fields.values() for ref in item["source_refs"]],
        extra={"supply_signal_status": _text(evidence.get("supply_signal_status")) or "需人工确认"},
    )

    profit_position = _subfactor(card, packet, "profit_position")
    profit_pool_status = _combine_status(profit_position["status"], industry["status"])
    pool_summary = "；".join(
        item for item in (
            profit_position["reason"] if profit_position["status"] != "需人工确认" else "",
            f"候选产业链位置：{industry['chain_stage']}" if industry["chain_stage"] != "需人工确认" else "",
        ) if item
    )
    if not pool_summary:
        pool_summary = "缺少行业利润分布、议价权或环节份额事实。"
    profit_pool = _facts_block(
        status=profit_pool_status,
        summary=pool_summary + "。该位置线索不等于行业利润池已被证明。",
        source_refs=profit_position["source_refs"] + industry["evidence_refs"],
    )

    chokepoint = _subfactor(card, packet, "chokepoint")
    upstream = _subfactor(card, packet, "upstream")
    leadership = _subfactor(card, packet, "leadership")
    scarcity_status = _combine_status(chokepoint["status"], leadership["status"])
    scarcity_reasons = [
        item["reason"] for item in (chokepoint, leadership, upstream) if item["status"] != "需人工确认"
    ]
    scarcity = _facts_block(
        status=scarcity_status,
        summary="；".join(scarcity_reasons) if scarcity_reasons else "未提供技术、认证、产能、资源或客户壁垒的可核验证据。",
        source_refs=chokepoint["source_refs"] + leadership["source_refs"] + upstream["source_refs"],
    )

    realization_factor = _subfactor(card, packet, "realization")
    metrics = {key: _field(evidence, packet, key) for key in ("order_growth", "revenue_yoy", "profit_yoy", "operating_cashflow")}
    metric_bits = [
        bit for bit in (
            _metric_sentence("订单增长", metrics["order_growth"], percent=True),
            _metric_sentence("营收同比", metrics["revenue_yoy"], percent=True),
            _metric_sentence("利润同比", metrics["profit_yoy"], percent=True),
            _metric_sentence("经营现金流", metrics["operating_cashflow"]),
        ) if bit
    ]
    profit_yoy = _number(metrics["profit_yoy"]["value"])
    cashflow = _number(metrics["operating_cashflow"]["value"])
    revenue_yoy = _number(metrics["revenue_yoy"]["value"])
    order_growth = _number(metrics["order_growth"]["value"])
    verified_profit = metrics["profit_yoy"]["status"] == "已验证"
    verified_cashflow = metrics["operating_cashflow"]["status"] == "已验证"
    verified_positive = any(
        value is not None and value > 0 and metrics[key]["status"] == "已验证"
        for key, value in (("order_growth", order_growth), ("revenue_yoy", revenue_yoy), ("profit_yoy", profit_yoy))
    )
    verified_measurement = any(item["status"] == "已验证" for item in metrics.values())
    if profit_yoy is not None and profit_yoy > 0 and verified_profit and cashflow is not None and cashflow > 0 and verified_cashflow:
        realization_state = "已兑现"
    elif verified_positive:
        realization_state = "开始兑现"
    elif verified_measurement:
        realization_state = "未证明"
    else:
        realization_state = "需人工确认"
    realization_status = _combine_status(realization_factor["status"], *(item["status"] for item in metrics.values()))
    if realization_factor["status"] != "需人工确认":
        metric_bits.append(realization_factor["reason"])
    realization = _facts_block(
        status=realization_status,
        summary="；".join(metric_bits) if metric_bits else "缺少订单、收入、利润或现金流的可核验事实。",
        source_refs=realization_factor["source_refs"] + [ref for item in metrics.values() for ref in item["source_refs"]],
        extra={"realization_state": realization_state},
    )

    expectation_gap = _subfactor(card, packet, "expectation_gap")
    price_position = _subfactor(card, packet, "price_position")
    pricing_fields = {
        key: _field(evidence, packet, key)
        for key in (
            "latest_price", "price_percentile_3y", "drawdown_from_3y_high",
            "market_congestion", "pe_ttm", "pb", "concepts",
        )
    }
    price_position_value = _number(pricing_fields["price_percentile_3y"]["value"])
    congestion_value = _number(pricing_fields["market_congestion"]["value"])
    price_known = pricing_fields["price_percentile_3y"]["status"] == "已验证"
    congestion_known = pricing_fields["market_congestion"]["status"] == "已验证"
    if price_known and price_position_value is not None and price_position_value > 0.80 and congestion_known and congestion_value is not None and congestion_value >= 0.80:
        pricing_state = "高位且拥挤"
    elif price_known and price_position_value is not None and price_position_value > 0.80:
        pricing_state = "价格高位，拥挤度待确认"
    elif price_known and price_position_value is not None and price_position_value <= 0.35:
        pricing_state = "价格位置偏低，仍需基本面验证"
    else:
        pricing_state = "已计价程度需人工确认"
    concept_status = "需人工确认"
    if industry["business_match_status"] == "已验证":
        concept_status = "主营嵌入支持"
    elif industry["status"] != "需人工确认":
        concept_status = "主营嵌入线索"
    elif _known(pricing_fields["concepts"]["value"]):
        concept_status = "主题关联"
    pricing_bits = [
        bit for bit in (
            _metric_sentence("三年价格分位", pricing_fields["price_percentile_3y"], percent=True),
            _metric_sentence("距三年高点回撤", pricing_fields["drawdown_from_3y_high"], percent=True),
            _metric_sentence("市场拥挤度", pricing_fields["market_congestion"], percent=True),
            _metric_sentence("PE(TTM)", pricing_fields["pe_ttm"]),
            _metric_sentence("PB", pricing_fields["pb"]),
        ) if bit
    ]
    if expectation_gap["status"] != "需人工确认":
        pricing_bits.append(expectation_gap["reason"])
    pricing_status = _combine_status(expectation_gap["status"], price_position["status"], *(item["status"] for item in pricing_fields.values()))
    market_pricing = _facts_block(
        status=pricing_status,
        summary=("；".join(pricing_bits) + "。" if pricing_bits else "缺少价格位置、估值、拥挤度或预期差事实。") + "仅据价格与拥挤度不能断言市场已充分定价。",
        source_refs=expectation_gap["source_refs"] + price_position["source_refs"] + [ref for item in pricing_fields.values() for ref in item["source_refs"]],
        extra={
            "concept_status": concept_status,
            "pricing_state": pricing_state,
            "valuation_snapshot": {
                "latest_price": _public_fact(pricing_fields["latest_price"]),
                "pe_ttm": _public_fact(pricing_fields["pe_ttm"]),
                "pb": _public_fact(pricing_fields["pb"]),
                "price_percentile_3y": _public_fact(pricing_fields["price_percentile_3y"], percent=True),
                "drawdown_from_3y_high": _public_fact(pricing_fields["drawdown_from_3y_high"], percent=True),
                "market_congestion": _public_fact(pricing_fields["market_congestion"], percent=True),
            },
        },
    )

    financial_safety = _subfactor(card, packet, "financial_safety")
    survival_risk = _subfactor(card, packet, "survival_risk")
    safety_fields = {
        key: _field(evidence, packet, key)
        for key in ("cash_to_debt", "debt_ratio", "net_profit", "operating_cashflow", "free_cash_flow")
    }
    hard_cap_status, hard_cap_summary, hard_caps, hard_cap_refs = _hard_cap_summary(card, packet)
    safety_status = _combine_status(
        financial_safety["status"],
        survival_risk["status"],
        *(item["status"] for item in safety_fields.values()),
    )
    if hard_cap_status == "风险已触发":
        safety_status = "风险已触发"
    elif hard_cap_status == "需人工确认" and safety_status == "已验证":
        safety_status = "部分验证"
    safety_bits = [
        item for item in (
            "财务安全已验证。" if financial_safety["status"] == "已验证" else financial_safety["reason"] if financial_safety["status"] == "部分验证" else "",
            "生存与审计风险已验证。" if survival_risk["status"] == "已验证" else survival_risk["reason"] if survival_risk["status"] == "部分验证" else "",
            hard_cap_summary,
        ) if item
    ]
    if financial_safety["status"] == "需人工确认" and not safety_bits:
        if any(item["status"] != "需人工确认" for item in safety_fields.values()):
            safety_bits.append("已提供部分财务字段，但尚不足以单独确认安全边际。")
    safety = _facts_block(
        status=safety_status,
        summary="；".join(safety_bits) if safety_bits else "缺少财务安全、现金流或 Hard Cap 事实，需人工确认。",
        source_refs=(
            financial_safety["source_refs"]
            + survival_risk["source_refs"]
            + [ref for item in safety_fields.values() for ref in item["source_refs"]]
            + hard_cap_refs
        ),
        extra={
            "financial_safety_status": financial_safety["status"],
            "survival_risk_status": survival_risk["status"],
            "hard_cap_status": hard_cap_status,
            "hard_caps": hard_caps,
            "safety_snapshot": {
                "cash_to_debt": _public_fact(safety_fields["cash_to_debt"]),
                "debt_ratio": _public_fact(safety_fields["debt_ratio"], percent=True),
                "net_profit": _public_fact(safety_fields["net_profit"]),
                "operating_cashflow": _public_fact(safety_fields["operating_cashflow"]),
                "free_cash_flow": _public_fact(safety_fields["free_cash_flow"]),
            },
        },
    )

    facts = {
        "industry": industry,
        "trend": trend,
        "supply_demand": supply,
        "profit_pool": profit_pool,
        "scarcity": scarcity,
        "profit_realization": realization,
        "market_pricing": market_pricing,
        "safety": safety,
    }
    return {
        "candidate_id": candidate_id,
        "code": code,
        "name": name,
        "input_kind": input_kind,
        "facts": facts,
        "unknowns": _candidate_unknowns(index, facts),
    }


def _coerce_inputs(packets_or_evidence: Any) -> list[Mapping[str, Any]]:
    if packets_or_evidence is None:
        return []
    if isinstance(packets_or_evidence, (str, Path, Mapping)):
        raw_items: Sequence[Any] = [packets_or_evidence]
    else:
        raw_items = list(packets_or_evidence)
    result: list[Mapping[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, (str, Path)):
            path = Path(raw)
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"无法读取板块输入 {path}: {exc}") from exc
            if not isinstance(loaded, Mapping):
                raise ValueError(f"板块输入必须是 JSON 对象: {path}")
            result.append(loaded)
        elif isinstance(raw, Mapping):
            result.append(raw)
        else:
            raise TypeError("packets_or_evidence 必须是 Mapping、JSON 路径或它们的序列")
    return result


def _direct_sector_section(raw: Any, fallback_ref: str) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    summary = _text(raw.get("summary") or raw.get("reason") or raw.get("value"))
    refs = _string_list(raw.get("evidence_refs") or raw.get("source_refs") or raw.get("sources"))
    if not summary or not refs:
        return None
    return {
        "status": _normalize_status(raw.get("status")),
        "summary": _clean_summary(summary, "需人工确认。"),
        "evidence_refs": refs or [fallback_ref],
        "unknowns": [item for item in _list(raw.get("unknowns")) if isinstance(item, Mapping)],
    }


def _sector_sections(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = _mapping(value)
    nested = _mapping(raw.get("sections"))
    return nested or raw


def _usable_direct_sector_section(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return bool(_text(value.get("summary") or value.get("reason") or value.get("value"))) and bool(
        _string_list(value.get("evidence_refs") or value.get("source_refs") or value.get("sources"))
    )


def _usable_collected_sector_section(value: Any, collection_status: Any) -> bool:
    """Return whether a collector section can replace candidate aggregation.

    A disabled, failed, or unresolved web search is an evidence gap, not an
    industry fact. It belongs in collection diagnostics and must not make a
    card more negative merely because collection was explicitly switched on.
    """
    if not _usable_direct_sector_section(value):
        return False
    if _text(collection_status) in {"disabled", "failed", "unavailable", "not_requested"}:
        return False
    return _normalize_status(_mapping(value).get("status")) != "需人工确认"


def _collect_sector_evidence(
    sector: str,
    *,
    context: str = "",
    provider: str | None = None,
    timeout: int = 12,
) -> dict[str, Any]:
    """Lazily call the optional industry web collector only when requested."""
    try:
        from tools.scoring.web_research import collect_sector_evidence
    except (ImportError, AttributeError):
        return {
            "sector": sector,
            "web_research_status": "unavailable",
            "sources": [],
            "errors": [{"reason": "行业级网页证据采集器不可用，需人工确认。"}],
            "sections": {},
        }
    try:
        collected = collect_sector_evidence(sector, context=context, provider=provider, timeout=timeout)
    except Exception:
        return {
            "sector": sector,
            "web_research_status": "failed",
            "sources": [],
            "errors": [{"reason": "行业级网页证据采集失败，需人工确认。"}],
            "sections": {},
        }
    if not isinstance(collected, Mapping):
        return {
            "sector": sector,
            "web_research_status": "failed",
            "sources": [],
            "errors": [{"reason": "行业级网页证据采集返回格式无效，需人工确认。"}],
            "sections": {},
        }
    return dict(collected)


def _collection_metadata(
    *,
    requested: bool,
    provided: Mapping[str, Any] | None,
    collected: Mapping[str, Any] | None,
    effective: Mapping[str, Any],
) -> dict[str, Any]:
    raw_provided = _mapping(provided)
    raw_collected = _mapping(collected)
    raw_errors = _list(raw_collected.get("errors"))
    errors: list[dict[str, str]] = []
    for item in raw_errors:
        if isinstance(item, Mapping):
            errors.append({"reason": "行业级网页证据存在未完成项，需人工确认。"})
        elif _text(item):
            errors.append({"reason": "行业级网页证据存在未完成项，需人工确认。"})
    return {
        "requested": requested,
        "web_research_status": _text(raw_collected.get("web_research_status"))
        or _text(raw_provided.get("web_research_status"))
        or "not_requested",
        "web_research_provider": _text(raw_collected.get("web_research_provider"))
        or _text(raw_provided.get("web_research_provider"))
        or "",
        "queries": _list(raw_collected.get("queries")) or _list(raw_provided.get("queries")),
        "sources": _list(raw_collected.get("sources")) or _list(raw_provided.get("sources")),
        "search_budget": _mapping(raw_collected.get("search_budget")) or _mapping(raw_provided.get("search_budget")),
        "errors": errors,
        "provided_sections": [key for key in SECTION_KEYS if key in _sector_sections(provided)],
        "collected_sections": [key for key in SECTION_KEYS if key in _sector_sections(collected)],
        "effective_sections": [key for key in SECTION_KEYS if key in effective],
    }


def _merge_sector_evidence(
    provided: Mapping[str, Any] | None,
    collected: Mapping[str, Any] | None,
) -> dict[str, Any]:
    provided_sections = _sector_sections(provided)
    collected_sections = _sector_sections(collected)
    collection_status = _mapping(collected).get("web_research_status")
    merged: dict[str, Any] = {}
    for key in SECTION_KEYS:
        collected_value = collected_sections.get(key)
        provided_value = provided_sections.get(key)
        if _usable_collected_sector_section(collected_value, collection_status):
            merged[key] = dict(collected_value)
        # An explicit user/industry JSON section wins only when it contains a
        # readable statement and a traceable reference.  Invalid input cannot
        # erase a collector result. User-provided manual-confirmation material
        # is kept as an explicit research boundary; collector-only unknowns
        # remain diagnostics rather than replacing candidate facts.
        if _usable_direct_sector_section(provided_value):
            merged[key] = dict(provided_value)
    return merged


def _aggregate_section(
    key: str,
    members: Sequence[Mapping[str, Any]],
    *,
    direct: Mapping[str, Any] | None,
    sector_ref: str,
) -> dict[str, Any]:
    labels = {
        "industry_trend": "产业趋势",
        "supply_demand": "供需变化",
        "profit_pool": "利润池",
        "scarcity": "稀缺环节",
        "profit_realization": "利润兑现",
        "market_pricing": "概念与已计价",
    }
    member_key = "trend" if key == "industry_trend" else key
    blocks = [_mapping(_mapping(member.get("facts")).get(member_key)) for member in members]
    blocks = [item for item in blocks if item]
    supporting_facts = []
    for index, member in enumerate(members):
        block = _mapping(_mapping(member.get("facts")).get(member_key))
        item = {
            "candidate": member.get("name") or member.get("code") or member.get("candidate_id"),
            "status": block.get("status", "需人工确认"),
            "summary": block.get("summary", "需人工确认。"),
            "evidence_refs": [f"members.{index}.facts.{member_key}"],
            "source_refs": _string_list(block.get("evidence_refs")),
        }
        if key == "market_pricing":
            item["concept_status"] = block.get("concept_status", "需人工确认")
            item["pricing_state"] = block.get("pricing_state", "已计价程度需人工确认")
        supporting_facts.append(item)
    if direct:
        unknowns = [dict(item) for item in _list(direct.get("unknowns")) if isinstance(item, Mapping)]
        return {
            "status": _normalize_status(direct.get("status")),
            "summary": _clean_summary(direct.get("summary"), "需人工确认。"),
            "evidence_refs": _string_list(direct.get("evidence_refs")) or [sector_ref],
            "supporting_facts": supporting_facts,
            "unknowns": unknowns,
        }

    verified = sum(block.get("status") == "已验证" for block in blocks)
    partial = sum(block.get("status") == "部分验证" for block in blocks)
    if verified or partial:
        status = "部分验证"
        summary = (
            f"现有 {len(blocks)} 个候选事实包中，{verified} 个已验证、{partial} 个部分验证"
            f"“{labels[key]}”相关事实；候选层证据不能替代行业级结论。"
        )
        if key == "market_pricing":
            embedded = sum(
                _mapping(_mapping(member.get("facts")).get("market_pricing")).get("concept_status") == "主营嵌入支持"
                for member in members
            )
            theme_only = sum(
                _mapping(_mapping(member.get("facts")).get("market_pricing")).get("concept_status") == "主题关联"
                for member in members
            )
            summary += f"其中主营嵌入支持 {embedded} 个、仅主题关联 {theme_only} 个；不能用主题热度替代已计价判断。"
        unknowns = [{
            "item": labels[key],
            "reason": "缺少独立行业级证据，需人工确认。",
            "evidence_refs": [f"members.{index}.facts.{member_key}" for index in range(len(members))] or [sector_ref],
        }]
    else:
        status = "需人工确认"
        summary = f"现有候选事实包未提供可核验的“{labels[key]}”证据。"
        unknowns = [{
            "item": labels[key],
            "reason": "缺少可核验事实，需人工确认。",
            "evidence_refs": [sector_ref],
        }]
    return {
        "status": status,
        "summary": summary,
        "evidence_refs": [f"members.{index}.facts.{member_key}" for index in range(len(members))] or [sector_ref],
        "supporting_facts": supporting_facts,
        "unknowns": unknowns,
    }


def build_sector_fact_packet(
    sector: str,
    packets_or_evidence: Any,
    *,
    sector_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize existing research artifacts into a sector fact packet.

    ``sector_evidence`` is optional direct, industry-level material.  Each
    supplied section must include ``summary`` and non-empty ``evidence_refs``;
    otherwise it is treated as unknown instead of being promoted to a fact.
    """
    sector_name = _text(sector)
    if not sector_name:
        raise ValueError("sector 不能为空")
    members = [_normalize_candidate(raw, index) for index, raw in enumerate(_coerce_inputs(packets_or_evidence))]
    direct_source = _sector_sections(sector_evidence)
    sector_ref = f"input:sector:{sector_name}"
    sections = {
        key: _aggregate_section(
            key,
            members,
            direct=_direct_sector_section(direct_source.get(key), sector_ref),
            sector_ref=sector_ref,
        )
        for key in SECTION_KEYS
    }
    gaps = [gap for section in sections.values() for gap in _list(section.get("unknowns")) if isinstance(gap, Mapping)]
    return {
        "schema_version": SECTOR_SCHEMA_VERSION,
        "packet_type": "sector_fact_packet",
        "sector": sector_name,
        "members": members,
        "sections": sections,
        "evidence_gaps": gaps,
        "source_note": "只汇总已有研究包/评分卡事实；候选层线索不自动构成行业级结论。",
    }


def _candidate_class(member: Mapping[str, Any]) -> str:
    industry = _mapping(_mapping(member.get("facts")).get("industry"))
    if industry.get("business_match_status") == "已验证":
        return "主营嵌入支持"
    if industry.get("status") != "需人工确认":
        return "主营嵌入线索"
    pricing = _mapping(_mapping(member.get("facts")).get("market_pricing"))
    if pricing.get("concept_status") == "主题关联":
        return "主题关联"
    return "需人工确认"


def _candidate_sort_key(member: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    facts = _mapping(member.get("facts"))
    relevance = _candidate_class(member)
    relevance_rank = {"主营嵌入支持": 0, "主营嵌入线索": 1, "主题关联": 3, "需人工确认": 4}[relevance]
    realization = _mapping(facts.get("profit_realization")).get("realization_state")
    realization_rank = {"已兑现": 0, "开始兑现": 1, "未证明": 2, "需人工确认": 3}.get(realization, 3)
    scarcity_rank = {"已验证": 0, "部分验证": 1, "需人工确认": 2}.get(_mapping(facts.get("scarcity")).get("status"), 2)
    pricing_state = _mapping(facts.get("market_pricing")).get("pricing_state")
    pricing_rank = 1 if pricing_state == "高位且拥挤" else 0
    return relevance_rank, realization_rank, scarcity_rank, pricing_rank, str(member.get("code") or member.get("candidate_id"))


def _snapshot_summary(snapshot: Mapping[str, Any], labels: Sequence[tuple[str, str]], fallback: str) -> str:
    values: list[str] = []
    for key, label in labels:
        fact = _mapping(snapshot.get(key))
        if fact.get("status") != "需人工确认" and _text(fact.get("display")):
            values.append(f"{label} {fact['display']}")
    return "；".join(values) if values else fallback


def compare_sector_candidates(fact_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Order candidates by evidence completeness without calculating a score."""
    members = [item for item in _list(fact_packet.get("members")) if isinstance(item, Mapping)]
    ordered = sorted(enumerate(members), key=lambda item: _candidate_sort_key(item[1]))
    rows: list[dict[str, Any]] = []
    for rank, (member_index, member) in enumerate(ordered, start=1):
        facts = _mapping(member.get("facts"))
        class_name = _candidate_class(member)
        industry = _mapping(facts.get("industry"))
        realization = _mapping(facts.get("profit_realization"))
        scarcity = _mapping(facts.get("scarcity"))
        pricing = _mapping(facts.get("market_pricing"))
        safety = _mapping(facts.get("safety"))
        valuation_snapshot = _mapping(pricing.get("valuation_snapshot"))
        safety_snapshot = _mapping(safety.get("safety_snapshot"))
        valuation_summary = _snapshot_summary(
            valuation_snapshot,
            (
                ("pe_ttm", "PE(TTM)"),
                ("pb", "PB"),
                ("price_percentile_3y", "三年价格分位"),
                ("drawdown_from_3y_high", "距三年高点回撤"),
                ("market_congestion", "市场拥挤度"),
            ),
            "缺少可核验的估值或价格位置字段。",
        )
        safety_summary = _snapshot_summary(
            safety_snapshot,
            (
                ("cash_to_debt", "现金/负债"),
                ("debt_ratio", "资产负债率"),
                ("net_profit", "归母净利润"),
                ("operating_cashflow", "经营现金流"),
                ("free_cash_flow", "自由现金流"),
            ),
            "缺少可核验的财务安全边际字段。",
        )
        safety_summary = f"{safety_summary}；{safety.get('summary', 'Hard Cap 状态需人工确认。')}"
        limitations: list[str] = []
        if class_name != "主营嵌入支持":
            limitations.append("主营与板块的直接嵌入关系尚未完全证实")
        if realization.get("realization_state") not in {"已兑现", "开始兑现"}:
            limitations.append("利润兑现尚未形成正向证据")
        if scarcity.get("status") != "已验证":
            limitations.append("稀缺性或替代难度待确认")
        if pricing.get("pricing_state") == "高位且拥挤":
            limitations.append("价格高位且拥挤，不能把板块逻辑直接转为配置理由")
        if all(_mapping(valuation_snapshot.get(key)).get("status") == "需人工确认" for key in ("pe_ttm", "pb", "price_percentile_3y", "drawdown_from_3y_high")):
            limitations.append("估值、价格位置或回撤缺少可核验数据，无法判断安全边际")
        if safety.get("hard_cap_status") == "风险已触发":
            limitations.append("已有 Hard Cap 风险触发，不能因板块逻辑忽略")
        elif safety.get("status") != "已验证":
            limitations.append("财务安全或 Hard Cap 状态尚未完整核验")
        refs = _unique([
            f"members.{member_index}.facts.industry",
            f"members.{member_index}.facts.profit_realization",
            f"members.{member_index}.facts.scarcity",
            f"members.{member_index}.facts.market_pricing",
            f"members.{member_index}.facts.safety",
        ])
        rows.append({
            "rank": rank,
            "code": member.get("code"),
            "name": member.get("name"),
            "candidate_class": class_name,
            "chain_position": industry.get("chain_stage", "需人工确认"),
            "profit_realization": realization.get("realization_state", "需人工确认"),
            "scarcity_status": scarcity.get("status", "需人工确认"),
            "pricing_state": pricing.get("pricing_state", "已计价程度需人工确认"),
            "valuation_snapshot": valuation_snapshot,
            "valuation_summary": valuation_summary,
            "safety_margin": {
                "status": safety.get("status", "需人工确认"),
                "hard_cap_status": safety.get("hard_cap_status", "需人工确认"),
                "summary": safety_summary,
                "snapshot": safety_snapshot,
                "evidence_refs": _string_list(safety.get("evidence_refs")) or [f"members.{member_index}.facts.safety"],
            },
            "why_not_higher": limitations or ["现有事实包未发现排序所需的明确缺口。"],
            "evidence_refs": refs,
            "unknowns": [item for item in _list(member.get("unknowns")) if isinstance(item, Mapping)],
        })
    if rows:
        top = rows[0]
        top_name = str(top.get("name") or top.get("code") or "首位候选")
        for row in rows:
            if row["rank"] == 1:
                summary = (
                    f"{top_name} 仅在已提供事实中暂居前；"
                    "没有至少两家同口径直接同行的完整对照时，不能证明它绝对优于同行。"
                )
            elif row["why_not_higher"]:
                summary = f"相对 {top_name} 当前不优先：{'；'.join(row['why_not_higher'])}。"
            else:
                summary = f"现有证据不足以证明该候选不如 {top_name}；当前只按稳定事实顺序展示。"
            row["why_not_peer"] = {
                "summary": summary,
                "evidence_refs": _unique(row["evidence_refs"] + top["evidence_refs"]),
            }
    if not rows:
        conclusion = "没有可比较的候选事实包，不能把板块主题落到个股选择。"
        refs = ["members"]
    elif rows[0]["candidate_class"] == "主题关联":
        conclusion = "候选目前主要是主题关联，尚不能证明谁真正处在利润兑现环节。"
        refs = rows[0]["evidence_refs"]
    elif rows[0]["profit_realization"] not in {"已兑现", "开始兑现"}:
        conclusion = "已有主营嵌入线索，但尚无候选证明产业逻辑已转成利润。"
        refs = rows[0]["evidence_refs"]
    else:
        conclusion = "排序优先展示主营嵌入且有利润兑现事实的候选；缺少同口径直接同行对照时，不能据此证明绝对优势或形成个股买卖结论。"
        refs = rows[0]["evidence_refs"]
    return {
        "comparison_type": "evidence_ordering_not_score",
        "ordering_rule": [
            "先区分主营嵌入支持、主营嵌入线索、主题关联和未知，不把概念当受益。",
            "再看订单、收入、利润和现金流是否已有兑现事实。",
            "稀缺性只在有技术、认证、产能、资源或客户壁垒证据时作为并列排序依据。",
            "价格高位且拥挤只提示已计价风险，不改变产业事实，也不构成交易指令。",
        ],
        "candidates": rows,
        "conclusion": {"summary": conclusion, "evidence_refs": refs},
    }


def _derive_sector_state(sections: Mapping[str, Any], comparison: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    trend = _mapping(sections.get("industry_trend"))
    supply = _mapping(sections.get("supply_demand"))
    pool = _mapping(sections.get("profit_pool"))
    scarcity = _mapping(sections.get("scarcity"))
    realization = _mapping(sections.get("profit_realization"))
    rows = [item for item in _list(comparison.get("candidates")) if isinstance(item, Mapping)]
    relevance = any(item.get("candidate_class") in {"主营嵌入支持", "主营嵌入线索"} for item in rows)
    realized = any(item.get("profit_realization") in {"已兑现", "开始兑现"} for item in rows)
    if trend.get("status") == "需人工确认" or not relevance:
        return (
            "暂不优先",
            "缺少可核验的产业趋势或主营嵌入候选，当前不把板块主题升级为个股机会。",
            _unique(trend.get("evidence_refs", []) + comparison.get("conclusion", {}).get("evidence_refs", [])),
        )
    if any(item.get("status") != "已验证" for item in (supply, pool, scarcity, realization)) or not realized:
        return (
            "等待验证",
            "板块可继续跟踪，但供需、利润池、稀缺性或利润兑现仍有关键断点，不能仅凭概念排序。",
            _unique(
                trend.get("evidence_refs", [])
                + supply.get("evidence_refs", [])
                + realization.get("evidence_refs", [])
            ),
        )
    return (
        "值得研究",
        "已有产业与候选兑现事实，下一步应验证行业级供需、利润池和市场已计价程度，而不是直接给出交易结论。",
        _unique(
            trend.get("evidence_refs", [])
            + supply.get("evidence_refs", [])
            + pool.get("evidence_refs", [])
            + scarcity.get("evidence_refs", [])
            + realization.get("evidence_refs", [])
        ),
    )


def _core_contradiction(sections: Mapping[str, Any]) -> dict[str, Any]:
    priorities = (
        ("supply_demand", "产业主题存在，但供需变化尚未形成行业级可验证闭环。"),
        ("profit_pool", "产业增长未必等于利润留在该环节，利润池和议价权仍待验证。"),
        ("scarcity", "候选可能处在产业链中，但稀缺性与替代难度尚未证明。"),
        ("profit_realization", "产业线索尚未稳定传导为订单、收入、利润和现金流。"),
        ("market_pricing", "产业与公司事实存在，但市场已交易多少仍无法仅凭主题判断。"),
    )
    for key, summary in priorities:
        section = _mapping(sections.get(key))
        if section.get("status") != "已验证":
            return {
                "status": section.get("status", "需人工确认"),
                "summary": summary,
                "evidence_refs": _string_list(section.get("evidence_refs")) or [f"sections.{key}"],
                "unknowns": [item for item in _list(section.get("unknowns")) if isinstance(item, Mapping)],
            }
    return {
        "status": "部分验证",
        "summary": "候选事实已形成部分闭环，但行业级证据仍需持续验证，不能把一次性数据外推为长期趋势。",
        "evidence_refs": _string_list(_mapping(sections.get("profit_realization")).get("evidence_refs")) or ["sections.profit_realization"],
        "unknowns": [],
    }


def _verification_plan(sections: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [
        ("供需变化", "价格、库存、订单、CR3 或扩产周期中至少两类独立行业证据。", "supply_demand"),
        ("利润兑现", "候选的订单、收入、利润和经营现金流是否连续同向。", "profit_realization"),
        ("市场已计价", "行业级估值、价格位置和拥挤度，与利润兑现交叉验证。", "market_pricing"),
    ]
    return [
        {
            "priority": index,
            "item": label,
            "required_evidence": requirement,
            "status": _mapping(sections.get(key)).get("status", "需人工确认"),
            "evidence_refs": _string_list(_mapping(sections.get(key)).get("evidence_refs")) or [f"sections.{key}"],
        }
        for index, (label, requirement, key) in enumerate(rows, start=1)
    ]


def build_sector_judgment_card(
    fact_packet: Mapping[str, Any],
    comparison: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the front-stage sector judgment card from a normalized fact packet."""
    if fact_packet.get("packet_type") != "sector_fact_packet":
        raise SectorValidationError("fact_packet 必须来自 build_sector_fact_packet")
    sections = _mapping(fact_packet.get("sections"))
    comparison = dict(comparison or compare_sector_candidates(fact_packet))
    state, state_reason, state_refs = _derive_sector_state(sections, comparison)
    contradiction = _core_contradiction(sections)
    one_sentence = {
        "值得研究": f"{fact_packet.get('sector')} 值得研究，但板块逻辑不等于候选个股已经具备配置条件。",
        "等待验证": f"{fact_packet.get('sector')} 可以跟踪，但当前核心矛盾尚未闭环，先验证利润而非追逐概念。",
        "暂不优先": f"{fact_packet.get('sector')} 当前证据不足，不能把主题关联当成产业机会。",
    }[state]
    gaps = [gap for section in sections.values() for gap in _list(_mapping(section).get("unknowns")) if isinstance(gap, Mapping)]
    result = {
        "schema_version": SECTOR_SCHEMA_VERSION,
        "packet_type": "sector_judgment",
        "sector": fact_packet.get("sector"),
        "sector_state": state,
        "not_stock_decision": True,
        "one_sentence": one_sentence,
        "one_sentence_evidence_refs": state_refs or ["sections"],
        "state_reason": {"summary": state_reason, "evidence_refs": state_refs or ["sections"]},
        "core_contradiction": contradiction,
        "sections": {key: _mapping(sections.get(key)) for key in SECTION_KEYS},
        "candidate_comparison": comparison,
        "verification": _verification_plan(sections),
        "evidence_gaps": gaps,
        "source_note": "板块状态仅表示研究优先级，不等同于个股五态、买卖建议或仓位建议。",
    }
    validate_sector_judgment(result)
    return result


def build_sector_judgment(
    sector: str,
    packets_or_evidence: Any,
    *,
    sector_evidence: Mapping[str, Any] | None = None,
    collect_sector: bool = False,
    sector_context: str = "",
    sector_provider: str | None = None,
    sector_timeout: int = 12,
) -> dict[str, Any]:
    """Build one sector judgment, optionally collecting industry evidence.

    Network collection is strictly opt-in through ``collect_sector``.  Provided
    industry JSON takes precedence when it is traceable; collection only fills
    missing sections and never promotes candidate aggregation into a sector fact.
    """
    if sector_timeout < 1:
        raise ValueError("sector_timeout 必须大于 0")
    collected = (
        _collect_sector_evidence(
            sector,
            context=sector_context,
            provider=sector_provider,
            timeout=sector_timeout,
        )
        if collect_sector
        else None
    )
    effective_evidence = _merge_sector_evidence(sector_evidence, collected)
    packet = build_sector_fact_packet(sector, packets_or_evidence, sector_evidence=effective_evidence)
    judgment = build_sector_judgment_card(packet, compare_sector_candidates(packet))
    judgment["sector_evidence_collection"] = _collection_metadata(
        requested=collect_sector,
        provided=sector_evidence,
        collected=collected,
        effective=effective_evidence,
    )
    return judgment


def validate_sector_judgment(payload: Mapping[str, Any]) -> None:
    """Check that the card remains evidence-first and not a second scorecard."""
    if payload.get("schema_version") != SECTOR_SCHEMA_VERSION or payload.get("packet_type") != "sector_judgment":
        raise SectorValidationError("板块判断卡 schema_version 或 packet_type 不正确")
    if payload.get("sector_state") not in SECTOR_STATES:
        raise SectorValidationError("sector_state 必须是独立板块研究状态")
    if payload.get("not_stock_decision") is not True:
        raise SectorValidationError("板块判断卡必须声明不属于个股交易决策")
    if not _string_list(payload.get("one_sentence_evidence_refs")):
        raise SectorValidationError("one_sentence 必须包含 evidence_refs")
    sections = _mapping(payload.get("sections"))
    for key in SECTION_KEYS:
        section = _mapping(sections.get(key))
        if section.get("status") not in {"已验证", "部分验证", "需人工确认"}:
            raise SectorValidationError(f"sections.{key}.status 不正确")
        if not _text(section.get("summary")):
            raise SectorValidationError(f"sections.{key}.summary 不能为空")
        if not _string_list(section.get("evidence_refs")):
            raise SectorValidationError(f"sections.{key}.evidence_refs 不能为空")
        if not isinstance(section.get("unknowns"), list):
            raise SectorValidationError(f"sections.{key}.unknowns 必须是列表")
    contradiction = _mapping(payload.get("core_contradiction"))
    if not _text(contradiction.get("summary")) or not _string_list(contradiction.get("evidence_refs")):
        raise SectorValidationError("core_contradiction 必须包含 summary 和 evidence_refs")
    comparison = _mapping(payload.get("candidate_comparison"))
    if comparison.get("comparison_type") != "evidence_ordering_not_score":
        raise SectorValidationError("候选比较不得退化为第二套评分")
    for row in _list(comparison.get("candidates")):
        if not isinstance(row, Mapping) or not _string_list(row.get("evidence_refs")):
            raise SectorValidationError("每个候选比较行必须包含 evidence_refs")
        safety = _mapping(row.get("safety_margin"))
        peer = _mapping(row.get("why_not_peer"))
        if not isinstance(row.get("valuation_snapshot"), Mapping) or not _string_list(safety.get("evidence_refs")):
            raise SectorValidationError("每个候选比较行必须包含可核验估值与安全边际字段")
        if not _text(peer.get("summary")) or not _string_list(peer.get("evidence_refs")):
            raise SectorValidationError("每个候选比较行必须说明为什么不是同行或为何不优先")


def _render_unknowns(items: Iterable[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        label = _text(item.get("item")) or "证据缺口"
        reason = _clean_summary(item.get("reason"), "需人工确认。")
        lines.append(f"- {label}：{reason}")
    return lines


def render_sector_judgment(payload: Mapping[str, Any]) -> str:
    """Render an investment-first sector card without raw collector errors."""
    validate_sector_judgment(payload)
    sections = _mapping(payload.get("sections"))
    trend = _mapping(sections.get("industry_trend"))
    supply = _mapping(sections.get("supply_demand"))
    pool = _mapping(sections.get("profit_pool"))
    scarcity = _mapping(sections.get("scarcity"))
    realization = _mapping(sections.get("profit_realization"))
    pricing = _mapping(sections.get("market_pricing"))
    contradiction = _mapping(payload.get("core_contradiction"))
    comparison = _mapping(payload.get("candidate_comparison"))
    rows = [item for item in _list(comparison.get("candidates")) if isinstance(item, Mapping)]

    def section_line(label: str, section: Mapping[str, Any]) -> str:
        return f"**{label}：** {section.get('summary', '需人工确认。')}（{section.get('status', '需人工确认')}）"

    lines = [
        f"# 莫大 Agent 板块判断：{payload.get('sector')}",
        "",
        "## 一句话判断",
        str(payload.get("one_sentence")),
        "",
        "## 我的判断",
        f"当前研究优先级：{payload.get('sector_state')}（不是个股买卖结论）",
        _mapping(payload.get("state_reason")).get("summary", "需人工确认。"),
        "",
        "## 为什么现在看这个行业",
        section_line("产业变化", trend),
        section_line("供需与景气", supply),
        f"**当前核心矛盾：** {contradiction.get('summary', '需人工确认。')}",
        "",
        "## 钱最终流向哪里",
        section_line("利润池", pool),
        section_line("稀缺环节", scarcity),
        "",
        "## 谁先把产业逻辑变成业绩",
        section_line("利润兑现", realization),
        "",
        "## 市场在交易什么",
        section_line("已计价与拥挤度", pricing),
        "",
        "## 候选公司：为什么是它，而不是同行",
        comparison.get("conclusion", {}).get("summary", "需人工确认。"),
        "",
        "| 排序 | 公司 | 归类 | 产业位置 | 利润兑现 | 稀缺性 | 已计价提示 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if rows:
        for row in rows:
            name = row.get("name") or row.get("code") or "需人工确认"
            lines.append(
                f"| {row.get('rank')} | {name} | {row.get('candidate_class')} | {row.get('chain_position')} | {row.get('profit_realization')} | {row.get('scarcity_status')} | {row.get('pricing_state')} |"
            )
    else:
        lines.append("| - | 暂无候选 | 需人工确认 | - | - | - | - |")
    for row in rows:
        safety = _mapping(row.get("safety_margin"))
        peer = _mapping(row.get("why_not_peer"))
        name = row.get("name") or row.get("code") or "需人工确认"
        lines.extend([
            "",
            f"### {row.get('rank')}. {name}",
            f"估值事实：{row.get('valuation_summary', '需人工确认。')}",
            f"安全边际事实：{safety.get('summary', '需人工确认。')}",
            f"为什么不是同行 / 为何不优先：{peer.get('summary', '需人工确认。')}",
        ])
    lines.extend(["", "## 下一步验证"])
    for item in _list(payload.get("verification")):
        if isinstance(item, Mapping):
            lines.append(f"{item.get('priority')}. {item.get('item')}：{item.get('required_evidence')}")
    if payload.get("evidence_gaps"):
        lines.extend(["", "## 需人工确认"])
        lines.extend(_render_unknowns(item for item in _list(payload.get("evidence_gaps")) if isinstance(item, Mapping)))
    return "\n".join(lines).strip() + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an evidence-first Moda sector judgment card")
    parser.add_argument("--sector", required=True, help="板块名称")
    parser.add_argument("--input", action="append", default=[], help="已有 scorecard JSON、schema-v4 research_packet JSON 或原始 evidence JSON；可重复")
    parser.add_argument("--sector-evidence", default="", help="可选的行业级事实 JSON")
    parser.add_argument("--collect-sector", action="store_true", help="显式采集行业级网页证据；默认不联网")
    parser.add_argument("--sector-context", default="", help="可选行业采集上下文，仅在 --collect-sector 时使用")
    parser.add_argument("--sector-provider", default="", help="可选行业网页证据提供方，仅在 --collect-sector 时使用")
    parser.add_argument("--sector-timeout", type=int, default=12, help="行业网页证据采集超时秒数，默认 12")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", default="", help="可选输出文件；未指定时写入标准输出")
    args = parser.parse_args(argv)
    sector_evidence: Mapping[str, Any] | None = None
    if args.sector_evidence:
        try:
            loaded = json.loads(Path(args.sector_evidence).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            parser.error(f"无法读取 --sector-evidence: {exc}")
        if not isinstance(loaded, Mapping):
            parser.error("--sector-evidence 必须是 JSON 对象")
        sector_evidence = loaded
    try:
        payload = build_sector_judgment(
            args.sector,
            args.input,
            sector_evidence=sector_evidence,
            collect_sector=args.collect_sector,
            sector_context=args.sector_context,
            sector_provider=args.sector_provider or None,
            sector_timeout=args.sector_timeout,
        )
    except (TypeError, ValueError, SectorValidationError) as exc:
        parser.error(str(exc))
    output = render_sector_judgment(payload) if args.format == "markdown" else json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
