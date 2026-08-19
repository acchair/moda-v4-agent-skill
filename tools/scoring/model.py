from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from tools.scoring.alpha_crosscheck import evaluate as evaluate_alpha_crosscheck


@dataclass(frozen=True)
class SubfactorResult:
    key: str
    label: str
    score: float
    maximum: float
    status: str
    reason: str
    sources: tuple[str, ...] = ()
    verified_points: float = 0.0
    provisional_points: float = 0.0
    unknown_maximum: float = 0.0
    coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactorResult:
    key: str
    label: str
    score: float
    maximum: float
    subfactors: tuple[SubfactorResult, ...]
    verified_points: float = 0.0
    provisional_points: float = 0.0
    unknown_maximum: float = 0.0
    coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "score": self.score,
            "maximum": self.maximum,
            "subfactors": [item.to_dict() for item in self.subfactors],
            "verified_points": self.verified_points,
            "provisional_points": self.provisional_points,
            "unknown_maximum": self.unknown_maximum,
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class AdjustmentResult:
    key: str
    label: str
    score: float
    minimum: float
    maximum: float
    status: str
    reason: str
    sources: tuple[str, ...] = ()
    verified_points: float = 0.0
    provisional_points: float = 0.0
    unknown_maximum: float = 0.0
    coverage: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Scorecard:
    factors: tuple[FactorResult, ...]
    adjustments: tuple[AdjustmentResult, ...]
    base_score: float
    adjustment_score: float
    final_score: float
    signal: str
    hard_caps: tuple[dict[str, str], ...]
    verified_points: float = 0.0
    provisional_points: float = 0.0
    unknown_maximum: float = 0.0
    coverage: float = 0.0
    research_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "factors": [item.to_dict() for item in self.factors],
            "adjustments": [item.to_dict() for item in self.adjustments],
            "base_score": self.base_score,
            "adjustment_score": self.adjustment_score,
            "final_score": self.final_score,
            "signal": self.signal,
            "hard_caps": list(self.hard_caps),
            "verified_points": self.verified_points,
            "provisional_points": self.provisional_points,
            "unknown_maximum": self.unknown_maximum,
            "coverage": self.coverage,
            "research_score": self.research_score,
        }


def _known(value: Any) -> bool:
    return value is not None and value != "" and value != []


def _number(value: Any) -> float | None:
    if not _known(value) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> float | None:
    number = _number(value)
    if number is None:
        return None
    return number / 100 if abs(number) > 1 else number


def _bounded(value: float, minimum: float, maximum: float) -> float:
    return round(min(maximum, max(minimum, value)), 2)


def _sources(evidence: dict[str, Any], keys: Iterable[str]) -> tuple[str, ...]:
    source_map = evidence.get("metric_sources", {})
    found: list[str] = []
    for key in keys:
        for source in source_map.get(key, []):
            if source and source not in found:
                found.append(source)
    return tuple(found)


def _subfactor(
    evidence: dict[str, Any], key: str, label: str, score: float, maximum: float,
    reason: str, metric_keys: Iterable[str], *, partial: bool = False,
    coverage_keys: Iterable[str | tuple[str, ...]] | None = None,
) -> SubfactorResult:
    metric_keys = tuple(dict.fromkeys(metric_keys))
    coverage_keys = tuple(coverage_keys or metric_keys)
    sources = _sources(evidence, metric_keys)
    sourced_keys = sum(
        any(
            _sources(evidence, (candidate,))
            for candidate in (coverage_key if isinstance(coverage_key, tuple) else (coverage_key,))
        )
        for coverage_key in coverage_keys
    )
    raw_score = _bounded(score, 0, maximum)
    if not sources:
        status = "需人工确认"
        verified_points = 0.0
        provisional_points = 0.0
        unknown_maximum = float(maximum)
        coverage = 0.0
    elif partial:
        status = "部分覆盖"
        verified_points = 0.0
        provisional_points = raw_score
        source_coverage = sourced_keys / len(coverage_keys) if coverage_keys else 0.0
        coverage = _bounded(source_coverage, 0, 1)
        unknown_maximum = round(maximum * (1 - coverage), 2)
    else:
        status = "已验证"
        verified_points = raw_score
        provisional_points = 0.0
        unknown_maximum = 0.0
        coverage = 1.0
    return SubfactorResult(
        key, label, raw_score, maximum, status, reason, sources,
        round(verified_points, 2), round(provisional_points, 2),
        round(unknown_maximum, 2), round(coverage, 4),
    )


def _missing(key: str, label: str, maximum: float, reason: str) -> SubfactorResult:
    return SubfactorResult(key, label, 0, maximum, "需人工确认", reason, (), 0.0, 0.0, maximum, 0.0)


def _factor(key: str, label: str, maximum: float, items: list[SubfactorResult]) -> FactorResult:
    verified = round(sum(item.verified_points for item in items), 2)
    provisional = round(sum(item.provisional_points for item in items), 2)
    unknown = round(sum(item.unknown_maximum for item in items), 2)
    coverage = _bounded((maximum - unknown) / maximum if maximum else 1.0, 0, 1)
    return FactorResult(
        key, label, round(sum(item.score for item in items), 2), maximum, tuple(items),
        verified, provisional, unknown, coverage,
    )


def _adjustment(
    key: str,
    label: str,
    score: float,
    maximum: float,
    status: str,
    reason: str,
    sources: tuple[str, ...] = (),
    *,
    known: bool = False,
    partial: bool = False,
) -> AdjustmentResult:
    score = _bounded(score, 0, maximum)
    if not known:
        return AdjustmentResult(key, label, score, 0, maximum, "需人工确认", reason, sources, 0, 0, maximum, 0)
    if partial:
        return AdjustmentResult(key, label, score, 0, maximum, status or "部分覆盖", reason, sources, 0, score, maximum / 2, 0.5)
    return AdjustmentResult(key, label, score, 0, maximum, status or "已验证", reason, sources, score, 0, 0, 1)


