from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.scoring.evidence import REPORTS, REPORT_ROOT, build_evidence, read_reports
from tools.scoring.model import FactorResult, Scorecard, SubfactorResult, score_evidence
from tools.scoring.thesis import (
    ThesisOutput,
    build_thesis_context,
    render_thesis_output,
    validate_thesis_output,
)


OUTPUT_BASE = REPORT_ROOT / "scoring"
SCORECARD_BASE = REPORT_ROOT / "scorecards"
JUDGMENT_BASE = REPORT_ROOT / "judgments"


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _fmt_decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "无"


def _progress_bar(value: float, maximum: float, width: int = 20) -> str:
    if width <= 0:
        return ""
    ratio = 0.0 if maximum <= 0 else max(0.0, min(float(value) / float(maximum), 1.0))
    filled = min(width, int(ratio * width + 0.5))
    return "█" * filled + "░" * (width - filled)


PUBLIC_ERROR_MARKERS = (
    "360搜索未返回相关结果",
    "搜索失败，需人工确认",
    "search_budget_exhausted",
    "target_budget_exhausted",
    "target_limit_exceeded",
    "global_budget_exhausted",
    "model_search:not_configured",
    "duckduckgo:",
    "connectionerror",
)


def _public_text(value: Any, empty: str = "无") -> str:
    """Remove collector diagnostics from visible prose while keeping JSON audit data intact."""
    text = " ".join(str(value or "").replace("|", "/").split())
    if not text:
        return empty
    clauses: list[str] = []
    for clause in re.split(r"[；;]", text):
        cleaned = clause.strip(" ，。")
        lowered = cleaned.lower()
        if not cleaned or any(marker in lowered for marker in PUBLIC_ERROR_MARKERS):
            continue
        cleaned = cleaned.replace("网络旁证 不可用（不可用，未核验）", "网络旁证：无")
        cleaned = cleaned.replace("网络旁证不可用（不可用，未核验）", "网络旁证：无")
        cleaned = cleaned.replace("搜索结果待正文核验，需人工确认", "网络候选待核验")
        cleaned = cleaned.replace("已搜索未命中", "可用数据：无")
        clauses.append(cleaned)
    return "；".join(clauses) or empty


def _public_status(value: Any) -> str:
    status = str(value or "需人工确认")
    lowered = status.lower()
    if status in {"需人工确认", "已搜索未命中", "不可用", "未执行", "过期或缺失，不计分"} or any(marker in lowered for marker in PUBLIC_ERROR_MARKERS):
        return "无"
    if status in {"网络命中（未核验）", "搜索结果待正文核验，需人工确认"}:
        return "待核验"
    return status


def _sanitize_visible_report(text: str) -> str:
    """Clean reader-facing Markdown without changing hidden audit comments."""
    chunks = re.split(r"(<!--.*?-->)", text, flags=re.DOTALL)
    for index in range(0, len(chunks), 2):
        segment = chunks[index]
        segment = segment.replace("搜索失败，需人工确认", "无")
        segment = segment.replace("搜索结果待正文核验，需人工确认", "待核验")
        segment = segment.replace("360搜索未返回相关结果", "无")
        segment = segment.replace("需人工确认", "无")
        segment = re.sub(r"duckduckgo\s*:\s*connectionerror", "无", segment, flags=re.IGNORECASE)
        for marker in (
            "search_budget_exhausted", "target_budget_exhausted",
            "target_limit_exceeded", "global_budget_exhausted",
            "model_search:not_configured",
        ):
            segment = re.sub(re.escape(marker), "无", segment, flags=re.IGNORECASE)
        chunks[index] = segment
    return "".join(chunks)


def _score_overview(card: Scorecard) -> list[str]:
    lines = [
        "## 研究概览",
        "",
        f"<!-- moda_scorecard: {json.dumps(card.to_dict(), ensure_ascii=False)} -->",
        "",
        "### 研究评分",
        "",
        "```text",
        f"  {_fmt(card.research_score)} / 100  [{_progress_bar(card.research_score, 100)}]",
        f"  证据覆盖率：{card.coverage:.1%}",
        "```",
        "",
        f"### 技术信号：{_status_icon(card.signal)} {card.signal}",
        "",
        "### 六层图形概览 🧩",
        "",
        "```text",
    ]
    for factor in card.factors:
        lines.append(
            f"{factor.key} [{_progress_bar(factor.score, factor.maximum)}] "
            f"{_fmt(factor.score):>5}/{_fmt(factor.maximum):<3}  {factor.label}"
        )
    lines += ["```", ""]
    return lines


