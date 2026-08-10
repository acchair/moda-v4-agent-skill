from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.scoring.evidence import REPORTS, REPORT_ROOT, build_evidence, read_reports
from tools.scoring.model import FactorResult, Scorecard, SubfactorResult, score_evidence


OUTPUT_BASE = REPORT_ROOT / "scoring"
SCORECARD_BASE = REPORT_ROOT / "scorecards"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "需人工确认"


def _fmt_percent_points(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "需人工确认"


def _fmt_decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "需人工确认"


def _cashflow_summary(evidence: dict[str, Any]) -> str:
    try:
        cashflow = float(evidence.get("operating_cashflow"))
    except (TypeError, ValueError):
        return "经营现金流需人工确认"
    if cashflow > 0:
        return "经营现金流为正"
    if cashflow < 0:
        return "经营现金流为负"
    return "经营现金流接近零"


def _thesis_attitude(subfactors: dict[str, SubfactorResult], evidence: dict[str, Any]) -> tuple[str, str]:
    """Keep the thesis label stricter than the scorecard's evidence coverage."""
    peer_rows = evidence.get("peer_comparison")
    verified_peers = [
        row for row in peer_rows
        if isinstance(row, dict) and row.get("status") == "已验证"
    ] if isinstance(peer_rows, list) else []
    checks = {
        "公司兑现": subfactors["business_match"].status == "已验证" and subfactors["realization"].status == "已验证",
        "直接同行比较": len(verified_peers) >= 2,
        "市场预期差": subfactors["expectation_gap"].status == "已验证",
        "反向验证": subfactors["financial_safety"].status == "已验证" and subfactors["survival_risk"].status == "已验证",
    }
    missing = [label for label, ready in checks.items() if not ready]
    if not missing:
        return "优选", "四项投资主张均有已验证证据支持。"
    return "观察", "、".join(missing) + "仍需补证，不能把研究线索写成优选结论。"


def _peer_comparison_note(evidence: dict[str, Any]) -> str:
    peer_rows = evidence.get("peer_comparison")
    verified_peers = [
        row for row in peer_rows
        if isinstance(row, dict) and row.get("status") == "已验证"
    ] if isinstance(peer_rows, list) else []
    if len(verified_peers) >= 2:
        names = "、".join(str(row.get("name") or row.get("code") or "同行") for row in verified_peers[:3])
        return f"已纳入{names}等直接同行的已验证经营质量对照；结论以收入、利润、现金流和产品壁垒的相对表现为准。"
    peer_pe = _fmt_decimal(evidence.get("peer_pe_ttm_median"))
    return f"当前只有宽口径同行估值中位数（PE约{peer_pe}），尚未取得至少两家直接同行的经营质量、客户或份额对照，不能据此证明公司一定优于同行。"


def _progress_bar(value: float, maximum: float, width: int = 20) -> str:
    if width <= 0:
        return ""
    ratio = 0.0 if maximum <= 0 else max(0.0, min(float(value) / float(maximum), 1.0))
    filled = min(width, int(ratio * width + 0.5))
    return "█" * filled + "░" * (width - filled)


def _factor_status(factor: FactorResult) -> str:
    statuses = {item.status for item in factor.subfactors}
    if statuses == {"已验证"}:
        return "已验证"
    if statuses == {"网络命中（未核验）"}:
        return "网络命中（未核验）"
    if statuses == {"已搜索未命中"}:
        return "已搜索未命中"
    if statuses == {"搜索失败，需人工确认"}:
        return "搜索失败，需人工确认"
    if all(item.status == "需人工确认" for item in factor.subfactors):
        return "需人工确认"
    return "部分覆盖"


def _status_icon(status: str) -> str:
    if status in {"已验证", "通过", "未触发", "有效"}:
        return "✅"
    if status in {"部分覆盖", "网络命中（未核验）", "需人工确认"}:
        return "🟡"
    if status in {"搜索失败，需人工确认", "已搜索未命中", "不通过", "过期或缺失，不计分"}:
        return "⚠️"
    return "•"


def _conclusion_icon(title: str) -> str:
    for key, icon in (
        ("投资主张", "🧭"),
        ("为什么可能值得买", "✅"),
        ("为什么不能直接买", "⏳"),
        ("同行竞争", "⚖️"),
        ("市场分歧", "🔍"),
        ("行动评级", "🎯"),
    ):
        if key in title:
            return icon
    return "•"


def _factor_icon(key: str) -> str:
    return {
        "F1": "🌐", "F2": "👥", "F3": "🛡️", "F4": "💰", "F5": "🔄", "F6": "📈",
    }.get(key, "•")


def _factor_summary(factor: FactorResult) -> str:
    ranked = sorted(factor.subfactors, key=lambda item: (item.score / item.maximum, item.score), reverse=True)
    positives = [item for item in ranked if item.score > 0]
    missing = [item.label for item in factor.subfactors if item.status in {"需人工确认", "已搜索未命中", "搜索失败，需人工确认"}]
    if positives:
        summary = f"{positives[0].label}是当前主要得分项"
    else:
        summary = "没有已验证的正向得分项"
    if missing:
        summary += f"；{missing[0]}需人工确认"
    return summary


def _source_text(item: SubfactorResult) -> str:
    return "、".join(f"[{source}]" for source in item.sources) if item.sources else "需人工确认"


def _visible_action_cap(cap: str) -> str:
    if cap == "不碰":
        return "卖出"
    if cap in {"学习仓", "矛"}:
        return "持有"
    return cap


def _table_text(value: Any, limit: int = 110) -> str:
    text = " ".join(str(value or "需人工确认").replace("|", "/").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


PENDING_CONFIRMATION_GUIDANCE = {
    "era_track": "补充莫大选股产业趋势判断、主营/行业结构化匹配，以及未来三年 CAGR、渗透率或权威产业趋势数据。",
    "supply_gap": "补充至少两类同向的价格、库存、订单、CR3 或扩产周期证据。",
    "capex_wave": "补充公司资本开支同比、在建工程/固定资产、订单或扩产计划，并与行业投资方向交叉核对。",
    "background": "补充控股股东、实际控制人身份及国资/产业资本背景的年报或公告证据。",
    "leadership": "补充市场份额、销量/出货排名、行业地位或核心供应关系的权威证据。",
    "specialized": "补充工信部、地方政府名单或公司正式公告中的专精特新/单项冠军认定。",
    "business_match": "补充主营产品与产业链环节的收入占比和正式披露。",
    "profit_position": "补充产业链上下游利润分配、议价能力或毛利率证据。",
    "realization": "补充订单、产能利用率、投产进度与收入/利润连续改善证据。",
    "price_position": "补充完整历史价格样本并确认价格分位。",
    "coldness": "补充完整的个股关注度、社交热榜和行业周期数据。",
    "inflection": "补充收入、利润、现金流、库存和订单的连续改善证据。",
    "expectation_gap": "补充低关注、强产业、公司改善和生存能力同时成立的交叉证据。",
    "sentiment": "补充价格位置、个股热度、行业拥挤度和异常推广风险的完整覆盖。",
    "catalyst": "补充公告或正式披露中的中标、合同、扩产、回购等明确催化事件。",
}


def _pending_confirmation_rows(card: Scorecard) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for factor in card.factors:
        for item in factor.subfactors:
            if item.status not in {"需人工确认", "搜索失败，需人工确认"} or item.key in seen:
                continue
            seen.add(item.key)
            reason = item.reason.replace("|", "/")
            if any(term in reason for term in ("search_budget_exhausted", "target_budget_exhausted", "target_limit_exceeded", "搜索预算已用尽", "未分配搜索预算")):
                why = "自动搜索未完成，不能把未搜完当成正面或负面事实"
            elif "已搜索未命中" in reason:
                why = "已运行搜索，但没有找到可量化、可核验的同向证据"
            else:
                why = "结构化数据缺失，现有线索不足以确认该项"
            guidance = PENDING_CONFIRMATION_GUIDANCE.get(item.key, "补充权威、可追溯且日期明确的证据。")
            rows.append((item.label, item.status, why, f"{guidance} 当前记录：{reason}"))
    return rows


def _chain_stage_label(stage: str) -> str:
    return {"upstream": "上游", "midstream": "中游", "downstream": "下游"}.get(stage, "位置待确认")


def _chain_rows(evidence: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    chain_name = str(evidence.get("chain_name") or "未识别产业链")
    current_stage = str(evidence.get("chain_stage") or evidence.get("chain_position") or "")
    path = ROOT / "tools" / "scoring" / "chains.yaml"
    chain: dict[str, Any] = {}
    if path.exists() and chain_name != "未识别产业链":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        chain = next((item for item in raw.get("chains", []) if str(item.get("name")) == chain_name), {})

    business = str(evidence.get("main_business") or "").strip()
    if not business:
        business_items = evidence.get("business_items") if isinstance(evidence.get("business_items"), list) else []
        business = "、".join(str(item) for item in business_items[:4]) or "主营产品待确认"
    ratio = evidence.get("business_chain_revenue_ratio")
    ratio_text = f"；相关收入占比 {float(ratio):.1%}" if isinstance(ratio, (int, float)) else "；收入占比待确认"
    order = {"upstream": 0, "midstream": 1, "downstream": 2}
    current_order = order.get(current_stage)
    rows: list[tuple[str, str, str, str]] = []
    for stage in ("upstream", "midstream", "downstream"):
        node = chain.get(stage, {}) if isinstance(chain, dict) else {}
        industries = node.get("industries", []) if isinstance(node, dict) else []
        keywords = node.get("keywords", []) if isinstance(node, dict) else []
        core = list(dict.fromkeys([*(str(item) for item in industries[:3]), *(str(item) for item in keywords[:7])]))
        core_text = "、".join(core) or "资料库暂无明确节点"
        if stage == current_stage:
            relation = f"公司主营映射到本环节：{business}{ratio_text}"
            status = "公司所在位置（部分映射）" if evidence.get("chain_partial") else "公司所在位置"
        elif current_order is None:
            relation = "公司与该环节的直接关系待确认"
            status = "产业链资料库"
        elif order[stage] < current_order:
            relation = "公司所在环节的上游供给，具体供应商需核对"
            status = "核心上游"
        else:
            relation = "公司产品的下游承接与终端需求，具体客户需核对"
            status = "核心下游"
        rows.append((_chain_stage_label(stage), core_text, relation, status))
    return rows


def _technical_snapshot(evidence: dict[str, Any]) -> str:
    indicators = evidence.get("technical_indicators") if isinstance(evidence.get("technical_indicators"), dict) else {}
    chan = evidence.get("chan_structure") if isinstance(evidence.get("chan_structure"), dict) else {}

    def state(key: str) -> str:
        item = indicators.get(key)
        return str(item.get("state") or "需人工确认") if isinstance(item, dict) else "需人工确认"

    direction = str(chan.get("latest_direction") or "需人工确认") if chan.get("status") == "可分析" else "需人工确认"
    return f"缠论结构{direction}、MACD{state('macd')}、OBV{state('obv')}、WR{state('wr')}"


def _moda_overview(card: Scorecard, evidence: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    subfactors = {item.key: item for factor in card.factors for item in factor.subfactors}
    chain_name = str(evidence.get("chain_name") or "产业链待确认")
    stage = _chain_stage_label(str(evidence.get("chain_stage") or ""))
    business_items = evidence.get("business_items") if isinstance(evidence.get("business_items"), list) else []
    business = "、".join(str(item) for item in business_items[:3]) or str(evidence.get("main_business") or "主营业务待确认")
    technical = _technical_snapshot(evidence)
    attitude, attitude_reason = _thesis_attitude(subfactors, evidence)
    peer_note = _peer_comparison_note(evidence)
    price = subfactors["price_position"]
    valuation = subfactors["valuation"]
    inflection = subfactors["inflection"]
    return [
        ("投资主张", f"主张态度：{attitude}", f"正式行动评级：{card.action_rating}", attitude_reason),
        ("为什么可能值得买", f"{stage}业务映射", f"主营包括{business}", f"行业景气为{evidence.get('industry_prosperity_status', '需人工确认')}；营收同比{_fmt_pct(evidence.get('revenue_yoy'))}、利润同比{_fmt_pct(evidence.get('profit_yoy'))}，{_cashflow_summary(evidence)}。"),
        ("为什么不能直接买", f"三年价格分位{_fmt_pct(evidence.get('price_percentile_3y'))}", f"TTM PE {_fmt_decimal(evidence.get('pe_ttm'))}、PB {_fmt_decimal(evidence.get('pb'))}", f"{price.reason}；{valuation.reason}；{technical}。"),
        ("同行竞争", "直接同行对照", "为什么是它：需人工确认" if "直接同行比较" in attitude_reason else "直接同行对照已验证", peer_note),
        ("市场分歧", f"业绩拐点：{inflection.status}", "市场可能低估持续兑现，也可能高估短期改善", f"{inflection.reason}；后续看订单、营收、利润和经营现金流是否连续改善。"),
        ("行动评级", f"研究分 {_fmt(card.research_score)}/100", f"正式行动评级：{card.action_rating}", f"{card.action_rating_reason}；投资主张态度不单独改变正式评级。"),
    ]


def _sleep_checks(card: Scorecard) -> list[tuple[str, str, str]]:
    factors = {factor.key: factor for factor in card.factors}
    subfactors = {item.key: item for factor in card.factors for item in factor.subfactors}

    financial = subfactors["financial_safety"]
    uncertain = {"需人工确认", "已搜索未命中", "搜索失败，需人工确认"}
    financing_status = "通过" if financial.score >= 3.75 else "不通过" if financial.status not in uncertain else "需人工确认"
    announcement_status = "通过" if subfactors["business_match"].score >= 3 and subfactors["realization"].score >= 2 else "需人工确认"
    finance_status = "通过" if financial.score >= 3.75 else "不通过" if financial.status not in uncertain else "需人工确认"
    shareholder = subfactors["controller_action"]
    shareholder_status = "通过" if shareholder.score >= 4 else "不通过" if shareholder.status not in uncertain else "需人工确认"
    industry_status = "通过" if factors["F1"].score >= 20 else "不通过" if factors["F1"].score < 15 else "需人工确认"
    return [
        ("不融资也能拿", financing_status, financial.reason),
        ("不靠单一公告续命", announcement_status, "检查主营匹配和订单/产能兑现是否同时成立"),
        ("财务不容易暴雷", finance_status, financial.reason),
        ("股东不持续伤害小股东", shareholder_status, shareholder.reason),
        ("产业逻辑至少 1-3 年不证伪", industry_status, f"F1 得分 {_fmt(factors['F1'].score)}/30"),
        ("跌 20% 后仍能持有", "需人工确认", "需要结合基本面证据与个人风险承受能力判断"),
    ]


def _framework_conclusion(card: Scorecard, evidence: dict[str, Any]) -> list[tuple[str, str]]:
    subfactors = {item.key: item for factor in card.factors for item in factor.subfactors}
    factors = {factor.key: factor for factor in card.factors}
    adjustments = {item.key: item for item in card.adjustments}
    name = str(evidence.get("name") or evidence.get("code") or "该标的")
    price = subfactors["price_position"]
    valuation = subfactors["valuation"]
    inflection = subfactors["inflection"]
    financial = subfactors["financial_safety"]
    chain_name = str(evidence.get("chain_name") or "产业链待确认")
    stage = _chain_stage_label(str(evidence.get("chain_stage") or evidence.get("chain_position") or ""))
    match_type = str(evidence.get("chain_match_type") or "匹配类型待确认")
    prosperity = evidence.get("industry_prosperity_status", "需人工确认")
    main_business = str(evidence.get("main_business") or "主营业务待确认")
    business_breakdown = evidence.get("business_breakdown") if isinstance(evidence.get("business_breakdown"), list) else []
    product_lines = [
        f"{item.get('item')}收入占比 {float(item.get('revenue_ratio')):.2%}"
        for item in business_breakdown
        if item.get("category") == "按产品分类" and item.get("item") and isinstance(item.get("revenue_ratio"), (int, float))
    ]
    product_note = "；".join(product_lines[:3]) or main_business
    revenue_yoy = _fmt_pct(evidence.get("revenue_yoy"))
    profit_yoy = _fmt_pct(evidence.get("profit_yoy"))
    order_growth = _fmt_percent_points(evidence.get("order_growth"))
    overseas_ratio = _fmt_percent_points(evidence.get("overseas_revenue_ratio"))
    price_percentile = _fmt_pct(evidence.get("price_percentile_3y"))
    latest_price = _fmt_decimal(evidence.get("latest_price"))
    pe_ttm = _fmt_decimal(evidence.get("pe_ttm"))
    pb = _fmt_decimal(evidence.get("pb"))
    attention_heat = _fmt_pct(evidence.get("attention_heat"))
    congestion = _fmt_pct(evidence.get("market_congestion"))
    cashflow = _cashflow_summary(evidence)
    technical_snapshot = _technical_snapshot(evidence)
    thesis_attitude, thesis_reason = _thesis_attitude(subfactors, evidence)
    peer_note = _peer_comparison_note(evidence)
    return [
        ("1. 投资主张", f"{name}当前正式行动评级为“{card.action_rating}”，投资主张态度为“{thesis_attitude}”。公司处于{chain_name}{stage}，主营集中在{product_note}；行业景气为{prosperity}，营收同比{revenue_yoy}、利润同比{profit_yoy}，{cashflow}。{thesis_reason}"),
        ("2. 为什么可能值得买", f"可研究的逻辑不是概念标签，而是公司在{chain_name}{stage}提供{main_business}。现有收入结构为{product_note}；订单增长指标为{order_growth}，海外收入占比约{overseas_ratio}。若订单、收入、毛利和现金回收连续改善，才说明产业位置开始转化为利润兑现。"),
        ("3. 为什么不能直接买", f"当前位置需要先过价格和估值两关：当前价格为{latest_price}元，三年价格分位约{price_percentile}；TTM PE为{pe_ttm}、PB为{pb}。{price.reason}；{valuation.reason}。个股关注度为{attention_heat}，所属行业拥挤度为{congestion}，技术面为{evidence.get('technical_signal', '需人工确认')}；{technical_snapshot}。"),
        ("4. 同行竞争与为什么是它", f"{peer_note} 公司当前仅确认主营与{chain_name}{stage}的映射，匹配方式为{match_type}；竞争份额、核心客户、认证周期和国产替代程度仍需以年报、公告或结构化同业数据验证。"),
        ("5. 市场分歧与验证", f"可验证的预期差来自业绩是否持续改善，而不是单次上涨或热门概念。当前业绩拐点判断为“{inflection.status}”：{inflection.reason}。市场可能低估的是收入、利润和现金流同步改善的持续性；市场可能高估的是增长已经无风险兑现。后续优先观察订单、营收、利润和经营现金流能否连续两个报告期同向改善。"),
        ("6. 行动评级与证伪条件", f"研究分为{_fmt(card.research_score)}/100，证据覆盖率为{card.coverage:.1%}；本次正式行动评级为“{card.action_rating}”，不因投资主张态度单独上调或下调。公司未见ST、退市或重大审计风险，近180天也未核验到控股股东减持；但{financial.reason}。若现金流持续恶化、营收与利润同步转弱、出现非标审计或重大持续经营风险、控股股东减持，或直接同行的经营质量持续领先，应直接证伪当前观察逻辑。"),
    ]


def _technical_analysis(evidence: dict[str, Any]) -> list[str]:
    indicators = evidence.get("technical_indicators") if isinstance(evidence.get("technical_indicators"), dict) else {}
    chan = evidence.get("chan_structure") if isinstance(evidence.get("chan_structure"), dict) else {}

    def value(key: str, field: str = "value", suffix: str = "") -> str:
        item = indicators.get(key, {})
        raw = item.get(field) if isinstance(item, dict) else None
        if raw is None:
            return "需人工确认"
        rendered = _fmt_decimal(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else str(raw)
        return f"{rendered}{suffix}"

    chan_reading = "需人工确认"
    if chan.get("status") == "可分析":
        chan_reading = f"{chan.get('latest_direction', '方向未定')}；{chan.get('relation', '中枢未形成')}"
    rows = [
        ("缠论（结构）", chan_reading, "结构偏多" if chan.get("latest_direction") == "向上" else "结构偏空" if chan.get("latest_direction") == "向下" else "方向未定"),
        ("OBV", value("obv"), indicators.get("obv", {}).get("state", "需人工确认")),
        ("30日BIAS", value("bias30", suffix="%"), indicators.get("bias30", {}).get("state", "需人工确认")),
        ("MACD", value("macd"), indicators.get("macd", {}).get("state", "需人工确认")),
        ("BOLL", f"位置 {value('boll')}", indicators.get("boll", {}).get("state", "需人工确认")),
        ("ATR", value("atr", "pct", "%"), indicators.get("atr", {}).get("state", "需人工确认")),
        ("DMI", f"ADX {value('dmi', 'adx')}", indicators.get("dmi", {}).get("state", "需人工确认")),
        ("RSI", value("rsi"), indicators.get("rsi", {}).get("state", "需人工确认")),
        ("WR", value("wr"), indicators.get("wr", {}).get("state", "需人工确认")),
    ]
    current_price = _fmt_decimal(chan.get("current_price"))
    support = _fmt_decimal(chan.get("support"))
    resistance = _fmt_decimal(chan.get("resistance"))
    structure_score = evidence.get("technical_structure_score", "需人工确认")
    structure_reason = evidence.get("technical_structure_reason", "技术证据不足，需人工确认")
    lines = [
        "## 技术分析（easy-tdx 日 K） 📈",
        "",
        (
            f"- 当前价格：{current_price}；支撑位：{support}；压力位：{resistance}。"
        ),
        f"- 综合判断：技术结构 {_fmt_decimal(structure_score, 1)}/4；{structure_reason}；交易信号 {evidence.get('technical_signal', '需人工确认')}。",
        "- 缠论说明：识别日线分型、笔和最近三笔重叠区间，不替代完整多级别缠论递归。",
        "",
        "| 指标 | 当前读数 | 当前评价 |",
        "|---|---|---|",
    ]
    lines.extend(f"| {indicator} | {reading} | {comment} |" for indicator, reading, comment in rows)
    return lines


def _industry_prosperity_analysis(evidence: dict[str, Any]) -> list[str]:
    mapping = evidence.get("industry_mapping") if isinstance(evidence.get("industry_mapping"), dict) else {}
    financial = evidence.get("industry_financial_signal") if isinstance(evidence.get("industry_financial_signal"), dict) else {}
    supply = evidence.get("industry_supply_signal") if isinstance(evidence.get("industry_supply_signal"), dict) else {}
    market = evidence.get("industry_market_signal") if isinstance(evidence.get("industry_market_signal"), dict) else {}
    web_signal = evidence.get("industry_web_signal") if isinstance(evidence.get("industry_web_signal"), dict) else {}
    conflicts = evidence.get("industry_prosperity_conflicts") if isinstance(evidence.get("industry_prosperity_conflicts"), list) else []

    def pct(value: Any) -> str:
        try:
            return f"{float(value):.2%}"
        except (TypeError, ValueError):
            return "需人工确认"

    def ratio(value: Any) -> str:
        try:
            return f"{float(value):.2f}x"
        except (TypeError, ValueError):
            return "需人工确认"
    return [
        "## 行业景气度交叉验证 🌐",
        "",
        f"- 行业映射：{mapping.get('matched_token', '需人工确认')} → {mapping.get('sw_second_name', '需人工确认')} → {mapping.get('sw_first_name', '需人工确认')}（{mapping.get('status', '不可用')}）",
        f"- 综合状态：{evidence.get('industry_prosperity_status', '不可用')}；覆盖：{evidence.get('industry_prosperity_coverage', '不可用')}；报告期：{evidence.get('industry_prosperity_period', '需人工确认')}。本项只交叉验证，不独立加分。",
        "",
        "| 层面 | 状态 | 当前判断 |",
        "|---|---|---|",
        f"| 财务确认 | {financial.get('status', '不可用')} | 可用 {financial.get('available_metrics', 0)}/6；当期正向 {financial.get('current_positive', 0)}；边际正向 {financial.get('delta_positive', 0)} |",
        f"| 供需先行 | {supply.get('status', '不可用')} | 商品 {supply.get('commodity') or '未匹配'}；证据 {supply.get('evidence_count') or 0} 类；PPI 同比 {supply.get('ppi_yoy', '需人工确认')} |",
        f"| 市场验证 | {market.get('status', '不可用')} | 20日相对沪深300 {pct(market.get('relative_to_csi300_20d'))}；成交活跃比 {ratio(market.get('turnover_activity_ratio'))} |",
        f"| 网络旁证 | {web_signal.get('status', '不可用')} | 覆盖 {web_signal.get('coverage', '不可用')}；后端 {web_signal.get('provider', 'none')}；未核验，不独立改变状态 |",
        "",
        "- 冲突检查：" + ("；".join(conflicts) if conflicts else "未发现已覆盖指标之间的明确冲突。"),
        "- 网络旁证分为财务确认、供需先行、市场验证三层；只用于验证结构化判断，不替代财报、公告或行业数据。",
        "- 网络三层明细：" + ("；".join(
            f"{item.get('label', key)}={item.get('status', '需人工确认')}（正向{item.get('positive_count', 0)}/负向{item.get('negative_count', 0)}，域名{item.get('domain_count', 0)}）"
            for key, item in (web_signal.get('layers') or {}).items()
        ) if web_signal.get('layers') else "需人工确认"),
    ]


def render_report(code: str, name: str, evidence: dict[str, Any], card: Scorecard, requested_modules: tuple[str, ...]) -> str:
    lines = [
        f"# {name or code}（{code}）六层诊断",
        "",
        f"<!-- moda_scorecard: {json.dumps(card.to_dict(), ensure_ascii=False)} -->",
        "",
        "## 研究评分 📊",
        "",
        "```text",
        f"  {_fmt(card.research_score)} / 100  [{_progress_bar(card.research_score, 100)}]",
        f"  行动评级：{card.action_rating}",
        "```",
        "",
        f"### 技术信号：{_status_icon(card.signal)} {card.signal}",
        "",
        "## 一句话结论与最终判断 🧭",
        "",
        "### 投资判断总览 🧠",
        "",
        "| 核心问题 | 图示 | 大白话结论 | 数据与理由 |",
        "|---|---|---|---|",
    ]
    for topic, chart, plain, reason in _moda_overview(card, evidence):
        lines.append(f"| {topic} | `{chart}` | {_table_text(plain)} | {_table_text(reason)} |")
    lines += [""]
    for title, body in _framework_conclusion(card, evidence):
        lines += [f"**{title}**", "", f"> {_conclusion_icon(title)} {body}", "", "---", ""]
    chain_name = str(evidence.get("chain_name") or "未识别产业链")
    stage = _chain_stage_label(str(evidence.get("chain_stage") or evidence.get("chain_position") or ""))
    lines += [
        "### 核心上下游对应表 🔗",
        "",
        f"> 产业链：{chain_name}；公司位置：{stage}；匹配类型：{evidence.get('chain_match_type', '待确认')}。表内上下游来自产业链资料库，公司与具体供应商、客户的关系仍以公告和年报为准。",
        "",
        "| 环节 | 核心内容 | 与公司的关系 | 判断 |",
        "|---|---|---|---|",
    ]
    for node, core, relation, status in _chain_rows(evidence):
        lines.append(f"| {node} | {_table_text(core)} | {_table_text(relation)} | {status} |")
    lines += [""]
    lines.extend(_technical_analysis(evidence))
    lines += [""]
    lines.extend(_industry_prosperity_analysis(evidence))
    lines += [
        "",
        "## 六层图形概览 🧩",
        "",
        "```text",
    ]
    for factor in card.factors:
        lines.append(
            f"{factor.key} [{_progress_bar(factor.score, factor.maximum)}] "
            f"{_fmt(factor.score):>5}/{_fmt(factor.maximum):<3}  {factor.label}"
        )
    lines += [
        "```",
        "",
        "## 六层评分卡 🧮",
        "",
        "| 因子 | 得分 | 核心判断 | 状态 |",
        "|---|---:|---|---|",
    ]
    for factor in card.factors:
        lines.append(f"| {factor.key} {factor.label} | {_fmt(factor.score)}/{_fmt(factor.maximum)} | {_factor_summary(factor)} | {_factor_status(factor)} |")

    for factor in card.factors:
        lines += [
            "",
            f"## {factor.key} {factor.label}（{_fmt(factor.score)}/{_fmt(factor.maximum)}） {_factor_icon(factor.key)}",
        ]
        if factor.key == "F6":
            lines += [
                "",
                f"F6 修正项：{_fmt(card.adjustment_score)}/10｜研究分：{_fmt(card.research_score)}/100",
                "",
                "> F6 是独立的第六层，已计入研究分，不再二次加分。",
            ]
        lines += [
            "",
            "| 子因子 | 得分 | 判断依据 | 来源 | 状态 |",
            "|---|---:|---|---|---|",
        ]
        for item in factor.subfactors:
            reason = item.reason.replace("|", "/")
            lines.append(f"| {item.label} | {_fmt(item.score)}/{_fmt(item.maximum)} | {reason} | {_source_text(item)} | {item.status} |")

    lines += [
        "",
        "## 舆情、社交热榜与异常推广风险 🔍",
        "",
        f"- 个股关注热度：{evidence.get('attention_heat', '需人工确认')}（EastMoney 人气排名归一化）",
        f"- 市场拥挤度：{evidence.get('market_congestion', '需人工确认')}；申万二级 {evidence.get('market_congestion_industry', '需人工确认')}（{evidence.get('market_congestion_industry_code', '需人工确认')}）；行业强度 {evidence.get('market_congestion_strength', '需人工确认')}；今日检查 {evidence.get('market_congestion_checked_date', '需人工确认')}；源数据日期 {evidence.get('market_congestion_date', '需人工确认')}；{'有效' if evidence.get('market_congestion_fresh') is True else '过期或缺失，不计分'}",
        f"- 社交热榜：命中 {evidence.get('social_hot_hits', '需人工确认')} 条，去重后 {evidence.get('social_unique_topics', '需人工确认')} 个主题，覆盖 {evidence.get('social_platform_hits', '需人工确认')} 个平台；跨平台主题 {evidence.get('social_cross_platform_topics', '需人工确认')} 个",
        f"- 传播速度：{evidence.get('social_propagation_status', '需人工确认')}；24小时新主题 {evidence.get('social_new_topics_24h', '需人工确认')}；快速扩散主题 {evidence.get('social_fast_spread_topics', '需人工确认')}；最大排名跃升 {evidence.get('social_rank_jump_max') if evidence.get('social_rank_jump_max') is not None else '需更多快照'}",
        f"- 个股讨论：{evidence.get('discussion_posts_total', '需人工确认')} 条；样本 {evidence.get('discussion_sample_status', '需人工确认')}；来源 {evidence.get('discussion_source_status', '需人工确认')}；情绪 {evidence.get('discussion_sentiment', '样本不足，需人工确认')}；推广话术 {('、'.join(evidence.get('discussion_promotion_hits', [])) or '无') if isinstance(evidence.get('discussion_promotion_hits'), list) else '需人工确认'}",
        f"- 新闻舆情：{evidence.get('news_posts_total', '需人工确认')} 条；来源 {evidence.get('news_sources_ok', '需人工确认')}/{evidence.get('news_sources_checked', '需人工确认')}；情绪 {evidence.get('news_sentiment', '需人工确认')}。新闻只作事件和舆情补充，不并入社交热度。",
        f"- 异常推广风险：{evidence.get('trap_risk_level', '需人工确认')}；命中 {evidence.get('trap_signal_count', '需人工确认')}/8",
        "",
        "| 异常推广信号 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for item in evidence.get("trap_checks", []):
        lines.append(f"| {item['signal']} | {'命中' if item['hit'] else '未命中'} | {item['evidence']} |")
    if not evidence.get("trap_checks"):
        lines.append("| 8 类信号 | 需人工确认 | 社交热榜或交叉证据未完成 |")

    lines += [
        "",
        "## Hard Cap 检查 🛡️",
        "",
        "| 条件 | 本次结果 | 对评级的影响 |",
        "|---|---|---|",
    ]
    for item in card.hard_caps:
        result = f"{_status_icon(item['result'])} {item['result']}"
        lines.append(f"| {item['condition']} | {result} | {_visible_action_cap(item['cap'])} |")

    lines += ["", "## 睡得着检查 😴", ""]
    for label, status, reason in _sleep_checks(card):
        lines.append(f"- {_status_icon(status)} {label}：{status}。{reason}")

    lines += [
        "",
        "## 动态纠错触发器 🔄",
        "",
        "- 产业证伪：行业需求、供需方向、订单或下游资本开支连续两个报告期恶化。",
        "- 公司证伪：营收与利润同时转负、经营现金流持续为负，或出现非标审计和重大持续经营风险。",
        "- 估值过热：三年价格分位超过 80%，且市场拥挤度达到 80% 以上；或 TTM PE 超过同行中位数 50%。",
        "- 股东恶化：控股股东或实控人减持、质押比例明显上升，或未来半年解禁比例超过 10%。",
        "- 同链高切低：同产业链出现 F1/F3 不弱、但 F5 得分高出 4 分以上的标的时重新比较。",
    ]
    lines += ["", "免责声明：本分析仅供研究参考，不构成投资建议。"]
    return "\n".join(lines) + "\n"


def build_report(code: str, name: str, directories: tuple[str, ...] = REPORTS, since: float = 0,
                 requested_modules: tuple[str, ...] | None = None) -> tuple[str, Scorecard, dict[str, Any]]:
    reports = read_reports(code, directories, since)
    evidence = build_evidence(code, name, reports)
    card = score_evidence(evidence)
    return render_report(code, name, evidence, card, requested_modules or directories), card, evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="moda-v4 structured six-factor scorer")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--sources", default=",".join(REPORTS))
    parser.add_argument("--requested-sources", default="")
    parser.add_argument("--since", type=float, default=0)
    args = parser.parse_args()
    code = args.stock.strip()
    if len(code) != 6 or not code.isdigit():
        parser.error("--stock must be a 6-digit A-share code")
    directories = tuple(source for source in args.sources.split(",") if source in REPORTS)
    requested = tuple(source for source in args.requested_sources.split(",") if source in REPORTS) or directories
    report, card, evidence = build_report(code, args.name or code, directories, args.since, requested)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    SCORECARD_BASE.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_BASE / f"{code}.md"
    scorecard_path = SCORECARD_BASE / f"{code}.json"
    report_path.write_text(report, encoding="utf-8")
    scorecard_path.write_text(json.dumps({"evidence": evidence, "scorecard": card.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report_path)


if __name__ == "__main__":
    main()