def _apply_web_fallback(factor: FactorResult, evidence: dict[str, Any]) -> FactorResult:
    if factor.key == "F6":
        return factor
    web_results = evidence.get("web_subfactor_results")
    if not isinstance(web_results, dict):
        return factor
    updated: list[SubfactorResult] = []
    for item in factor.subfactors:
        result = web_results.get(f"{factor.key}.{item.key}")
        if item.status not in {"需人工确认", "部分覆盖"} or not isinstance(result, dict):
            updated.append(item)
            continue
        web_status = str(result.get("status") or "搜索失败，需人工确认")
        if web_status == "网络命中（未核验）":
            web_score = _bounded(_number(result.get("score")) or 0, 0, item.maximum)
            score = web_score if item.status == "需人工确认" else max(item.score, web_score)
            provider = str(result.get("provider") or "web")
            source = "DuckDuckGo Lite（未核验）" if provider == "duckduckgo_lite" else "模型联网搜索（未核验）" if provider in {"deepseek_web_search", "openai_web_search"} else "网络搜索（未核验）"
            reason = f"{item.reason}；网络补缺：{result.get('reason', '命中搜索线索')}" if item.status == "部分覆盖" else str(result.get("reason") or item.reason)
            sources = tuple(dict.fromkeys((*item.sources, source)))
            coverage = max(item.coverage, 0.5)
            status = "部分覆盖" if item.status == "部分覆盖" else web_status
            updated.append(SubfactorResult(
                item.key, item.label, score, item.maximum, status, reason, sources,
                item.verified_points, round(max(item.provisional_points, score), 2),
                round(item.maximum * (1 - coverage), 2), coverage,
            ))
        else:
            reason = f"{item.reason}；{result.get('reason', web_status)}"
            # A failed or empty web search is a gap annotation. It must not
            # turn already supported structured evidence into a negative fact.
            status = item.status if item.status == "部分覆盖" else web_status
            updated.append(SubfactorResult(
                item.key, item.label, item.score, item.maximum, status, reason, item.sources,
                item.verified_points, item.provisional_points, item.unknown_maximum, item.coverage,
            ))
    return _factor(factor.key, factor.label, factor.maximum, updated)


def _score_f1(evidence: dict[str, Any]) -> FactorResult:
    items: list[SubfactorResult] = []

    cagr = _fraction(evidence.get("industry_cagr_3y"))
    penetration = _fraction(evidence.get("penetration_rate"))
    track = _number(evidence.get("track_strength"))
    if cagr is not None or penetration is not None:
        score, details, keys = 0.0, [], []
        if cagr is not None:
            keys.append("industry_cagr_3y")
            score += 5 if cagr > 0.30 else 4 if cagr >= 0.20 else 2.5 if cagr >= 0.10 else 0
            details.append(f"行业未来三年 CAGR {cagr:.1%}")
        if penetration is not None:
            keys.append("penetration_rate")
            score += 5 if 0.05 <= penetration <= 0.20 else 3 if penetration <= 0.50 else 1
            details.append(f"产业渗透率 {penetration:.1%}")
        if track is not None:
            keys.append("track_strength")
            track_reason = evidence.get("track_reason", "莫大选股产业趋势判断")
            details.append(f"莫大选股判断：{track_reason}")
        items.append(_subfactor(
            evidence, "era_track", "大时代赛道", score, 10, "；".join(details), keys,
            partial=cagr is None or penetration is None,
            coverage_keys=("industry_cagr_3y", "penetration_rate"),
        ))
    elif track is None:
        items.append(_missing("era_track", "大时代赛道", 10, "缺少未来三年 CAGR、产业渗透率或可核验产业趋势"))
    else:
        track_reason = evidence.get("track_reason", "按行业、主营和产业标签匹配")
        if evidence.get("industry_prosperity_reason"):
            track_reason = f"{track_reason}；{evidence['industry_prosperity_reason']}"
        items.append(_subfactor(
            evidence, "era_track", "大时代赛道", track * 10, 10,
            f"莫大选股判断：{track_reason}",
            ("track_strength",), partial=True,
            coverage_keys=("industry_cagr_3y", "penetration_rate", "track_strength"),
        ))

    stage = evidence.get("chain_position") or evidence.get("chain_stage")
    stage_scores = {
        "resource": 7, "material": 7, "core_equipment": 7, "key_component": 6,
        "module": 4, "whole_machine": 2, "application": 1,
        "upstream": 7, "midstream": 4, "downstream": 1,
    }
    if stage not in stage_scores:
        items.append(_missing("upstream", "上游/卖铲子", 7, "未识别出可靠的产业链位置"))
    else:
        stage_score = stage_scores[stage] * (0.5 if evidence.get("chain_partial", False) else 1.0)
        items.append(_subfactor(
            evidence, "upstream", "上游/卖铲子", stage_score, 7,
            f"产业链位置：{evidence.get('chain_name', '未命名产业链')} / {stage}",
            ("chain_stage", "business_chain_revenue_ratio"), partial=evidence.get("chain_partial", False),
        ))

    supply_count = _number(evidence.get("supply_evidence_count"))
    supply_tightening = evidence.get("supply_tightening")
    supply_categories = _number(evidence.get("supply_category_count"))
    supply_status = evidence.get("supply_signal_status")
    cr3 = _number(evidence.get("supply_cr3"))
    expansion_years = _number(evidence.get("capacity_expansion_cycle_years"))
    if supply_count is None and cr3 is None and expansion_years is None:
        items.append(_missing("supply_gap", "供需失衡", 5, "缺少价格、库存、订单、CR3 或扩产周期证据"))
    else:
        score, details, keys = 0.0, [], []
        if supply_count is not None:
            keys.extend(("supply_evidence_count", "supply_tightening"))
            # Full supply-gap credit requires at least two independent
            # categories agreeing. Conflicting/insufficient evidence stays
            # partial and cannot be converted into a positive signal.
            category_count = supply_categories if supply_categories is not None else supply_count
            score += 2 if category_count >= 2 and supply_tightening is True and supply_status != "conflict" else 0.75 if category_count >= 1 and supply_tightening is not False and supply_status != "conflict" else 0
            details.append(f"价格/库存/订单证据 {supply_count:g} 类（独立类别 {category_count:g}），趋紧={supply_tightening}")
        if cr3 is not None:
            keys.append("supply_cr3")
            score += 1.5 if cr3 > 70 else 1 if cr3 >= 50 else 0.5 if cr3 >= 30 else 0
            details.append(f"供给集中度 CR3 {cr3:g}%")
        if expansion_years is not None:
            keys.append("capacity_expansion_cycle_years")
            score += 1.5 if expansion_years > 3 else 1 if expansion_years >= 1 else 0.5
            details.append(f"扩产周期 {expansion_years:g} 年")
        items.append(_subfactor(
            evidence, "supply_gap", "供需失衡", score, 5, "；".join(details), keys,
            partial=bool(supply_status in {"conflict", "insufficient"}) or not all(
                value is not None for value in (supply_count, supply_tightening, cr3, expansion_years)
            ),
            coverage_keys=(
                ("supply_evidence_count", "supply_tightening"),
                "supply_cr3",
                "capacity_expansion_cycle_years",
            ),
        ))

    choke = _number(evidence.get("chokepoint_score"))
    if choke is None:
        items.append(_missing("chokepoint", "卡脖子/国产替代", 4, "未匹配到精确卡脖子环节或标的"))
    else:
        score = 4 if choke >= 80 else 3 if choke >= 65 else 2 if choke >= 50 else 0
        items.append(_subfactor(
            evidence, "chokepoint", "卡脖子/国产替代", score, 4,
            f"卡脖子数据库评分 {choke:g}", ("chokepoint_score",),
            partial=evidence.get("chokepoint_partial", False),
        ))

    capex_yoy = _fraction(evidence.get("capex_yoy"))
    capex = _number(evidence.get("capex_strength"))
    capex_conflict = evidence.get("capex_conflict") is True
    if capex_conflict:
        items.append(_subfactor(
            evidence, "capex_wave", "资本开支浪潮", 0, 4,
            evidence.get("capex_reason", "公司侧与行业侧资本开支方向冲突"),
            tuple(key for key in ("capex_yoy", "capex_strength", "capex_conflict") if key in evidence),
            partial=True,
        ))
    elif capex_yoy is None and capex is None:
        items.append(_missing("capex_wave", "资本开支浪潮", 4, "缺少资本开支同比、订单或扩产证据"))
    elif capex_yoy is not None:
        score = 4 if capex_yoy > 0.30 else 3 if capex_yoy >= 0.10 else 2 if capex_yoy > 0 else 0
        items.append(_subfactor(evidence, "capex_wave", "资本开支浪潮", score, 4, f"资本开支同比 {capex_yoy:.1%}", ("capex_yoy",)))
    else:
        items.append(_subfactor(
            evidence, "capex_wave", "资本开支浪潮", capex * 4, 4,
            evidence.get("capex_reason", "按订单、扩产和资本开支证据判断"),
            ("capex_strength",), partial=evidence.get("capex_partial", False),
        ))
    return _factor("F1", "产业趋势与资本开支", 30, items)


