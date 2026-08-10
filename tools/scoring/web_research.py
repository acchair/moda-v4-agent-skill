from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from html.parser import HTMLParser
import ipaddress
from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from hashlib import sha256

import requests
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.scoring.classification_db import has_category, lookup
from tools.scoring.search_rules import RULES, evaluate as evaluate_gap, queries_for


OUTPUT_BASE = ROOT / "knowledge" / "research" / "web_research"
USER_AGENT = "moda-v4-research/1.0"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
MAX_FETCH_BYTES = 600_000
MAX_PDF_FETCH_BYTES = 10_000_000
MAX_PAGES = 30
MAX_PDF_PAGES = 30
MAX_PDF_TEXT_CHARS = 120_000
MAX_PAGES_PER_PURPOSE = 6
MAX_GAP_TARGETS = 12
MAX_GAP_QUERIES_PER_TARGET = 2
MAX_GAP_BUDGET_SECONDS = 75.0
MIN_TARGET_BUDGET_SECONDS = 3.0
MAX_GAP_WORKERS = 3
GAP_PRIORITY = {
    "F2.controller_action": 120,
    "F3.survival_risk": 115,
    "F3.leadership": 108,
    "F3.specialized": 107,
    "F1.capex_wave": 106,
    "F1.era_track": 102,
    "F1.chokepoint": 100,
    "F1.supply_gap": 98,
    "F4.realization": 96,
    "F4.business_match": 94,
    "F4.profit_position": 92,
    "F5.inflection": 88,
    "F5.expectation_gap": 84,
    "F3.background": 82,
}
CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "web_search_daily.json"
_RUNTIME_LOCAL = threading.local()
_CACHE_LOCK = threading.RLock()
_CACHE_PAYLOADS: dict[str, dict[str, Any]] = {}
_CACHE_PATHS: dict[str, Path] = {}
_CACHE_DIRTY: set[str] = set()
_CACHE_BATCH_DEPTH = 0
_PAGE_SNAPSHOT_LOCK = threading.RLock()
_PAGE_SNAPSHOT: dict[str, tuple[str, str]] = {}
_PAGE_INFLIGHT: dict[str, threading.Event] = {}
AUTHORITY_DOMAINS = (
    "gov.cn", "cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn",
    "stats.gov.cn", "miit.gov.cn", "ndrc.gov.cn", "customs.gov.cn",
)
STATUTORY_DOMAINS = ("cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn")
FINANCE_MEDIA_DOMAINS = (
    "eastmoney.com", "10jqka.com.cn", "stcn.com", "cs.com.cn", "cnstock.com",
    "yicai.com", "cls.cn", "jrj.com.cn",
)
CLUE_ONLY_DOMAINS = (
    "xueqiu.com", "guba.eastmoney.com", "caifuhao.eastmoney.com", "gw.com.cn", "dzh.com.cn",
)
CHINA_FINANCE_SITE_GROUPS = {
    "disclosure": "(site:cninfo.com.cn OR site:sse.com.cn OR site:szse.cn OR site:bse.cn)",
    "market": "(site:eastmoney.com OR site:10jqka.com.cn OR site:stcn.com OR site:cs.com.cn OR site:cnstock.com OR site:yicai.com OR site:cls.cn OR site:jrj.com.cn)",
}
SUPPLY_CATEGORIES = {
    "price": ("价格", "涨价", "降价", "报价", "基差"),
    "inventory": ("库存", "仓单", "去库存"),
    "orders": ("订单", "在手订单", "交付", "排产", "交期"),
    "capacity": ("产能", "产能利用率", "扩产", "供不应求", "供过于求", "紧缺", "产能过剩"),
}
TIGHTENING_TERMS = ("供不应求", "紧缺", "缺货", "涨价", "库存下降", "去库存", "订单增长", "排产饱满", "交期延长", "产能利用率提升")
LOOSENING_TERMS = ("供过于求", "库存上升", "降价", "产能过剩", "订单下降", "需求下滑", "开工率下降")
COMPANY_RELATION_TERMS = ("产品", "设备", "业务", "供应商", "客户", "产业化", "量产")
REPLACEMENT_TERMS = ("国产替代", "进口替代", "自主可控", "国产化")
DEPENDENCY_TERMS = ("进口依赖", "卡脖子", "受制于人", "海外垄断", "国外垄断", "关键核心技术", "国产化率")
DELISTING_TERMS = ("退市风险警示", "终止上市", "暂停上市", "重大违法强制退市", "*ST", "ST ")
AUDIT_RISK_PATTERNS = (
    r"审计意见(?:为|类型为|[:：])\s*(?:保留意见|无法表示意见|否定意见)",
    r"(?:被出具|出具了?|形成了?)\s*(?:保留意见|无法表示意见|否定意见)",
)
GOODWILL_RISK_PATTERNS = (
    r"计提(?:了)?[^。；;\n]{0,20}商誉减值",
    r"商誉减值(?:准备|损失)",
    r"发生(?:了)?[^。；;\n]{0,12}商誉减值",
)
SPECIALIZED_TERMS = ("专精特新小巨人", "专精特新", "制造业单项冠军", "单项冠军")
CATALYST_CATEGORIES = {
    "orders": ("中标", "重大合同", "新增订单", "订单增长"),
    "capacity": ("扩产", "投产", "项目落地", "产线建设"),
    "performance": ("业绩预增", "扭亏", "利润增长"),
    "shareholder": ("回购", "增持"),
    "policy": ("纳入名单", "政策支持", "补贴", "获批"),
}
CAPEX_UP_TERMS = ("投资增长", "投资同比增长", "加快投资", "扩大投资", "新增产能", "扩产", "产能建设", "设备更新")
CAPEX_DOWN_TERMS = ("投资下降", "投资同比下降", "压减产能", "削减投资", "延缓投资", "停止扩产")
CAPEX_CATEGORIES = {
    "investment": ("固定资产投资", "投资增长", "投资同比增长", "设备投资"),
    "capacity": ("新增产能", "扩产", "产能建设", "投产"),
    "equipment": ("设备更新", "设备采购", "产线建设"),
}


def _load_local_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    allowed = {
        "MODA_SEARCH_PROVIDER", "SEARXNG_URL", "DDG_MCP_URL",
        "DDG_HTML_URL", "DDG_LITE_URL",
    }
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self.parts.append(value)


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_authority(domain: str) -> bool:
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in AUTHORITY_DOMAINS) or domain.endswith(".org.cn")


def _matches_domain(domain: str, suffixes: tuple[str, ...]) -> bool:
    domain = domain.lower().removeprefix("www.")
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in suffixes)


