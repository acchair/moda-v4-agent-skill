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


DEFAULT_RAW_URL_LIMIT = 120
DEFAULT_BODY_PAGE_LIMIT = 100
DEFAULT_QUERY_TIMEOUT = 6.0
DEFAULT_FETCH_TIMEOUT = 6.0
DEFAULT_WORKERS = 3
MIN_QUERY_COVERAGE = 6
MIN_DIMENSION_COVERAGE = 3
MAX_TOTAL_SECONDS = 300.0

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
    if selected in {"so360", "duckduckgo"}:
        # A configured default normally chooses one fast backend.  In broad
        # mode it is a preference, not a reason to discard the other public
        # fallback when the first backend is sparse or blocked.
        backends = [selected]
        if web._secret("BRAVE_SEARCH_API_KEY"):
            backends.append("brave")
        if public_enabled:
            backends.extend(item for item in ("so360", "duckduckgo") if item != selected)
        return list(dict.fromkeys(backends))
    if selected != "auto":
        return [selected]
    backends: list[str] = []
    if os.getenv("SEARXNG_URL", "").strip():
        backends.append("searxng")
    if web._secret("BRAVE_SEARCH_API_KEY"):
        backends.append("brave")
    if public_enabled and os.getenv("SO360_SEARCH_URL", web.SO360_SEARCH_URL).strip():
        backends.append("so360")
    if public_enabled:
        backends.append("duckduckgo")
    if os.getenv("MODA_MODEL_SEARCH_URL", "").strip() or web._secret("OPENAI_API_KEY"):
        backends.append("model")
    return backends or ["duckduckgo"]


def _source_priority(url: str) -> tuple[int, str, str]:
    domain = web._domain(url)
    role, tier = web._source_role(domain)
    if role == "法定信息披露":
        return 0, role, tier
    if tier == "A":
        return 1, role, tier
    if role in {"财经媒体", "行业研究"}:
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
    """Search 100+ candidates when available, with source and body audit data."""
    theme = str(sector or "").strip()
    if not theme:
        raise ValueError("sector 不能为空")
    selected = (provider or os.getenv("MODA_SEARCH_PROVIDER", "auto")).strip().lower()
    if selected not in {"auto", "searxng", "brave", "duckduckgo", "so360", "model", "off"}:
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
        return {
            "sector": theme,
            "entity_resolution": entity_resolution,
            "web_research_status": "disabled",
            "web_research_provider": "off",
            "query_plan": plan,
            "queries": [],
            "sources": [],
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
            if target_reached and minimum_queries_done and dimensions_covered:
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
        "body_attempted": len(body_targets),
        "body_readable": body_readable,
        "usable_evidence_count": len(usable),
        "source_role_counts": dict(role_counts),
        "provider_query_counts": dict(provider_counts),
        "budget_total_seconds": MAX_TOTAL_SECONDS,
        "budget_used_seconds": used_seconds,
        "global_time_exhausted": time.monotonic() >= deadline,
    }
    status = "completed" if audit["raw_url_target_met"] or executed_plan_count == len(plan) else "partial"
    return {
        "sector": theme,
        "entity_resolution": entity_resolution,
        "web_research_status": status,
        "web_research_provider": ",".join(provider_counts) or "none",
        "query_plan": plan,
        "queries": queries,
        "sources": sources,
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
