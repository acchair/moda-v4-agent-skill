from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
CASE_KINDS = {"stock", "sector", "concept"}
SCREENING_KINDS = {"sector", "concept"}
CASE_STATUSES = {
    "needs_logic",
    "needs_evidence",
    "needs_candidate_selection",
    "needs_deep_research",
    "needs_judgment",
    "ready",
    "failed",
}
CLAIM_STATUSES = {"unverified", "partial", "verified", "contradicted"}
EVIDENCE_RELATIONS = {"supports", "contradicts", "context", "candidate"}
DECISION_STATES = {"观察", "等待", "试错", "买入", "退出"}
BENEFIT_LEVELS = {"A 已坐实", "B 高概率受益", "C 主题关联", "需人工确认"}

REQUEST_RULES = {
    "system_change": ("F1", "era_track", "大时代产业变化", 10.0),
    "supply": ("F1", "supply_gap", "供需关系与扩产周期", 5.0),
    "bottleneck": ("F1", "chokepoint", "产业瓶颈与替代难度", 4.0),
    "capex": ("F1", "capex_wave", "资本开支与新增需求", 4.0),
    "company_position": ("F4", "business_match", "主营与产业链位置", 4.0),
    "profit": ("F4", "realization", "订单到利润的兑现", 4.0),
    "peers": ("F3", "leadership", "同行地位与竞争壁垒", 5.0),
    "expectation": ("F5", "expectation_gap", "市场预期差", 1.5),
    "valuation": ("F5", "valuation", "估值与市场隐含预期", 2.0),
    "risk": ("F3", "survival_risk", "生存与治理风险", 3.0),
}