def _factor_status(factor: FactorResult) -> str:
    statuses = {item.status for item in factor.subfactors}
    if statuses == {"已验证"}:
        return "已验证"
    if statuses == {"网络命中（未核验）"}:
        return "网络命中（未核验）"
    if statuses == {"已搜索未命中"}:
        return "无"
    if statuses == {"搜索失败，需人工确认"}:
        return "无"
    if all(item.status == "需人工确认" for item in factor.subfactors):
        return "无"
    return "部分覆盖"


def _status_icon(status: str) -> str:
    if status in {"已验证", "通过", "未触发", "有效"}:
        return "✅"
    if status in {"部分覆盖", "网络命中（未核验）", "需人工确认"}:
        return "🟡"
    if status in {"搜索失败，需人工确认", "已搜索未命中", "不通过", "过期或缺失，不计分"}:
        return "⚠️"
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
        summary += f"；{missing[0]}可用数据：无"
    return summary


def _source_text(item: SubfactorResult) -> str:
    return "、".join(f"[{source}]" for source in item.sources) if item.sources else "无"


def _table_text(value: Any, limit: int = 110) -> str:
    text = _public_text(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _count(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number >= 0 and number.is_integer() else None


def _news_sentiment_summary(evidence: dict[str, Any]) -> str:
    realtime_count = _count(evidence.get("news_posts_total"))
    candidate_count = _count(evidence.get("news_search_count"))
    realtime = f"实时快讯匹配 {realtime_count} 条" if realtime_count is not None else "实时快讯：无"
    if candidate_count is None:
        candidates = "网络候选：无"
    elif candidate_count:
        candidates = "网络候选 " + str(candidate_count) + " 条（未核验，待正文核验，不计入情绪、评分、热度或异常推广判断）"
    else:
        candidates = "可用网络候选：无"
    sources_ok = evidence.get("news_sources_ok")
    sources_checked = evidence.get("news_sources_checked")
    sources = f"实时来源 {sources_ok}/{sources_checked}" if sources_ok is not None and sources_checked is not None else "实时来源：无"
    sentiment = evidence.get("news_sentiment") or "无"
    return f"{realtime}；{candidates}；{sources}；情绪 {_public_status(sentiment)}"


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
    return {"upstream": "上游", "midstream": "中游", "downstream": "下游"}.get(stage, "无")


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
        business = "、".join(str(item) for item in business_items[:4]) or "无"
    ratio = evidence.get("business_chain_revenue_ratio")
    ratio_text = f"；相关收入占比 {float(ratio):.1%}" if isinstance(ratio, (int, float)) else "；收入占比无"
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
            relation = "公司与该环节的直接关系：无"
            status = "产业链资料库"
        elif order[stage] < current_order:
            relation = "公司所在环节的上游供给，具体供应商需核对"
            status = "核心上游"
        else:
            relation = "公司产品的下游承接与终端需求，具体客户需核对"
            status = "核心下游"
        rows.append((_chain_stage_label(stage), core_text, relation, status))
    return rows


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


def _validated_thesis_output(
    card: Scorecard, evidence: dict[str, Any], thesis_output: ThesisOutput | dict[str, Any] | None,
) -> tuple[ThesisOutput | None, str]:
    if thesis_output is None:
        return None, "collector_only"
    context = build_thesis_context(card, evidence).to_dict()
    if isinstance(thesis_output, ThesisOutput):
        try:
            return validate_thesis_output(thesis_output.to_dict(), context), "agent_generated"
        except (TypeError, ValueError):
            return None, "expression_failed"
    try:
        return validate_thesis_output(thesis_output, context), "agent_generated"
    except (TypeError, ValueError):
        return None, "expression_failed"


def _technical_analysis(evidence: dict[str, Any]) -> list[str]:
    indicators = evidence.get("technical_indicators") if isinstance(evidence.get("technical_indicators"), dict) else {}
    chan = evidence.get("chan_structure") if isinstance(evidence.get("chan_structure"), dict) else {}

    def value(key: str, field: str = "value", suffix: str = "") -> str:
        item = indicators.get(key, {})
        raw = item.get(field) if isinstance(item, dict) else None
        if raw is None:
            return "无"
        rendered = _fmt_decimal(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else str(raw)
        return f"{rendered}{suffix}"

    chan_reading = "无"
    if chan.get("status") == "可分析":
        chan_reading = f"{chan.get('latest_direction', '方向未定')}；{chan.get('relation', '中枢未形成')}"
    rows = [
        ("缠论（结构）", chan_reading, "结构偏多" if chan.get("latest_direction") == "向上" else "结构偏空" if chan.get("latest_direction") == "向下" else "方向未定"),
        ("OBV", value("obv"), indicators.get("obv", {}).get("state", "无")),
        ("30日BIAS", value("bias30", suffix="%"), indicators.get("bias30", {}).get("state", "无")),
        ("MACD", value("macd"), indicators.get("macd", {}).get("state", "无")),
        ("BOLL", f"位置 {value('boll')}", indicators.get("boll", {}).get("state", "无")),
        ("ATR", value("atr", "pct", "%"), indicators.get("atr", {}).get("state", "无")),
        ("DMI", f"ADX {value('dmi', 'adx')}", indicators.get("dmi", {}).get("state", "无")),
        ("RSI", value("rsi"), indicators.get("rsi", {}).get("state", "无")),
        ("WR", value("wr"), indicators.get("wr", {}).get("state", "无")),
    ]
    current_price = _fmt_decimal(chan.get("current_price"))
    support = _fmt_decimal(chan.get("support"))
    resistance = _fmt_decimal(chan.get("resistance"))
    structure_score = evidence.get("technical_structure_score")
    structure_reason = _public_text(evidence.get("technical_structure_reason"), "技术证据：无")
    lines = [
        "## 技术分析（easy-tdx 日 K） 📈",
        "",
        (
            f"- 当前价格：{current_price}；支撑位：{support}；压力位：{resistance}。"
        ),
        f"- 综合判断：技术结构 {_fmt_decimal(structure_score, 1)}/4；{structure_reason}；交易信号 {_public_status(evidence.get('technical_signal'))}。",
        "- 缠论说明：识别日线分型、笔和最近三笔重叠区间，不替代完整多级别缠论递归。",
        "",
        "| 指标 | 当前读数 | 当前评价 |",
        "|---|---|---|",
    ]
    lines.extend(f"| {indicator} | {reading} | {_public_status(comment)} |" for indicator, reading, comment in rows)
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
            return "无"

    def ratio(value: Any) -> str:
        try:
            return f"{float(value):.2f}x"
        except (TypeError, ValueError):
            return "无"
    return [
        "## 行业景气度交叉验证 🌐",
        "",
        f"- 行业映射：{_public_text(mapping.get('matched_token'))} → {_public_text(mapping.get('sw_second_name'))} → {_public_text(mapping.get('sw_first_name'))}（{_public_status(mapping.get('status'))}）",
        f"- 综合状态：{_public_status(evidence.get('industry_prosperity_status'))}；覆盖：{_public_status(evidence.get('industry_prosperity_coverage'))}；报告期：{_public_text(evidence.get('industry_prosperity_period'))}。本项只交叉验证，不独立加分。",
        "",
        "| 层面 | 状态 | 当前判断 |",
        "|---|---|---|",
        f"| 财务确认 | {_public_status(financial.get('status'))} | 可用 {financial.get('available_metrics', 0)}/6；当期正向 {financial.get('current_positive', 0)}；边际正向 {financial.get('delta_positive', 0)} |",
        f"| 供需先行 | {_public_status(supply.get('status'))} | 商品 {_public_text(supply.get('commodity'))}；证据 {supply.get('evidence_count') or 0} 类；PPI 同比 {_public_text(supply.get('ppi_yoy'))} |",
        f"| 市场验证 | {_public_status(market.get('status'))} | 20日相对沪深300 {pct(market.get('relative_to_csi300_20d'))}；成交活跃比 {ratio(market.get('turnover_activity_ratio'))} |",
        f"| 网络旁证 | {_public_status(web_signal.get('status'))} | 可用覆盖 {_public_status(web_signal.get('coverage'))}；仅作交叉验证，不独立改变状态 |",
        "",
        "- 冲突检查：" + ("；".join(_public_text(item) for item in conflicts) if conflicts else "未发现已覆盖指标之间的明确冲突。"),
        "- 网络旁证分为财务确认、供需先行、市场验证三层；只用于验证结构化判断，不替代财报、公告或行业数据。",
        "- 网络三层明细：" + ("；".join(
            f"{item.get('label', key)}={_public_status(item.get('status', '需人工确认'))}（正向{item.get('positive_count', 0)}/负向{item.get('negative_count', 0)}，来源{item.get('domain_count', 0)}）"
            for key, item in (web_signal.get('layers') or {}).items()
        ) if web_signal.get('layers') else "无"),
    ]


def render_report(
    code: str,
    name: str,
    evidence: dict[str, Any],
    card: Scorecard,
    requested_modules: tuple[str, ...],
    thesis_output: ThesisOutput | dict[str, Any] | None = None,
) -> str:
    validated_thesis, expression_status = _validated_thesis_output(card, evidence, thesis_output)
    thesis_context = build_thesis_context(card, evidence).to_dict()
    lines = [
        f"# {name or code}（{code}）投资判断与六层诊断",
        "",
        f"<!-- moda_thesis: {json.dumps({'expression_status': expression_status}, ensure_ascii=False)} -->",
        "",
    ]
    if validated_thesis is not None:
        lines += render_thesis_output(validated_thesis, thesis_context)
    else:
        state_text = "判断层生成失败，请依据下方证据审计重新生成" if expression_status == "expression_failed" else "采集器仅提供事实包；莫大判断等待生成"
        lines += ["## 莫大判断", "", "### 判断层状态", "", f"> {state_text}。", ""]
    lines += [
        "## 证据与六层诊断",
        "",
    ]
    chain_name = str(evidence.get("chain_name") or "未识别产业链")
    stage = _chain_stage_label(str(evidence.get("chain_stage") or evidence.get("chain_position") or ""))
    lines += [
        "### 核心上下游对应表 🔗",
        "",
        f"> 产业链：{_public_text(chain_name)}；公司位置：{stage}；匹配类型：{_public_text(evidence.get('chain_match_type'))}。表内上下游来自产业链资料库，公司与具体供应商、客户的关系仍以公告和年报为准。",
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
    lines += [""]
    lines += _score_overview(card)
    lines += [
        "",
        "## 六层评分与量化审计",
        "",
        "### 六层评分卡 🧮",
        "",
        "| 因子 | 得分 | 核心判断 | 状态 |",
        "|---|---:|---|---|",
    ]
    for factor in card.factors:
        lines.append(f"| {factor.key} {factor.label} | {_fmt(factor.score)}/{_fmt(factor.maximum)} | {_factor_summary(factor)} | {_factor_status(factor)} |")

    for factor in card.factors:
        lines += [
            "",
            f"#### {factor.key} {factor.label}（{_fmt(factor.score)}/{_fmt(factor.maximum)}） {_factor_icon(factor.key)}",
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
            reason = _public_text(item.reason, "可用数据：无")
            lines.append(f"| {item.label} | {_fmt(item.score)}/{_fmt(item.maximum)} | {reason} | {_source_text(item)} | {_public_status(item.status)} |")

    lines += [
        "",
        "### 舆情、社交热榜与异常推广风险 🔍",
        "",
        f"- 个股关注热度：{_public_text(evidence.get('attention_heat'))}（EastMoney 人气排名归一化）",
        f"- 市场拥挤度：{_public_text(evidence.get('market_congestion'))}；申万二级 {_public_text(evidence.get('market_congestion_industry'))}（{_public_text(evidence.get('market_congestion_industry_code'))}）；行业强度 {_public_text(evidence.get('market_congestion_strength'))}；今日检查 {_public_text(evidence.get('market_congestion_checked_date'))}；源数据日期 {_public_text(evidence.get('market_congestion_date'))}；{'有效' if evidence.get('market_congestion_fresh') is True else '无（不计分）'}",
        f"- 社交热榜：命中 {_public_text(evidence.get('social_hot_hits'))} 条，去重后 {_public_text(evidence.get('social_unique_topics'))} 个主题，覆盖 {_public_text(evidence.get('social_platform_hits'))} 个平台；跨平台主题 {_public_text(evidence.get('social_cross_platform_topics'))} 个",
        f"- 传播速度：{_public_status(evidence.get('social_propagation_status'))}；24小时新主题 {_public_text(evidence.get('social_new_topics_24h'))}；快速扩散主题 {_public_text(evidence.get('social_fast_spread_topics'))}；最大排名跃升 {_public_text(evidence.get('social_rank_jump_max'))}",
        f"- 个股讨论：{evidence.get('discussion_posts_total', '无')} 条；样本 {_public_status(evidence.get('discussion_sample_status', '无'))}；来源 {_public_status(evidence.get('discussion_source_status', '无'))}；情绪 {_public_status(evidence.get('discussion_sentiment', '无'))}；推广话术 {('、'.join(evidence.get('discussion_promotion_hits', [])) or '无') if isinstance(evidence.get('discussion_promotion_hits'), list) else '无'}",
        f"- 新闻舆情：{_news_sentiment_summary(evidence)}。",
        f"- 异常推广风险：{_public_status(evidence.get('trap_risk_level'))}；命中 {_public_text(evidence.get('trap_signal_count'))}/8",
        "",
        "| 异常推广信号 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for item in evidence.get("trap_checks", []):
        lines.append(f"| {item['signal']} | {'命中' if item['hit'] else '未命中'} | {_public_text(item['evidence'])} |")
    if not evidence.get("trap_checks"):
        lines.append("| 8 类信号 | 无 | 可用社交热榜或交叉证据：无 |")

    lines += [
        "",
        "### Hard Cap 检查 🛡️",
        "",
        "| 条件 | 本次结果 | 对五态的影响 |",
        "|---|---|---|",
    ]
    for item in card.hard_caps:
        raw_result = str(item.get("result") or "需人工确认")
        visible_result = "待核验（可用证据：无）" if raw_result in {"需人工确认", "已搜索未命中", "搜索失败，需人工确认"} else _public_status(raw_result)
        result = f"{_status_icon(raw_result)} {visible_result}"
        lines.append(f"| {item['condition']} | {result} | {_public_text(item.get('decision_effect'))} |")

    lines += ["", "### 睡得着检查 😴", ""]
    for label, status, reason in _sleep_checks(card):
        visible_status = "待核验（可用证据：无）" if status == "需人工确认" else _public_status(status)
        lines.append(f"- {_status_icon(status)} {label}：{visible_status}。{_public_text(reason, '可用数据：无')}")

    lines += [
        "",
        "### 动态纠错触发器 🔄",
        "",
        "- 产业证伪：行业需求、供需方向、订单或下游资本开支连续两个报告期恶化。",
        "- 公司证伪：营收与利润同时转负、经营现金流持续为负，或出现非标审计和重大持续经营风险。",
        "- 估值过热：三年价格分位超过 80%，且市场拥挤度达到 80% 以上；或 TTM PE 超过同行中位数 50%。",
        "- 股东恶化：控股股东或实控人减持、质押比例明显上升，或未来半年解禁比例超过 10%。",
        "- 同链高切低：同产业链出现 F1/F3 不弱、但 F5 得分高出 4 分以上的标的时重新比较。",
    ]
    lines += ["", "免责声明：本分析仅供研究参考，不构成投资建议。"]
    return _sanitize_visible_report("\n".join(lines) + "\n")


def _load_prior_judgment(code: str) -> dict[str, Any]:
    path = JUDGMENT_BASE / f"{code}.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    latest = payload.get("latest") if isinstance(payload, dict) else None
    return dict(latest) if isinstance(latest, dict) else {}


def build_report(
    code: str,
    name: str,
    directories: tuple[str, ...] = REPORTS,
    since: float = 0,
    requested_modules: tuple[str, ...] | None = None,
    thesis_output: ThesisOutput | dict[str, Any] | None = None,
) -> tuple[str, Scorecard, dict[str, Any]]:
    reports = read_reports(code, directories, since)
    evidence = build_evidence(code, name, reports)
    evidence["prior_judgment"] = _load_prior_judgment(code)
    card = score_evidence(evidence)
    return render_report(code, name, evidence, card, requested_modules or directories, thesis_output), card, evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="moda-v4 structured six-factor scorer")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--sources", default=",".join(REPORTS))
    parser.add_argument("--requested-sources", default="")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--thesis-json", default="", help="Agent Judgment V4 JSON; prior judgment contracts must be regenerated from the current fact packet")
    args = parser.parse_args()
    code = args.stock.strip()
    if len(code) != 6 or not code.isdigit():
        parser.error("--stock must be a 6-digit A-share code")
    directories = tuple(source for source in args.sources.split(",") if source in REPORTS)
    requested = tuple(source for source in args.requested_sources.split(",") if source in REPORTS) or directories
    thesis_payload: dict[str, Any] | None = None
    if args.thesis_json:
        try:
            parsed = json.loads(Path(args.thesis_json).read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                thesis_payload = parsed
        except (OSError, ValueError):
            thesis_payload = None
    report, card, evidence = build_report(code, args.name or code, directories, args.since, requested, thesis_payload)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    SCORECARD_BASE.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_BASE / f"{code}.md"
    scorecard_path = SCORECARD_BASE / f"{code}.json"
    report_path.write_text(report, encoding="utf-8")
    context = build_thesis_context(card, evidence).to_dict()
    metadata: dict[str, Any] = {
        "expression_status": "collector_only",
        "research_packet": context,
        "thesis_context": context,
    }
    if thesis_payload is not None:
        try:
            metadata["thesis_output"] = validate_thesis_output(thesis_payload, context).to_dict()
            metadata["expression_status"] = metadata["thesis_output"]["expression_status"]
        except (TypeError, ValueError):
            metadata["expression_status"] = "expression_failed"
    scorecard_path.write_text(
        json.dumps({"evidence": evidence, "scorecard": card.to_dict(), "thesis": metadata}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(report_path)


if __name__ == "__main__":
    main()