def _source_role(domain: str) -> tuple[str, str]:
    if _matches_domain(domain, STATUTORY_DOMAINS):
        return "法定信息披露", "A"
    if _matches_domain(domain, CLUE_ONLY_DOMAINS):
        return "线索来源", "C"
    if _is_authority(domain):
        return "权威来源", "A"
    if _matches_domain(domain, FINANCE_MEDIA_DOMAINS):
        return "财经媒体", "B"
    return "一般来源", "B"


def _search_rank(row: dict[str, Any]) -> tuple[int, int]:
    """Prefer disclosure and mainstream financial sources without trusting them automatically."""
    role, tier = _source_role(_domain(str(row.get("url") or "")))
    priority = {
        ("法定信息披露", "A"): 0,
        ("权威来源", "A"): 1,
        ("财经媒体", "B"): 2,
        ("一般来源", "B"): 3,
        ("线索来源", "C"): 4,
    }.get((role, tier), 5)
    return priority, int(row.get("rank") or 999)


def _prioritize_search_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=_search_rank)


def _china_finance_query(key: str, name: str, code: str, context: str) -> str:
    short_context = " ".join(context.split()[:12])
    market_keys = {
        "F1.era_track", "F1.supply_gap", "F1.capex_wave",
        "F5.price_position", "F5.coldness", "F5.inflection", "F5.expectation_gap",
    }
    site_group = CHINA_FINANCE_SITE_GROUPS["market"] if key in market_keys else CHINA_FINANCE_SITE_GROUPS["disclosure"]
    return f"{name} {code} {short_context} {site_group} {key.replace('.', ' ')}".strip()


def _confirmable(row: dict[str, Any]) -> bool:
    return row.get("fetch_status") == "ok" and row.get("source_role") != "线索来源"


def _safe_public_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def _response_payload(response: requests.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        payloads: list[dict[str, Any]] = []
        text = response.content.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    payloads.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    continue
        return next((payload for payload in reversed(payloads) if "result" in payload), payloads[-1] if payloads else {})
    try:
        return response.json()
    except ValueError:
        return {}


def _http_session() -> requests.Session:
    session = getattr(_RUNTIME_LOCAL, "http_session", None)
    if session is None:
        session = requests.Session()
        _RUNTIME_LOCAL.http_session = session
    return session


def _ddg_runtime(url: str, timeout: float) -> tuple[requests.Session, dict[str, str], int]:
    runtime = getattr(_RUNTIME_LOCAL, "ddg_runtime", None)
    if runtime and runtime[0] == url:
        endpoint, session, headers, next_id = runtime
        _RUNTIME_LOCAL.ddg_runtime = (endpoint, session, headers, next_id + 1)
        return session, dict(headers), next_id

    session = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "moda-v4", "version": "1.0"}},
    }
    response = session.post(url, json=initialize, headers=headers, timeout=timeout)
    response.raise_for_status()
    session_id = response.headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    session.post(
        url,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=headers,
        timeout=timeout,
    ).raise_for_status()
    _RUNTIME_LOCAL.ddg_runtime = (url, session, headers, 3)
    return session, dict(headers), 2


def _reset_ddg_runtime() -> None:
    runtime = getattr(_RUNTIME_LOCAL, "ddg_runtime", None)
    if runtime:
        try:
            runtime[1].close()
        except Exception:
            pass
        delattr(_RUNTIME_LOCAL, "ddg_runtime")


