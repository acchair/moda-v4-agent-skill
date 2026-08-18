from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = ROOT / "knowledge" / "research" / "social_sentiment"
CACHE_BASE = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "social_hot"
HISTORY_BASE = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "social_history"
CACHE_TTL = 300
HISTORY_TTL = 7 * 86400
FETCH_TIMEOUT = 5
COLLECT_DEADLINE = 35
NEWS_SEARCH_TIMEOUT = 12
UA = "moda-v4-social/1.1"
PROMOTION_TERMS = ("必涨", "稳赚", "翻倍", "内部消息", "老师带", "加群", "主力建仓", "最后上车", "股神", "跟单")
RUMOR_TERMS = ("谣言", "辟谣", "澄清", "虚假", "操纵", "荐股骗局", "杀猪盘")

DISCUSSION_SCRIPT = ROOT / "tools" / "scoring" / "stock_discussion.py"


def _json(url: str, timeout: float = FETCH_TIMEOUT) -> dict:
    response = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _weibo() -> list[dict]:
    rows = (_json("https://weibo.com/ajax/side/hotSearch").get("data") or {}).get("realtime") or []
    return [{"rank": i, "title": row.get("word", ""), "url": f"https://s.weibo.com/weibo?q={quote(row.get('word', ''))}"} for i, row in enumerate(rows[:50], 1) if row.get("word")]


def _zhihu() -> list[dict]:
    rows = _json("https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=50&desktop=true").get("data") or []
    output = []
    for i, row in enumerate(rows[:50], 1):
        target = row.get("target") or {}
        title = (target.get("title_area") or {}).get("text") or target.get("title") or ""
        if title:
            output.append({"rank": i, "title": title, "url": ((target.get("link") or {}).get("url") or "")})
    return output


def _baidu() -> list[dict]:
    cards = (_json("https://top.baidu.com/api/board?platform=wise&tab=realtime").get("data") or {}).get("cards") or []
    rows = (cards[0] or {}).get("content") or [] if cards else []
    if rows and isinstance(rows[0], dict) and isinstance(rows[0].get("content"), list):
        rows = rows[0]["content"]
    return [{"rank": i, "title": row.get("word") or row.get("query") or "", "url": row.get("url") or ""} for i, row in enumerate(rows[:50], 1) if row.get("word") or row.get("query")]


def _douyin() -> list[dict]:
    rows = (_json("https://www.douyin.com/aweme/v1/web/hot/search/list/").get("data") or {}).get("word_list") or []
    return [{"rank": i, "title": row.get("word", ""), "url": f"https://www.douyin.com/search/{quote(row.get('word', ''))}"} for i, row in enumerate(rows[:50], 1) if row.get("word")]


def _toutiao() -> list[dict]:
    rows = _json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc").get("data") or []
    return [{"rank": i, "title": row.get("Title") or row.get("title") or "", "url": ""} for i, row in enumerate(rows[:50], 1) if row.get("Title") or row.get("title")]


def _bilibili() -> list[dict]:
    rows = _json("https://s.search.bilibili.com/main/hotword?limit=50").get("list") or []
    return [{"rank": i, "title": row.get("keyword") or row.get("show_name") or "", "url": ""} for i, row in enumerate(rows[:50], 1) if row.get("keyword") or row.get("show_name")]


FETCHERS: dict[str, Callable[[], list[dict]]] = {
    "weibo": _weibo,
    "zhihu": _zhihu,
    "baidu": _baidu,
    "douyin": _douyin,
    "toutiao": _toutiao,
    "bilibili": _bilibili,
}


def _cached(platform: str, fetcher: Callable[[], list[dict]]) -> tuple[list[dict], str]:
    CACHE_BASE.mkdir(parents=True, exist_ok=True)
    path = CACHE_BASE / f"{platform}.json"
    if path.exists() and time.time() - path.stat().st_mtime <= CACHE_TTL:
        try:
            return json.loads(path.read_text(encoding="utf-8")), "cache"
        except (json.JSONDecodeError, OSError):
            pass
    rows = fetcher()
    if rows:
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows, "live"


def _aliases(name: str, code: str) -> list[str]:
    values = [name.strip(), code]
    compact = name.strip()
    for suffix in ("股份有限公司", "有限责任公司", "股份", "集团"):
        if compact.endswith(suffix) and len(compact.removesuffix(suffix)) >= 3:
            values.append(compact.removesuffix(suffix))
    return list(dict.fromkeys(value for value in values if len(value) >= 3))