def _score_f2(evidence: dict[str, Any]) -> FactorResult:
    items: list[SubfactorResult] = []
    action = evidence.get("controller_action")
    action_scores = {"increase": 5, "stable": 4, "reduction": 0}
    action_reasons = {"increase": "控股股东或实控人有增持证据", "stable": "已核验期间未发生减持", "reduction": "控股股东或实控人存在减持"}
    if action not in action_scores:
        items.append(_missing("controller_action", "第一大股东增减持", 5, "未完成控股股东或实控人增减持核验"))
    else:
        items.append(_subfactor(evidence, "controller_action", "第一大股东增减持", action_scores[action], 5, action_reasons[action], ("controller_action",)))

    top1 = _number(evidence.get("top1_holder_pct"))
    if top1 is None:
        items.append(_missing("top1_ratio", "Top1 持股比例", 3, "缺少第一大股东持股比例"))
    else:
        score = 3 if 20 <= top1 <= 40 else 2 if 10 <= top1 <= 55 else 1
        items.append(_subfactor(evidence, "top1_ratio", "Top1 持股比例", score, 3, f"第一大股东持股 {top1:.2f}%", ("top1_holder_pct",)))

    holder_trend = _number(evidence.get("holder_count_change_pct"))
    if holder_trend is None:
        items.append(_missing("holder_trend", "股东户数趋势", 3, "缺少可比期间股东户数变化"))
    else:
        score = 3 if holder_trend <= -20 else 2 if holder_trend <= -5 else 1 if holder_trend < 0 else 0
        items.append(_subfactor(evidence, "holder_trend", "股东户数趋势", score, 3, f"股东户数变化 {holder_trend:.2f}%", ("holder_count_change_pct",)))

    quality = _number(evidence.get("top10_quality"))
    fund_change = _number(evidence.get("fund_holding_change_pct"))
    northbound_change = _number(evidence.get("northbound_holding_change_20d_pct"))
    institution_change = fund_change if fund_change is not None else northbound_change
    if quality is None and institution_change is None:
        items.append(_missing("top10_quality", "前十大股东质量", 2, "缺少前十大股东名单、基金季度变化或沪深港通持股变化"))
    else:
        score = (quality or 0) * 1.5
        if institution_change is not None:
            score += 0.5 if institution_change > 0 else 0.25 if institution_change == 0 else 0
        change_reason = (
            f"基金持股比例变化 {fund_change:.2f} 个百分点" if fund_change is not None
            else f"沪深港通持股比例20日变化 {northbound_change:.2f} 个百分点" if northbound_change is not None
            else "机构持仓变化需人工确认"
        )
        items.append(_subfactor(
            evidence, "top10_quality", "前十大股东质量", score, 2,
            f"{evidence.get('top10_quality_reason', '按国资、产业资本和长期机构判断')}；{change_reason}",
            ("top10_quality", "fund_holding_change_pct", "northbound_holding_change_20d_pct"),
            partial=evidence.get("top10_partial", False) or quality is None or institution_change is None,
        ))

    pledge = _number(evidence.get("pledge_ratio"))
    unlock = _number(evidence.get("unlock_ratio"))
    if pledge is None and unlock is None:
        items.append(_missing("pledge_unlock", "质押/解禁风险", 2, "质押和未来解禁数据均缺失"))
    else:
        known_count = int(pledge is not None) + int(unlock is not None)
        pledge_ok = pledge is None or pledge <= 10
        unlock_ok = unlock is None or unlock <= 5
        score = 2 if known_count == 2 and pledge_ok and unlock_ok else 1 if pledge_ok and unlock_ok else 0
        reason = f"质押比例 {pledge if pledge is not None else '待确认'}%；未来解禁比例 {unlock if unlock is not None else '待确认'}%"
        items.append(_subfactor(evidence, "pledge_unlock", "质押/解禁风险", score, 2, reason, ("pledge_ratio", "unlock_ratio"), partial=known_count < 2))
    return _factor("F2", "股东与筹码", 15, items)