def _searxng_search(base_url: str, query: str, timeout: float) -> list[dict[str, Any]]:
    response = _http_session().get(
        base_url.rstrip("/") + "/search",
        params={"q": query, "format": "json", "language": "zh-CN", "safesearch": 1},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = response.json().get("results", [])
    return _prioritize_search_rows([
        {
            "title": str(row.get("title") or "").strip(),
            "url": str(row.get("url") or "").strip(),
            "snippet": str(row.get("content") or "").strip(),
            "date": str(row.get("publishedDate") or "").strip(),
            "engine": ",".join(row.get("engines") or [str(row.get("engine") or "")]),
        }
        for row in rows
        if row.get("url")
    ])[:8]


def _parse_ddg_text(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(?:^|\n)(?:##\s*)?\d+\.\s*(.*?)\n\s*(?:\*\*)?URL:(?:\*\*)?\s*(\S+)"
        r"(?:\n\s*(?:\*\*)?Summary:(?:\*\*)?\s*(.*?))?(?=\n\s*(?:##\s*)?\d+\.|\Z)",
        re.S,
    )
    return _prioritize_search_rows([
        {"title": title.strip(), "url": url.strip(), "snippet": (snippet or "").strip(), "date": "", "engine": "DuckDuckGo"}
        for title, url, snippet in pattern.findall(text)
    ])


def _ddg_mcp_search(url: str, query: str, timeout: float) -> list[dict[str, Any]]:
    session, headers, request_id = _ddg_runtime(url, timeout)
    call = {
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": query, "max_results": 8, "region": "cn-zh"}},
    }
    response = session.post(url, json=call, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = _response_payload(response)
    blocks = payload.get("result", {}).get("content", [])
    text = "\n".join(str(block.get("text") or "") for block in blocks if block.get("type") == "text")
    return _parse_ddg_text(text)


def _duckduckgo_html_search(query: str, timeout: float) -> list[dict[str, Any]]:
    """Use DuckDuckGo's public HTML endpoint when no local service exists."""
    response = _http_session().get(
        os.getenv("DDG_HTML_URL", DDG_HTML_URL).strip() or DDG_HTML_URL,
        params={"q": query, "kl": "cn-zh"},
        headers={"User-Agent": PUBLIC_SEARCH_USER_AGENT, "Accept": "text/html"},
        timeout=timeout,
    )
    response.raise_for_status()
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S
    )
    rows: list[dict[str, Any]] = []
    for href, title_html in pattern.findall(response.text):
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        parsed = urlparse(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [""])[0] or href
        if not href.startswith(("http://", "https://")):
            continue
        rows.append({"title": title, "url": href, "snippet": "", "date": "", "engine": "DuckDuckGo HTML"})
        if len(rows) >= 8:
            break
    return _prioritize_search_rows(rows)


def _duckduckgo_lite_search(query: str, timeout: float) -> list[dict[str, Any]]:
    """Use DuckDuckGo Lite, which is less JavaScript- and bandwidth-dependent."""
    response = _http_session().get(
        os.getenv("DDG_LITE_URL", DDG_LITE_URL).strip() or DDG_LITE_URL,
        params={"q": query, "kl": "cn-zh"},
        headers={"User-Agent": PUBLIC_SEARCH_USER_AGENT, "Accept": "text/html"},
        timeout=timeout,
    )
    response.raise_for_status()
    pattern = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
    rows: list[dict[str, Any]] = []
    for attrs, title_html in pattern.findall(response.text):
        class_match = re.search(r'class=["\'][^"\']*\bresult-link\b[^"\']*["\']', attrs, re.I)
        href_match = re.search(r'href=["\']([^"\']+)["\']', attrs, re.I)
        if not class_match or not href_match:
            continue
        href = href_match.group(1)
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        parsed = urlparse(urljoin("https://lite.duckduckgo.com/lite/", href))
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [""])[0] or href
        href = urljoin("https://lite.duckduckgo.com/lite/", href)
        if not href.startswith(("http://", "https://")):
            continue
        rows.append({"title": title, "url": href, "snippet": "", "date": "", "engine": "DuckDuckGo Lite"})
        if len(rows) >= 8:
            break
    return _prioritize_search_rows(rows)


def _search(provider: str, query: str, timeout: float, cache_scope: str = "") -> tuple[str, list[dict[str, Any]], list[str]]:
    cache_enabled = os.getenv("MODA_SEARCH_CACHE", "on").strip().lower() not in {"0", "false", "off", "no"}
    cache_key = _search_cache_key(provider, query, cache_scope) if cache_scope and cache_enabled else ""
    if cache_key:
        cached = _load_search_cache().get(cache_key)
        if cached and cached.get("date") == datetime.now().date().isoformat() and cached.get("rows"):
            return str(cached.get("used") or "none"), list(cached.get("rows") or []), []
    errors: list[str] = []
    searxng = os.getenv("SEARXNG_URL", "").strip()
    ddg = os.getenv("DDG_MCP_URL", "").strip()
    if provider in {"auto", "searxng"} and searxng:
        try:
            rows = _searxng_search(searxng, query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "searxng", rows)
                return "searxng", rows, errors
            errors.append("searxng:no_results")
        except Exception as exc:
            errors.append(f"searxng:{type(exc).__name__}")
    if provider in {"auto", "duckduckgo"} and ddg:
        try:
            rows = _ddg_mcp_search(ddg, query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "duckduckgo", rows)
                return "duckduckgo", rows, errors
            errors.append("duckduckgo:no_results")
        except Exception as exc:
            _reset_ddg_runtime()
            errors.append(f"duckduckgo:{type(exc).__name__}")
    public_search = os.getenv("MODA_PUBLIC_SEARCH", "auto").strip().lower()
    if provider in {"auto", "duckduckgo"} and public_search not in {"0", "false", "off", "no"}:
        try:
            rows = _duckduckgo_html_search(query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "duckduckgo_html", rows)
                return "duckduckgo_html", rows, errors
            errors.append("duckduckgo_html:no_results")
        except Exception as exc:
            errors.append(f"duckduckgo_html:{type(exc).__name__}")
        try:
            rows = _duckduckgo_lite_search(query, timeout)
            if rows:
                if cache_key:
                    _save_search_cache(cache_key, "duckduckgo_lite", rows)
                return "duckduckgo_lite", rows, errors
            errors.append("duckduckgo_lite:no_results")
        except Exception as exc:
            errors.append(f"duckduckgo_lite:{type(exc).__name__}")
    if not searxng and not ddg and not errors:
        errors.append("search_backend_not_configured")
    return "none", [], errors


def _search_cache_key(provider: str, query: str, cache_scope: str = "") -> str:
    configured = "|".join((provider, os.getenv("SEARXNG_URL", "").strip(), os.getenv("DDG_MCP_URL", "").strip()))
    return sha256(f"{cache_scope}|{configured}|{query.strip()}".encode("utf-8")).hexdigest()


def _load_search_cache() -> dict[str, Any]:
    cache_path = CACHE_PATH.resolve()
    cache_key = str(cache_path)
    with _CACHE_LOCK:
        if cache_key not in _CACHE_PAYLOADS:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                _CACHE_PAYLOADS[cache_key] = payload if isinstance(payload, dict) else {}
            except (OSError, ValueError, TypeError):
                _CACHE_PAYLOADS[cache_key] = {}
            _CACHE_PATHS[cache_key] = cache_path
        return _CACHE_PAYLOADS[cache_key]


def _flush_search_cache() -> None:
    with _CACHE_LOCK:
        dirty_keys = list(_CACHE_DIRTY)
        for cache_key in dirty_keys:
            path = _CACHE_PATHS[cache_key]
            payload = _CACHE_PAYLOADS[cache_key]
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, path)
                _CACHE_DIRTY.discard(cache_key)
            except OSError:
                continue


@contextmanager
def _search_cache_batch():
    global _CACHE_BATCH_DEPTH
    with _CACHE_LOCK:
        _CACHE_BATCH_DEPTH += 1
    try:
        yield
    finally:
        with _CACHE_LOCK:
            _CACHE_BATCH_DEPTH -= 1
            should_flush = _CACHE_BATCH_DEPTH == 0
        if should_flush:
            _flush_search_cache()