def _normalized_topic(value: str, aliases: list[str] | None = None) -> str:
    text = (value or "").lower()
    for alias in sorted(aliases or [], key=len, reverse=True):
        text = text.replace(alias.lower(), "")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _cluster_mentions(mentions: dict[str, list[dict]], aliases: list[str]) -> list[dict]:
    topics: list[dict] = []
    for platform, rows in mentions.items():
        for row in rows:
            title = str(row.get("title") or "")
            normalized = _normalized_topic(title, aliases) or _normalized_topic(title)
            matched = None
            for topic in topics:
                current = str(topic["normalized"])
                if normalized == current or (min(len(normalized), len(current)) >= 6 and SequenceMatcher(None, normalized, current).ratio() >= 0.82):
                    matched = topic
                    break
            if matched is None:
                matched = {"key": normalized[:120], "normalized": normalized, "title": title, "platforms": [], "ranks": {}, "promotion_hits": [], "rumor_hits": []}
                topics.append(matched)
            if platform not in matched["platforms"]:
                matched["platforms"].append(platform)
            rank = int(row.get("rank") or 50)
            matched["ranks"][platform] = min(rank, int(matched["ranks"].get(platform, rank)))
            matched["promotion_hits"] = sorted(set(matched["promotion_hits"]) | {term for term in PROMOTION_TERMS if term in title})
            matched["rumor_hits"] = sorted(set(matched["rumor_hits"]) | {term for term in RUMOR_TERMS if term in title})
    for topic in topics:
        topic["best_rank"] = min(topic["ranks"].values()) if topic["ranks"] else None
        topic.pop("normalized", None)
    return topics


def _history_metrics(previous: list[dict], current: dict) -> dict:
    current_ts = float(current["ts"])
    prior_24h = [snapshot for snapshot in previous if current_ts - float(snapshot.get("ts") or 0) <= 86400]
    topic_history: dict[str, list[tuple[float, dict]]] = {}
    for snapshot in previous:
        snapshot_ts = float(snapshot.get("ts") or 0)
        for topic in snapshot.get("topics") or []:
            topic_history.setdefault(str(topic.get("key") or ""), []).append((snapshot_ts, topic))
    new_topics = persistent_topics = fast_spread_topics = 0
    rank_jumps: list[int] = []
    first_seen_values: list[float] = []
    prior_24h_keys = {str(topic.get("key") or "") for snapshot in prior_24h for topic in snapshot.get("topics") or []}
    for topic in current.get("topics") or []:
        key = str(topic.get("key") or "")
        history = topic_history.get(key, [])
        if key not in prior_24h_keys:
            new_topics += 1
        if history:
            persistent_topics += 1
            first_seen = min(item[0] for item in history)
            latest_topic = max(history, key=lambda item: item[0])[1]
            previous_rank = latest_topic.get("best_rank")
            current_rank = topic.get("best_rank")
            if isinstance(previous_rank, int) and isinstance(current_rank, int):
                rank_jumps.append(previous_rank - current_rank)
        else:
            first_seen = current_ts
        first_seen_values.append(first_seen)
        platform_first_seen: dict[str, float] = {}
        for snapshot_ts, historical_topic in history + [(current_ts, topic)]:
            for platform in historical_topic.get("platforms") or []:
                platform_first_seen.setdefault(str(platform), snapshot_ts)
        if len(platform_first_seen) >= 3 and max(platform_first_seen.values()) - min(platform_first_seen.values()) <= 3600:
            fast_spread_topics += 1
    return {
        "social_history_snapshots": len(previous) + 1,
        "social_new_topics_24h": new_topics,
        "social_persistent_topics": persistent_topics,
        "social_fast_spread_topics": fast_spread_topics,
        "social_rank_jump_max": max(rank_jumps) if rank_jumps else None,
        "social_first_seen_at": datetime.fromtimestamp(min(first_seen_values or [current_ts])).astimezone().isoformat(timespec="seconds"),
        "social_propagation_status": "已有历史快照" if previous else "首次快照，等待时间序列",
    }