def _score_f3(evidence: dict[str, Any]) -> FactorResult:
    items: list[SubfactorResult] = []
    background = _number(evidence.get("background_quality"))
    if background is None:
        items.append(_missing("background", "好爹/产业背景", 5, "缺少控股股东和实控人背景资料"))
    else:
        items.append(_subfactor(evidence, "background", "好爹/产业背景", background * 5, 5, evidence.get("background_reason", "按国资、央企或强产业资本背景判断"), ("background_quality",), partial=evidence.get("background_partial", False)))

    leadership = _number(evidence.get("leadership_strength"))
    if leadership is None:
        items.append(_missing(
            "leadership", "龙头/核心供应商", 5,
            evidence.get(
                "leadership_missing_reason",
                "缺少名单数据库命中、行业地位、市场份额/规模、客户供应关系、技术或资质证据",
            ),
        ))
    else:
        items.append(_subfactor(evidence, "leadership", "龙头/核心供应商", leadership * 5, 5, evidence.get("leadership_reason", "按行业地位证据判断"), ("leadership_strength",), partial=evidence.get("leadership_partial", False)))

    net_cash_ratio = _fraction(evidence.get("net_cash_ratio"))
    short_cover = _number(evidence.get("cash_to_short_debt"))
    cash_quality = _number(evidence.get("operating_cashflow_to_net_profit"))
    operating_cashflow = _number(evidence.get("operating_cashflow"))
    net_profit = _number(evidence.get("net_profit"))
    debt_ratio = _fraction(evidence.get("debt_ratio"))
    receivables_ratio = _fraction(evidence.get("receivables_to_assets"))
    details, used_keys, financial_score = [], [], 0.0
    if net_cash_ratio is not None:
        used_keys.append("net_cash_ratio")
        financial_score += 2 if net_cash_ratio > 0.20 else 1.5 if net_cash_ratio >= 0.10 else 0.75 if net_cash_ratio >= 0 else 0
        details.append(f"净现金率 {net_cash_ratio:.1%}")
    if short_cover is not None:
        used_keys.append("cash_to_short_debt")
        financial_score += 1.25 if short_cover > 3 else 0.75 if short_cover >= 1 else 0
        details.append(f"现金覆盖短债 {short_cover:.2f} 倍")
    if cash_quality is not None:
        used_keys.append("operating_cashflow_to_net_profit")
        financial_score += 1 if cash_quality > 1 else 0.6 if cash_quality >= 0.5 else 0
        details.append(f"经营现金流/净利润 {cash_quality:.2f}")
    elif operating_cashflow is not None and net_profit is not None:
        used_keys.extend(("operating_cashflow", "net_profit"))
        financial_score += 0.5 if net_profit <= 0 < operating_cashflow else 0
        details.append("亏损期经营现金流为正" if net_profit <= 0 < operating_cashflow else "经营造血未通过")
    balance_checks = []
    if debt_ratio is not None:
        used_keys.append("debt_ratio")
        balance_checks.append(debt_ratio <= 0.70)
        details.append(f"资产负债率 {debt_ratio:.1%}")
    if receivables_ratio is not None:
        used_keys.append("receivables_to_assets")
        balance_checks.append(receivables_ratio <= 0.30)
        details.append(f"应收账款/总资产 {receivables_ratio:.1%}")
    if balance_checks:
        financial_score += 0.75 * sum(balance_checks) / len(balance_checks)
    if not used_keys:
        items.append(_missing("financial_safety", "财务安全", 5, "缺少净现金、短债覆盖、经营造血和资产质量数据"))
    else:
        coverage_groups = sum((net_cash_ratio is not None, short_cover is not None, cash_quality is not None or (operating_cashflow is not None and net_profit is not None), bool(balance_checks)))
        items.append(_subfactor(
            evidence, "financial_safety", "财务安全", financial_score, 5, "；".join(details), used_keys,
            partial=coverage_groups < 4,
            coverage_keys=(
                "net_cash_ratio",
                "cash_to_short_debt",
                ("operating_cashflow_to_net_profit", "operating_cashflow", "net_profit"),
                ("debt_ratio", "receivables_to_assets"),
            ),
        ))

    risk_checks = (
        ("st_risk", "ST/退市风险"),
        ("audit_risk", "审计重大风险"),
        ("goodwill_risk", "商誉风险"),
    )
    known_risks = [(key, label, evidence.get(key)) for key, label in risk_checks if evidence.get(key) is not None]
    if not known_risks:
        items.append(_missing("survival_risk", "退市/审计/商誉风险", 3, "缺少 ST、审计或重大风险核验"))
    else:
        any_risk = any(value is True for _, _, value in known_risks)
        score = 0 if any_risk else sum(value is False for _, _, value in known_risks)
        details = []
        for key, label, value in known_risks:
            if key == "goodwill_risk" and _number(evidence.get("goodwill_to_assets")) is not None:
                ratio = _number(evidence.get("goodwill_to_assets"))
                details.append(f"商誉占总资产 {ratio:.2%}，{'风险偏高' if value is True else '未触发10%观察线'}")
            else:
                details.append(f"{label}：{'有' if value is True else '未见'}")
        items.append(_subfactor(
            evidence, "survival_risk", "退市/审计/商誉风险", score, 3, "；".join(details),
            tuple(key for key, _, _ in known_risks), partial=len(known_risks) < len(risk_checks),
            coverage_keys=tuple(key for key, _ in risk_checks),
        ))

    special = _number(evidence.get("specialized_strength"))
    if special is None:
        items.append(_missing(
            "specialized", "专精特新/单项冠军", 2,
            evidence.get(
                "specialized_missing_reason",
                "完整版名单数据库未命中，且未发现可核验的专精特新或单项冠军证据",
            ),
        ))
    else:
        items.append(_subfactor(evidence, "specialized", "专精特新/单项冠军", special * 2, 2, evidence.get("specialized_reason", "按公开标签判断"), ("specialized_strength",), partial=evidence.get("specialized_partial", False)))
    return _factor("F3", "生存能力与龙头", 20, items)


