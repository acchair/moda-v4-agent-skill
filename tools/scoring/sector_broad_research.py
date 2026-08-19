"""Auditable wide web collection for an A-share sector or theme.

This is a collection pass, not a sector verdict.  It first preserves a broad
set of URLs, then reads a bounded subset in source-priority order.  The audit
therefore distinguishes raw search coverage, readable bodies and usable
evidence instead of treating a small set of top-ranked snippets as research.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import time
from typing import Any, Iterable

from tools.scoring import sector_search_planner
from tools.scoring import web_research as web


DEFAULT_RAW_URL_LIMIT = 100
DEFAULT_BODY_PAGE_LIMIT = 100
DEFAULT_QUERY_TIMEOUT = 6.0
DEFAULT_FETCH_TIMEOUT = 6.0
DEFAULT_WORKERS = 3
MIN_QUERY_COVERAGE = 6
MIN_DIMENSION_COVERAGE = 3
MAX_TOTAL_SECONDS = 300.0


# Event order follows the research contract: a pivotal clinical readout is
# more material than a normal company item.  These rules only classify a
# body-verified overseas candidate; they never assert an A-share benefit.
OVERSEAS_EVENT_RULES: tuple[dict[str, Any], ...] = (
    {
        "event_type": "III期/关键临床读出",
        "priority": "P1",
        "priority_rank": 1,
        "terms": ("phase 3", "phase iii", "phase-3", "pivotal trial", "iii期", "3期临床"),
        "catalyst_type": "产业催化",
        "mapping_priority": "中",
    },
    {
        "event_type": "监管审批重大节点",
        "priority": "P2",
        "priority_rank": 2,
        "terms": ("fda approval", "fda approved", "breakthrough therapy", "fast track", "complete response letter", "批准上市", "突破性疗法", "快速通道"),
        "catalyst_type": "产业催化",
        "mapping_priority": "中",
    },
    {
        "event_type": "大额并购或授权",
        "priority": "P3",
        "priority_rank": 3,
        "terms": ("acquisition", "acquire", "merger", "license agreement", "licensing deal", "license-out", "并购", "授权交易", "许可协议"),
        "catalyst_type": "产业催化",
        "mapping_priority": "中",
    },
    {
        "event_type": "订单/采购/产能落地",
        "priority": "P4",
        "priority_rank": 4,
        "terms": ("purchase order", "supply agreement", "bookings", "contract award", "orders", "采购订单", "供应协议", "中标", "订单"),
        "catalyst_type": "订单催化",
        "mapping_priority": "高",
    },
    {
        "event_type": "业绩/指引/资本开支变化",
        "priority": "P4",
        "priority_rank": 4,
        "terms": ("earnings guidance", "revenue guidance", "capital expenditure", "capex", "research and development", "业绩指引", "资本开支", "研发方向"),
        "catalyst_type": "利润催化",
        "mapping_priority": "高",
    },
    {
        "event_type": "技术路线验证或行业融资",
        "priority": "P5",
        "priority_rank": 5,
        "terms": ("proof of concept", "clinical validation", "validated", "financing", "funding round", "技术验证", "临床验证", "融资"),
        "catalyst_type": "产业催化",
        "mapping_priority": "中",
    },
    {
        "event_type": "股价异动或普通公司新闻",
        "priority": "P6",
        "priority_rank": 6,
        "terms": ("shares rose", "shares fell", "stock jumped", "stock plunged", "股价大涨", "股价大跌", "暴涨", "暴跌"),
        "catalyst_type": "情绪催化",
        "mapping_priority": "低",
    },
)

OVERSEAS_A_SHARE_VALIDATION = (
    "核对A股公司F10主营、产品/业务分部与产业链位置",
    "核对同一披露维度的收入暴露，不以概念标签代替收入",
    "核对订单、产能利用率、收入/毛利率或现金流是否出现传导",
    "与至少两家同行比较供给位置、壁垒和当前估值是否已交易",
)

def _compact_context(context: str) -> str:
    values: list[str] = []
    for token in re.split(r"[\s、,，;；|/]+", str(context or "")):
        token = token.strip()
        if len(token) < 2 or token in values:
            continue
        values.append(token)
        if len(" ".join(values)) >= 72 or len(values) >= 6:
            break
    return " ".join(values)


def build_query_plan(
    sector: str,
    context: str = "",
    *,
    entity_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    return sector_search_planner.build_broad_query_plan(
        sector,
        context,
        entity_context=entity_context,
    )


def _search_backends(selected: str) -> list[str]:
    """Use each configured search family for wide collection coverage."""
    if selected == "off":
        return [selected]
    public_enabled = os.getenv("MODA_PUBLIC_SEARCH", "auto").strip().lower() not in {"0", "false", "off", "no"}
    if selected != "auto":
        return [selected]
    backends: list[str] = ["duckduckgo"] if public_enabled else []
    if web._secret("BRAVE_SEARCH_API_KEY"):
        backends.append("brave")
    if web._secret("DEEPSEEK_API_KEY"):
        backends.append("deepseek")
    if web._secret("OPENAI_API_KEY"):
        backends.append("openai")
    if os.getenv("MODA_MODEL_SEARCH_URL", "").strip():
        backends.append("bridge")
    return backends or ["duckduckgo"]


def _source_priority(url: str) -> tuple[int, str, str]:
    domain = web._domain(url)
    role, tier = web._source_role(domain)
    if role in {"法定信息披露", "海外监管/法定披露"}:
        return 0, role, tier
    if tier == "A":
        return 1, role, tier
    if role in {"财经媒体", "行业研究", "海外行业专业"}:
        return 2, role, tier
    if role == "线索来源":
        return 4, role, tier
    return 3, role, tier


def _dedupe_rows(rows: Iterable[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        unique.append(row)
    return unique[:max(0, int(limit))], duplicates


def _scope_match(sector: str, context: str, text: str) -> bool:
    terms = [str(sector or "").strip(), *_compact_context(context).split()]
    lowered = str(text or "").lower()
    return any(term and term.lower() in lowered for term in terms)


def _fetch_one(row: dict[str, Any], sector: str, context: str, timeout: float, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {**row, "fetch_status": "global_budget_exhausted", "content_excerpt": "", "body_scope_match": False}
    status, content = web._fetch_page(str(row["url"]), min(timeout, max(0.2, remaining)))
    excerpt = content[:6000] if content else ""
    return {
        **row,
        "fetch_status": status,
        "content_excerpt": excerpt,
        "body_scope_match": status == "ok" and _scope_match(sector, context, excerpt),
    }


def _classify_overseas_event(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return an event label, never a company-benefit conclusion."""
    if str(row.get("bucket") or "") != "海外增量雷达":
        return None
    if row.get("fetch_status") != "ok" or not row.get("body_scope_match"):
        return None
    if str(row.get("source_role") or "") == "线索来源":
        return None
    text = " ".join(str(row.get(key) or "") for key in ("title", "snippet", "content_excerpt")).lower()
    for rule in OVERSEAS_EVENT_RULES:
        if any(term.lower() in text for term in rule["terms"]):
            return dict(rule)
    return None