def _update_history(code: str, topics: list[dict], now: float | None = None) -> dict:
    current_ts = float(now if now is not None else time.time())
    HISTORY_BASE.mkdir(parents=True, exist_ok=True)
    path = HISTORY_BASE / f"{code}.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        previous = previous if isinstance(previous, list) else []
    except (OSError, json.JSONDecodeError):
        previous = []
    previous = [snapshot for snapshot in previous if current_ts - float(snapshot.get("ts") or 0) <= HISTORY_TTL]
    current = {"ts": current_ts, "topics": [{key: topic.get(key) for key in ("key", "title", "platforms", "best_rank", "promotion_hits")} for topic in topics]}
    metrics = _history_metrics(previous, current)
    try:
        path.write_text(json.dumps([*previous, current][-1000:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return metrics


def _news_search_not_run() -> dict:
    return {
        "news_search_count": 0,
        "news_search_records": [],
        "news_search_provider": "",
        "news_search_query": "",
        "news_search_errors": [],
        "news_search_status": "未执行",
        "news_search_fetch_state": "not_run",
        "news_search_source_chain": [],
    }


def _collect_news_candidates(code: str, name: str) -> dict:
    """Find company-specific news leads without changing structured news metrics."""
    company = (name or code).strip()
    query = f"{company} {code} 新闻 公告"
    provider = os.getenv("MODA_SEARCH_PROVIDER", "auto").strip().lower() or "auto"
    try:
        from tools.scoring.web_research import _search

        used, rows, errors = _search(
            provider,
            query,
            NEWS_SEARCH_TIMEOUT,
            cache_scope=f"social-news:{code}",
        )
    except Exception as exc:
        used, rows, errors = "none", [], [f"news_search:{type(exc).__name__}"]

    records: list[dict] = []
    seen: set[str] = set()
    for row in rows[:8]:
        title = str(row.get("title") or "").strip()
        snippet = str(row.get("snippet") or "").strip()
        url = str(row.get("url") or "").strip()
        text = f"{title} {snippet} {url}"
        if not title or (code not in text and company not in text):
            continue
        key = _normalized_topic(title) or url
        if not key or key in seen:
            continue
        seen.add(key)
        records.append({
            "source": "网络搜索",
            "title": title,
            "snippet": snippet,
            "text": f"{title}。{snippet}".strip("。"),
            "url": url,
            "published_at": str(row.get("date") or ""),
            "status": "网络候选新闻（未核验）",
            "provider": used,
            "query": query,
        })

    explicit_no_results = bool(errors) and all(error.endswith(":no_results") for error in errors)
    successful_empty = used != "none" and not rows and not errors
    no_match = explicit_no_results or successful_empty
    fetch_state = "fallback_ok" if records else "empty" if no_match else "failed"
    status = "网络候选新闻（未核验）" if records else "已搜索未命中" if no_match else "搜索失败，需人工确认"
    return {
        "news_search_count": len(records),
        "news_search_records": records,
        "news_search_provider": used,
        "news_search_query": query,
        "news_search_errors": errors,
        "news_search_status": status,
        "news_search_fetch_state": fetch_state,
        "news_search_source_chain": [{
            "source": used if used != "none" else "网络搜索",
            "status": "ok" if records else "empty" if no_match else "failed",
            "error": "; ".join(errors),
            "query": query,
        }],
    }


def _collect_news(code: str, name: str, aliases: list[str]) -> dict:
    try:
        from tools.scoring.news_sentiment import collect as collect_news

        news = collect_news(aliases)
        # Search results remain explicitly unverified leads and never enter news sentiment.
        if not news.get("news_posts_total") and int(news.get("news_sources_ok") or 0) > 0:
            return {**news, **_collect_news_candidates(code, name)}
        return {**news, **_news_search_not_run()}
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
        return {
            "news_posts_total": 0,
            "news_sources_checked": 3,
            "news_sources_ok": 0,
            "news_partial": True,
            "news_sentiment": None,
            "news_sentiment_score": None,
            "news_records": [],
            "news_source_status": {},
            "news_rumor_hits": [],
            "fetch_state": "failed",
            "source_chain": [{"source": "新闻舆情", "status": "failed", "error": error}],
            **_news_search_not_run(),
        }


def collect(code: str, name: str) -> dict:
    results: dict[str, dict] = {}
    aliases = _aliases(name, code)

    def one(item: tuple[str, Callable[[], list[dict]]]) -> tuple[str, dict]:
        platform, fetcher = item
        try:
            rows, mode = _cached(platform, fetcher)
            return platform, {
                "ok": bool(rows),
                "mode": mode,
                "items": rows,
                "fetch_state": "ok" if rows else "empty",
                "source_chain": [{"source": platform, "status": "ok" if rows else "empty", "error": "" if rows else "empty response"}],
                "error": "" if rows else "empty response",
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:100]}"
            return platform, {"ok": False, "mode": "failed", "items": [], "fetch_state": "failed", "source_chain": [{"source": platform, "status": "failed", "error": error}], "error": error}

    started = time.monotonic()
    auxiliary_executor = ThreadPoolExecutor(max_workers=2)
    auxiliary_futures = {
        auxiliary_executor.submit(_collect_discussion, code, name, COLLECT_DEADLINE): "discussion",
        auxiliary_executor.submit(_collect_news, code, name, aliases): "news",
    }
    executor = ThreadPoolExecutor(max_workers=len(FETCHERS))
    futures = {executor.submit(one, item): item[0] for item in FETCHERS.items()}
    try:
        remaining = max(1.0, COLLECT_DEADLINE - (time.monotonic() - started))
        for future in as_completed(futures, timeout=remaining):
            platform = futures[future]
            try:
                result_platform, result = future.result()
            except Exception as exc:
                result_platform = platform
                error = f"{type(exc).__name__}: {str(exc)[:100]}"
                result = {"ok": False, "mode": "failed", "items": [], "fetch_state": "failed", "source_chain": [{"source": platform, "status": "failed", "error": error}], "error": error}
            results[result_platform] = result
    except TimeoutError:
        for future, platform in futures.items():
            if not future.done():
                future.cancel()
                results[platform] = {"ok": False, "mode": "timeout", "items": [], "fetch_state": "failed", "source_chain": [{"source": platform, "status": "failed", "error": "platform deadline exceeded"}], "error": "platform deadline exceeded"}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    for platform in FETCHERS:
        results.setdefault(platform, {"ok": False, "mode": "timeout", "items": [], "fetch_state": "failed", "source_chain": [{"source": platform, "status": "failed", "error": "platform deadline exceeded"}], "error": "platform deadline exceeded"})

    auxiliary_results: dict[str, dict] = {}
    try:
        remaining = max(1.0, COLLECT_DEADLINE - (time.monotonic() - started))
        for future in as_completed(auxiliary_futures, timeout=remaining):
            label = auxiliary_futures[future]
            try:
                auxiliary_results[label] = future.result()
            except Exception as exc:
                auxiliary_results[label] = {"fetch_state": "failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    except TimeoutError:
        for future, label in auxiliary_futures.items():
            if not future.done():
                future.cancel()
                auxiliary_results[label] = {"fetch_state": "failed", "error": "collector deadline exceeded"}
    finally:
        auxiliary_executor.shutdown(wait=False, cancel_futures=True)

    discussion = auxiliary_results.get("discussion") or _collect_discussion(code, name, timeout=2)
    news = auxiliary_results.get("news") or _collect_news(code, name, aliases)
    mentions: dict[str, list[dict]] = {}
    for platform, result in results.items():
        mentions[platform] = [row for row in result["items"] if any(alias in row.get("title", "") for alias in aliases)]
    hits = sum(len(rows) for rows in mentions.values())
    platform_hits = sum(bool(rows) for rows in mentions.values())
    checked = sum(result["ok"] for result in results.values())
    matched_text = " ".join(row.get("title", "") for rows in mentions.values() for row in rows)
    promotion_hits = [term for term in PROMOTION_TERMS if term in matched_text]
    rumor_hits = [term for term in RUMOR_TERMS if term in matched_text]
    topics = _cluster_mentions(mentions, aliases)
    history = _update_history(code, topics)
    promotional_platforms = sorted({
        platform
        for topic in topics if topic.get("promotion_hits")
        for platform in topic.get("platforms") or []
    } | set(str(item) for item in discussion.get("discussion_promotion_sources", []) if item))
    rank_weight = sum(max(0.0, (51 - float(row.get("rank", 50))) / 50) for rows in mentions.values() for row in rows)
    social_heat = min(1.0, (platform_hits / 3) * 0.6 + min(0.4, rank_weight * 0.12)) if checked >= 3 else None
    platform_fetch_states = [result.get("fetch_state", "failed") for result in results.values()]
    platform_has_data = any(result.get("ok") for result in results.values())
    discussion_has_data = bool(discussion.get("discussion_posts_total"))
    platform_all_failed = bool(platform_fetch_states) and all(state == "failed" for state in platform_fetch_states)
    discussion_state = discussion.get("fetch_state", "failed")
    news_state = news.get("fetch_state", "failed")
    if platform_all_failed and not discussion_has_data and discussion_state == "failed":
        module_fetch_state = "failed"
    elif platform_has_data or discussion_has_data:
        module_fetch_state = "fallback_ok" if any(state in {"failed", "empty"} for state in platform_fetch_states) or discussion_state in {"failed", "empty", "fallback_ok"} or news_state in {"failed", "stale", "fallback_ok"} else "ok"
    elif all(state == "empty" for state in platform_fetch_states) and discussion_state == "empty":
        module_fetch_state = "empty"
    else:
        module_fetch_state = "failed"
    return {
        "social_platforms_checked": checked,
        "social_platforms_total": len(FETCHERS),
        "social_hot_hits": hits,
        "social_platform_hits": platform_hits,
        "social_heat": round(social_heat, 4) if social_heat is not None else None,
        "social_mentions": mentions,
        "social_topics": topics,
        "social_unique_topics": len(topics),
        "social_cross_platform_topics": sum(len(topic.get("platforms") or []) >= 2 for topic in topics),
        "social_promotional_platforms": promotional_platforms,
        "social_promotional_platform_hits": len(promotional_platforms),
        "promotional_keyword_hits": promotion_hits,
        "rumor_keyword_hits": rumor_hits,
        "social_aliases": aliases,
        "social_platform_status": {key: {"ok": value["ok"], "mode": value["mode"], "fetch_state": value.get("fetch_state", "failed"), "source_chain": value.get("source_chain", []), "error": value["error"]} for key, value in results.items()},
        "social_partial": checked < len(FETCHERS),
        **history,
        **discussion,
        **{f"news_{key}": value for key, value in news.items() if key in {"fetch_state", "source_chain"}},
        **{key: value for key, value in news.items() if key not in {"fetch_state", "source_chain"}},
        "fetch_state": module_fetch_state,
        "discussion_fetch_state": discussion_state,
        "news_fetch_state": news_state,
        "source_chain": {
            "platforms": {key: value.get("source_chain", []) for key, value in results.items()},
            "discussion": discussion.get("source_chain", []),
            "news": news.get("source_chain", []),
            "news_candidates": news.get("news_search_source_chain", []),
        },
    }


def _collect_discussion(code: str, name: str, timeout: float = 8) -> dict:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("moda_stock_discussion", DISCUSSION_SCRIPT)
        if spec is None or spec.loader is None:
            raise ImportError("discussion module unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.collect(code, name, timeout=timeout)
    except Exception as exc:
        return {
            "discussion_posts_total": 0,
            "discussion_structured_count": 0,
            "discussion_search_count": 0,
            "discussion_source_count": 0,
            "discussion_source_status": "搜索失败，需人工确认",
            "discussion_source_status_detail": {},
            "discussion_sources": [],
            "discussion_partial": True,
            "discussion_sample_status": "样本不足",
            "discussion_minimum_sample": 10,
            "discussion_duplicate_count": 0,
            "discussion_duplicate_ratio": 0.0,
            "discussion_records": [],
            "discussion_search_errors": [f"{type(exc).__name__}: {str(exc)[:120]}"],
            "discussion_sentiment": None,
            "discussion_sentiment_score": None,
            "discussion_raw_sentiment": None,
            "discussion_sentiment_confidence": "样本不足",
            "fetch_state": "failed",
            "source_chain": [{"source": "个股讨论接口/搜索", "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}],
            "discussion_positive_count": 0,
            "discussion_negative_count": 0,
            "discussion_neutral_count": 0,
            "discussion_promotion_hits": [],
            "discussion_promotion_record_count": 0,
            "discussion_promotion_source_count": 0,
            "discussion_promotion_sources": [],
            "discussion_author_count": 0,
            "discussion_template_cluster_count": 0,
            "discussion_rumor_hits": [],
        }


def build_report(code: str, name: str, data: dict) -> str:
    news_posts = int(data.get("news_posts_total") or 0)
    news_candidates = int(data.get("news_search_count") or 0)
    if news_posts:
        news_summary = (
            f"实时快讯匹配 {news_posts} 条；网络候选未执行（已有实时匹配）；"
            f"情绪 {data.get('news_sentiment') or '需人工确认'}"
        )
    elif news_candidates:
        news_summary = (
            f"实时快讯匹配 0 条；网络候选 {news_candidates} 条（未核验，待正文核验，"
            "不计入情绪、评分、热度或异常推广判断）"
        )
    else:
        news_summary = (
            f"实时快讯匹配 0 条；网络候选 0 条（{data.get('news_search_status') or '未执行'}，"
            "不代表无新闻）"
        )
    lines = [
        f"# 社交热榜与异常推广风险：{name or code}（{code}）",
        "",
        f"> 采集时间：{time.strftime('%Y-%m-%d %H:%M:%S')}  |  数据源：微博/知乎/百度/抖音/头条/B站公开热榜",
        "",
        f"<!-- moda_social_sentiment: {json.dumps(data, ensure_ascii=False)} -->",
        "",
        f"- 可用平台：{data['social_platforms_checked']} / {data['social_platforms_total']}",
        f"- 命中：{data['social_hot_hits']} 条，去重后 {data.get('social_unique_topics', 0)} 个主题，覆盖 {data['social_platform_hits']} 个平台；跨平台主题 {data.get('social_cross_platform_topics', 0)} 个",
        f"- 社交热度：{data['social_heat'] if data['social_heat'] is not None else '需人工确认'}",
        f"- 传播状态：{data.get('social_propagation_status', '需人工确认')}；24小时新主题 {data.get('social_new_topics_24h', '需人工确认')}；快速扩散主题 {data.get('social_fast_spread_topics', '需人工确认')}；最大排名跃升 {data.get('social_rank_jump_max') if data.get('social_rank_jump_max') is not None else '需更多快照'}",
        f"- 推广话术命中：{'、'.join(data['promotional_keyword_hits']) or '无'}；个股讨论：{'、'.join(data.get('discussion_promotion_hits') or []) or '无'}",
        f"- 谣言/风险词命中：{'、'.join(data['rumor_keyword_hits']) or '无'}",
        f"- 个股讨论：{data.get('discussion_posts_total', 0)} 条；样本 {data.get('discussion_sample_status', '需人工确认')}；情绪 {data.get('discussion_sentiment') or '需人工确认'}；来源 {data.get('discussion_source_status', '需人工确认')}",
        f"- 新闻舆情：{news_summary}；实时来源 {data.get('news_sources_ok', 0)}/{data.get('news_sources_checked', 3)}",
        "",
        "## 命中明细",
        "",
        "| 平台 | 排名 | 标题 |",
        "|---|---:|---|",
    ]
    for platform, rows in data["social_mentions"].items():
        for row in rows:
            lines.append(f"| {platform} | {row.get('rank', '-')} | {str(row.get('title', '')).replace('|', '/')} |")
    if not data["social_hot_hits"]:
        lines.append("| - | - | 当前可用热榜未命中 |")
    if news_posts == 0:
        lines += [
            "",
            "## 网络候选新闻（未核验）",
            "",
            f"- 搜索状态：{data.get('news_search_status') or '未执行'}；后端：{data.get('news_search_provider') or '未执行'}；查询：{data.get('news_search_query') or '未执行'}",
            "",
            "| 后端 | 标题 | 链接 |",
            "|---|---|---|",
        ]
        for row in data.get("news_search_records") or []:
            title = str(row.get("title") or "").replace("|", "/")
            url = str(row.get("url") or "").replace("|", "%7C")
            lines.append(f"| {row.get('provider') or '-'} | {title} | {url or '-'} |")
        if not news_candidates:
            lines.append("| - | - | 未获得可核验候选 |")
    lines += [
        "",
        "说明：热榜只证明关注度。异常推广风险必须与基本面、K 线、公告澄清等独立证据交叉验证，未命中不等于安全。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect social hot-list and promotion-risk evidence")
    parser.add_argument("--stock", required=True)
    parser.add_argument("--name", default="")
    args = parser.parse_args()
    code = args.stock.strip()
    data = collect(code, args.name or code)
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_BASE / f"{code}.md"
    path.write_text(build_report(code, args.name or code, data), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
