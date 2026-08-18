from __future__ import annotations

import re
from datetime import date
from typing import Any


CATALYST_RULES = {
    "orders": ("中标", "重大合同", "新增订单", "在手订单", "订单增长", "签订合同", "销售合同", "定点"),
    "capacity": ("扩产", "投产", "产能", "产线", "设备采购", "设备投资", "项目开工", "项目建设"),
    "performance": ("业绩预增", "业绩快报", "扭亏", "净利润增长", "利润增长", "营收增长"),
    "shareholder": ("回购", "增持"),
    "pricing": ("涨价", "提价", "调价"),
    "policy": ("获批", "纳入名单", "政策支持", "补贴", "认证", "许可"),
}
ACTION_TERMS = {
    "orders": ("中标", "签订", "签约", "定点", "新增订单", "重大合同", "销售合同"),
    "capacity": ("扩产", "投产", "设备采购", "设备投资", "项目开工", "项目建设", "产能"),
    "performance": ("预增", "快报", "扭亏", "增长"),
    "shareholder": ("回购", "增持"),
    "pricing": ("涨价", "提价", "调价"),
    "policy": ("获批", "纳入名单", "政策支持", "补贴", "认证", "许可"),
}
NEGATIVE_EVENT_TERMS = (
    "风险", "终止", "取消", "暂停", "延期", "下滑", "下降", "过剩",
    "减产", "停产", "不及预期", "澄清", "辟谣",
)
HARD_DETAIL_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*(?:亿元|万元|万台|万吨|吨|条|座|MW|GWh)|"
    r"(?:金额|投资额|产能|产线|设备|订单金额|合同金额|回购金额|增持金额|"
    r"同比|环比|数量|规模|覆盖|期限|有效期))",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
CORPORATE_ACTION_RULES = {
    "equity_incentive": ("股权激励", "限制性股票", "股票期权", "员工持股计划"),
    "buyback": ("回购",),
    "increase": ("增持",),
    "reduction": ("减持",),
    "pledge": ("质押",),
}
CONTROLLER_TERMS = ("控股股东", "实际控制人", "实控人")
NON_REDUCTION_TERMS = ("不减持", "未减持", "终止减持", "取消减持", "停止减持")


def _valid_date(value: Any) -> str:
    text = str(value or "").strip()
    match = DATE_PATTERN.search(text)
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def is_non_reduction_notice(title: str) -> bool:
    """Return whether a title explicitly says a reduction will not proceed."""
    text = str(title or "")
    return "减持" in text and any(term in text for term in NON_REDUCTION_TERMS)


def classify_controller_action(titles: list[str]) -> str | None:
    """Classify controller action without treating a no-reduction pledge as a sale."""
    has_increase = False
    for raw_title in titles:
        title = str(raw_title or "")
        if not title or not any(term in title for term in CONTROLLER_TERMS):
            continue
        if "减持" in title and not is_non_reduction_notice(title):
            return "reduction"
        if "增持" in title:
            has_increase = True
    return "increase" if has_increase else None


def extract_announcement_events(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn dated announcement records into distinct, auditable event records."""
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in announcements:
        title = str(item.get("title") or "").strip()
        event_date = _valid_date(item.get("date"))
        if not title or not event_date:
            continue
        for category, terms in CATALYST_RULES.items():
            matched_terms = [term for term in terms if term in title]
            action_terms = [term for term in ACTION_TERMS[category] if term in title]
            if not matched_terms or not action_terms or any(term in title for term in NEGATIVE_EVENT_TERMS):
                continue
            key = (event_date, category, title)
            if key in seen:
                continue
            seen.add(key)
            hard_detail = bool(HARD_DETAIL_PATTERN.search(title))
            events.append({
                "date": event_date,
                "title": title,
                "url": str(item.get("url") or "").strip(),
                "category": category,
                "matched_terms": matched_terms[:5],
                "hard_detail": hard_detail,
                "evidence_level": "明确事件" if hard_detail else "事件线索",
            })
    return sorted(events, key=lambda item: (item["date"], item["category"], item["title"]), reverse=True)


def catalyst_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    categories = sorted({str(item.get("category")) for item in events if item.get("category")})
    confirmed_categories = sorted({
        str(item.get("category"))
        for item in events
        if item.get("category") and item.get("hard_detail") is True
    })
    return {
        "catalyst_event_count": len(events),
        "catalyst_categories": categories,
        "verified_catalyst_count": min(2, len(confirmed_categories)) if events else 0,
    }


def extract_corporate_action_events(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract neutral corporate-action records without treating them as catalysts."""
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in announcements:
        title = str(item.get("title") or "").strip()
        event_date = _valid_date(item.get("date"))
        if not title or not event_date:
            continue
        for category, terms in CORPORATE_ACTION_RULES.items():
            matched = [term for term in terms if term in title]
            if not matched:
                continue
            if category == "reduction" and is_non_reduction_notice(title):
                category = "non_reduction_commitment"
                matched = [term for term in NON_REDUCTION_TERMS if term in title]
            key = (event_date, category, title)
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "date": event_date,
                "title": title,
                "url": str(item.get("url") or "").strip(),
                "category": category,
                "matched_terms": matched,
                "hard_detail": bool(HARD_DETAIL_PATTERN.search(title)),
            })
    return sorted(events, key=lambda item: (item["date"], item["category"], item["title"]), reverse=True)


def capex_event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    capex_events = [
        item for item in events
        if item.get("category") == "capacity" and item.get("hard_detail") is True
    ]
    return {
        "capex_events": capex_events,
        "capex_event_count": len(capex_events),
        "capex_event_evidence_level": "已核验事件" if capex_events else "仅有事件线索或无事件",
    }