class LogicValidationError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _slug(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", value.strip()).strip("-")
    return normalized[:32] or "logic"


def make_case_id(query: str, kind: str) -> str:
    normalized = f"{kind}:{' '.join(query.split()).lower()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{_slug(query)}-{digest}"


def _packet_value(packet: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = packet
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, "", [], {}):
            return current
    return None


def _default_hypotheses(query: str, kind: str, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = _text(_packet_value(packet, "company.name", "company.security_name")) or query
    company_claim = (
        f"{name}能否把产业变化转化为订单、收入、利润和现金流"
        if kind == "stock"
        else f"{query}中哪些公司能把产业变化转化为可持续利润"
    )
    return [
        {
            "claim_id": "H1",
            "claim": f"{query}对应的系统变化是否具有三年以上持续性",
            "status": "unverified",
            "support_refs": [],
            "counter_refs": [],
            "invalidation_conditions": ["需求、资本开支或政策方向连续转弱"],
            "confidence": "低",
        },
        {
            "claim_id": "H2",
            "claim": f"{query}是否形成难替代的产业瓶颈和新增利润池",
            "status": "unverified",
            "support_refs": [],
            "counter_refs": [],
            "invalidation_conditions": ["瓶颈被快速扩产、替代或价格竞争消除"],
            "confidence": "低",
        },
        {
            "claim_id": "H3",
            "claim": company_claim,
            "status": "unverified",
            "support_refs": [],
            "counter_refs": [],
            "invalidation_conditions": ["订单无法转化为收入、利润和现金流"],
            "confidence": "低",
        },
    ]


def _default_requests(query: str, kind: str, packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    name = _text(_packet_value(packet, "company.name", "company.security_name")) or query
    requests = [
        {
            "request_id": "R1",
            "claim_id": "H1",
            "kind": "system_change",
            "question": f"{query}真正发生了什么结构性变化，为什么不是短期题材？",
            "support_queries": [f"{query} 产业趋势 需求增长 资本开支 权威报告"],
            "counter_queries": [f"{query} 需求放缓 产能过剩 技术替代 风险"],
            "source_types": ["产业权威来源", "法定披露", "行业数据"],
            "status": "pending",
        },
        {
            "request_id": "R2",
            "claim_id": "H2",
            "kind": "bottleneck",
            "question": f"{query}的扩张究竟会卡在哪一层，谁拥有定价权？",
            "support_queries": [f"{query} 瓶颈 扩产周期 客户认证 供应商 稀缺"],
            "counter_queries": [f"{query} 可替代 扩产 竞争加剧 价格下降"],
            "source_types": ["产业权威来源", "公司法定披露"],
            "status": "pending",
        },
        {
            "request_id": "R3",
            "claim_id": "H3",
            "kind": "profit" if kind == "stock" else "peers",
            "question": f"{name}如何把需求变化转成订单、收入、利润和现金流？",
            "support_queries": [f"{name} 主营 收入占比 订单 客户 产能 毛利率"],
            "counter_queries": [f"{name} 订单下降 毛利率下降 客户流失 现金流风险"],
            "source_types": ["年报", "公告", "财报", "客户认证"],
            "status": "pending",
        },
        {
            "request_id": "R4",
            "claim_id": "H3",
            "kind": "expectation",
            "question": f"市场已经如何定价{name}，还有什么尚未被相信？",
            "support_queries": [f"{name} 市场预期 估值 订单改善 机构覆盖"],
            "counter_queries": [f"{name} 充分定价 估值高位 交易拥挤"],
            "source_types": ["市场数据", "法定披露", "权威财经媒体"],
            "status": "pending",
        },
    ]
    return requests


def _candidate_rows(kind: str, packet: Mapping[str, Any], screening: Mapping[str, Any]) -> list[dict[str, Any]]:
    if kind == "stock":
        code = _text(_packet_value(packet, "company.code"))
        name = _text(_packet_value(packet, "company.name", "company.security_name"))
        if not code and not name:
            return []
        return [{
            "code": code,
            "name": name or code,
            "role": "待定位",
            "benefit_level": "需人工确认",
            "business_purity": "需人工确认",
            "barrier": "需人工确认",
            "profit_realization": "需人工确认",
            "expectation_gap": "需人工确认",
            "selected_for_deep_research": True,
            "evidence_refs": ["research_packet.company.main_business"],
        }]
    rows = (
        _list(screening.get("priority_research"))
        or _list(screening.get("shortlist"))
        or _list(screening.get("candidates"))
    )
    result = []
    for raw in rows[:12]:
        if not isinstance(raw, Mapping):
            continue
        result.append({
            "code": _text(raw.get("code")),
            "name": _text(raw.get("name")) or _text(raw.get("code")),
            "role": _text(raw.get("chain_stage") or raw.get("industry_position")) or "待定位",
            "benefit_level": "需人工确认",
            "business_purity": _text(raw.get("business_purity") or raw.get("concept_exposure_tier") or raw.get("business_status")) or "需人工确认",
            "barrier": _text(raw.get("barrier_status") or raw.get("barrier_evidence")) or "需人工确认",
            "profit_realization": _text(raw.get("profit_status") or raw.get("profit_reason")) or "需人工确认",
            "expectation_gap": "需人工确认",
            "selected_for_deep_research": False,
            "evidence_refs": [],
        })
    return result


def new_logic_case(
    query: str,
    kind: str,
    *,
    research_packet: Mapping[str, Any] | None = None,
    screening: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in CASE_KINDS:
        raise LogicValidationError(f"kind 必须是 stock、sector 或 concept，当前为 {kind!r}")
    packet = _mapping(research_packet)
    screen = _mapping(screening)
    created = _now()
    case_id = make_case_id(query, kind)
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "kind": kind,
        "query": query.strip(),
        "title": query.strip(),
        "phase": "logic_draft",
        "status": "needs_logic",
        "system_change": {
            "claim": "",
            "why_structural": "",
            "time_horizon": "",
            "fact": "",
            "inference": "",
            "evidence_refs": [],
        },
        "chain_map": {"nodes": [], "edges": []},
        "bottleneck": {
            "link": "",
            "scarcity_type": "",
            "why_scarce": "",
            "replacement_risk": "需人工确认",
            "evidence_refs": [],
        },
        "profit_pool": {
            "payer": "",
            "receiver": "",
            "mechanism": "",
            "realization_window": "",
            "evidence_refs": [],
        },
        "hypotheses": _default_hypotheses(query, kind, packet),
        "evidence_requests": _default_requests(query, kind, packet),
        "evidence_graph": [],
        "company_branches": _candidate_rows(kind, packet, screen),
        "market_expectation": {
            "known": "",
            "priced_in": "",
            "unpriced": "",
            "mispriced": "",
            "evidence_refs": [],
        },
        "decision": {
            "state": "",
            "rationale": "",
            "why_not_higher_state": "",
            "odds": "需人工确认",
            "evidence_refs": [],
        },
        "verification": {"top_variables": []},
        "context": {
            "research_packet": packet,
            "screening": screen,
        },
        "audit": {
            "research_packet_schema": packet.get("schema_version"),
            "score_role": "evidence_dashboard_only",
            "collection_runs": [],
        },
        "history": [],
        "created_at": created,
        "updated_at": created,
    }


def evidence_requests_to_targets(
    case: Mapping[str, Any],
    request_kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    selected = {_text(item) for item in _list(request_kinds) if _text(item)}
    targets: list[dict[str, Any]] = []
    for request in _list(case.get("evidence_requests")):
        if not isinstance(request, Mapping) or _text(request.get("status")) == "completed":
            continue
        kind = _text(request.get("kind"))
        if selected and kind not in selected:
            continue
        rule = REQUEST_RULES.get(kind)
        if rule is None:
            continue
        factor_key, subfactor_key, label, maximum = rule
        queries = [
            _text(item)
            for item in [*_list(request.get("support_queries")), *_list(request.get("counter_queries"))]
            if _text(item)
        ]
        targets.append({
            "factor_key": factor_key,
            "subfactor_key": subfactor_key,
            "label": label,
            "maximum": maximum,
            "original_status": "逻辑命题待核验",
            "original_reason": _text(request.get("question")),
            "logic_request_id": _text(request.get("request_id")),
            "claim_id": _text(request.get("claim_id")),
            "request_kind": kind,
            "queries": queries,
        })
    return targets


def merge_web_evidence(case: Mapping[str, Any], web_data: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(case))
    existing_ids = {_text(item.get("evidence_id")) for item in _list(result.get("evidence_graph")) if isinstance(item, Mapping)}
    graph = _list(result.get("evidence_graph"))
    completed_requests: set[str] = set()
    for gap in _list(web_data.get("web_gap_results")):
        if not isinstance(gap, Mapping):
            continue
        request_id = _text(gap.get("logic_request_id"))
        claim_id = _text(gap.get("claim_id"))
        if request_id:
            completed_requests.add(request_id)
        for row in _list(gap.get("evidence")):
            if not isinstance(row, Mapping):
                continue
            url = _text(row.get("url"))
            title = _text(row.get("title"))
            seed = f"{claim_id}|{url}|{title}"
            evidence_id = "E" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
            if evidence_id in existing_ids:
                continue
            existing_ids.add(evidence_id)
            graph.append({
                "evidence_id": evidence_id,
                "fact": title or _text(row.get("snippet")) or "网络候选线索",
                "source": _text(row.get("provider")) or "web",
                "source_url": url,
                "tier": "C" if _text(row.get("body_status")) != "正文已核验" else "B",
                "published_at": "",
                "observed_at": _now(),
                "status": "candidate" if _text(row.get("body_status")) != "正文已核验" else "body_verified",
                "claim_links": [{"claim_id": claim_id, "relation": "candidate", "strength": "low"}],
            })
    result["evidence_graph"] = graph
    requests = []
    for request in _list(result.get("evidence_requests")):
        if not isinstance(request, Mapping):
            continue
        item = dict(request)
        if _text(item.get("request_id")) in completed_requests:
            item["status"] = "searched"
        requests.append(item)
    result["evidence_requests"] = requests
    result["updated_at"] = _now()
    return result


def _reference_exists(case: Mapping[str, Any], reference: str) -> bool:
    if reference.startswith("research_packet."):
        current: Any = _mapping(case.get("context")).get("research_packet")
        for part in reference.removeprefix("research_packet.").split("."):
            if not isinstance(current, Mapping) or part not in current:
                return False
            current = current[part]
        return current not in (None, "", [], {})
    ids = {
        _text(item.get("evidence_id"))
        for item in _list(case.get("evidence_graph"))
        if isinstance(item, Mapping)
    }
    return reference in ids


def _normalize_payload(payload: Mapping[str, Any], previous: Mapping[str, Any] | None) -> dict[str, Any]:
    base = deepcopy(dict(previous or {}))
    for key, value in payload.items():
        if key in {"created_at", "history"} and key in base:
            continue
        base[key] = deepcopy(value)
    base.setdefault("created_at", _now())
    base.setdefault("history", [])
    base["updated_at"] = _now()
    return base


def _derive_status(case: Mapping[str, Any]) -> str:
    system_change = _mapping(case.get("system_change"))
    chain_map = _mapping(case.get("chain_map"))
    hypotheses = [item for item in _list(case.get("hypotheses")) if isinstance(item, Mapping)]
    if not _text(system_change.get("claim")) or len(_list(chain_map.get("edges"))) < 2:
        return "needs_logic"
    if not hypotheses or any(_text(item.get("status")) == "unverified" for item in hypotheses):
        return "needs_evidence"
    if _text(case.get("kind")) in SCREENING_KINDS and not any(
        bool(item.get("selected_for_deep_research"))
        for item in _list(case.get("company_branches"))
        if isinstance(item, Mapping)
    ):
        return "needs_candidate_selection"
    packet = _mapping(_mapping(case.get("context")).get("research_packet"))
    if _text(case.get("kind")) == "stock" and packet.get("schema_version") != 4:
        return "needs_deep_research"
    decision = _mapping(case.get("decision"))
    if _text(case.get("kind")) == "stock" and _text(decision.get("state")) not in DECISION_STATES:
        return "needs_judgment"
    if _text(case.get("kind")) in SCREENING_KINDS:
        context = _mapping(case.get("context"))
        return "ready" if context.get("sector_research") else "needs_deep_research"
    return "ready"


def derive_status(case: Mapping[str, Any]) -> str:
    """Return the public workflow status for a Logic Case."""
    return _derive_status(case)


def validate_logic_case(
    payload: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
    require_decision: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise LogicValidationError("logic_case 必须是对象")
    case = _normalize_payload(payload, previous)
    if int(case.get("schema_version") or 0) != SCHEMA_VERSION:
        raise LogicValidationError(f"logic_case.schema_version 必须为 {SCHEMA_VERSION}")
    kind = _text(case.get("kind"))
    if kind not in CASE_KINDS:
        raise LogicValidationError("logic_case.kind 必须为 stock、sector 或 concept")
    query = _text(case.get("query"))
    if not query:
        raise LogicValidationError("logic_case.query 不能为空")
    expected_id = make_case_id(query, kind)
    if _text(case.get("case_id")) != expected_id:
        raise LogicValidationError(f"logic_case.case_id 必须为 {expected_id}")

    system_change = _mapping(case.get("system_change"))
    if not _text(system_change.get("claim")):
        raise LogicValidationError("system_change.claim 必须说明发生了什么变化")
    if not _text(system_change.get("why_structural")):
        raise LogicValidationError("system_change.why_structural 必须说明为何不是短期题材")
    refs = [_text(item) for item in _list(system_change.get("evidence_refs")) if _text(item)]
    if not refs:
        raise LogicValidationError("system_change.evidence_refs 不能为空")
    for reference in refs:
        if not _reference_exists(case, reference):
            raise LogicValidationError(f"system_change 引用了不存在的证据：{reference}")

    chain = _mapping(case.get("chain_map"))
    edges = [item for item in _list(chain.get("edges")) if isinstance(item, Mapping)]
    hypotheses = [item for item in _list(case.get("hypotheses")) if isinstance(item, Mapping)]
    declared_claim_ids = {_text(item.get("claim_id")) for item in hypotheses if _text(item.get("claim_id"))}
    if not 2 <= len(edges) <= 10:
        raise LogicValidationError("chain_map.edges 必须包含 2-10 条因果箭头")
    for index, edge in enumerate(edges):
        if not all(_text(edge.get(field)) for field in ("from", "to", "claim", "claim_id", "status")):
            raise LogicValidationError(f"chain_map.edges.{index} 缺少 from/to/claim/claim_id/status")
        if _text(edge.get("claim_id")) not in declared_claim_ids:
            raise LogicValidationError(f"chain_map.edges.{index}.claim_id 未关联有效命题")
        if _text(edge.get("status")) not in CLAIM_STATUSES:
            raise LogicValidationError(f"chain_map.edges.{index}.status 不受支持")
        if not [_text(item) for item in _list(edge.get("invalidation_conditions")) if _text(item)]:
            raise LogicValidationError(f"chain_map.edges.{index} 缺少失效条件")
        support = [_text(item) for item in _list(edge.get("support_refs")) if _text(item)]
        counter = [_text(item) for item in _list(edge.get("counter_refs")) if _text(item)]
        if _text(edge.get("status")) == "verified" and not support:
            raise LogicValidationError(f"chain_map.edges.{index} 已验证但没有支持证据")
        if _text(edge.get("status")) == "contradicted" and not counter:
            raise LogicValidationError(f"chain_map.edges.{index} 已证伪但没有反证")
        for reference in [*support, *counter]:
            if not _reference_exists(case, reference):
                raise LogicValidationError(f"chain_map.edges.{index} 引用了不存在的证据：{reference}")

    bottleneck = _mapping(case.get("bottleneck"))
    for field in ("link", "scarcity_type", "why_scarce"):
        if not _text(bottleneck.get(field)):
            raise LogicValidationError(f"bottleneck.{field} 不能为空")
    profit_pool = _mapping(case.get("profit_pool"))
    for field in ("payer", "receiver", "mechanism"):
        if not _text(profit_pool.get(field)):
            raise LogicValidationError(f"profit_pool.{field} 不能为空")

    if not 1 <= len(hypotheses) <= 10:
        raise LogicValidationError("hypotheses 必须包含 1-10 条命题")
    claim_ids: set[str] = set()
    for index, hypothesis in enumerate(hypotheses):
        claim_id = _text(hypothesis.get("claim_id"))
        if not claim_id or claim_id in claim_ids:
            raise LogicValidationError(f"hypotheses.{index}.claim_id 缺失或重复")
        claim_ids.add(claim_id)
        if not _text(hypothesis.get("claim")):
            raise LogicValidationError(f"hypotheses.{index}.claim 不能为空")
        status = _text(hypothesis.get("status"))
        if status not in CLAIM_STATUSES:
            raise LogicValidationError(f"hypotheses.{index}.status 不受支持")
        support = [_text(item) for item in _list(hypothesis.get("support_refs")) if _text(item)]
        counter = [_text(item) for item in _list(hypothesis.get("counter_refs")) if _text(item)]
        if status == "verified" and not support:
            raise LogicValidationError(f"hypotheses.{index} 已验证但没有支持证据")
        if status == "contradicted" and not counter:
            raise LogicValidationError(f"hypotheses.{index} 已证伪但没有反证")
        if not [_text(item) for item in _list(hypothesis.get("invalidation_conditions")) if _text(item)]:
            raise LogicValidationError(f"hypotheses.{index} 缺少失效条件")
        for reference in [*support, *counter]:
            if not _reference_exists(case, reference):
                raise LogicValidationError(f"hypotheses.{index} 引用了不存在的证据：{reference}")

    for index, item in enumerate(_list(case.get("evidence_graph"))):
        if not isinstance(item, Mapping):
            raise LogicValidationError(f"evidence_graph.{index} 必须是对象")
        if not _text(item.get("evidence_id")) or not _text(item.get("fact")):
            raise LogicValidationError(f"evidence_graph.{index} 缺少 evidence_id 或 fact")
        for link in _list(item.get("claim_links")):
            if not isinstance(link, Mapping):
                raise LogicValidationError(f"evidence_graph.{index}.claim_links 必须是对象数组")
            if _text(link.get("claim_id")) not in claim_ids:
                raise LogicValidationError(f"evidence_graph.{index} 关联了不存在的命题")
            if _text(link.get("relation")) not in EVIDENCE_RELATIONS:
                raise LogicValidationError(f"evidence_graph.{index}.relation 不受支持")

    request_ids: set[str] = set()
    request_claim_ids: set[str] = set()
    for index, request in enumerate(_list(case.get("evidence_requests"))):
        if not isinstance(request, Mapping):
            raise LogicValidationError(f"evidence_requests.{index} 必须是对象")
        request_id = _text(request.get("request_id"))
        claim_id = _text(request.get("claim_id"))
        if not request_id or request_id in request_ids:
            raise LogicValidationError(f"evidence_requests.{index}.request_id 缺失或重复")
        request_ids.add(request_id)
        if claim_id not in claim_ids:
            raise LogicValidationError(f"evidence_requests.{index}.claim_id 未关联有效命题")
        request_claim_ids.add(claim_id)
        if not [_text(item) for item in _list(request.get("support_queries")) if _text(item)]:
            raise LogicValidationError(f"evidence_requests.{index} 缺少支持证据查询")
        if not [_text(item) for item in _list(request.get("counter_queries")) if _text(item)]:
            raise LogicValidationError(f"evidence_requests.{index} 缺少反证查询")
    for index, edge in enumerate(edges):
        if _text(edge.get("claim_id")) not in request_claim_ids:
            raise LogicValidationError(f"chain_map.edges.{index} 没有对应的支持与反证请求")

    for index, branch in enumerate(_list(case.get("company_branches"))):
        if not isinstance(branch, Mapping):
            raise LogicValidationError(f"company_branches.{index} 必须是对象")
        if _text(branch.get("benefit_level")) not in BENEFIT_LEVELS:
            raise LogicValidationError(f"company_branches.{index}.benefit_level 不受支持")

    decision = _mapping(case.get("decision"))
    state = _text(decision.get("state"))
    if state and state not in DECISION_STATES:
        raise LogicValidationError("decision.state 不受支持")
    variables = [item for item in _list(_mapping(case.get("verification")).get("top_variables")) if isinstance(item, Mapping)]
    if require_decision:
        if kind != "stock" or state not in DECISION_STATES:
            raise LogicValidationError("正式个股逻辑必须给出五态 decision.state")
        if len(variables) != 3:
            raise LogicValidationError("正式个股逻辑必须包含恰好三项验证变量")
        if any(_text(item.get("status")) == "contradicted" for item in hypotheses) and state != "退出":
            raise LogicValidationError("核心命题已证伪时 decision.state 只能为退出")

    case["status"] = _derive_status(case)
    if require_decision and case["status"] != "ready":
        raise LogicValidationError(f"逻辑尚未闭环，当前状态：{case['status']}")
    return case


def attach_research_packet(case: Mapping[str, Any], packet: Mapping[str, Any], run: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = deepcopy(dict(case))
    context = _mapping(result.get("context"))
    context["research_packet"] = deepcopy(dict(packet))
    result["context"] = context
    audit = _mapping(result.get("audit"))
    audit["research_packet_schema"] = packet.get("schema_version")
    runs = _list(audit.get("collection_runs"))
    if run:
        runs.append(deepcopy(dict(run)))
    audit["collection_runs"] = runs
    result["audit"] = audit
    result["updated_at"] = _now()
    result["status"] = _derive_status(result)
    return result


def apply_judgment(case: Mapping[str, Any], judgment: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(case))
    result["market_expectation"] = deepcopy(_mapping(judgment.get("market_expectation")))
    result["decision"] = deepcopy(_mapping(judgment.get("decision")))
    result["verification"] = deepcopy(_mapping(judgment.get("verification")))
    transition = _mapping(judgment.get("state_transition"))
    if transition:
        history = _list(result.get("history"))
        history.append({**transition, "recorded_at": _now()})
        result["history"] = history
    result["phase"] = "decision"
    result["updated_at"] = _now()
    result["status"] = _derive_status(result)
    return result


def case_paths(root: Path, case_id: str) -> dict[str, Path]:
    base = root / "knowledge" / "research" / "logic_cases" / case_id
    return {
        "base": base,
        "json": base / "current.json",
        "markdown": base / "current.md",
        "history": base / "history",
    }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def load_logic_case(root: Path, case_id: str) -> dict[str, Any] | None:
    path = case_paths(root, case_id)["json"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def save_logic_case(root: Path, case: Mapping[str, Any]) -> dict[str, str]:
    case_id = _text(case.get("case_id"))
    paths = case_paths(root, case_id)
    previous = load_logic_case(root, case_id)
    if previous and previous != case:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        history_path = paths["history"] / f"{stamp}.json"
        _atomic_write(history_path, json.dumps(previous, ensure_ascii=False, indent=2))
    markdown = render_logic_case(case)
    _atomic_write(paths["json"], json.dumps(dict(case), ensure_ascii=False, indent=2))
    _atomic_write(paths["markdown"], markdown)
    return {"case_path": str(paths["json"]), "report_path": str(paths["markdown"])}


def _status_label(value: str) -> str:
    return {
        "unverified": "待核验",
        "partial": "部分验证",
        "verified": "已验证",
        "contradicted": "矛盾",
    }.get(value, value or "待核验")


def render_logic_case(case: Mapping[str, Any]) -> str:
    system = _mapping(case.get("system_change"))
    bottleneck = _mapping(case.get("bottleneck"))
    profit = _mapping(case.get("profit_pool"))
    market = _mapping(case.get("market_expectation"))
    decision = _mapping(case.get("decision"))
    lines = [
        f"# {_text(case.get('title')) or _text(case.get('query'))}：投资逻辑主档",
        "",
        f"> 状态：{_text(case.get('status'))}｜类型：{_text(case.get('kind'))}｜逻辑主档：{_text(case.get('case_id'))}",
        "",
        "## 为什么这个方向",
        "",
        _text(system.get("claim")) or "系统变化待定义。",
        "",
        f"- 为什么不是短期题材：{_text(system.get('why_structural')) or '待核验'}",
        f"- 时间范围：{_text(system.get('time_horizon')) or '待核验'}",
        f"- 当前事实：{_text(system.get('fact')) or '待核验'}",
        f"- 当前推断：{_text(system.get('inference')) or '待核验'}",
        "",
        "## 产业链与利润池",
        "",
    ]
    edges = [item for item in _list(_mapping(case.get("chain_map")).get("edges")) if isinstance(item, Mapping)]
    if edges:
        lines += ["| 从 | 到 | 核心判断 | 状态 | 失效条件 |", "|---|---|---|---|---|"]
        for edge in edges:
            lines.append(
                f"| {_text(edge.get('from'))} | {_text(edge.get('to'))} | {_text(edge.get('claim'))} | "
                f"{_status_label(_text(edge.get('status')))} | "
                f"{'；'.join(_text(item) for item in _list(edge.get('invalidation_conditions')) if _text(item)) or '待定义'} |"
            )
    else:
        lines.append("产业链因果箭头待建立。")
    lines += [
        "",
        f"- 真正瓶颈：{_text(bottleneck.get('link')) or '待核验'}",
        f"- 稀缺类型：{_text(bottleneck.get('scarcity_type')) or '待核验'}",
        f"- 为什么稀缺：{_text(bottleneck.get('why_scarce')) or '待核验'}",
        f"- 利润支付方：{_text(profit.get('payer')) or '待核验'}",
        f"- 利润承接方：{_text(profit.get('receiver')) or '待核验'}",
        f"- 利润兑现机制：{_text(profit.get('mechanism')) or '待核验'}",
        "",
        "## 命题、支持与反证",
        "",
        "| 命题 | 状态 | 支持证据 | 反证 |",
        "|---|---|---|---|",
    ]
    for hypothesis in _list(case.get("hypotheses")):
        if not isinstance(hypothesis, Mapping):
            continue
        lines.append(
            f"| {_text(hypothesis.get('claim'))} | {_status_label(_text(hypothesis.get('status')))} | "
            f"{', '.join(_text(item) for item in _list(hypothesis.get('support_refs'))) or '无'} | "
            f"{', '.join(_text(item) for item in _list(hypothesis.get('counter_refs'))) or '无'} |"
        )
    lines += ["", "## 为什么是它，而不是同行", ""]
    branches = [item for item in _list(case.get("company_branches")) if isinstance(item, Mapping)]
    if branches:
        lines += [
            "| 公司 | 受益层级 | 产业位置 | 主营纯度 | 壁垒 | 利润兑现 | 预期差 | 深研 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for branch in branches:
            lines.append(
                f"| {_text(branch.get('name')) or _text(branch.get('code'))} | {_text(branch.get('benefit_level'))} | "
                f"{_text(branch.get('role'))} | {_text(branch.get('business_purity'))} | {_text(branch.get('barrier'))} | "
                f"{_text(branch.get('profit_realization'))} | {_text(branch.get('expectation_gap'))} | "
                f"{'是' if branch.get('selected_for_deep_research') else '否'} |"
            )
    else:
        lines.append("候选公司待建立。")
    broad = _mapping(_mapping(case.get("context")).get("sector_broad_research"))
    radar = _mapping(broad.get("overseas_event_radar"))
    if radar:
        lines += ["", "## 海外增量到A股的映射", ""]
        lines.append(_text(radar.get("summary")) or "海外增量雷达待执行。")
        events = [item for item in _list(radar.get("events")) if isinstance(item, Mapping)]
        if events:
            lines += [
                "",
                "| 海外事件 | 优先级 | 催化层级 | A股映射 | 必须补的验证 |",
                "|---|---|---|---|---|",
            ]
            for event in events:
                checks = "；".join(_text(item) for item in _list(event.get("a_share_validation")) if _text(item))
                lines.append(
                    f"| {_text(event.get('event_type'))}：{_text(event.get('title')) or _text(event.get('url'))} | "
                    f"{_text(event.get('event_priority'))} | {_text(event.get('catalyst_type'))} | "
                    f"{_text(event.get('mapping_status')) or '待核验'} | {checks or 'F10、订单与利润表待核验'} |"
                )
        else:
            lines.append("本轮没有可展示的正文核验事件；这不是“海外没有事件”或“没有A股受益”的结论。")
    pending_requests = [
        item
        for item in _list(case.get("evidence_requests"))
        if isinstance(item, Mapping) and _text(item.get("status")) != "completed"
    ]
    lines += ["", "## 下一步补什么证据", ""]
    if pending_requests:
        for request in pending_requests:
            support = "；".join(_text(item) for item in _list(request.get("support_queries")) if _text(item))
            counter = "；".join(_text(item) for item in _list(request.get("counter_queries")) if _text(item))
            lines.append(f"- {_text(request.get('question')) or _text(request.get('request_id'))}")
            lines.append(f"  - 支持证据：{support or '待定义'}")
            lines.append(f"  - 反证：{counter or '待定义'}")
    else:
        lines.append("当前没有待补证请求。")
    lines += [
        "",
        "## 为什么是现在",
        "",
        f"- 市场已知：{_text(market.get('known')) or '待核验'}",
        f"- 已经反映：{_text(market.get('priced_in')) or '待核验'}",
        f"- 尚未反映：{_text(market.get('unpriced')) or '待核验'}",
        f"- 可能错定价：{_text(market.get('mispriced')) or '待核验'}",
        "",
    ]
    if _text(case.get("kind")) == "stock":
        lines += [
            "## 当前行动",
            "",
            f"- 五态：{_text(decision.get('state')) or '判断待生成'}",
            f"- 原因：{_text(decision.get('rationale')) or '待核验'}",
            f"- 为什么不能更高：{_text(decision.get('why_not_higher_state')) or '待核验'}",
            f"- 赔率：{_text(decision.get('odds')) or '需人工确认'}",
            "",
        ]
    lines += [
        "## 证据审计",
        "",
        "F1-F6、Coverage、技术指标、来源状态和 Hard Cap 保留在 moda-v4 审计报告中；它们不替代本逻辑主档的投资判断。",
        "",
    ]
    return "\n".join(lines)
