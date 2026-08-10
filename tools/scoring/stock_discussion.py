from __future__ import annotations

import argparse
from datetime import datetime
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.scoring.sentiment_engine import SentimentScorer
from tools.scoring.web_research import _search

OUTPUT_BASE = ROOT / "knowledge" / "research" / "stock_discussion"
UA = "moda-v4-discussion/1.0"
PROMOTION_TERMS = ("必涨", "稳赚", "翻倍", "内部消息", "老师带", "加群", "微信群", "VIP", "收费群", "主力建仓", "最后上车", "股神", "跟单")
RUMOR_TERMS = ("谣言", "辟谣", "澄清", "虚假", "操纵", "荐股骗局", "杀猪盘")
MIN_DISCUSSION_SAMPLE = 10
LIMITED_DISCUSSION_SAMPLE = 5


def _json_get(url: str, params: dict[str, Any] | None = None, timeout: float = 5) -> Any:
    response = requests.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _symbol(code: str) -> str:
    return f"{'SH' if code.startswith('6') else 'BJ' if code.startswith(('4', '8')) else 'SZ'}{code}"


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "").replace("&nbsp;", " ").strip()


def _published_at(value: Any) -> str:
    if value in {None, ""}:
        return ""
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _normalized_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())


def _xueqiu(code: str, name: str, count: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    successes = 0
    errors: list[str] = []
    for query in (name, _symbol(code), code):
        try:
            payload = _json_get("https://xueqiu.com/statuses/search.json", {"q": query, "count": count})
            successes += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:80]}")
            continue
        items = payload.get("statuses") or payload.get("list") or []
        for item in items:
            text = _strip_html(str(item.get("text") or item.get("description") or ""))
            key = str(item.get("id") or text[:100])
            if len(text) < 5 or key in seen:
                continue
            seen.add(key)
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            rows.append({
                "source": "xueqiu",
                "title": text[:80],
                "text": text,
                "snippet": text[:300],
                "url": f"https://xueqiu.com/S/{_symbol(code)}",
                "author": user.get("screen_name", ""),
                "likes": item.get("like_count"),
                "replies": item.get("reply_count"),
                "retweets": item.get("retweet_count"),
                "published_at": _published_at(item.get("created_at") or item.get("created_at_ts")),
                "status": "结构化接口",
            })
    if successes == 0 and errors:
        raise RuntimeError("; ".join(errors[:2]))
    return rows[:count]


def _eastmoney(code: str, count: int = 20) -> list[dict[str, Any]]:
    urls = [
        f"https://guba.eastmoney.com/list,{code},99,f.html",
        f"https://guba.eastmoney.com/list,{code}.html",
    ]
    errors: list[str] = []
    successes = 0
    for url in urls:
        try:
            response = requests.get(url, headers={"User-Agent": UA}, timeout=5)
            response.raise_for_status()
            html = response.text
            successes += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:80]}")
            continue
        rows: list[dict[str, Any]] = []
        for match in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*>([^<]{3,120})</a>", html, re.I):
            title = re.sub(r"\s+", " ", _strip_html(match.group(2)))
            href = urljoin("https://guba.eastmoney.com/", match.group(1))
            if not title or title in {row["title"] for row in rows}:
                continue
            if not any(token in href for token in ("/caifuhao.eastmoney.com/news/", "/news,")):
                continue
            rows.append({"source": "eastmoney", "title": title, "text": title, "snippet": title, "url": href, "author": "", "published_at": "", "status": "结构化接口"})
            if len(rows) >= count:
                return rows
        if rows:
            return rows
    if successes == 0 and errors:
        raise RuntimeError("; ".join(errors[:2]))
    return []


def _search_fallback(code: str, name: str, timeout: float = 12) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    providers: list[str] = []
    query = f"{name} {code} 股票 讨论 看多 看空 风险"
    used, rows, search_errors = _search("auto", query, timeout)
    errors.extend(search_errors)
    if used != "none":
        providers.append(used)
    for row in rows[:10]:
        title = str(row.get("title") or "")
        snippet = str(row.get("snippet") or "")
        records.append({
            "source": used,
            "title": title,
            "text": f"{title}。{snippet}".strip("。"),
            "snippet": snippet,
            "url": row.get("url") or "",
            "author": "",
            "status": "网络命中（未核验）",
            "query": query,
            "provider": used,
        })
    return records, providers, errors


