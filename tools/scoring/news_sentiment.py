from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
from pathlib import Path
import re
import time
from typing import Any, Callable

import requests

from tools.scoring.sentiment_engine import SentimentScorer


ROOT = Path(__file__).resolve().parents[2]
CACHE_BASE = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "news_sentiment"
CACHE_TTL = 600
FETCH_TIMEOUT = 8
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
RUMOR_TERMS = ("谣言", "辟谣", "澄清", "虚假", "操纵", "荐股骗局", "杀猪盘")


def _clean(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", text).strip()


def _normalized_title(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _get(url: str, timeout: float = FETCH_TIMEOUT) -> requests.Response:
    response = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=timeout)
    response.raise_for_status()
    return response


def _jin10(limit: int = 40) -> list[dict[str, Any]]:
    response = _get("https://www.jin10.com/flash_newest.js")
    match = re.search(r"var newest\s*=\s*(\[.*?\]);", response.text, re.S)
    if not match:
        return []
    rows = json.loads(match.group(1))
    output = []
    for row in rows[:limit]:
        data = row.get("data") if isinstance(row, dict) else {}
        data = data if isinstance(data, dict) else {}
        body = _clean(str(data.get("content") or ""))
        title = _clean(str(data.get("title") or body[:100]))
        if title:
            output.append({"source": "金十快讯", "title": title[:200], "text": body[:500], "url": "https://www.jin10.com/", "published_at": str(row.get("time") or "")})
    return output


def _eastmoney(limit: int = 50) -> list[dict[str, Any]]:
    response = _get("https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html")
    response.encoding = response.apparent_encoding or "utf-8"
    match = re.search(r"var ajaxResult\s*=\s*(\{.*\})\s*;?\s*$", response.text, re.S)
    if not match:
        return []
    payload = json.loads(match.group(1))
    output = []
    for row in (payload.get("LivesList") or [])[:limit]:
        title = _clean(str(row.get("title") or row.get("digest") or ""))
        body = _clean(str(row.get("digest") or ""))
        if title:
            output.append({"source": "东方财富快讯", "title": title[:200], "text": body[:500], "url": str(row.get("url_w") or row.get("url_mobile") or ""), "published_at": str(row.get("showtime") or "")})
    return output


def _tonghuashun(limit: int = 40) -> list[dict[str, Any]]:
    response = _get("http://news.10jqka.com.cn/today_list/")
    response.encoding = response.apparent_encoding or "gbk"
    output = []
    seen: set[str] = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', response.text, re.I | re.S):
        title = _clean(match.group(2))
        key = _normalized_title(title)
        if len(title) < 8 or not key or key in seen:
            continue
        seen.add(key)
        output.append({"source": "同花顺快讯", "title": title[:200], "text": title[:500], "url": match.group(1), "published_at": ""})
        if len(output) >= limit:
            break
    return output


FETCHERS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "jin10": _jin10,
    "eastmoney": _eastmoney,
    "tonghuashun": _tonghuashun,
}


def _read_cache(path: Path) -> tuple[list[dict[str, Any]], float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload.get("items") or []), max(0.0, time.time() - float(payload.get("ts") or 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [], float("inf")


def _cached(source: str, fetcher: Callable[[], list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], str, str, str]:
    CACHE_BASE.mkdir(parents=True, exist_ok=True)
    path = CACHE_BASE / f"{source}.json"
    cached_rows, age = _read_cache(path) if path.exists() else ([], float("inf"))
    if cached_rows and age <= CACHE_TTL:
        return cached_rows, "cache", "ok", ""
    try:
        rows = fetcher()
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if cached_rows:
            return cached_rows, "stale_cache", "stale", error
        return [], "failed", "failed", error
    if rows:
        try:
            path.write_text(json.dumps({"ts": time.time(), "items": rows}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return rows, "live", "ok" if rows else "empty", "" if rows else "empty response"


def _relevant(record: dict[str, Any], aliases: list[str]) -> bool:
    text = f"{record.get('title', '')} {record.get('text', '')}"
    return any(alias and alias in text for alias in aliases)


def collect(aliases: list[str]) -> dict[str, Any]:
    statuses: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as executor:
        futures = {executor.submit(_cached, source, fetcher): source for source, fetcher in FETCHERS.items()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                rows, mode, state, error = future.result()
            except Exception as exc:
                rows, mode, state, error = [], "failed", "failed", f"{type(exc).__name__}: {str(exc)[:120]}"
            relevant = [row for row in rows if _relevant(row, aliases)]
            statuses[source] = {"fetch_state": state, "mode": mode, "items": len(rows), "matches": len(relevant), "error": error}
            records.extend(relevant)

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = _normalized_title(str(record.get("title") or record.get("text") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)

    scorer = SentimentScorer()
    scores = []
    positive = negative = neutral = 0
    rumor_hits: set[str] = set()
    for record in unique:
        text = f"{record.get('title', '')}。{record.get('text', '')}"
        result = scorer.score_text(text)
        record["sentiment"] = result["label"]
        record["sentiment_score"] = result["score"]
        scores.append(float(result["score"]))
        positive += result["label"] == "看多"
        negative += result["label"] == "看空"
        neutral += result["label"] == "中性"
        rumor_hits.update(term for term in RUMOR_TERMS if term in text)

    average = sum(scores) / len(scores) if scores else None
    sentiment = "看多" if average is not None and average > 0.1 else "看空" if average is not None and average < -0.1 else "中性" if average is not None else None
    states = [item["fetch_state"] for item in statuses.values()]
    sources_ok = sum(state in {"ok", "stale"} for state in states)
    return {
        "news_posts_total": len(unique),
        "news_sources_checked": len(FETCHERS),
        "news_sources_ok": sources_ok,
        "news_partial": sources_ok < len(FETCHERS) or any(state == "stale" for state in states),
        "news_sentiment": sentiment,
        "news_sentiment_score": round(average, 3) if average is not None else None,
        "news_positive_count": positive,
        "news_negative_count": negative,
        "news_neutral_count": neutral,
        "news_rumor_hits": sorted(rumor_hits),
        "news_records": unique[:30],
        "news_source_status": statuses,
        "fetch_state": "failed" if states and all(state == "failed" for state in states) else "fallback_ok" if any(state in {"failed", "stale"} for state in states) else "empty" if unique == [] and all(state == "empty" for state in states) else "ok",
        "source_chain": [{"source": source, "status": status["fetch_state"], "error": status["error"]} for source, status in statuses.items()],
    }