def _score_f4(evidence: dict[str, Any]) -> FactorResult:
    items: list[SubfactorResult] = []
    match = _number(evidence.get("business_chain_match"))
    if match is None:
        items.append(_missing("business_match", "主营匹配产业链", 4, "缺少主营构成或产业链匹配结果"))
    else:
        partial = evidence.get("business_match_partial", False)
        score = min(match * 4, 2) if partial else match * 4
        items.append(_subfactor(
            evidence, "business_match", "主营匹配产业链", score, 4,
            evidence.get("business_match_reason", "按主营构成和产业链匹配判断"),
            ("business_chain_match", "business_chain_revenue_ratio"), partial=partial,
        ))

    stage = evidence.get("chain_stage")
    stage_scores = {"upstream": 4, "midstream": 2, "downstream": 1}
    if stage not in stage_scores:
        items.append(_missing("profit_position", "利润分配位置", 4, "未识别产业链利润位置"))
    else:
        stage_score = stage_scores[stage] * (0.5 if evidence.get("chain_partial", False) else 1.0)
        items.append(_subfactor(evidence, "profit_position", "利润分配位置", stage_score, 4, f"产业链位置为 {stage}", ("chain_stage", "business_chain_revenue_ratio"), partial=evidence.get("chain_partial", False)))

    overseas = _number(evidence.get("overseas_revenue_ratio"))
    if overseas is None:
        items.append(_missing("overseas", "出口/海外收入", 3, "缺少按地区披露的海外收入比例"))
    else:
        score = 3 if overseas >= 30 else 2 if overseas >= 10 else 1 if overseas > 0 else 0
        period = str(evidence.get("overseas_revenue_period") or "")
        period_text = f"（{period}）" if period else ""
        items.append(_subfactor(
            evidence, "overseas", "出口/海外收入", score, 3,
            f"海外收入占比 {overseas:.2f}%{period_text}",
            ("overseas_revenue_ratio",),
            partial=evidence.get("overseas_revenue_partial", False),
        ))

    realization_score, details, keys = 0.0, [], []
    for key, label in (("revenue_yoy", "营收同比"), ("profit_yoy", "归母净利润同比")):
        value = _number(evidence.get(key))
        if value is not None:
            keys.append(key)
            details.append(f"{label} {value * 100:.2f}%")
            realization_score += 1 if value > 0 else 0
    order_growth = _number(evidence.get("order_growth"))
    if order_growth is not None:
        keys.append("order_growth")
        details.append(f"订单增长 {order_growth:.2f}%")
        realization_score += 2 if order_growth > 0 else 0
    if not keys:
        items.append(_missing("realization", "订单/产能兑现", 4, "缺少营收、利润、订单或产能兑现数据"))
    else:
        industry_financial = evidence.get("industry_financial_signal") if isinstance(evidence.get("industry_financial_signal"), dict) else {}
        industry_status = industry_financial.get("status")
        if industry_status:
            details.append(f"行业财务景气 {industry_status}（仅交叉验证）")
        conflict = bool(evidence.get("industry_prosperity_conflicts")) or industry_status in {"走弱", "不可用"}
        items.append(_subfactor(
            evidence, "realization", "订单/产能兑现", realization_score, 4,
            "；".join(details), keys,
            partial=len(keys) < 3 or conflict or evidence.get("industry_prosperity_coverage") not in (None, "完整"),
            coverage_keys=("revenue_yoy", "profit_yoy", "order_growth"),
        ))
    return _factor("F4", "利润兑现路径", 15, items)