def _collect_source(label: str, function) -> tuple[list[dict[str, Any]], dict[str, str]]:
    try:
        rows = function()
        return rows, {"source": label, "status": "ok" if rows else "empty", "error": "" if rows else "empty response"}
    except Exception as exc:
        return [], {"source": label, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _dedupe_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        text = str(record.get("text") or record.get("snippet") or record.get("title") or "")
        key = _normalized_text(text) or str(record.get("url") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique, max(0, len(records) - len(unique))


def _template_cluster_count(records: list[dict[str, Any]]) -> int:
    promotional = [record for record in records if record.get("promotion_hits")]
    used: set[int] = set()
    clusters = 0
    for index, left in enumerate(promotional):
        if index in used:
            continue
        left_text = _normalized_text(str(left.get("text") or left.get("snippet") or ""))
        members = [index]
        for other_index in range(index + 1, len(promotional)):
            if other_index in used:
                continue
            right_text = _normalized_text(str(promotional[other_index].get("text") or promotional[other_index].get("snippet") or ""))
            if left_text and right_text and SequenceMatcher(None, left_text, right_text).ratio() >= 0.82:
                members.append(other_index)
        if len(members) >= 2:
            clusters += 1
            used.update(members)
    return clusters


def _record_weight(record: dict[str, Any]) -> float:
    engagement = sum(float(record.get(key) or 0) for key in ("likes", "replies", "retweets"))
    engagement_weight = 1.0 + min(1.5, math.log1p(max(0.0, engagement)) / 4)
    freshness_weight = 1.0
    published_at = str(record.get("published_at") or "")
    if published_at:
        try:
            published = datetime.fromisoformat(published_at)
            if published.tzinfo is None:
                published = published.astimezone()
            age_days = max(0.0, (datetime.now().astimezone() - published).total_seconds() / 86400)
            freshness_weight = max(0.25, 0.5 ** (age_days / 3))
        except ValueError:
            pass
    return engagement_weight * freshness_weight


def _score(records: list[dict[str, Any]], sample_status: str) -> dict[str, Any]:
    scorer = SentimentScorer()
    promotions: set[str] = set()
    rumors: set[str] = set()
    weighted_total = 0.0
    weight_sum = 0.0
    for record in records:
        text = str(record.get("text") or record.get("snippet") or "")
        result = scorer.score_text(text)
        record["sentiment"] = result["label"]
        record["sentiment_score"] = result["score"]
        record["promotion_hits"] = [term for term in PROMOTION_TERMS if term in text]
        record["rumor_hits"] = [term for term in RUMOR_TERMS if term in text]
        promotions.update(record["promotion_hits"])
        rumors.update(record["rumor_hits"])
        weight = _record_weight(record)
        record["sentiment_weight"] = round(weight, 3)
        weighted_total += float(result["score"]) * weight
        weight_sum += weight
    labels = [item.get("sentiment") for item in records]
    avg = weighted_total / weight_sum if weight_sum else None
    raw_sentiment = "看多" if avg is not None and avg > 0.1 else "看空" if avg is not None and avg < -0.1 else "中性" if avg is not None else None
    sentiment = raw_sentiment if sample_status in {"充分", "有限"} else None
    promotion_records = [record for record in records if record.get("promotion_hits")]
    promotion_sources = sorted({str(record.get("source") or "") for record in promotion_records if record.get("source")})
    authors = sorted({str(record.get("author") or "").strip() for record in records if str(record.get("author") or "").strip()})
    return {
        "discussion_sentiment": sentiment,
        "discussion_sentiment_score": round(avg, 3) if avg is not None else None,
        "discussion_raw_sentiment": raw_sentiment,
        "discussion_sentiment_confidence": "高" if sample_status == "充分" else "低" if sample_status == "有限" else "样本不足",
        "discussion_positive_count": labels.count("看多"),
        "discussion_negative_count": labels.count("看空"),
        "discussion_neutral_count": labels.count("中性"),
        "discussion_promotion_hits": sorted(promotions),
        "discussion_rumor_hits": sorted(rumors),
        "discussion_promotion_record_count": len(promotion_records),
        "discussion_promotion_source_count": len(promotion_sources),
        "discussion_promotion_sources": promotion_sources,
        "discussion_author_count": len(authors),
        "discussion_template_cluster_count": _template_cluster_count(records),
    }


def collect(code: str, name: str, timeout: float = 8) -> dict[str, Any]:
    xueqiu, xueqiu_status = _collect_source("雪球", lambda: _xueqiu(code, name))
    eastmoney, eastmoney_status = _collect_source("东方财富股吧", lambda: _eastmoney(code))
    structured: list[dict[str, Any]] = []
    structured.extend(xueqiu)
    structured.extend(eastmoney)
    providers = [source for source, rows in (("xueqiu", xueqiu), ("eastmoney", eastmoney)) if rows]
    records = list(structured)
    search_errors: list[str] = []
    if not records:
        records, search_providers, search_errors = _search_fallback(code, name, timeout)
        providers.extend(search_providers)
    records, duplicate_count = _dedupe_records(records)
    structured_keys = {
        _normalized_text(str(record.get("text") or record.get("snippet") or record.get("title") or ""))
        for record in structured
    }
    structured_count = sum(
        _normalized_text(str(record.get("text") or record.get("snippet") or record.get("title") or "")) in structured_keys
        for record in records
    ) if structured_keys else 0
    structured_sources_ok = sum(status["status"] == "ok" for status in (xueqiu_status, eastmoney_status))
    if structured_count >= MIN_DISCUSSION_SAMPLE and structured_sources_ok >= 2:
        sample_status = "充分"
    elif structured_count >= LIMITED_DISCUSSION_SAMPLE and structured_sources_ok >= 1:
        sample_status = "有限"
    else:
        sample_status = "样本不足"
    summary = _score(records, sample_status)
    only_no_results = bool(search_errors) and all(item.endswith(":no_results") for item in search_errors)
    structured_partial = structured_sources_ok < 2 or sample_status != "充分"
    source_status = "结构化接口（部分覆盖）" if structured and structured_partial else "结构化接口" if structured else "网络命中（未核验）" if records else "已搜索未命中" if only_no_results else "搜索失败，需人工确认"
    fetch_state = "fallback_ok" if structured and structured_partial else "ok" if structured else "fallback_ok" if records else "empty" if only_no_results else "failed"
    source_chain = [xueqiu_status, eastmoney_status]
    if not structured:
        source_chain.append({"source": "SearXNG/DuckDuckGo", "status": "ok" if records else "failed" if search_errors and not only_no_results else "empty", "error": "; ".join(search_errors)})
    return {
        "discussion_posts_total": len(records),
        "discussion_structured_count": structured_count,
        "discussion_search_count": max(0, len(records) - structured_count),
        "discussion_source_count": len(set(providers)),
        "discussion_source_status": source_status,
        "discussion_source_status_detail": {"xueqiu": xueqiu_status, "eastmoney": eastmoney_status},
        "discussion_sources": providers,
        "discussion_partial": not structured or structured_partial or bool(search_errors),
        "discussion_sample_status": sample_status,
        "discussion_minimum_sample": MIN_DISCUSSION_SAMPLE,
        "discussion_duplicate_count": duplicate_count,
        "discussion_duplicate_ratio": round(duplicate_count / (len(records) + duplicate_count), 3) if records or duplicate_count else 0.0,
        "discussion_records": records[:25],
        "discussion_search_errors": search_errors,
        "fetch_state": fetch_state,
        "source_chain": source_chain,
        **summary,
    }


def build_report(code: str, name: str, data: dict[str, Any]) -> str:
    lines = [
        f"# 个股讨论与情绪：{name}（{code}）",
        "",
        f"> 采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  来源：雪球/东方财富公开接口；失败后 SearXNG → DuckDuckGo MCP",
        "",
        f"<!-- moda_stock_discussion: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 讨论条数：{data.get('discussion_posts_total', 0)}；结构化 {data.get('discussion_structured_count', 0)}；搜索补缺 {data.get('discussion_search_count', 0)}",
        f"- 来源状态：{data.get('discussion_source_status', '需人工确认')}；来源：{'、'.join(data.get('discussion_sources', [])) or '无'}",
        f"- 样本状态：{data.get('discussion_sample_status', '需人工确认')}；去重 {data.get('discussion_duplicate_count', 0)} 条；作者 {data.get('discussion_author_count', 0)} 个",
        f"- 汇总情绪：{data.get('discussion_sentiment') or '样本不足，需人工确认'}（{data.get('discussion_sentiment_score') if data.get('discussion_sentiment_score') is not None else '需人工确认'}；置信度 {data.get('discussion_sentiment_confidence', '需人工确认')}）",
        f"- 看多/中性/看空：{data.get('discussion_positive_count', 0)} / {data.get('discussion_neutral_count', 0)} / {data.get('discussion_negative_count', 0)}",
        f"- 推广话术：{'、'.join(data.get('discussion_promotion_hits') or []) or '无'}；涉及 {data.get('discussion_promotion_record_count', 0)} 条、{data.get('discussion_promotion_source_count', 0)} 个来源；模板簇 {data.get('discussion_template_cluster_count', 0)}",
        f"- 谣言/风险词：{'、'.join(data.get('discussion_rumor_hits') or []) or '无'}",
        "",
        "## 讨论明细",
        "",
        "| 来源 | 情绪 | 标题/摘要 | 状态 |",
        "|---|---|---|---|",
    ]
    for item in data.get("discussion_records", []):
        text = str(item.get("text") or item.get("snippet") or "").replace("|", "/").replace("\n", " ")[:180]
        lines.append(f"| {item.get('source', '-')} | {item.get('sentiment', '需人工确认')} | {text} | {item.get('status', '需人工确认')} |")
    if not data.get("discussion_records"):
        lines.append("| - | 需人工确认 | 未获得个股讨论 | 已搜索未命中或搜索失败 |")
    lines.extend(["", "说明：搜索摘要只作为未核验线索，不覆盖行情、财务和公告结构化证据；未命中不等于安全。", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect stock discussion from public interfaces and search fallback")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    name = args.name or args.stock
    data = collect(args.stock.strip(), name)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{args.stock.strip()}.md"
    path.write_text(build_report(args.stock.strip(), name, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