def _save_search_cache(key: str, used: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with _CACHE_LOCK:
        payload = _load_search_cache()
        payload[key] = {"date": datetime.now().date().isoformat(), "used": used, "rows": rows[:8]}
        cache_key = str(CACHE_PATH.resolve())
        _CACHE_DIRTY.add(cache_key)
        should_flush = _CACHE_BATCH_DEPTH == 0
    if should_flush:
        _flush_search_cache()


def _gap_relevant(row: dict[str, Any], key: str, code: str, name: str, context: str) -> bool:
    text = " ".join(str(row.get(field) or "") for field in ("title", "snippet"))
    if name and name in text or code and code in text:
        return True
    tokens = [token for token in re.split(r"[\s、,，/|]+", context) if len(token) >= 2 and not token.isdigit()]
    rule = RULES.get(key, {})
    terms = tuple(rule.get("positive", ())) + tuple(rule.get("negative", ()))
    context_hit = any(token in text for token in tokens)
    rule_hit = any(term and term.lower() in text.lower() for term in terms)
    if key.startswith("F1."):
        return context_hit or rule_hit
    return context_hit or rule_hit


def _gap_target_key(target: dict[str, Any]) -> str:
    return f"{target.get('factor_key')}.{target.get('subfactor_key')}"


def _gap_priority(target: dict[str, Any]) -> float:
    key = _gap_target_key(target)
    maximum = float(target.get("maximum") or 0)
    status = str(target.get("original_status") or "")
    status_bonus = 4 if status == "需人工确认" else 2
    return GAP_PRIORITY.get(key, 70) + maximum * 4 + status_bonus


def _select_gap_targets(targets: list[dict[str, Any]], limit: int = MAX_GAP_TARGETS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select high-value gaps while reserving one search slot per factor."""
    if limit <= 0:
        return [], list(targets)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        grouped.setdefault(str(target.get("factor_key") or ""), []).append(target)
    ranked_groups = {
        factor: sorted(items, key=_gap_priority, reverse=True)
        for factor, items in grouped.items()
    }
    selected_keys: set[str] = set()
    selected: list[dict[str, Any]] = []
    for factor in ("F1", "F2", "F3", "F4", "F5"):
        candidate = ranked_groups.get(factor, [])
        if candidate and len(selected) < limit:
            selected.append(candidate[0])
            selected_keys.add(_gap_target_key(candidate[0]))
    ranked_all = sorted(targets, key=_gap_priority, reverse=True)
    for target in ranked_all:
        key = _gap_target_key(target)
        if key in selected_keys or len(selected) >= limit:
            continue
        selected.append(target)
        selected_keys.add(key)
    selected.sort(key=_gap_priority, reverse=True)
    skipped = [target for target in targets if _gap_target_key(target) not in selected_keys]
    return selected, skipped


def _allocate_gap_budgets(targets: list[dict[str, Any]], total_seconds: float = MAX_GAP_BUDGET_SECONDS) -> dict[str, float]:
    if not targets:
        return {}
    total = max(0.0, float(total_seconds))
    reserve = min(MIN_TARGET_BUDGET_SECONDS, total / len(targets))
    weights = { _gap_target_key(target): max(1.0, _gap_priority(target)) for target in targets }
    remaining = max(0.0, total - reserve * len(targets))
    weight_total = sum(weights.values()) or 1.0
    return {
        key: round(reserve + remaining * weight / weight_total, 3)
        for key, weight in weights.items()
    }


def _collect_gap_target(
    code: str,
    name: str,
    context: str,
    target: dict[str, Any],
    provider: str,
    timeout: float,
    deadline: float,
    target_budget: float,
) -> dict[str, Any]:
    factor_key = str(target.get("factor_key") or "")
    subfactor_key = str(target.get("subfactor_key") or "")
    key = f"{factor_key}.{subfactor_key}"
    if key not in RULES or factor_key == "F6":
        return {"key": key, "processed": False, "gap_result": None, "results": [], "providers": [], "errors": []}

    target_started = time.monotonic()
    target_deadline = min(deadline, target_started + target_budget)
    target_rows: list[dict[str, Any]] = []
    target_queries = queries_for(key, name, code, context)
    target_queries.append(_china_finance_query(key, name, code, context))
    target_queries = target_queries[:MAX_GAP_QUERIES_PER_TARGET]
    target_errors: list[str] = []
    used_providers: list[str] = []
    all_results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for query_index, query in enumerate(target_queries):
        remaining = min(deadline, target_deadline) - time.monotonic()
        if remaining <= 0:
            target_errors.append(
                "global_budget_exhausted"
                if time.monotonic() >= deadline
                else "target_budget_exhausted"
            )
            break
        queries_left = max(1, len(target_queries) - query_index)
        if remaining < 0.2:
            target_errors.append(
                "global_budget_exhausted"
                if time.monotonic() >= deadline
                else "target_budget_exhausted"
            )
            break
        query_timeout = min(timeout, max(0.2, remaining / queries_left))
        used, rows, query_errors = _search(provider, query, query_timeout, cache_scope=f"{code}|{key}")
        relevant = [row for row in rows if _gap_relevant(row, key, code, name, context)]
        if used == "searxng" and not relevant and provider == "auto" and os.getenv("DDG_MCP_URL", "").strip():
            fallback_remaining = min(deadline, target_deadline) - time.monotonic()
            fallback_timeout = min(timeout, max(0.2, fallback_remaining / 2)) if fallback_remaining >= 0.2 else 0
            fallback_used, fallback_rows, fallback_errors = (
                _search("duckduckgo", query, fallback_timeout, cache_scope=f"{code}|{key}")
                if fallback_timeout > 0 else (
                    "none",
                    [],
                    ["global_budget_exhausted" if time.monotonic() >= deadline else "target_budget_exhausted"],
                )
            )
            query_errors.extend(fallback_errors)
            if fallback_rows:
                used = fallback_used
                relevant = [row for row in fallback_rows if _gap_relevant(row, key, code, name, context)]
        target_errors.extend(query_errors)
        if used != "none" and used not in used_providers:
            used_providers.append(used)
        for rank, row in enumerate(relevant[:5], 1):
            url = str(row.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            remaining = min(deadline, target_deadline) - time.monotonic()
            fetch_status, content = (
                _fetch_page(url, min(timeout, 5, max(0.2, remaining)))
                if rank == 1 and remaining > 0 else (
                    "global_budget_exhausted" if time.monotonic() >= deadline else "target_budget_exhausted",
                    "",
                )
            )
            enriched = {
                **row,
                "factor_key": factor_key,
                "subfactor_key": subfactor_key,
                "query": query,
                "provider": used,
                "rank": rank,
                "fetch_status": fetch_status,
                "content_excerpt": content[:6000] if content else "",
            }
            target_rows.append(enriched)
            all_results.append({field: value for field, value in enriched.items() if field != "content_excerpt"})

    if target_rows:
        assessment = evaluate_gap(key, float(target.get("maximum") or 0), target_rows)
    else:
        hard_errors = [error for error in target_errors if not error.endswith(":no_results")]
        assessment = {
            "status": "搜索失败，需人工确认" if hard_errors else "已搜索未命中",
            "score": 0.0,
            "reason": "；".join(hard_errors[:4]) if hard_errors else "SearXNG 与 DuckDuckGo MCP 均未返回相关结果",
            "signals": [],
            "conflict": False,
        }
    evidence_rows = [
        {field: row.get(field) for field in ("title", "url", "snippet", "provider", "rank", "fetch_status", "query")}
        for row in target_rows[:5]
    ]
    gap_result = {
        **target,
        **assessment,
        "queries": target_queries,
        "provider": next((row.get("provider") for row in target_rows if row.get("provider")), "none"),
        "evidence": evidence_rows,
        "errors": target_errors,
        "selection_status": "selected",
        "selection_reason": "按因子覆盖、风险优先级、最大分值和缺口状态选入",
        "target_budget_seconds": target_budget,
        "budget_used_seconds": round(min(target_budget, max(0.0, time.monotonic() - target_started)), 3),
    }
    return {
        "key": key,
        "processed": True,
        "gap_result": gap_result,
        "results": all_results,
        "providers": used_providers,
        "errors": target_errors,
    }


def _collect_gap_targets(code: str, name: str, context: str, targets: list[dict[str, Any]],
                         provider: str, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + MAX_GAP_BUDGET_SECONDS
    gap_results: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    errors: list[str] = []
    used_providers: list[str] = []
    selected_targets, skipped_targets = _select_gap_targets(targets)
    target_budgets = _allocate_gap_budgets(selected_targets)
    jobs = [
        (
            code,
            name,
            context,
            target,
            provider,
            timeout,
            deadline,
            target_budgets.get(_gap_target_key(target), MIN_TARGET_BUDGET_SECONDS),
        )
        for target in selected_targets
    ]
    with _search_cache_batch():
        with ThreadPoolExecutor(max_workers=min(MAX_GAP_WORKERS, len(jobs) or 1)) as executor:
            target_outputs = list(executor.map(lambda values: _collect_gap_target(*values), jobs))
    processed_targets = [item["key"] for item in target_outputs if item["processed"]]
    for item in target_outputs:
        if item["gap_result"] is not None:
            gap_results.append(item["gap_result"])
        all_results.extend(item["results"])
        for used in item["providers"]:
            if used not in used_providers:
                used_providers.append(used)
        errors.extend(f"{item['key']}:{error}" for error in item["errors"])
    for target in skipped_targets:
        factor_key = str(target.get("factor_key") or "")
        subfactor_key = str(target.get("subfactor_key") or "")
        key = f"{factor_key}.{subfactor_key}"
        gap_results.append({
            **target,
            "status": "搜索失败，需人工确认",
            "score": 0.0,
            "reason": f"超过缺口目标数量上限 {MAX_GAP_TARGETS}，本目标未分配搜索预算",
            "signals": [],
            "conflict": False,
            "queries": [],
            "provider": "none",
            "evidence": [],
            "errors": ["target_limit_exceeded"],
            "selection_status": "skipped",
            "selection_reason": "超过缺口目标数量上限，优先保障各因子和高价值风险缺口",
            "skip_reason": "target_limit_exceeded",
            "target_budget_seconds": 0.0,
            "budget_used_seconds": 0.0,
        })
        errors.append(f"{key}:target_limit_exceeded")
    used_seconds = round(min(MAX_GAP_BUDGET_SECONDS, max(0.0, time.monotonic() - started)), 3)
    exhausted = used_seconds >= MAX_GAP_BUDGET_SECONDS or any(
        "global_budget_exhausted" in error for error in errors
    )
    return {
        "web_research_status": "completed" if gap_results else "unavailable",
        "web_research_provider": ",".join(used_providers) or "none",
        "web_gap_targets": targets,
        "web_gap_results": gap_results,
        "web_subfactor_results": {f"{item['factor_key']}.{item['subfactor_key']}": item for item in gap_results},
        "results": all_results,
        "errors": errors,
        "search_budget": {
            "budget_total_seconds": MAX_GAP_BUDGET_SECONDS,
            "budget_used_seconds": used_seconds,
            "target_limit": MAX_GAP_TARGETS,
            "targets_total": len(targets),
            "targets_selected": len(processed_targets),
            "targets_skipped": len(skipped_targets),
            "selection_policy": "各 F1-F5 至少保留一个机会，其余按风险、最大分值、缺口状态和因子优先级排序",
            "allocation_policy": f"每个入选目标至少 {MIN_TARGET_BUDGET_SECONDS:g} 秒，剩余时间按优先级加权分配",
            "global_time_exhausted": exhausted,
            "skip_reasons": {
                "target_limit_exceeded": len(skipped_targets),
                "global_time_exhausted": sum(
                    "global_budget_exhausted" in item.get("errors", [])
                    for item in gap_results
                ),
                "target_time_exhausted": sum(
                    "target_budget_exhausted" in item.get("errors", [])
                    for item in gap_results
                ),
            },
        },
    }


def _read_pdf_text(body: bytes) -> str:
    parts: list[str] = []
    text_length = 0
    reader = PdfReader(BytesIO(body))
    for page in reader.pages[:MAX_PDF_PAGES]:
        page_text = page.extract_text() or ""
        if not page_text:
            continue
        parts.append(page_text)
        text_length += len(page_text) + 1
        if text_length >= MAX_PDF_TEXT_CHARS:
            break
    return " ".join(parts)[:MAX_PDF_TEXT_CHARS]


def _pdf_text_worker(body: bytes, connection: Any) -> None:
    try:
        connection.send(("ok", _read_pdf_text(body)))
    except Exception as exc:
        connection.send((f"pdf_{type(exc).__name__}", ""))
    finally:
        connection.close()


def _extract_pdf_text(body: bytes, timeout: float) -> tuple[str, str]:
    if timeout <= 0:
        return "pdf_timeout", ""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(target=_pdf_text_worker, args=(body, send_connection))
    process.daemon = True
    process.start()
    send_connection.close()
    try:
        if receive_connection.poll(timeout):
            status, text = receive_connection.recv()
            process.join(0.2)
            return status, text
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
        return "pdf_timeout", ""
    finally:
        receive_connection.close()
        if process.is_alive():
            process.terminate()
            process.join(1)


def _fetch_page_uncached(url: str, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + max(0.0, timeout)
    current = url
    try:
        for _ in range(4):
            if not _safe_public_url(current):
                return "unsafe_url", ""
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", ""
            response = _http_session().get(
                current,
                headers={"User-Agent": USER_AGENT},
                timeout=remaining,
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            is_pdf = "application/pdf" in content_type or urlparse(current).path.lower().endswith(".pdf")
            byte_limit = MAX_PDF_FETCH_BYTES if is_pdf else MAX_FETCH_BYTES
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(32_768):
                if time.monotonic() >= deadline:
                    return "timeout", ""
                total += len(chunk)
                if total > byte_limit:
                    break
                chunks.append(chunk)
            body = b"".join(chunks)
            if total > byte_limit:
                return "document_too_large", ""
            if is_pdf:
                remaining = deadline - time.monotonic()
                status, text = _extract_pdf_text(body, remaining)
                if status != "ok":
                    return status, ""
                return ("ok", text[:120_000]) if text.strip() else ("pdf_no_text", "")
            encoding = response.encoding or "utf-8"
            html = body.decode(encoding, errors="replace")
            parser = _TextExtractor()
            parser.feed(html)
            return "ok", " ".join(parser.parts)[:120_000]
        return "too_many_redirects", ""
    except Exception as exc:
        return type(exc).__name__, ""


def _fetch_page(url: str, timeout: float) -> tuple[str, str]:
    started = time.monotonic()
    with _PAGE_SNAPSHOT_LOCK:
        cached = _PAGE_SNAPSHOT.get(url)
        if cached is not None:
            return cached
        event = _PAGE_INFLIGHT.get(url)
        if event is None:
            event = threading.Event()
            _PAGE_INFLIGHT[url] = event
            owner = True
        else:
            owner = False
    if not owner:
        if event.wait(max(0.0, timeout)):
            with _PAGE_SNAPSHOT_LOCK:
                cached = _PAGE_SNAPSHOT.get(url)
            if cached is not None:
                return cached
            remaining = timeout - (time.monotonic() - started)
            if remaining > 0:
                return _fetch_page(url, remaining)
        return "timeout", ""

    result = _fetch_page_uncached(url, timeout)
    with _PAGE_SNAPSHOT_LOCK:
        if result[0] == "ok" and result[1]:
            _PAGE_SNAPSHOT[url] = result
        _PAGE_INFLIGHT.pop(url, None)
        event.set()
    return result


def _reset_run_snapshot() -> None:
    with _PAGE_SNAPSHOT_LOCK:
        _PAGE_SNAPSHOT.clear()
        for event in _PAGE_INFLIGHT.values():
            event.set()
        _PAGE_INFLIGHT.clear()


def _classify(record: dict[str, Any], name: str, context: str = "") -> dict[str, Any]:
    text = str(record.get("content", ""))
    categories = [category for category, terms in SUPPLY_CATEGORIES.items() if any(term in text for term in terms)]
    tightening = any(term in text for term in TIGHTENING_TERMS)
    loosening = any(term in text for term in LOOSENING_TERMS)
    company_relation = bool(name and name in text and any(term in text for term in COMPANY_RELATION_TERMS) and any(term in text for term in REPLACEMENT_TERMS))
    industry_dependency = any(term in text for term in DEPENDENCY_TERMS) and any(term in text for term in REPLACEMENT_TERMS)
    company_named = bool(name and name in text)
    audit_hits = [match.group(0) for pattern in AUDIT_RISK_PATTERNS for match in re.finditer(pattern, text)]
    goodwill_hits = [match.group(0) for pattern in GOODWILL_RISK_PATTERNS for match in re.finditer(pattern, text)]
    risk_signals = {
        "delisting": [term for term in DELISTING_TERMS if term in text],
        "audit": list(dict.fromkeys(audit_hits)),
        "goodwill": list(dict.fromkeys(goodwill_hits)),
    }
    specialized_labels = [term for term in SPECIALIZED_TERMS if term in text]
    catalyst_categories = [category for category, terms in CATALYST_CATEGORIES.items() if any(term in text for term in terms)]
    evidence_date = _extract_evidence_date(record, text)
    domain = _domain(record.get("url", ""))
    source_role, source_tier = _source_role(domain)
    context_tokens = {
        token for token in re.split(r"[\s、,，/|]+", context)
        if len(token) >= 2 and not token.isdigit()
    }
    capex_up = any(term in text for term in CAPEX_UP_TERMS)
    capex_down = any(term in text for term in CAPEX_DOWN_TERMS)
    capex_categories = [category for category, terms in CAPEX_CATEGORIES.items() if any(term in text for term in terms)]
    return {
        **record,
        "domain": domain,
        "source_tier": source_tier,
        "source_role": source_role,
        "supply_categories": categories,
        "supply_direction": "tightening" if tightening and not loosening else "loosening" if loosening and not tightening else "conflict" if tightening and loosening else "unknown",
        "company_product_relation": company_relation,
        "industry_dependency": industry_dependency,
        "company_named": company_named,
        "risk_signals": risk_signals,
        "specialized_labels": specialized_labels,
        "catalyst_categories": catalyst_categories,
        "evidence_date": evidence_date,
        "evidence_fresh": _is_fresh_date(evidence_date),
        "industry_context_match": any(token in text for token in context_tokens),
        "industry_capex_direction": "up" if capex_up and not capex_down else "down" if capex_down and not capex_up else "conflict" if capex_up and capex_down else "unknown",
        "capex_categories": capex_categories,
    }


def _extract_evidence_date(record: dict[str, Any], text: str) -> str:
    candidates = [str(record.get("date") or ""), text[:6000]]
    for candidate in candidates:
        match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", candidate)
        if not match:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            continue
    return ""


def _is_fresh_date(value: str, days: int = 365) -> bool:
    if not value:
        return False
    try:
        age = (date.today() - datetime.strptime(value, "%Y-%m-%d").date()).days
    except ValueError:
        return False
    return 0 <= age <= days


def _validate_supply(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in records if _confirmable(row) and row.get("supply_categories") and row.get("supply_direction") in {"tightening", "loosening"}]
    domains = {row["domain"] for row in usable}
    categories = {category for row in usable for category in row["supply_categories"]}
    has_authority = any(row["source_tier"] == "A" for row in usable)
    directions = {row["supply_direction"] for row in usable}
    # Require independent domains and independent evidence categories. A
    # single article repeating the same signal cannot confirm a cycle.
    confirmed = len(domains) >= 2 and len(categories) >= 2 and has_authority and len(directions) == 1
    category_directions: dict[str, set[str]] = {}
    for row in usable:
        for category in row.get("supply_categories", []):
            category_directions.setdefault(category, set()).add(row["supply_direction"])
    category_conflict = any(len(values) > 1 for values in category_directions.values())
    if category_conflict:
        confirmed = False
    return {
        "status": "已验证" if confirmed else "证据冲突" if len(directions) > 1 or category_conflict else "需人工确认",
        "evidence_count": len(usable),
        "domain_count": len(domains),
        "categories": sorted(categories),
        "has_authority": has_authority,
        "tightening": next(iter(directions)) == "tightening" if confirmed else None,
        "reason": "两个不同域名、两类证据且含权威来源同向" if confirmed else "未满足双域名、双类别、权威来源和同向要求，或同一类别方向冲突",
    }


def _validate_chokepoint(records: list[dict[str, Any]]) -> dict[str, Any]:
    company_rows = [row for row in records if _confirmable(row) and row.get("company_product_relation")]
    industry_rows = [row for row in records if _confirmable(row) and row.get("industry_dependency")]
    domains = {row["domain"] for row in company_rows + industry_rows}
    has_authority = any(row["source_tier"] == "A" for row in company_rows + industry_rows)
    confirmed = bool(company_rows and industry_rows and len(domains) >= 2 and has_authority)
    return {
        "status": "已验证" if confirmed else "需人工确认",
        "company_evidence_count": len(company_rows),
        "industry_evidence_count": len(industry_rows),
        "domain_count": len(domains),
        "has_authority": has_authority,
        "score": 80 if confirmed else None,
        "reason": "公司产品关系与行业进口依赖由不同来源交叉确认" if confirmed else "缺少公司产品关系、行业依赖、独立域名或权威来源",
    }


def _validate_risk(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A" and row.get("company_named")
        and any(row.get("risk_signals", {}).values())
    ]
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "st_risk": any(row.get("risk_signals", {}).get("delisting") for row in usable) or None,
        "audit_risk": any(row.get("risk_signals", {}).get("audit") for row in usable) or None,
        "goodwill_risk": any(row.get("risk_signals", {}).get("goodwill") for row in usable) or None,
        "reason": "权威正文命中公司退市、审计或商誉风险" if usable else "未取得命中公司名称和风险事项的权威正文；不能以无搜索结果证明无风险",
    }


def _validate_specialized(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("company_named") and row.get("specialized_labels")
    ]
    labels = sorted({label for row in usable for label in row.get("specialized_labels", [])})
    strength = 1.0 if any(label in {"专精特新小巨人", "制造业单项冠军", "单项冠军"} for label in labels) else 0.75 if labels else None
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "labels": labels,
        "strength": strength,
        "reason": "政府、协会或交易所权威正文确认公司资质" if usable else "缺少同时包含公司名称和资质名称的权威正文",
    }


def _validate_catalysts(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("company_named") and row.get("catalyst_categories") and row.get("evidence_fresh")
    ]
    categories = sorted({category for row in usable for category in row.get("catalyst_categories", [])})
    return {
        "status": "已验证" if usable else "需人工确认",
        "evidence_count": len(usable),
        "verified_count": min(2, len(categories)) if usable else None,
        "categories": categories,
        "reason": "一年内权威正文确认公司具体催化事件" if usable else "缺少公司关系、权威正文、具体事件或有效日期",
    }


def _validate_industry_capex(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in records
        if _confirmable(row) and row.get("source_tier") == "A"
        and row.get("industry_context_match") and row.get("evidence_fresh")
        and row.get("industry_capex_direction") in {"up", "down"}
    ]
    domains = {row.get("domain") for row in usable if row.get("domain")}
    directions = {row["industry_capex_direction"] for row in usable}
    # Industry capex is only confirmable when the sources are both fresh and
    # independent. Require two distinct authority domains and two evidence
    # categories (investment/expansion/equipment) where available.
    capex_categories = {category for row in usable for category in row.get("capex_categories", [])}
    confirmed = len(domains) >= 2 and len(directions) == 1 and (len(capex_categories) >= 2 or not capex_categories)
    direction = next(iter(directions)) if confirmed else None
    return {
        "status": "已验证" if confirmed else "证据冲突" if len(directions) > 1 else "需人工确认",
        "evidence_count": len(usable),
        "domain_count": len(domains),
        "signal": "上行" if direction == "up" else "下行" if direction == "down" else None,
        "reason": "两家独立权威来源的一年内正文同向确认行业投资" if confirmed else "未满足行业匹配、有效日期、双权威域名、双类别和同向要求",
    }


def collect(code: str, name: str, context: str, provider: str | None = None, timeout: float = 12,
            targets: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    _reset_run_snapshot()
    selected = (provider or os.getenv("MODA_SEARCH_PROVIDER", "auto")).strip().lower()
    if selected not in {"auto", "searxng", "duckduckgo", "off"}:
        selected = "off"
    if selected == "off":
        return {"web_research_status": "disabled", "web_research_provider": "off", "queries": [], "results": [], "errors": []}
    classification_db = lookup(code, name)
    database_specialized = has_category(classification_db, "specialized")
    database_leadership = (
        has_category(classification_db, "leadership")
        or has_category(classification_db, "core_supplier")
    )
    if targets is not None:
        skipped_database_targets = [
            target for target in targets
            if (
                database_specialized
                and target.get("factor_key") == "F3"
                and target.get("subfactor_key") == "specialized"
            )
            or (
                database_leadership
                and target.get("factor_key") == "F3"
                and target.get("subfactor_key") == "leadership"
            )
        ]
        filtered_targets = [
            target for target in targets
            if target not in skipped_database_targets
        ]
        result = _collect_gap_targets(code, name, context, filtered_targets, selected, timeout)
        result["classification_db_found"] = bool(classification_db.get("found"))
        result["classification_db_categories"] = list(classification_db.get("categories") or ())
        result["classification_db_specialized"] = database_specialized
        result["classification_db_leadership"] = database_leadership
        result["database_skipped_targets"] = skipped_database_targets
        return result

    short_context = " ".join(context.split()[:12])
    query_specs = [
        ("supply", f"{name} {short_context} 供不应求 订单 产能 库存"),
        ("chokepoint", f"{name} {short_context} 国产替代 核心供应商 进口依赖"),
        ("chokepoint", f"site:cninfo.com.cn {name} 订单 产能 国产替代"),
        ("risk", f"site:cninfo.com.cn {name} 退市 审计意见 商誉减值"),
        ("risk", f"site:szse.cn {code} {name} 风险警示 审计 商誉"),
        ("specialized", f"site:gov.cn {name} 专精特新 小巨人 单项冠军"),
        ("specialized", f"site:miit.gov.cn {name} 专精特新 单项冠军"),
        ("capex", f"site:stats.gov.cn {short_context} 固定资产投资 投资增长 产能"),
        ("capex", f"site:miit.gov.cn OR site:ndrc.gov.cn {short_context} 投资 扩产 设备更新"),
        ("finance_disclosure", f"{name} {short_context} {CHINA_FINANCE_SITE_GROUPS['disclosure']} 年报 季报 公告 主营业务"),
        ("finance_market", f"{name} {short_context} {CHINA_FINANCE_SITE_GROUPS['market']} 订单 价格 产能 估值"),
    ]
    if database_specialized or database_leadership:
        query_specs = [
            item for item in query_specs
            if not (database_specialized and item[0] == "specialized")
            and not (database_leadership and item[0] == "leadership")
        ]
    queries = [query for _, query in query_specs]
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    used_providers: list[str] = []
    seen: set[str] = set()
    purpose_counts: dict[str, int] = {}
    for purpose, query in query_specs:
        used, rows, query_errors = _search(selected, query, timeout)
        errors.extend(f"{query[:24]}:{error}" for error in query_errors)
        if used != "none" and used not in used_providers:
            used_providers.append(used)
        for row in rows:
            url = row.get("url", "")
            if (not url or url in seen or len(results) >= MAX_PAGES
                    or purpose_counts.get(purpose, 0) >= MAX_PAGES_PER_PURPOSE):
                continue
            seen.add(url)
            purpose_counts[purpose] = purpose_counts.get(purpose, 0) + 1
            fetch_status, content = _fetch_page(url, timeout)
            classified = _classify({**row, "purpose": purpose, "query": query, "fetch_status": fetch_status, "content": content}, name, context)
            classified.pop("content", None)
            results.append(classified)

    supply = _validate_supply(results)
    chokepoint = _validate_chokepoint(results)
    risk = _validate_risk(results)
    specialized = _validate_specialized(results)
    industry_capex = _validate_industry_capex(results)
    status = "completed" if results else "unavailable"
    return {
        "web_research_status": status,
        "web_research_provider": ",".join(used_providers) or "none",
        "classification_db_found": bool(classification_db.get("found")),
        "classification_db_categories": list(classification_db.get("categories") or ()),
        "classification_db_specialized": database_specialized,
        "classification_db_leadership": database_leadership,
        "queries": queries,
        "results": results,
        "errors": errors,
        "web_supply_validation": supply,
        "web_chokepoint_validation": chokepoint,
        "web_risk_validation": risk,
        "web_specialized_validation": specialized,
        "web_industry_capex_validation": industry_capex,
    }


def build_report(code: str, name: str, data: dict[str, Any]) -> str:
    if data.get("web_gap_results") is not None:
        lines = [
            f"# 定向搜索补缺：{name or code}（{code}）",
            "",
            f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  后端：{data.get('web_research_provider', 'none')}",
            "",
            f"<!-- moda_web_research: {json.dumps(data, ensure_ascii=False)} -->",
            "",
            "| 因子 | 子因子 | 原状态 | 选择状态 | 搜索结果 | 未核验得分 | 目标预算 | 实际用时 | 后端 | 判断依据 |",
            "|---|---|---|---|---|---:|---:|---:|---|---|",
        ]
        for item in data.get("web_gap_results", []):
            reason = str(item.get("reason") or "").replace("|", "/")
            lines.append(
                f"| {item.get('factor_key')} | {item.get('label')} | {item.get('original_status')} | "
                f"{item.get('selection_status', 'selected')} | {item.get('status')} | "
                f"{item.get('score', 0):g}/{item.get('maximum', 0):g} | "
                f"{item.get('target_budget_seconds', 0):g} | {item.get('budget_used_seconds', 0):g} | "
                f"{item.get('provider', 'none')} | {reason} |"
            )
        lines += ["", "## 搜索明细", "", "| 子因子 | 标题 | URL | 查询词 | 后端 |", "|---|---|---|---|---|"]
        for item in data.get("web_gap_results", []):
            for row in item.get("evidence", []):
                title = str(row.get("title") or "").replace("|", "/")
                query = str(row.get("query") or "").replace("|", "/")
                lines.append(f"| {item.get('factor_key')}.{item.get('subfactor_key')} | {title} | {row.get('url', '')} | {query} | {row.get('provider', '')} |")
        budget = data.get("search_budget") or {}
        lines += [
            "",
            "## 搜索预算",
            "",
            f"- 总预算：{budget.get('budget_total_seconds', MAX_GAP_BUDGET_SECONDS):g} 秒；实际使用：{budget.get('budget_used_seconds', 0):g} 秒。",
            f"- 缺口目标：共 {budget.get('targets_total', 0)} 个；入选 {budget.get('targets_selected', 0)} 个；未入选 {budget.get('targets_skipped', 0)} 个；全局时间耗尽：{'是' if budget.get('global_time_exhausted') else '否'}。",
            f"- 分配规则：{budget.get('selection_policy', '按优先级选择')}；{budget.get('allocation_policy', '按优先级分配剩余时间')}。",
            f"- 预算原因：目标数量上限 {((budget.get('skip_reasons') or {}).get('target_limit_exceeded', 0))} 个；全局时间耗尽 {((budget.get('skip_reasons') or {}).get('global_time_exhausted', 0))} 个；单目标时间耗尽 {((budget.get('skip_reasons') or {}).get('target_time_exhausted', 0))} 个。",
            "",
            "搜索结果只用于未核验补缺；结构化数据优先，F6 不使用网页补分。",
            "",
        ]
        return "\n".join(lines)
    lines = [
        f"# 搜索交叉验证：{name or code}（{code}）",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  后端：{data.get('web_research_provider', 'none')}",
        "",
        f"<!-- moda_web_research: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 运行状态：{data.get('web_research_status')}",
        f"- 供需验证：{data.get('web_supply_validation', {}).get('status', '需人工确认')}；{data.get('web_supply_validation', {}).get('reason', '搜索未运行')}",
        f"- 国产替代验证：{data.get('web_chokepoint_validation', {}).get('status', '需人工确认')}；{data.get('web_chokepoint_validation', {}).get('reason', '搜索未运行')}",
        f"- 退市/审计/商誉验证：{data.get('web_risk_validation', {}).get('status', '需人工确认')}；{data.get('web_risk_validation', {}).get('reason', '搜索未运行')}",
        f"- 专精特新/单项冠军验证：{data.get('web_specialized_validation', {}).get('status', '需人工确认')}；{data.get('web_specialized_validation', {}).get('reason', '搜索未运行')}",
        f"- 行业资本开支验证：{data.get('web_industry_capex_validation', {}).get('status', '需人工确认')}；{data.get('web_industry_capex_validation', {}).get('reason', '搜索未运行')}",
        "",
        "| 用途 | 来源角色 | 来源等级 | 标题 | 域名 | 正文 | 证据日期 | 查询词 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in data.get("results", []):
        title = str(row.get("title", "")).replace("|", "/")
        query = str(row.get("query", "")).replace("|", "/")
        lines.append(f"| {row.get('purpose', '')} | {row.get('source_role', '一般来源')} | {row.get('source_tier', 'B')} | [{title}]({row.get('url', '')}) | {row.get('domain', '')} | {row.get('fetch_status', '')} | {row.get('evidence_date', '') or '未识别'} | {query} |")
    if not data.get("results"):
        lines.append("| - | - | - | 无可核验结果 | - | - | - | - |")
    lines += ["", "法定信息披露平台正文可作为高确信度证据；雪球、东方财富、大智慧等金融论坛只收集线索，不参与确认或计分。搜索摘要只用于发现线索；正文未成功读取或未通过交叉验证时不得计分。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate framework evidence with optional web search")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--targets-json", default="")
    parser.add_argument("--refresh", action="store_true", help="Bypass same-day successful search cache")
    args = parser.parse_args()
    if args.refresh:
        os.environ["MODA_SEARCH_CACHE"] = "off"
    code = args.stock.strip()
    targets = json.loads(args.targets_json) if args.targets_json else None
    with _search_cache_batch():
        data = collect(code, args.name or code, args.context, args.provider, targets=targets)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