def _score_f5(evidence: dict[str, Any], f3_score: float) -> FactorResult:
    items: list[SubfactorResult] = []
    price = _number(evidence.get("price_percentile_3y"))
    product_cycle = _fraction(evidence.get("product_price_to_history_high"))
    if price is None and product_cycle is None:
        items.append(_missing("price_position", "价格分位", 2.5, "缺少三年股价分位和产品价格周期"))
    else:
        score, details, keys = 0.0, [], []
        if price is not None:
            keys.append("price_percentile_3y")
            score += 1.25 if price <= 0.20 else 1 if price <= 0.35 else 0.625 if price <= 0.50 else 0.25 if price <= 0.70 else 0
            details.append(f"三年股价分位 {price:.1%}")
        if product_cycle is not None:
            keys.append("product_price_to_history_high")
            score += 1.25 if product_cycle < 0.30 else 0.875 if product_cycle <= 0.50 else 0.375 if product_cycle <= 0.70 else 0
            details.append(f"产品价格/历史高点 {product_cycle:.1%}")
        items.append(_subfactor(
            evidence, "price_position", "价格分位（逆向）", score, 2.5,
            "逆向评分：价格位置越低越有利；" + "；".join(details), keys,
            partial=len(keys) < 2,
            coverage_keys=("price_percentile_3y", "product_price_to_history_high"),
        ))

    pe = _number(evidence.get("pe_ttm"))
    peer = _number(evidence.get("peer_pe_ttm_median"))
    pb = _number(evidence.get("pb"))
    pe_percentile = _number(evidence.get("pe_percentile_5y"))
    pb_percentile = _number(evidence.get("pb_percentile_5y"))
    pb_median_ratio = _number(evidence.get("pb_to_5y_median"))
    if pe is None and pb is None and pe_percentile is None and pb_percentile is None and pb_median_ratio is None:
        items.append(_missing("valuation", "PE/PB 相对位置", 2, "PE、PB 和同行估值均缺失"))
    else:
        comparison_scores: list[float] = []
        details, keys = [], []
        if pe is not None:
            keys.append("pe_ttm")
            details.append(f"TTM PE {pe:.2f}")
            if pe > 0 and peer and peer > 0:
                keys.append("peer_pe_ttm_median")
                ratio = pe / peer
                comparison_scores.append(2 if ratio <= 0.75 else 1 if ratio <= 1 else 0)
                details.append(f"同行中位数 {peer:.2f}")
        if pb is not None:
            keys.append("pb")
            comparison_scores.append(2 if 0 < pb <= 1 else 1 if pb <= 1.5 else 0.5 if pb <= 2 else 0)
            details.append(f"PB {pb:.2f}")
        if pe_percentile is not None:
            keys.append("pe_percentile_5y")
            comparison_scores.append(2 if pe_percentile <= 0.20 else 1.5 if pe_percentile <= 0.35 else 1 if pe_percentile <= 0.50 else 0)
            details.append(f"五年 PE 分位 {pe_percentile:.1%}")
        if pb_median_ratio is not None:
            keys.append("pb_to_5y_median")
            comparison_scores.append(2 if pb_median_ratio < 0.50 else 1 if pb_median_ratio <= 1 else 0)
            details.append(f"PB/五年中位数 {pb_median_ratio:.2f}")
        elif pb_percentile is not None:
            keys.append("pb_percentile_5y")
            comparison_scores.append(2 if pb_percentile <= 0.20 else 1.5 if pb_percentile <= 0.35 else 1 if pb_percentile <= 0.50 else 0)
            details.append(f"五年 PB 分位 {pb_percentile:.1%}")
        score = sum(comparison_scores) / len(comparison_scores) if comparison_scores else 0
        has_pe_component = pe is not None or pe_percentile is not None
        has_pb_component = pb is not None or pb_percentile is not None or pb_median_ratio is not None
        if has_pe_component != has_pb_component:
            score = min(score, 1.0)
        items.append(_subfactor(
            evidence, "valuation", "PE/PB 相对位置（逆向）", score, 2, "逆向评分：估值分位越低越有利；" + "；".join(details), keys,
            partial=len(comparison_scores) < 2 or (pe_percentile is None and pb_percentile is None),
            coverage_keys=(
                ("pe_ttm", "peer_pe_ttm_median", "pe_percentile_5y"),
                ("pb", "pb_percentile_5y", "pb_to_5y_median"),
                ("pe_percentile_5y", "pb_percentile_5y", "pb_to_5y_median"),
            ),
        ))

    attention_heat = _number(evidence.get("attention_heat"))
    social_heat = _number(evidence.get("social_heat"))
    heat_values = [value for value in (attention_heat, social_heat) if value is not None]
    heat = max(heat_values) if heat_values else None
    prosperity = evidence.get("industry_prosperity_status")
    industry_cold_value = evidence.get("industry_cycle_cold")
    industry_cold = industry_cold_value is True
    if heat is None and industry_cold_value is None:
        items.append(_missing("coldness", "行业冰点/市场冷落", 2, "缺少个股关注度、人气排名或行业冷落证据"))
    else:
        attention_score = 1 if heat is not None and heat <= 0.20 else 0.75 if heat is not None and heat <= 0.40 else 0.25 if heat is not None and heat <= 0.60 else 0
        score = attention_score + (1 if industry_cold else 0)
        reason = f"逆向评分：关注热度越低越有利；当前 {heat:.2f}" if heat is not None else "逆向评分：个股热度缺失"
        reason += f"；行业周期 {'处于冰点' if industry_cold else prosperity or '未确认冰点'}"
        items.append(_subfactor(
            evidence, "coldness", "行业冰点/市场冷落", score, 2, reason,
            ("attention_heat", "social_heat", "industry_cycle_cold"),
            partial=heat is None or industry_cold_value is None or evidence.get("attention_partial", False) or evidence.get("social_partial", False),
        ))

    details, keys = [], []
    values: dict[str, float] = {}
    for key, label in (("revenue_yoy", "营收同比"), ("profit_yoy", "利润同比"), ("revenue_yoy_delta", "营收同比改善"), ("profit_yoy_delta", "利润同比改善")):
        value = _number(evidence.get(key))
        if value is not None:
            keys.append(key)
            values[key] = value
            details.append(f"{label} {value * 100:.2f}%")
    if not keys:
        items.append(_missing("inflection", "业绩拐点", 2, "缺少营收、利润及其趋势数据"))
    else:
        revenue = values.get("revenue_yoy")
        profit = values.get("profit_yoy")
        revenue_delta = values.get("revenue_yoy_delta")
        profit_delta = values.get("profit_yoy_delta")
        order_growth = _number(evidence.get("order_growth"))
        cashflow = _number(evidence.get("operating_cashflow"))
        if cashflow is not None:
            keys.append("operating_cashflow")
            details.append(f"经营现金流 {'为正' if cashflow > 0 else '为负'}")
        early_reversal = revenue is not None and revenue > 0 and profit is not None and profit < 0 and ((profit_delta or 0) > 0 or (cashflow or 0) > 0)
        confirmed_reversal = revenue is not None and revenue > 0 and profit is not None and profit > 0 and ((revenue_delta or 0) > 0 or (profit_delta or 0) > 0)
        improving_count = sum(
            value > 0 for key, value in values.items() if key in {"revenue_yoy_delta", "profit_yoy_delta"}
        )
        supply_improving = evidence.get("supply_tightening") is True and (order_growth is None or order_growth > 0)
        inflection_score = 2 if early_reversal or confirmed_reversal else 1 if improving_count >= 2 or supply_improving else 0
        if early_reversal:
            details.append("营收转正、利润仍弱但造血或利润趋势改善，符合周期底部前兆")
        elif confirmed_reversal:
            details.append("营收利润转正且同比趋势改善")
        industry_financial = evidence.get("industry_financial_signal") if isinstance(evidence.get("industry_financial_signal"), dict) else {}
        industry_status = industry_financial.get("status")
        supply_status = (evidence.get("industry_supply_signal") or {}).get("status") if isinstance(evidence.get("industry_supply_signal"), dict) else None
        if industry_status:
            details.append(f"行业财务 {industry_status}（交叉验证）")
        if supply_status:
            details.append(f"行业价格/库存供需 {supply_status}（交叉验证）")
        conflict = bool(evidence.get("industry_prosperity_conflicts")) or industry_status == "走弱"
        items.append(_subfactor(
            evidence, "inflection", "业绩拐点", inflection_score, 2,
            "反转确认：低位本身不等于反转；" + "；".join(details), keys,
            partial=len(keys) < 4 or conflict or evidence.get("industry_prosperity_coverage") not in (None, "完整"),
            coverage_keys=("revenue_yoy", "profit_yoy", "revenue_yoy_delta", "profit_yoy_delta"),
        ))

    gap_details, gap_keys = [], []
    track = _number(evidence.get("track_strength"))
    low_attention = heat is not None and heat <= 0.40
    strong_track = track is not None and track >= 0.70
    if heat is not None and track is not None:
        gap_keys.extend(("attention_heat", "track_strength"))
        if low_attention and strong_track:
            gap_details.append("关注度偏低但产业逻辑较强")
    order_growth = _number(evidence.get("order_growth"))
    supply_tightening = evidence.get("supply_tightening")
    company_improvement = False
    if order_growth is not None:
        gap_keys.append("order_growth")
        if order_growth > 0:
            company_improvement = True
            gap_details.append("订单已经改善")
    elif supply_tightening is not None:
        gap_keys.append("supply_tightening")
        if supply_tightening:
            company_improvement = True
            gap_details.append("供需证据开始改善")
    revenue_delta = _number(evidence.get("revenue_yoy_delta"))
    profit_delta = _number(evidence.get("profit_yoy_delta"))
    if revenue_delta is not None or profit_delta is not None:
        gap_keys.extend(key for key, value in (("revenue_yoy_delta", revenue_delta), ("profit_yoy_delta", profit_delta)) if value is not None)
        if (revenue_delta or 0) > 0 or (profit_delta or 0) > 0:
            company_improvement = True
            gap_details.append("财务同比趋势边际改善")
    if not gap_keys:
        items.append(_missing("expectation_gap", "预期差", 1.5, "缺少关注度与产业、订单或财务拐点的交叉证据"))
    else:
        gap_score = 1.5 if low_attention and strong_track and company_improvement else 0
        prosperity = evidence.get("industry_prosperity_status")
        if prosperity:
            gap_details.append(f"行业景气 {prosperity}（只影响确信度）")
        if gap_score == 0:
            gap_details.append("低关注、强产业和公司改善未同时满足；低位只提供赔率，不替代反转证据")
        industry_conflict = bool(evidence.get("industry_prosperity_conflicts")) or prosperity == "走弱"
        items.append(_subfactor(
            evidence, "expectation_gap", "预期差", gap_score, 1.5,
            "；".join(gap_details), gap_keys,
            partial=gap_score == 0 or industry_conflict or evidence.get("industry_prosperity_coverage") not in (None, "完整"),
            coverage_keys=(
                ("attention_heat", "social_heat"),
                "track_strength",
                ("order_growth", "supply_tightening", "revenue_yoy_delta", "profit_yoy_delta"),
            ),
        ))
    return _factor("F5", "低位与困境反转", 10, items)