def _build_overseas_event_radar(
    sector: str,
    context: str,
    plan: list[dict[str, str]],
    sources: list[dict[str, Any]],
    *,
    status: str = "completed",
    entity_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make the overseas-to-A-share handoff explicit and auditable.

    It retains only readable, theme-matched bodies from the dedicated radar
    queries.  A high mapping priority means *verify the earnings bridge first*,
    not that a company has been selected or rated.
    """
    profile = sector_search_planner.overseas_event_profile(
        sector,
        context,
        entity_context=entity_context,
        profile_id=str(next((item.get("profile_id") for item in plan if item.get("profile_id")), "generic")),
    )
    radar_queries = [item for item in plan if item.get("bucket") == "海外增量雷达"]
    selected_sources = []
    for item in radar_queries:
        domain = str(item.get("domain_hint") or "")
        role, tier = web._source_role(domain)
        selected_sources.append({"domain": domain, "source_role": role, "source_tier": tier})
    if status == "disabled":
        return {
            "status": "disabled",
            "profile": {"id": profile["id"], "label": profile["label"]},
            "selected_sources": selected_sources,
            "events": [],
            "mapping_guardrails": list(OVERSEAS_A_SHARE_VALIDATION),
            "summary": "海外增量雷达已关闭，未将空结果解释为没有海外事件。",
        }

    events: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in sources:
        event = _classify_overseas_event(row)
        url = str(row.get("url") or "")
        if event is None or not url or url in seen_urls:
            continue
        seen_urls.add(url)
        events.append({
            "event_type": event["event_type"],
            "event_priority": event["priority"],
            "event_priority_rank": event["priority_rank"],
            "catalyst_type": event["catalyst_type"],
            "a_share_mapping_priority": event["mapping_priority"],
            "mapping_status": "待A股主营、收入暴露与订单/利润核验",
            "mapping_chain": profile["mapping_chain"],
            "a_share_validation": list(OVERSEAS_A_SHARE_VALIDATION),
            "title": str(row.get("title") or ""),
            "url": url,
            "date": str(row.get("date") or ""),
            "source_role": str(row.get("source_role") or ""),
            "source_tier": str(row.get("source_tier") or ""),
            "body_status": "正文已核验",
        })
    events.sort(key=lambda item: (int(item["event_priority_rank"]), item["url"]))
    return {
        "status": "body_verified_events" if events else "no_body_verified_event",
        "profile": {"id": profile["id"], "label": profile["label"]},
        "selected_sources": selected_sources,
        "events": events[:10],
        "mapping_guardrails": list(OVERSEAS_A_SHARE_VALIDATION),
        "summary": (
            f"海外增量雷达读取到 {len(events)} 条正文核验事件；"
            "事件只提供产业链待核验映射，不构成A股受益、收入或利润事实。"
            if events else
            "未检出正文核验的海外增量事件；这表示本轮覆盖内未匹配，不表示海外没有事件或A股不存在受益。"
        ),
    }


def _search_backend(backend: str, query: str, timeout: float, cache_scope: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Let the paginated Brave source contribute a full wide-search page."""
    if backend == "brave":
        try:
            rows: list[dict[str, Any]] = []
            for offset in range(6):
                page = web._brave_search(query, timeout, count=20, offset=offset)
                rows.extend(page)
                if len(page) < 20:
                    break
            return "brave", rows, [] if rows else ["brave:no_results"]
        except Exception as exc:
            return "none", [], [f"brave:{type(exc).__name__}"]
    return web._search(backend, query, timeout, cache_scope=cache_scope)


def collect_sector_broad_evidence(
    sector: str,
    *,
    context: str = "",
    provider: str | None = None,
    raw_url_limit: int = DEFAULT_RAW_URL_LIMIT,
    body_page_limit: int = DEFAULT_BODY_PAGE_LIMIT,
    query_timeout: float = DEFAULT_QUERY_TIMEOUT,
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    screening: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search up to 100 candidates, with an auditable coverage stop."""
    theme = str(sector or "").strip()
    if not theme:
        raise ValueError("sector 不能为空")
    selected = (provider or os.getenv("MODA_SEARCH_PROVIDER", "auto")).strip().lower()
    if selected not in {"auto", "brave", "duckduckgo", "deepseek", "openai", "model", "bridge", "off"}:
        selected = "off"
    entity_resolution = (
        sector_search_planner.resolve_entity_context(theme, screening=screening)
        if screening or selected != "off"
        else {
            "status": "not_requested",
            "input": theme,
            "query_kind": "unknown",
            "board_names": [],
            "representative_companies": [],
            "business_phrases": [],
            "universe_source": "",
            "coverage_status": "not_requested",
            "constituent_count": 0,
        }
    )
    plan = build_query_plan(theme, context, entity_context=entity_resolution)
    if selected == "off":
        overseas_event_radar = _build_overseas_event_radar(
            theme, context, plan, [], status="disabled", entity_context=entity_resolution,
        )
        return {
            "sector": theme,
            "entity_resolution": entity_resolution,
            "web_research_status": "disabled",
            "web_research_provider": "off",
            "query_plan": plan,
            "queries": [],
            "sources": [],
            "overseas_event_radar": overseas_event_radar,
            "audit": {"raw_result_count": 0, "raw_url_count": 0, "body_attempted": 0, "body_readable": 0, "usable_evidence_count": 0},
            "errors": [],
            "summary": "行业广搜已关闭，未将空结果解释为行业事实。",
        }

    started = time.monotonic()
    deadline = started + MAX_TOTAL_SECONDS
    raw_rows: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    errors: list[str] = []
    provider_counts: Counter[str] = Counter()
    backends = _search_backends(selected)
    discovered_urls: set[str] = set()
    covered_dimensions: set[str] = set()
    executed_plan_count = 0
    early_stop_reason = ""
    web._reset_run_snapshot()
    with web._search_cache_batch():
        for item in plan:
            if time.monotonic() >= deadline:
                errors.append("global_budget_exhausted_before_query_plan_complete")
                break
            executed_plan_count += 1
            covered_dimensions.add(str(item.get("dimension") or item["bucket"]))
            for backend in backends:
                if time.monotonic() >= deadline:
                    errors.append("global_budget_exhausted_during_query_plan")
                    break
                used, rows, search_errors = _search_backend(
                    backend,
                    item["query"],
                    min(float(query_timeout), max(0.2, deadline - time.monotonic())),
                    cache_scope=f"sector-broad|{theme}|{item['bucket']}|{item['domain_hint']}|{backend}",
                )
                queries.append({**item, "requested_backend": backend, "provider": used, "result_count": len(rows), "errors": search_errors})
                errors.extend(f"{item['bucket']}:{backend}:{error}" for error in search_errors)
                if used != "none":
                    provider_counts[used] += 1
                for rank, row in enumerate(rows, start=1):
                    url = str(row.get("url") or "").strip()
                    if not url:
                        continue
                    discovered_urls.add(url)
                    priority, role, tier = _source_priority(url)
                    raw_rows.append({
                        "title": str(row.get("title") or "").strip(),
                        "url": url,
                        "snippet": str(row.get("snippet") or "").strip(),
                        "date": str(row.get("date") or "").strip(),
                        "query": item["query"],
                        "bucket": item["bucket"],
                        "domain_hint": item["domain_hint"],
                        "provider": used,
                        "rank": rank,
                        "source_role": role,
                        "source_tier": tier,
                        "source_priority": priority,
                    })
            target_reached = len(discovered_urls) >= max(0, int(raw_url_limit))
            minimum_queries_done = executed_plan_count >= min(len(plan), MIN_QUERY_COVERAGE)
            dimensions_covered = len(covered_dimensions) >= min(MIN_DIMENSION_COVERAGE, len(plan))
            source_roles = {
                str(row.get("source_role") or "") for row in raw_rows
                if str(row.get("source_role") or "") not in {"", "线索来源"}
            }
            coverage_ready = minimum_queries_done and dimensions_covered and len(source_roles) >= 3
            if target_reached and minimum_queries_done and dimensions_covered:
                early_stop_reason = "raw_url_target"
                break
            if coverage_ready:
                early_stop_reason = "candidate_source_coverage"
                break

    unique_rows, duplicate_count = _dedupe_rows(raw_rows, raw_url_limit)
    ranked = sorted(unique_rows, key=lambda row: (int(row["source_priority"]), int(row["rank"]), str(row["url"])))
    body_targets = ranked[:max(0, int(body_page_limit))]
    fetched: list[dict[str, Any]] = []
    if body_targets and time.monotonic() < deadline:
        max_workers = max(1, min(int(workers), DEFAULT_WORKERS, len(body_targets)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_fetch_one, row, theme, context, float(fetch_timeout), deadline) for row in body_targets]
            for future in as_completed(futures):
                try:
                    fetched.append(future.result())
                except Exception as exc:
                    errors.append(f"body_fetch:{type(exc).__name__}")
    fetched.sort(key=lambda row: (int(row["source_priority"]), int(row["rank"]), str(row["url"])))
    fetched_by_url = {row["url"]: row for row in fetched}
    sources = [fetched_by_url.get(row["url"], {**row, "fetch_status": "not_selected_for_body", "content_excerpt": "", "body_scope_match": False}) for row in ranked]
    body_readable = sum(row.get("fetch_status") == "ok" for row in sources)
    usable = [
        row for row in sources
        if row.get("fetch_status") == "ok" and row.get("body_scope_match")
        and row.get("source_role") != "线索来源"
    ]
    role_counts = Counter(str(row.get("source_role") or "未知") for row in sources)
    used_seconds = round(time.monotonic() - started, 3)
    audit = {
        "query_planned": len(plan),
        "query_executed": executed_plan_count,
        "query_dimension_coverage": sorted(covered_dimensions),
        "search_attempt_count": len(queries),
        "raw_result_count": len(raw_rows),
        "duplicate_url_count": duplicate_count,
        "raw_url_count": len(ranked),
        "raw_url_target": max(0, int(raw_url_limit)),
        "raw_url_target_met": len(ranked) >= max(0, int(raw_url_limit)),
        "early_stop_reason": early_stop_reason or ("query_plan_complete" if executed_plan_count == len(plan) else "budget_exhausted"),
        "candidate_source_coverage_ready": early_stop_reason == "candidate_source_coverage",
        "body_attempted": len(body_targets),
        "body_readable": body_readable,
        "usable_evidence_count": len(usable),
        "source_role_counts": dict(role_counts),
        "provider_query_counts": dict(provider_counts),
        "budget_total_seconds": MAX_TOTAL_SECONDS,
        "budget_used_seconds": used_seconds,
        "global_time_exhausted": time.monotonic() >= deadline,
    }
    status = "completed" if audit["raw_url_target_met"] or audit["candidate_source_coverage_ready"] or executed_plan_count == len(plan) else "partial"
    overseas_event_radar = _build_overseas_event_radar(
        theme,
        context,
        plan,
        sources,
        status=status,
        entity_context=entity_resolution,
    )
    return {
        "sector": theme,
        "entity_resolution": entity_resolution,
        "web_research_status": status,
        "web_research_provider": ",".join(provider_counts) or "none",
        "query_plan": plan,
        "queries": queries,
        "sources": sources,
        "overseas_event_radar": overseas_event_radar,
        "audit": audit,
        "errors": list(dict.fromkeys(errors)),
        "summary": (
            f"已检索 {audit['raw_result_count']} 条原始结果，去重保留 {audit['raw_url_count']} 个 URL；"
            f"读取正文 {audit['body_readable']}/{audit['body_attempted']}，其中 {audit['usable_evidence_count']} 条仅作为后续核验材料。"
            "数量和摘要均不直接构成行业结论。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect an auditable broad web corpus for an A-share sector")
    parser.add_argument("sector")
    parser.add_argument("--context", default="")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--raw-url-limit", type=int, default=DEFAULT_RAW_URL_LIMIT)
    parser.add_argument("--body-page-limit", type=int, default=DEFAULT_BODY_PAGE_LIMIT)
    args = parser.parse_args()
    print(json.dumps(collect_sector_broad_evidence(
        args.sector,
        context=args.context,
        provider=args.provider,
        raw_url_limit=args.raw_url_limit,
        body_page_limit=args.body_page_limit,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