def _score_f6(adjustments: tuple[AdjustmentResult, ...]) -> FactorResult:
    items = tuple(
        SubfactorResult(
            item.key, item.label, item.score, item.maximum, item.status, item.reason, item.sources,
            item.verified_points, item.provisional_points, item.unknown_maximum, item.coverage,
        )
        for item in adjustments
    )
    return _factor("F6", "修正项", 10, items)


def _adjustments(evidence: dict[str, Any], f1_score: float) -> tuple[AdjustmentResult, ...]:
    crosscheck = evaluate_alpha_crosscheck(evidence)
    methods = crosscheck.get("methods", [])
    available = [item for item in methods if item.get("available_checks", 0) >= 3]
    directions = [int(item.get("direction", 0)) for item in available]
    direction_sum = sum(directions)
    institutional_score = {2: 2.0, 1: 1.5, 0: 1.0, -1: 0.5, -2: 0.0}.get(direction_sum, 1.0) if len(available) == 2 else 0.0
    institutional_label = "看多" if direction_sum > 0 else "看空" if direction_sum < 0 else "中性/分歧"
    institutional_status = "已验证" if len(available) == 2 else "部分覆盖" if available else "需人工确认"
    crosscheck["status"] = institutional_label
    evidence["alpha_crosscheck"] = crosscheck
    source_map = evidence.setdefault("metric_sources", {})
    source_map["institutional_direction"] = ["机构方法/量化选股筛选", "机构方法/投资逻辑追踪"]
    method_text = "；".join(f"{item['method']}={item['label']}（{item['reason']}）" for item in methods)
    institutional_result = _adjustment(
        "institutional_direction", "机构方向", institutional_score, 2, institutional_status,
        f"机构方向{institutional_label}；{method_text}", tuple(source_map["institutional_direction"]),
        known=bool(available), partial=len(available) < 2,
    )

    technical_score = _number(evidence.get("technical_structure_score"))
    if technical_score is None:
        technical_result = _adjustment("technical_structure", "技术结构", 0, 4, "需人工确认", "技术分析未生成结构化得分")
    else:
        technical_result = _adjustment(
            "technical_structure", "技术结构", _bounded(technical_score, 0, 4), 4, "已验证",
            str(evidence.get("technical_structure_reason") or "按缠论结构与技术指标综合判断"),
            _sources(evidence, ("technical_structure_score",)),
            known=bool(_sources(evidence, ("technical_structure_score",))),
        )

    price = _number(evidence.get("price_percentile_3y"))
    attention_heat = _number(evidence.get("attention_heat"))
    social_heat = _number(evidence.get("social_heat"))
    heat_values = [value for value in (attention_heat, social_heat) if value is not None]
    heat = max(heat_values) if heat_values else None
    congestion = _number(evidence.get("market_congestion"))
    congestion_fresh = evidence.get("market_congestion_fresh") is True
    trap_risk = evidence.get("trap_risk_level")
    sentiment_keys: list[str] = []
    if price is not None:
        sentiment_keys.append("price_percentile_3y")
    if attention_heat is not None:
        sentiment_keys.append("attention_heat")
    if social_heat is not None:
        sentiment_keys.append("social_heat")
    if congestion is not None and congestion_fresh:
        sentiment_keys.append("market_congestion")
    social_checked = _number(evidence.get("social_platforms_checked"))
    social_total = _number(evidence.get("social_platforms_total"))
    social_complete = social_checked is not None and social_total is not None and social_checked >= social_total
    trap_complete = evidence.get("trap_risk_level") in {"低", "注意", "高"}
    if not sentiment_keys:
        sentiment_result = _adjustment("sentiment", "情绪/拥挤度", 0, 2, "需人工确认", "缺少价格位置、个股热度和市场拥挤度证据")
    else:
        score, reason = 1, "情绪和拥挤度处于中性区间"
        if trap_risk == "高":
            sentiment_keys.append("trap_risk_level")
            score, reason = 0, "至少两类独立证据形成高异常推广风险"
        elif price is not None and price > 0.80 and ((heat is not None and heat >= 0.80) or (congestion is not None and congestion_fresh and congestion >= 0.80)):
            score, reason = 0, "高位叠加个股过热或市场高拥挤"
        elif price is not None and price <= 0.35 and heat is not None and heat <= 0.35 and f1_score >= 15:
            score, reason = 2, "低位冷门且产业逻辑未破"
        fully_covered = len(sentiment_keys) >= 3 and social_complete and trap_complete
        sentiment_result = _adjustment(
            "sentiment", "情绪/拥挤度", score, 2, "已验证" if fully_covered else "部分覆盖", reason,
            _sources(evidence, sentiment_keys), known=True, partial=not fully_covered,
        )

    catalysts = _number(evidence.get("verified_catalyst_count"))
    if catalysts is None:
        catalyst_result = _adjustment("catalyst", "风口催化", 0, 2, "需人工确认", "缺少公告或研报中的可验证催化")
    else:
        score = min(2, max(0, int(catalysts)))
        partial = evidence.get("catalyst_partial", False) or score == 0
        status = "部分覆盖" if partial else "已验证"
        reason = evidence.get("catalyst_reason") or (
            f"发现 {int(catalysts)} 项可验证催化" if score > 0 else "公告标题未发现可验证催化"
        )
        catalyst_result = _adjustment(
            "catalyst", "风口催化", score, 2, status, reason,
            _sources(evidence, ("verified_catalyst_count", "catalyst_event_count", "catalyst_confirmed_event_count")),
            known=True, partial=partial,
        )
    return institutional_result, technical_result, sentiment_result, catalyst_result


def score_evidence(evidence: dict[str, Any]) -> Scorecard:
    core_factors = tuple(_apply_web_fallback(factor, evidence) for factor in (
        _score_f1(evidence), _score_f2(evidence), _score_f3(evidence), _score_f4(evidence),
    ))
    adjustments = _adjustments(evidence, core_factors[0].score)
    f5 = _apply_web_fallback(_score_f5(evidence, core_factors[2].score), evidence)
    f6 = _score_f6(adjustments)
    factors = (*core_factors, f5, f6)
    base_score = round(sum(factor.score for factor in (*core_factors, f5)), 2)
    adjustment_score = round(f6.score, 2)
    final_score = _bounded(base_score + adjustment_score, 0, 100)
    caps: list[dict[str, str]] = []

    web_results = evidence.get("web_subfactor_results") if isinstance(evidence.get("web_subfactor_results"), dict) else {}
    web_hard_caps = {
        key for result in web_results.values()
        if isinstance(result, dict) and result.get("status") == "已验证"
        for key, value in (result.get("hard_cap_signals") or {}).items() if value is True
    }
    st_risk = evidence.get("st_risk") is True or (evidence.get("st_risk") is None and "st_risk" in web_hard_caps)
    if st_risk:
        caps.append({"condition": "ST 或退市风险", "result": "已触发", "decision_effect": "强制退出"})
    else:
        caps.append({"condition": "ST 或退市风险", "result": "未触发" if evidence.get("st_risk") is False else "需人工确认", "decision_effect": "无" if evidence.get("st_risk") is False else "需人工确认"})

    controller_action = evidence.get("controller_action")
    if controller_action is None and "controller_reduction" in web_hard_caps:
        controller_action = "reduction"
    if controller_action == "reduction":
        caps.append({"condition": "控股股东或实控人减持", "result": "已触发", "decision_effect": "最高等待"})
    else:
        caps.append({"condition": "控股股东或实控人减持", "result": "未触发" if controller_action in ("increase", "stable") else "需人工确认", "decision_effect": "无" if controller_action in ("increase", "stable") else "需人工确认"})

    price = _number(evidence.get("price_percentile_3y"))
    congestion = _number(evidence.get("market_congestion"))
    congestion_fresh = evidence.get("market_congestion_fresh") is True
    hot_cap = price is not None and price > 0.80 and congestion is not None and congestion_fresh and congestion >= 0.80
    hot_known_safe = price is not None and price <= 0.80 or (price is not None and congestion is not None and congestion_fresh)
    caps.append({"condition": "价格高位且市场拥挤过热", "result": "已触发" if hot_cap else "未触发" if hot_known_safe else "需人工确认", "decision_effect": "最高等待" if hot_cap else "无" if hot_known_safe else "需人工确认"})
    all_items = [item for factor in factors for item in factor.subfactors]
    total_maximum = sum(item.maximum for item in all_items)
    verified_points = round(sum(item.verified_points for item in all_items), 2)
    provisional_points = round(sum(item.provisional_points for item in all_items), 2)
    unknown_maximum = round(sum(item.unknown_maximum for item in all_items), 2)
    coverage = _bounded((total_maximum - unknown_maximum) / total_maximum if total_maximum else 1.0, 0, 1)
    known_maximum = max(0.0, total_maximum - unknown_maximum)
    research_score = _bounded(
        (verified_points + provisional_points) / known_maximum * 100 if known_maximum else 0.0,
        0,
        100,
    )
    return Scorecard(
        factors=factors,
        adjustments=adjustments,
        base_score=base_score,
        adjustment_score=adjustment_score,
        final_score=final_score,
        signal=str(evidence.get("technical_signal") or "需人工确认"),
        hard_caps=tuple(caps),
        verified_points=verified_points,
        provisional_points=provisional_points,
        unknown_maximum=unknown_maximum,
        coverage=coverage,
        research_score=research_score,
    )
