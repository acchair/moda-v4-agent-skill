"""Full-universe, evidence-first quick screening for sector stock selection.

This module is deliberately narrower than the individual-stock pipeline.  It
uses a small, repeatable snapshot for every constituent, then selects a short
list for the user to decide whether to research deeply.  It never emits a
``research_score`` or a five-state stock decision.
"""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHORTLIST_LIMIT = 6
MAX_SHORTLIST_LIMIT = 12
# Eastmoney's F10 endpoints are deliberately serialised.  The provider is
# shared by the F10 fallback and AKShare, so a large candidate pool must not
# turn into a concurrent burst merely because the screen is "lightweight".
DEFAULT_BUSINESS_WORKERS = 1
DEFAULT_BUSINESS_TIMEOUT = 4
DEFAULT_PEER_SNAPSHOT_WORKERS = 1
DEFAULT_PEER_SNAPSHOT_TIMEOUT = 5
DEFAULT_PEER_SNAPSHOT_LIMIT = 30

# These terms only create a *clue*.  A confirmed technical barrier still
# requires primary disclosure, customer certification, process data, or a
# full individual-stock research packet.
TECHNICAL_BARRIER_TERMS = (
    "核心", "高端", "专用", "精密", "工艺", "配方", "认证", "专利", "技术",
    "材料", "设备", "芯片", "算法", "软件", "数据", "平台", "模组", "部件",
)

# Theme aliases are only for building an initial candidate pool.  They do not
# establish that a company has revenue exposure to the theme; that remains a
# business/F10 and disclosure verification step in the quick screen.
THEME_ALIASES: dict[str, tuple[str, ...]] = {
    "端侧ai": (
        "端侧AI", "端侧人工智能", "端侧智能", "端侧大模型", "终端侧AI",
        "边缘智能", "边缘AI", "AI手机", "AIPC", "AI眼镜", "AIoT",
    ),
}
MAX_THEME_CONCEPT_BOARDS = 8
QUERY_KINDS = {"auto", "sector", "concept"}
CONCEPT_INPUT_SUFFIXES = ("概念股", "概念", "题材")
CONCEPT_EXPOSURE_ORDER = {
    "核心主业": 0,
    "重要业务": 1,
    "边际受益": 3,
    "收入待核验": 4,
    "仅题材关联": 5,
    "无相关主营证据": 6,
    "需人工确认": 7,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normal(value: Any) -> str:
    text = _text(value).lower()
    return re.sub(r"[\s\-_/，、,;；:：()（）【】\[\]]+", "", text)


def _code(value: Any) -> str:
    text = _text(value)
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = _text(value).replace(",", "").replace("%", "")
    if not text or text in {"-", "--", "nan", "none", "null"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _normal(value)
    if text in {"true", "yes", "是", "已触发", "高", "risk"}:
        return True
    if text in {"false", "no", "否", "未触发", "低", "none"}:
        return False
    return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    normalized = {_normal(key): value for key, value in mapping.items()}
    for key in keys:
        value = normalized.get(_normal(key))
        if value not in (None, ""):
            return value
    return None


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [item for item in (_text(item) for item in value) if item]
    text = _text(value)
    return [item for item in re.split(r"[、,，/;；|]+", text) if item]


def _base_variants(sector: str) -> list[str]:
    raw = _normal(sector)
    values = [raw]
    for suffix in ("板块选股", "产业链", "概念股", "概念", "板块", "行业", "股票", "选股"):
        if raw.endswith(suffix):
            values.append(raw[: -len(suffix)])
    return list(dict.fromkeys(item for item in values if len(item) >= 2))


def _variants(sector: str) -> list[str]:
    values = _base_variants(sector)
    requested = _normal(sector)
    for canonical, aliases in THEME_ALIASES.items():
        terms = (_normal(canonical), *(_normal(item) for item in aliases))
        # Exact aliases are accepted; longer aliases contained in a more
        # specific user query are also accepted.  Do not let a generic "AI"
        # request accidentally activate all of the 端侧 AI aliases.
        if any(requested == term or (len(term) >= 4 and term in requested) for term in terms):
            values.extend(terms)
    return list(dict.fromkeys(item for item in values if len(item) >= 2))


def infer_query_kind(query: str) -> str:
    """Classify only unambiguous theme expressions without guessing industries.

    A generic noun such as ``半导体`` can denote either an industry or a market
    theme.  It remains an industry request unless the user explicitly says
    "概念"/"题材" or the expression is a registered theme alias.  This avoids
    silently replacing an industry universe with a much narrower concept board.
    """
    raw = _normal(query)
    if any(raw.endswith(_normal(suffix)) for suffix in CONCEPT_INPUT_SUFFIXES):
        return "concept"
    for canonical, aliases in THEME_ALIASES.items():
        terms = {_normal(canonical), *(_normal(item) for item in aliases)}
        if raw in terms:
            return "concept"
    return "sector"


def _matches_sector(value: Any, sector: str) -> bool:
    text = _normal(value)
    return any(item and item in text for item in _variants(sector))


def _safe_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    try:
        if frame.empty:
            return []
        return [dict(item) for item in frame.to_dict("records")]
    except (AttributeError, TypeError, ValueError):
        return []


def _normalize_candidate(raw: Mapping[str, Any], source: str) -> dict[str, Any] | None:
    code = _code(_first(raw, "code", "代码", "证券代码", "股票代码"))
    name = _text(_first(raw, "name", "名称", "证券简称", "股票名称"))
    if not re.fullmatch(r"\d{6}", code):
        return None
    if not name:
        name = code
    concepts = _as_list(_first(raw, "concepts", "概念", "概念名称"))
    category = _text(_first(raw, "category", "分类"))
    return {
        "code": code,
        "name": name,
        "industry": _text(_first(raw, "industry", "行业", "行业名称", "所属行业")),
        "concepts": concepts,
        "category": category,
        "main_business": _text(_first(raw, "main_business", "主营业务", "主营")),
        "business_items": _as_list(_first(raw, "business_items", "主营构成", "业务构成")),
        "business_breakdown": raw.get("business_breakdown") if isinstance(raw.get("business_breakdown"), list) else [],
        "barrier_status": _text(_first(raw, "barrier_status", "技术壁垒状态")),
        "barrier_evidence": _text(_first(raw, "barrier_evidence", "技术壁垒证据")),
        "barrier_evidence_refs": _as_list(_first(raw, "barrier_evidence_refs", "技术壁垒来源")),
        "profit_yoy": _number(_first(raw, "profit_yoy", "净利润同比", "净利润增长率", "归母净利润同比")),
        "net_profit": _number(_first(raw, "net_profit", "净利润", "归母净利润")),
        "pe_ttm": _number(_first(raw, "pe_ttm", "市盈率-ttm", "市盈率ttm", "市盈率-动态", "市盈率(动态)", "市盈率")),
        "pb": _number(_first(raw, "pb", "市净率")),
        "market_cap": _number(_first(raw, "market_cap", "总市值")),
        "price_percentile_3y": _number(_first(raw, "price_percentile_3y", "三年价格分位")),
        "pe_percentile_5y": _number(_first(raw, "pe_percentile_5y", "五年pe分位")),
        "pb_percentile_5y": _number(_first(raw, "pb_percentile_5y", "五年pb分位")),
        "market_congestion": _number(_first(raw, "market_congestion", "市场拥挤度")),
        "st_risk": _bool(_first(raw, "st_risk", "st风险", "st状态")),
        "audit_status": _text(_first(raw, "audit_status", "审计意见")),
        "controller_action": _text(_first(raw, "controller_action", "实控人动作", "控股股东动作")),
        "shareholder_warning": _text(_first(raw, "shareholder_warning", "股东筹码提示")),
        "source": source,
        "source_fields": [key for key, value in raw.items() if value not in (None, "")],
    }


def _dedupe(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        code = candidate["code"]
        if code in seen:
            continue
        seen.add(code)
        rows.append(candidate)
    return rows


def _load_local_universe(
    root: Path,
    sector: str,
    *,
    query_kind: str = "sector",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = root / "tools" / "scoring" / "专精特新_行业龙头_核心供应商_A股名单_完整版.csv"
    if not path.is_file():
        return [], {
            "source": "本地名单数据库不可用",
            "coverage_status": "unavailable",
            "error": "本地候选名单不存在。",
        }
    direct: list[dict[str, Any]] = []
    thematic: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            candidate = _normalize_candidate(raw, path.name)
            if candidate is None:
                continue
            if _matches_sector(candidate["industry"], sector):
                candidate["universe_match"] = "行业名称"
                direct.append(candidate)
            elif any(_matches_sector(item, sector) for item in candidate["concepts"]):
                candidate["universe_match"] = "概念名称"
                thematic.append(candidate)
    rows = thematic if query_kind == "concept" else [*direct, *thematic]
    selection_note = (
        "本地名单只作为离线主题候选池，不代表完整概念样本；概念命中必须经主营和收入分部核验。"
        if query_kind == "concept"
        else "本地名单只作为离线回退，不代表板块全样本；概念命中不得替代主营核验。"
    )
    return _dedupe(rows), {
        "source": path.name,
        "coverage_status": "local_partial",
        "selection_note": selection_note,
    }


def _pick_board_names(rows: Sequence[Mapping[str, Any]], sector: str, limit: int = 1) -> list[str]:
    candidates: list[tuple[int, str]] = []
    variants = _variants(sector)
    for row in rows:
        name = _text(_first(row, "板块名称", "名称", "行业名称", "name"))
        normalized = _normal(name)
        if not name:
            continue
        exact = any(normalized == item for item in variants)
        partial = any(item in normalized for item in variants)
        if exact:
            candidates.append((0, name))
        elif partial:
            candidates.append((1, name))
    return [name for _, name in sorted(candidates, key=lambda item: (item[0], len(item[1]), item[1]))[:max(1, limit)]]


def _pick_board_name(rows: Sequence[Mapping[str, Any]], sector: str) -> str:
    names = _pick_board_names(rows, sector)
    return names[0] if names else ""


def _load_eastmoney_universe(sector: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        import akshare as ak

        boards = _safe_records(ak.stock_board_industry_name_em())
        board_name = _pick_board_name(boards, sector)
        if not board_name:
            return [], {
                "source": "AKShare/东方财富行业板块成分股",
                "coverage_status": "unavailable",
                "error": "未匹配到行业板块名称。",
            }
        components = _safe_records(ak.stock_board_industry_cons_em(symbol=board_name))
        candidates = _dedupe(
            candidate
            for row in components
            if (candidate := _normalize_candidate(row, "AKShare/东方财富行业板块成分股")) is not None
        )
        for candidate in candidates:
            candidate["universe_match"] = "行业成分股"
            candidate["industry"] = candidate["industry"] or board_name
        return candidates, {
            "source": "AKShare/东方财富行业板块成分股",
            "coverage_status": "live_full",
            "board_name": board_name,
            "selection_note": "按实时行业成分股覆盖候选池；主营与产业链位置仍需轻量核验。",
        }
    except Exception as exc:
        return [], {
            "source": "AKShare/东方财富行业板块成分股",
            "coverage_status": "unavailable",
            "error": f"{type(exc).__name__}: 行业成分股获取失败",
        }


def _load_eastmoney_concept_universe(sector: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use concept-board components when an industry name cannot express a theme.

    A concept board is intentionally labelled as a thematic candidate pool,
    rather than being presented as a complete industry universe.
    """
    try:
        import akshare as ak

        board_fetcher = getattr(ak, "stock_board_concept_name_em", None)
        component_fetcher = getattr(ak, "stock_board_concept_cons_em", None)
        if not callable(board_fetcher) or not callable(component_fetcher):
            return [], {
                "source": "AKShare/东方财富概念板块成分股",
                "coverage_status": "unavailable",
                "error": "当前 AKShare 未提供东方财富概念板块接口。",
            }
        boards = _safe_records(board_fetcher())
        board_names = _pick_board_names(boards, sector, limit=MAX_THEME_CONCEPT_BOARDS)
        if not board_names:
            return [], {
                "source": "AKShare/东方财富概念板块成分股",
                "coverage_status": "unavailable",
                "error": "未匹配到概念板块名称。",
            }
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        for board_name in board_names:
            try:
                components = _safe_records(component_fetcher(symbol=board_name))
            except Exception as exc:
                errors.append(f"{board_name}:{type(exc).__name__}")
                continue
            for row in components:
                candidate = _normalize_candidate(row, "AKShare/东方财富概念板块成分股")
                if candidate is None:
                    continue
                candidate["universe_match"] = "概念板块成分股"
                candidate["concepts"] = list(dict.fromkeys([*candidate["concepts"], board_name]))
                candidates.append(candidate)
        rows = _dedupe(candidates)
        if not rows:
            return [], {
                "source": "AKShare/东方财富概念板块成分股",
                "coverage_status": "unavailable",
                "board_names": board_names,
                "error": "；".join(errors) or "概念板块未返回有效成分股。",
            }
        return rows, {
            "source": "AKShare/东方财富概念板块成分股",
            "coverage_status": "live_theme",
            "board_names": board_names,
            "selection_note": "按概念板块构建主题候选池，不代表完整行业样本；概念命中必须经主营、收入或公告核验。",
            "partial_errors": errors,
        }
    except Exception as exc:
        return [], {
            "source": "AKShare/东方财富概念板块成分股",
            "coverage_status": "unavailable",
            "error": f"{type(exc).__name__}: 概念成分股获取失败",
        }
def _load_shenwan_universe(sector: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Use Shenwan level-2 components when the Eastmoney board source is down."""
    try:
        import akshare as ak

        industries = _safe_records(ak.sw_index_second_info())
        industry_name = _pick_board_name(industries, sector)
        matched = next(
            (row for row in industries if _text(_first(row, "行业名称", "名称", "板块名称")) == industry_name),
            {},
        )
        industry_code = _text(_first(matched, "行业代码", "指数代码", "code"))
        if not industry_name or not industry_code:
            return [], {
                "source": "申万宏源研究/申万二级成分股",
                "coverage_status": "unavailable",
                "error": "未匹配到申万二级行业。",
            }
        components = _safe_records(ak.index_component_sw(symbol=industry_code))
        candidates = _dedupe(
            candidate
            for row in components
            if (candidate := _normalize_candidate(row, "申万宏源研究/申万二级成分股")) is not None
        )
        for candidate in candidates:
            candidate["universe_match"] = "申万二级成分股"
            candidate["industry"] = candidate["industry"] or industry_name
        return candidates, {
            "source": "申万宏源研究/申万二级成分股",
            "coverage_status": "live_full",
            "board_name": industry_name,
            "selection_note": "按申万二级成分股覆盖候选池；行情快照若缺失会明确标为需人工确认。",
        }
    except Exception as exc:
        return [], {
            "source": "申万宏源研究/申万二级成分股",
            "coverage_status": "unavailable",
            "error": f"{type(exc).__name__}: 申万行业成分股获取失败",
        }


def _load_live_universe(sector: str, *, query_kind: str = "sector") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if query_kind == "concept":
        concept_rows, concept_metadata = _load_eastmoney_concept_universe(sector)
        if concept_rows:
            return concept_rows, concept_metadata
        return [], {
            "source": "东方财富概念板块成分股",
            "coverage_status": "unavailable",
            "error": _text(concept_metadata.get("error")) or "概念成分股未返回候选。",
            "selection_note": "概念模式只接受概念成分股或本地概念字段作为候选池，不用行业成分股替代。",
        }
    eastmoney_rows, eastmoney_metadata = _load_eastmoney_universe(sector)
    if eastmoney_rows:
        return eastmoney_rows, eastmoney_metadata
    shenwan_rows, shenwan_metadata = _load_shenwan_universe(sector)
    if shenwan_rows:
        shenwan_metadata["primary_fallback"] = eastmoney_metadata.get("error") or "东方财富行业成分股未返回候选。"
        return shenwan_rows, shenwan_metadata
    concept_rows, concept_metadata = _load_eastmoney_concept_universe(sector)
    if concept_rows:
        concept_metadata["primary_fallback"] = "；".join(
            item for item in (
                _text(eastmoney_metadata.get("error")),
                _text(shenwan_metadata.get("error")),
            ) if item
        ) or "行业成分股未匹配，已改用概念板块候选池。"
        return concept_rows, concept_metadata
    return [], {
        "source": "行业及概念成分股三源",
        "coverage_status": "unavailable",
        "error": "；".join(
            item for item in (
                _text(eastmoney_metadata.get("error")),
                _text(shenwan_metadata.get("error")),
                _text(concept_metadata.get("error")),
            ) if item
        ) or "行业及概念成分股源未返回候选。",
    }


def load_universe_file(path: str | Path) -> list[dict[str, Any]]:
    """Load a reproducible candidate universe from JSON or CSV."""
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"候选池文件不存在：{source}")
    if source.suffix.lower() == ".csv":
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取候选池 JSON：{source}: {exc}") from exc
    if isinstance(raw, Mapping):
        raw = raw.get("candidates") or raw.get("rows") or raw.get("universe") or []
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError("候选池 JSON 必须是对象数组，或包含 candidates/rows/universe 数组。")
    return [dict(item) for item in raw]


def resolve_sector_universe(
    root: Path,
    sector: str,
    *,
    candidates: Sequence[str | Mapping[str, Any]] | None = None,
    universe_rows: Sequence[Mapping[str, Any]] | None = None,
    use_live_universe: bool = True,
    query_kind: str = "sector",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return every available constituent before any selection occurs."""
    if query_kind not in QUERY_KINDS - {"auto"}:
        raise ValueError("query_kind 必须是 sector 或 concept")
    if universe_rows is not None:
        rows = _dedupe(
            candidate
            for raw in universe_rows
            if (candidate := _normalize_candidate(raw, "用户提供候选池")) is not None
        )
        return rows, {
            "source": "用户提供候选池",
            "coverage_status": "user_supplied",
            "selection_note": "候选池范围由用户文件决定；系统不把它表述为行业全样本。",
        }
    if candidates:
        raw_rows: list[dict[str, Any]] = []
        for item in candidates:
            if isinstance(item, Mapping):
                raw_rows.append(dict(item))
            else:
                raw_rows.append({"code": str(item), "name": str(item)})
        rows = _dedupe(
            candidate
            for raw in raw_rows
            if (candidate := _normalize_candidate(raw, "用户指定候选")) is not None
        )
        return rows, {
            "source": "用户指定候选",
            "coverage_status": "user_supplied",
            "selection_note": "用户指定的名单用于快速比较，不代表行业全样本。",
        }
    if use_live_universe:
        rows, metadata = _load_live_universe(sector, query_kind=query_kind)
        if rows:
            return rows, metadata
    local_rows, local_metadata = _load_local_universe(root, sector, query_kind=query_kind)
    if use_live_universe and local_rows:
        live_error = _text(metadata.get("error")) if isinstance(metadata, Mapping) else ""
        local_metadata["live_fallback"] = live_error or (
            "概念成分股接口不可用，已降级到本地部分概念名单。"
            if query_kind == "concept"
            else "行业成分股接口不可用，已降级到本地部分名单。"
        )
    return local_rows, local_metadata


def _default_business_fetcher(code: str, timeout: int) -> dict[str, Any]:
    from tools.akshare.business_data import build_structured, fetch_business_data

    return build_structured(fetch_business_data(code, timeout=timeout))


def _business_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    if candidate.get("business_items") or candidate.get("main_business") or candidate.get("business_breakdown"):
        return {
            "fetch_state": "provided",
            "business_items": list(candidate.get("business_items") or []),
            "main_business": _text(candidate.get("main_business")),
            "business_breakdown": list(candidate.get("business_breakdown") or []),
        }
    return None


def collect_business_snapshots(
    candidates: Sequence[Mapping[str, Any]],
    *,
    fetch: bool = True,
    timeout: int = DEFAULT_BUSINESS_TIMEOUT,
    workers: int = DEFAULT_BUSINESS_WORKERS,
    fetcher: Callable[[str, int], Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch only F10 business composition, concurrently, for each constituent."""
    snapshots: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for candidate in candidates:
        code = _text(candidate.get("code"))
        supplied = _business_from_candidate(candidate)
        if supplied is not None:
            snapshots[code] = supplied
        elif not fetch:
            snapshots[code] = {"fetch_state": "not_requested"}
        else:
            pending.append(dict(candidate))
    if not pending:
        return snapshots
    active_fetcher = fetcher or _default_business_fetcher
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            executor.submit(active_fetcher, candidate["code"], int(timeout)): candidate["code"]
            for candidate in pending
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                payload = future.result()
                snapshots[code] = dict(payload) if isinstance(payload, Mapping) else {"fetch_state": "invalid"}
            except Exception as exc:
                snapshots[code] = {"fetch_state": "failed", "fetch_error": type(exc).__name__}
    return snapshots


def _default_peer_scale_fetcher(code: str, timeout: int) -> dict[str, Any]:
    """Get only the low-cost scale endpoint for a light-screen tie-breaker."""
    from tools.akshare.finance_data import fetch_industry_peer_snapshot

    snapshot = fetch_industry_peer_snapshot(
        code,
        timeout=timeout,
        comparisons=("scale",),
    )
    target = snapshot.get("target") if isinstance(snapshot.get("target"), Mapping) else {}
    return {
        "fetch_state": snapshot.get("fetch_state", "failed"),
        "status": snapshot.get("status", "需人工确认"),
        "source": "AKShare/东方财富同行规模比较",
        "source_tier": "B",
        "scope": "目标公司所在行业的规模/营收/净利排名；仅作轻筛同级条件下的透明排序依据",
        "market_cap_rank": target.get("market_cap_rank"),
        "float_market_cap_rank": target.get("float_market_cap_rank"),
        "revenue_rank": target.get("revenue_rank"),
        "net_profit_rank": target.get("net_profit_rank"),
        "source_chain": snapshot.get("source_chain", {}),
    }


def _peer_snapshot_from_candidate(candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    supplied = candidate.get("industry_scale")
    if not isinstance(supplied, Mapping):
        supplied = candidate.get("industry_scale_snapshot")
    if not isinstance(supplied, Mapping) or not supplied:
        return None
    result = dict(supplied)
    result.setdefault("fetch_state", "provided")
    result.setdefault("source", "用户提供行业规模排名")
    return result


def collect_peer_scale_snapshots(
    candidates: Sequence[Mapping[str, Any]],
    *,
    fetch: bool = False,
    timeout: int = DEFAULT_PEER_SNAPSHOT_TIMEOUT,
    workers: int = DEFAULT_PEER_SNAPSHOT_WORKERS,
    fetcher: Callable[[str, int], Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Collect one scale ranking per candidate, never a full stock pipeline."""
    snapshots: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for candidate in candidates:
        code = _text(candidate.get("code"))
        supplied = _peer_snapshot_from_candidate(candidate)
        if supplied is not None:
            snapshots[code] = supplied
        elif fetch:
            pending.append(code)
        else:
            snapshots[code] = {"fetch_state": "not_requested"}
    if not pending:
        return snapshots
    active_fetcher = fetcher or _default_peer_scale_fetcher
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            executor.submit(active_fetcher, code, int(timeout)): code
            for code in pending
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                payload = future.result()
                snapshots[code] = dict(payload) if isinstance(payload, Mapping) else {"fetch_state": "invalid"}
            except Exception as exc:
                snapshots[code] = {"fetch_state": "failed", "fetch_error": type(exc).__name__}
    return snapshots


def _scale_rank_values(snapshot: Mapping[str, Any]) -> tuple[float, float, float, float]:
    """Return transparent scale ranks as a final tie-breaker only."""
    fallback = 9_999_999.0
    ranks = tuple(
        _number(snapshot.get(key))
        for key in ("net_profit_rank", "revenue_rank", "market_cap_rank", "float_market_cap_rank")
    )
    available = [value for value in ranks if value is not None and value > 0]
    return (
        0.0 if available else 1.0,
        *(value if value is not None and value > 0 else fallback for value in ranks[:3]),
    )


def _scale_rank_text(snapshot: Mapping[str, Any]) -> str:
    if _normal(snapshot.get("fetch_state")) not in {"ok", "provided"}:
        return "需人工确认"
    fields = (
        ("营收", "revenue_rank"),
        ("净利", "net_profit_rank"),
        ("市值", "market_cap_rank"),
    )
    values = [
        f"{label}#{_display_number(snapshot.get(key))}"
        for label, key in fields
        if _number(snapshot.get(key)) is not None
    ]
    return " / ".join(values) if values else "需人工确认"


def _profile_chain_names(sector: str) -> set[str]:
    try:
        import yaml

        path = ROOT / "tools" / "scoring" / "chains.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        matches: set[str] = set()
        for chain in raw.get("chains", []):
            name = _text(chain.get("name"))
            aliases = [_text(item) for item in chain.get("aliases", [])]
            if _matches_sector(name, sector) or any(_matches_sector(alias, sector) for alias in aliases):
                matches.add(name)
        return matches
    except Exception:
        return set()


def _enrich_chain(evidence: dict[str, Any]) -> None:
    try:
        from tools.scoring import evidence as evidence_module

        evidence_module._chain_match(evidence)
        if evidence.get("chain_name") or evidence.get("chain_stage"):
            evidence_module._chokepoint_match(evidence)
    except Exception:
        # A quick screen remains usable with raw F10 fields.  It must simply
        # show the chain/barrier status as unverified instead of fabricating it.
        return


def _business_snapshot_ready(business: Mapping[str, Any]) -> bool:
    state = _normal(business.get("fetch_state"))
    return state in {"ok", "ready", "success", "provided"} and bool(
        _text(business.get("main_business")) or business.get("business_items") or business.get("business_breakdown")
    )


def _ratio_fraction(value: Any) -> float | None:
    """Normalize F10 ratio fields that may be fractions or percentages."""
    ratio = _number(value)
    if ratio is None or ratio < 0:
        return None
    if ratio <= 1:
        return ratio
    if ratio <= 100:
        return ratio / 100
    return None


def _concept_exposure(
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any],
    sector: str,
    *,
    business_snapshot_ready: bool,
) -> dict[str, Any]:
    """Describe a theme's disclosed business exposure without inventing purity.

    F10 often contains more than one dimension (product, industry and region).
    Ratios from different dimensions cannot be added together, so this function
    selects one matching dimension only and records it in the result.
    """
    concept_label_hit = any(_matches_sector(item, sector) for item in candidate.get("concepts") or [])
    main_business = _text(evidence.get("main_business"))
    business_items = list(evidence.get("business_items") or [])
    text_hit = _matches_sector(main_business, sector) or any(
        _matches_sector(item, sector) for item in business_items
    )
    if not business_snapshot_ready:
        return {
            "tier": "仅题材关联" if concept_label_hit else "需人工确认",
            "revenue_ratio": None,
            "matched_items": [],
            "measurement": "未取得 F10 主营构成",
            "reason": "仅有概念标签，未取得 F10 主营构成，不能判断收入暴露。"
            if concept_label_hit else "未取得 F10 主营构成，无法判断概念收入暴露。",
        }

    grouped: dict[str, list[tuple[str, float | None]]] = {}
    for raw in evidence.get("business_breakdown") or []:
        if not isinstance(raw, Mapping):
            continue
        item = _text(raw.get("item"))
        if not item or not _matches_sector(item, sector):
            continue
        category = _text(raw.get("category")) or "未标注维度"
        grouped.setdefault(category, []).append((item, _ratio_fraction(raw.get("revenue_ratio"))))

    ranked_groups: list[tuple[int, float, str, list[tuple[str, float | None]]]] = []
    for category, items in grouped.items():
        known = [ratio for _, ratio in items if ratio is not None]
        category_rank = 0 if "产品" in category else 1 if "行业" in category else 2
        ranked_groups.append((category_rank, -sum(known), category, items))
    if ranked_groups:
        _, _, category, matches = sorted(ranked_groups, key=lambda item: (item[0], item[1], item[2]))[0]
        known_ratios = [ratio for _, ratio in matches if ratio is not None]
        matched_items = [item for item, _ in matches]
        if known_ratios:
            ratio = min(sum(known_ratios), 1.0)
            tier = "核心主业" if ratio >= 0.50 else "重要业务" if ratio >= 0.20 else "边际受益"
            return {
                "tier": tier,
                "revenue_ratio": round(ratio, 6),
                "matched_items": matched_items,
                "measurement": category,
                "reason": f"F10 {category}中“{'、'.join(matched_items[:4])}”合计收入占比 {ratio:.1%}。",
            }
        return {
            "tier": "收入待核验",
            "revenue_ratio": None,
            "matched_items": matched_items,
            "measurement": category,
            "reason": f"F10 {category}命中“{'、'.join(matched_items[:4])}”，但该分部未披露可用收入占比。",
        }
    if text_hit:
        return {
            "tier": "收入待核验",
            "revenue_ratio": None,
            "matched_items": [item for item in business_items if _matches_sector(item, sector)][:8],
            "measurement": "主营文本",
            "reason": "F10 主营文本提及该概念，但没有可归因的分部收入占比。",
        }
    if concept_label_hit:
        return {
            "tier": "仅题材关联",
            "revenue_ratio": None,
            "matched_items": [],
            "measurement": "概念标签",
            "reason": "只命中概念标签，F10 主营和收入分部未出现可归因业务。",
        }
    return {
        "tier": "无相关主营证据",
        "revenue_ratio": None,
        "matched_items": [],
        "measurement": "F10 主营构成",
        "reason": "F10 主营和收入分部未形成该概念的可归因证据。",
    }


def _business_status(
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any],
    sector: str,
    profile_names: set[str],
    *,
    business_snapshot_ready: bool,
) -> tuple[str, str]:
    # A theme or broad industry label must never become a main-business match
    # merely because a chain definition contains a generic industry keyword.
    if not business_snapshot_ready:
        if candidate.get("universe_match") in {"行业成分股", "申万二级成分股"} or _matches_sector(candidate.get("industry"), sector):
            return "行业归属线索", "行业成分股或行业分类命中；F10 主营构成尚未取得，不能确认具体受益环节。"
        if candidate.get("universe_match") == "概念名称" or any(_matches_sector(item, sector) for item in candidate.get("concepts") or []):
            return "主题关联", "只命中概念或主题字段，F10 主营未核验，不能升级为产业链受益。"
        return "需人工确认", "未取得 F10 主营构成，且没有行业成分股身份可作最低限度的候选线索。"
    chain_name = _text(evidence.get("chain_name"))
    match_type = _text(evidence.get("chain_match_type"))
    if chain_name and (not profile_names or chain_name in profile_names):
        if "主营嵌入支持" in match_type:
            return "主营嵌入支持", _text(evidence.get("business_match_reason")) or "F10 主营与产业链关键环节匹配。"
        if "主营嵌入线索" in match_type or _text(evidence.get("chain_stage")):
            return "主营嵌入线索", _text(evidence.get("business_match_reason")) or "F10 主营与产业链存在可继续验证的匹配。"
    if _matches_sector(candidate.get("industry"), sector):
        return "行业归属线索", "行业成分股或行业分类命中，主营收入与具体环节仍需核验。"
    if _matches_sector(candidate.get("main_business"), sector) or any(_matches_sector(item, sector) for item in candidate.get("business_items") or []):
        return "主营文本线索", "主营文本提及板块方向，但收入占比与具体受益路径待核验。"
    if any(_matches_sector(item, sector) for item in candidate.get("concepts") or []):
        return "主题关联", "仅概念字段命中，不能作为板块受益或壁垒依据。"
    return "需人工确认", "未形成主营嵌入证据，不能因名称或主题列入优先名单。"


def _barrier_status(candidate: Mapping[str, Any], evidence: Mapping[str, Any], business_status: str) -> tuple[str, str, list[str]]:
    provided_status = _text(candidate.get("barrier_status"))
    provided_evidence = _text(candidate.get("barrier_evidence"))
    provided_refs = list(candidate.get("barrier_evidence_refs") or [])
    if provided_status == "已验证" and provided_evidence and provided_refs:
        return "已验证", provided_evidence, provided_refs
    stage = _text(evidence.get("chain_stage"))
    context = " ".join([
        _text(evidence.get("main_business")),
        " ".join(_text(item) for item in evidence.get("business_items") or []),
    ]).lower()
    hits = [term for term in TECHNICAL_BARRIER_TERMS if term.lower() in context]
    if business_status in {"主营嵌入支持", "主营嵌入线索"} and evidence.get("chokepoint_segment"):
        segment = _text(evidence.get("chokepoint_segment"))
        return "关键位置线索", f"静态关键环节名单命中“{segment}”，仍需以公司披露验证壁垒。", ["chokepoint_segments.csv"]
    if business_status in {"主营嵌入支持", "主营嵌入线索"} and stage in {"upstream", "midstream"} and hits:
        return "技术/工艺壁垒线索", f"主营位于{stage}，并命中“{'、'.join(hits[:4])}”等技术词；尚非壁垒确认。", ["F10主营构成", "chains.yaml"]
    if business_status in {"主营嵌入支持", "主营嵌入线索"} and stage == "upstream":
        return "上游位置线索", "主营位于产业链上游；是否具备难替代技术或认证壁垒仍待验证。", ["F10主营构成", "chains.yaml"]
    return "需人工确认", "没有足以确认技术、工艺、认证或资源壁垒的轻量证据。", []


def _survival_status(candidate: Mapping[str, Any]) -> tuple[str, str, bool]:
    name = _text(candidate.get("name"))
    if candidate.get("st_risk") is True or name.upper().startswith(("ST", "*ST")):
        return "风险排除", "存在 ST 或退市风险信号，轻筛阶段直接排除。", True
    audit = _text(candidate.get("audit_status"))
    if any(item in audit for item in ("否定", "无法表示", "保留")):
        return "风险排除", f"审计意见为“{audit}”，需先排除持续经营风险。", True
    net_profit = candidate.get("net_profit")
    pe_ttm = candidate.get("pe_ttm")
    if net_profit is not None and net_profit < 0:
        return "盈利待验证", "最新净利润为负，需验证是否属于可承受的反转阶段。", False
    if pe_ttm is not None and pe_ttm <= 0:
        return "盈利待验证", "动态 PE 非正，可能处于亏损或估值异常状态，不能直接当作便宜。", False
    if net_profit is not None and net_profit > 0:
        return "初步通过", "最新快照未见 ST 或亏损信号；债务、现金流和治理仍待深研。", False
    return "需人工确认", "轻量数据尚不足以判断现金、负债、审计与持续经营安全。", False


def _pricing_status(candidate: Mapping[str, Any]) -> tuple[str, str]:
    price = candidate.get("price_percentile_3y")
    congestion = candidate.get("market_congestion")
    pe_percentile = candidate.get("pe_percentile_5y")
    pb_percentile = candidate.get("pb_percentile_5y")
    if price is not None and price >= 0.85 and congestion is not None and congestion >= 0.70:
        return "高位且拥挤", "价格位置与拥挤度均偏高，板块逻辑不能直接转成配置理由。"
    low_percentiles = [value for value in (price, pe_percentile, pb_percentile) if value is not None]
    if low_percentiles and min(low_percentiles) <= 0.30:
        return "低位线索", "至少一项历史价格或估值分位偏低；是否存在预期差仍待验证。"
    if any(value is not None for value in (price, pe_percentile, pb_percentile, candidate.get("pe_ttm"), candidate.get("pb"))):
        return "位置待比较", "已有价格或估值快照，但缺少完整历史分位或拥挤度交叉验证。"
    return "需人工确认", "缺少历史价格位置、估值分位或拥挤度，无法判断安全边际。"


def _chips_status(candidate: Mapping[str, Any]) -> tuple[str, str]:
    action = _text(candidate.get("controller_action"))
    warning = _text(candidate.get("shareholder_warning"))
    if "减持" in action:
        return "筹码警示", f"检测到“{action}”；需在深研时核实主体、比例、是否完成和是否持续。"
    if warning:
        return "筹码提示", warning
    return "需人工确认", "轻量阶段未逐公告核验减持、质押和解禁。"


def _profit_status(candidate: Mapping[str, Any]) -> tuple[str, str]:
    yoy = candidate.get("profit_yoy")
    net_profit = candidate.get("net_profit")
    if yoy is not None and yoy > 0 and (net_profit is None or net_profit > 0):
        return "改善线索", f"净利润同比 {yoy:.1f}% 为正；仍需核验收入、现金流和一次性因素。"
    if yoy is not None and yoy < 0:
        return "承压", f"净利润同比 {yoy:.1f}% 为负，需验证是否已有边际改善。"
    if net_profit is not None and net_profit > 0:
        return "盈利", "最新净利润为正，但缺少同比趋势。"
    return "需人工确认", "未取得可比较的利润趋势。"


def _opportunity_model(
    business_status: str,
    barrier_status: str,
    pricing_status: str,
    profit_status: str,
) -> str:
    if barrier_status in {"已验证", "关键位置线索", "技术/工艺壁垒线索"}:
        return "瓶颈/技术候选"
    if business_status in {"主营嵌入支持", "主营嵌入线索"} and pricing_status == "低位线索":
        return "低位修复候选"
    if business_status in {"主营嵌入支持", "主营嵌入线索"} and profit_status in {"改善线索", "盈利"}:
        return "成长兑现候选"
    return "主营受益待验证"


def _rank_value(value: str, mapping: Mapping[str, int], fallback: int = 9) -> int:
    return mapping.get(value, fallback)


def _sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, int, float, float, float, float, str]:
    business = _text(row.get("business_status"))
    barrier = _text(row.get("barrier_status"))
    survival = _text(row.get("survival_status"))
    pricing = _text(row.get("pricing_status"))
    profit = _text(row.get("profit_status"))
    eligible = 0 if row.get("eligible_for_shortlist") else 1
    exposure = _text(row.get("concept_exposure_tier"))
    scale_available, scale_profit, scale_revenue, scale_market_cap = _scale_rank_values(
        row.get("industry_scale") if isinstance(row.get("industry_scale"), Mapping) else {}
    )
    return (
        eligible,
        _rank_value(exposure, CONCEPT_EXPOSURE_ORDER) if row.get("query_kind") == "concept" else 0,
        _rank_value(business, {"主营嵌入支持": 0, "主营嵌入线索": 1, "行业归属线索": 2, "主营文本线索": 3, "主题关联": 5, "需人工确认": 6}),
        _rank_value(barrier, {"已验证": 0, "关键位置线索": 1, "技术/工艺壁垒线索": 1, "上游位置线索": 2, "需人工确认": 4}),
        _rank_value(survival, {"初步通过": 0, "需人工确认": 1, "盈利待验证": 2, "风险排除": 6}),
        _rank_value(pricing, {"低位线索": 0, "位置待比较": 1, "需人工确认": 2, "高位且拥挤": 4}),
        _rank_value(profit, {"改善线索": 0, "盈利": 1, "需人工确认": 2, "承压": 3}),
        # Cross-sectional ranks may come from different sub-industries.  They
        # therefore break otherwise equal light-screen rows only.
        scale_available,
        scale_profit,
        scale_revenue,
        scale_market_cap,
        _text(row.get("code")),
    )


def _screen_candidate(
    candidate: Mapping[str, Any],
    business: Mapping[str, Any],
    sector: str,
    profile_names: set[str],
    *,
    query_kind: str,
) -> dict[str, Any]:
    evidence = {
        "name": candidate.get("name"),
        "security_name": candidate.get("name"),
        "industry": candidate.get("industry"),
        "concepts": list(candidate.get("concepts") or []),
        "main_business": _text(business.get("main_business")) or _text(candidate.get("main_business")),
        "business_items": list(business.get("business_items") or candidate.get("business_items") or []),
        "business_breakdown": list(business.get("business_breakdown") or candidate.get("business_breakdown") or []),
    }
    _enrich_chain(evidence)
    business_ready = _business_snapshot_ready(business)
    business_status, business_reason = _business_status(
        candidate,
        evidence,
        sector,
        profile_names,
        business_snapshot_ready=business_ready,
    )
    concept_exposure = _concept_exposure(
        candidate,
        evidence,
        sector,
        business_snapshot_ready=business_ready,
    )
    barrier_status, barrier_reason, barrier_refs = _barrier_status(candidate, evidence, business_status)
    survival_status, survival_reason, hard_reject = _survival_status(candidate)
    pricing_status, pricing_reason = _pricing_status(candidate)
    chips_status, chips_reason = _chips_status(candidate)
    profit_status, profit_reason = _profit_status(candidate)
    # A concept board is only a discovery list.  In concept mode, neither an
    # industry label nor a passing textual mention can substitute for a
    # disclosed, material revenue exposure.
    if query_kind == "concept":
        eligible = not hard_reject and concept_exposure["tier"] in {"核心主业", "重要业务"}
    else:
        eligible = not hard_reject and business_status not in {"主题关联", "需人工确认"}
    refs = [candidate.get("source") or "候选池"]
    if business_ready:
        refs.append("F10主营构成")
    if evidence.get("chain_name"):
        refs.append("chains.yaml")
    refs.extend(barrier_refs)
    return {
        "code": candidate.get("code"),
        "name": candidate.get("name"),
        "universe_match": candidate.get("universe_match", "需人工确认"),
        "industry": candidate.get("industry") or "需人工确认",
        "main_business": evidence.get("main_business") or "需人工确认",
        "business_items": evidence.get("business_items") or [],
        "chain_name": evidence.get("chain_name") or "需人工确认",
        "chain_stage": evidence.get("chain_stage") or "需人工确认",
        "business_status": business_status,
        "business_reason": business_reason,
        "business_chain_revenue_ratio": evidence.get("business_chain_revenue_ratio"),
        "query_kind": query_kind,
        "concept_exposure": concept_exposure,
        "concept_exposure_tier": concept_exposure["tier"],
        "concept_revenue_ratio": concept_exposure["revenue_ratio"],
        "business_purity": concept_exposure["tier"] if query_kind == "concept" else business_status,
        "barrier_status": barrier_status,
        "barrier_reason": barrier_reason,
        "survival_status": survival_status,
        "survival_reason": survival_reason,
        "profit_status": profit_status,
        "profit_reason": profit_reason,
        "pricing_status": pricing_status,
        "pricing_reason": pricing_reason,
        "chips_status": chips_status,
        "chips_reason": chips_reason,
        "opportunity_model": _opportunity_model(business_status, barrier_status, pricing_status, profit_status),
        "market_snapshot": {
            "pe_ttm": candidate.get("pe_ttm"),
            "pb": candidate.get("pb"),
            "market_cap": candidate.get("market_cap"),
            "price_percentile_3y": candidate.get("price_percentile_3y"),
            "pe_percentile_5y": candidate.get("pe_percentile_5y"),
            "pb_percentile_5y": candidate.get("pb_percentile_5y"),
            "market_congestion": candidate.get("market_congestion"),
        },
        "industry_scale": {},
        "industry_scale_fetch_state": "not_requested",
        "business_fetch_state": _text(business.get("fetch_state")) or "需人工确认",
        "eligible_for_shortlist": eligible,
        "evidence_refs": list(dict.fromkeys(item for item in refs if item)),
    }


def _candidate_summary(row: Mapping[str, Any]) -> str:
    parts = [
        _text(row.get("business_reason")),
        _text((row.get("concept_exposure") or {}).get("reason")),
        _text(row.get("barrier_reason")),
        _text(row.get("survival_reason")),
    ]
    scale = row.get("industry_scale") if isinstance(row.get("industry_scale"), Mapping) else {}
    scale_text = _scale_rank_text(scale)
    if scale_text != "需人工确认":
        parts.append(f"东财同行规模比较（仅同级条件下排序）：{scale_text}")
    return "；".join(parts)


def screen_sector(
    sector: str,
    *,
    root: str | Path | None = None,
    candidates: Sequence[str | Mapping[str, Any]] | None = None,
    universe_rows: Sequence[Mapping[str, Any]] | None = None,
    use_live_universe: bool = True,
    fetch_business: bool = True,
    business_timeout: int = DEFAULT_BUSINESS_TIMEOUT,
    business_workers: int = DEFAULT_BUSINESS_WORKERS,
    business_fetcher: Callable[[str, int], Mapping[str, Any]] | None = None,
    fetch_peer_snapshot: bool = False,
    peer_snapshot_timeout: int = DEFAULT_PEER_SNAPSHOT_TIMEOUT,
    peer_snapshot_workers: int = DEFAULT_PEER_SNAPSHOT_WORKERS,
    peer_snapshot_limit: int = DEFAULT_PEER_SNAPSHOT_LIMIT,
    peer_snapshot_fetcher: Callable[[str, int], Mapping[str, Any]] | None = None,
    shortlist_limit: int = DEFAULT_SHORTLIST_LIMIT,
    query_kind: str = "auto",
) -> dict[str, Any]:
    """Screen every available constituent and return a user-confirmation gate.

    The function is designed to be testable with ``universe_rows`` and a
    ``business_fetcher``.  In normal use it first attempts a live industry
    constituent universe and then falls back to the local partial list.
    """
    if shortlist_limit < 1 or shortlist_limit > MAX_SHORTLIST_LIMIT:
        raise ValueError(f"shortlist_limit 必须在 1 到 {MAX_SHORTLIST_LIMIT} 之间")
    if peer_snapshot_limit < 1:
        raise ValueError("peer_snapshot_limit 必须至少为 1")
    if query_kind not in QUERY_KINDS:
        raise ValueError("query_kind 必须是 auto、sector 或 concept")
    resolved_query_kind = infer_query_kind(sector) if query_kind == "auto" else query_kind
    base = Path(root) if root else ROOT
    universe, universe_metadata = resolve_sector_universe(
        base,
        sector,
        candidates=candidates,
        universe_rows=universe_rows,
        use_live_universe=use_live_universe,
        query_kind=resolved_query_kind,
    )
    universe_metadata["query_kind"] = resolved_query_kind
    businesses = collect_business_snapshots(
        universe,
        fetch=fetch_business,
        timeout=business_timeout,
        workers=business_workers,
        fetcher=business_fetcher,
    )
    profile_names = _profile_chain_names(sector)
    rows = [
        _screen_candidate(
            candidate,
            businesses.get(candidate["code"], {"fetch_state": "missing"}),
            sector,
            profile_names,
            query_kind=resolved_query_kind,
        )
        for candidate in universe
    ]
    base_ordered = sorted(rows, key=_sort_key)
    peer_targets = [
        row for row in base_ordered
        if row["eligible_for_shortlist"]
    ][:peer_snapshot_limit]
    peer_snapshots = collect_peer_scale_snapshots(
        peer_targets,
        fetch=fetch_peer_snapshot,
        timeout=peer_snapshot_timeout,
        workers=peer_snapshot_workers,
        fetcher=peer_snapshot_fetcher,
    )
    for row in rows:
        snapshot = peer_snapshots.get(row["code"], {"fetch_state": "not_selected"})
        row["industry_scale"] = snapshot
        row["industry_scale_fetch_state"] = _text(snapshot.get("fetch_state")) or "需人工确认"
        if snapshot.get("fetch_state") in {"ok", "provided"}:
            row["evidence_refs"] = list(dict.fromkeys([
                *row.get("evidence_refs", []),
                _text(snapshot.get("source")) or "AKShare/东方财富同行规模比较",
            ]))
    ordered = sorted(rows, key=_sort_key)
    eligible = [row for row in ordered if row["eligible_for_shortlist"]]
    shortlist = eligible[:shortlist_limit]
    shortlist_codes = {row["code"] for row in shortlist}
    watchlist = [row for row in eligible if row["code"] not in shortlist_codes]
    excluded = [row for row in ordered if not row["eligible_for_shortlist"]]
    for rank, row in enumerate(shortlist, start=1):
        row["rank"] = rank
        row["selection_reason"] = _candidate_summary(row)
    for row in watchlist:
        row["why_not_shortlist"] = "当前相对前六的主营嵌入、壁垒线索、生存性、位置或利润线索较弱；不等于没有投资价值。"
    coverage = {
        "total": len(ordered),
        "shortlist": len(shortlist),
        "watchlist": len(watchlist),
        "excluded": len(excluded),
        "business_supported": sum(row["business_status"] == "主营嵌入支持" for row in ordered),
        "business_clue": sum(row["business_status"] in {"主营嵌入线索", "行业归属线索", "主营文本线索"} for row in ordered),
        "theme_only": sum(row["business_status"] == "主题关联" for row in ordered),
        "business_unverified": sum(row["business_status"] == "需人工确认" for row in ordered),
        "business_fetch_failed": sum(row["business_fetch_state"] in {"failed", "empty", "invalid"} for row in ordered),
        "concept_core": sum(row["concept_exposure_tier"] == "核心主业" for row in ordered),
        "concept_material": sum(row["concept_exposure_tier"] == "重要业务" for row in ordered),
        "concept_marginal": sum(row["concept_exposure_tier"] == "边际受益" for row in ordered),
        "concept_unattributed": sum(row["concept_exposure_tier"] == "收入待核验" for row in ordered),
        "peer_scale_requested": len(peer_targets) if fetch_peer_snapshot else 0,
        "peer_scale_available": sum(
            row["industry_scale_fetch_state"] in {"ok", "provided"} for row in ordered
        ),
        "peer_scale_failed": sum(
            row["industry_scale_fetch_state"] in {"failed", "empty", "invalid"} for row in ordered
        ),
    }
    noun = "概念" if resolved_query_kind == "concept" else "板块"
    confirmation_prompt = (
        f"已完成{sector}{noun}全量轻筛，选出 {len(shortlist)} 家优先深研候选。是否继续对优先名单启动完整个股研究，扩展到 Top 12，或对全{noun}逐家深研？"
        if shortlist
        else f"已完成{sector}{noun}全量轻筛，但未形成收入可归因的优先名单。是否补充候选池或 F10 主营数据后重试，扩展到 Top 12，或对全{noun}逐家深研？"
    )
    result = {
        "schema_version": 1,
        "screening_type": "moda_full_universe_quick_screen",
        "sector": sector,
        "query_kind": resolved_query_kind,
        "universe": {**universe_metadata, **coverage},
        "selection_rules": [
            "先覆盖可获得的全量行业成分股；行业成分股、主营嵌入和主题关联分开处理。"
            if resolved_query_kind == "sector"
            else "概念模式先覆盖可获得的概念成分股；概念板块只用于发现候选，不用行业成分股替代。",
            "先排除 ST、退市或明确持续经营风险，再看主营受益纯度。",
            "概念模式只有 F10 分部收入可归因且占比至少 20% 的核心主业或重要业务可进入优先深研；边际受益、收入待核验和纯题材不入围。"
            if resolved_query_kind == "concept"
            else "概念标签只作候选线索，不能替代主营或产业链受益证据。",
            "产业链上游、核心设备、材料、工艺、认证和关键位置只在有 F10 或静态资料线索时优先；线索不等于壁垒已验证。",
            "价格位置、利润趋势和股东筹码用于相对比较，不汇总成综合分，也不生成买卖结论。",
            "AKShare/东财同行规模排名只在主营、壁垒、生存、位置和利润条件相同的候选之间作透明末级排序；不把它当成完整同行池或公司优劣证明。",
            "前六仅是优先深研名单；完整个股流水线必须等待用户确认。",
        ],
        "shortlist": shortlist,
        "watchlist": watchlist,
        "excluded": excluded,
        "all_candidates": ordered,
        "next_action": {
            "requires_user_confirmation": True,
            "prompt": confirmation_prompt,
            "options": [
                {"id": "deep_research_top6", "label": "对前六做完整深研", "recommended": True},
                {"id": "expand_to_top12", "label": "扩展到 Top 12 后再选"},
                {"id": "deep_research_all", "label": "全板块逐家深研", "warning": "耗时较长，且多数公司可能不值得投入完整研究资源。"},
            ],
        },
        "not_stock_decision": True,
        "research_score_used": False,
        "full_pipeline_triggered": False,
    }
    validate_quick_screen(result)
    return result


def validate_quick_screen(payload: Mapping[str, Any]) -> None:
    if payload.get("screening_type") != "moda_full_universe_quick_screen":
        raise ValueError("不是有效的板块快速筛选结果")
    if payload.get("research_score_used") is not False or payload.get("full_pipeline_triggered") is not False:
        raise ValueError("板块轻筛不得使用正式研究分或触发完整个股流水线")
    shortlist = payload.get("shortlist") if isinstance(payload.get("shortlist"), list) else []
    query_kind = _text(payload.get("query_kind")) or "sector"
    if query_kind not in QUERY_KINDS - {"auto"}:
        raise ValueError("轻筛结果缺少有效的 query_kind")
    if len(shortlist) > MAX_SHORTLIST_LIMIT:
        raise ValueError("前置候选数超过轻筛上限")
    for row in shortlist:
        if not isinstance(row, Mapping) or not row.get("eligible_for_shortlist"):
            raise ValueError("轻筛入围名单包含不合格候选")
        if "research_score" in row or "decision" in row:
            raise ValueError("轻筛候选不得携带正式评分或五态结论")
        if query_kind == "concept" and _text(row.get("concept_exposure_tier")) not in {"核心主业", "重要业务"}:
            raise ValueError("概念轻筛入围名单必须有可归因的核心或重要收入暴露")
    next_action = payload.get("next_action") if isinstance(payload.get("next_action"), Mapping) else {}
    if next_action.get("requires_user_confirmation") is not True:
        raise ValueError("轻筛完成后必须要求用户确认是否进入深研")


def _display_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "需人工确认"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _cell(value: Any) -> str:
    return _text(value).replace("|", "/").replace("\n", " ") or "需人工确认"


def render_quick_screen(payload: Mapping[str, Any]) -> str:
    """Render the light-screen result without turning it into a stock call."""
    validate_quick_screen(payload)
    universe = payload.get("universe") if isinstance(payload.get("universe"), Mapping) else {}
    shortlist = payload.get("shortlist") if isinstance(payload.get("shortlist"), list) else []
    watchlist = payload.get("watchlist") if isinstance(payload.get("watchlist"), list) else []
    excluded = payload.get("excluded") if isinstance(payload.get("excluded"), list) else []
    query_kind = _text(payload.get("query_kind")) or "sector"
    title_kind = "概念" if query_kind == "concept" else "板块"
    lines = [
        f"# 莫大 Agent {title_kind}全量轻筛：{_cell(payload.get('sector'))}",
        "",
        "这是基于公开材料整理的方法型 AI，不代表莫大本人或其最新观点。",
        "",
        "## 覆盖范围",
        "",
        f"- 候选来源：{_cell(universe.get('source'))}；覆盖状态：{_cell(universe.get('coverage_status'))}。",
        f"- 输入识别：{_cell(query_kind)}；概念核心主业：{universe.get('concept_core', 0)} 家；重要业务：{universe.get('concept_material', 0)} 家；边际受益：{universe.get('concept_marginal', 0)} 家；收入待核验：{universe.get('concept_unattributed', 0)} 家。",
        f"- 全量候选：{universe.get('total', 0)} 家；主营嵌入支持：{universe.get('business_supported', 0)} 家；主营/行业线索：{universe.get('business_clue', 0)} 家；主题关联：{universe.get('theme_only', 0)} 家；排除：{universe.get('excluded', 0)} 家。",
    ]
    if universe.get("peer_scale_requested") or universe.get("peer_scale_available"):
        lines.append(
            f"- 同行规模快照：请求 {universe.get('peer_scale_requested', 0)} 家；"
            f"可用 {universe.get('peer_scale_available', 0)} 家；"
            f"失败/空 {universe.get('peer_scale_failed', 0)} 家。"
        )
    if universe.get("selection_note"):
        lines.append(f"- 范围说明：{_cell(universe.get('selection_note'))}")
    if universe.get("live_fallback"):
        lines.append(f"- 数据降级：{_cell(universe.get('live_fallback'))}")
    if universe.get("error"):
        lines.append(f"- 数据状态：{_cell(universe.get('error'))}；结果按可获得候选展示，不把缺失写成行业事实。")
    lines += ["", "## 快速筛选口径", ""]
    lines.extend(f"- {_cell(item)}" for item in payload.get("selection_rules", []))
    lines += ["", "## 前六：优先深研候选", ""]
    if not shortlist:
        lines += ["没有形成可进入优先深研名单的主营嵌入候选；不以概念股凑足六家。", ""]
    else:
        lines += [
            "| 排名 | 公司 | 产业位置/受益 | 概念收入暴露 | 技术与稀缺性 | 生存性 | 位置 | 行业横截面 | 筹码 | 入围理由 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in shortlist:
            company = f"{_cell(row.get('name'))}（{_cell(row.get('code'))}）"
            position = f"{_cell(row.get('chain_stage'))} / {_cell(row.get('business_status'))}"
            scale = _scale_rank_text(
                row.get("industry_scale") if isinstance(row.get("industry_scale"), Mapping) else {}
            )
            lines.append(
                "| " + " | ".join([
                    _cell(row.get("rank")), company, position,
                    _cell(row.get("concept_exposure_tier")),
                    _cell(row.get("barrier_status")), _cell(row.get("survival_status")),
                    _cell(row.get("pricing_status")), _cell(scale), _cell(row.get("chips_status")),
                    _cell(row.get("selection_reason")),
                ]) + " |"
            )
        lines.append("")
    lines += ["## 观察池", ""]
    if not watchlist:
        lines += ["暂无主营嵌入但相对前六不够强的观察候选。", ""]
    else:
        lines += ["| 公司 | 当前模型 | 主要缺口 |", "| --- | --- | --- |"]
        for row in watchlist[:6]:
            company = f"{_cell(row.get('name'))}（{_cell(row.get('code'))}）"
            gap = _cell(row.get("why_not_shortlist"))
            lines.append(f"| {company} | {_cell(row.get('opportunity_model'))} | {gap} |")
        if len(watchlist) > 6:
            lines.append(f"| … | 其余 {len(watchlist) - 6} 家 | 完整清单见 JSON 结果。 |")
        lines.append("")
    lines += ["## 淘汰或暂不纳入", ""]
    if not excluded:
        lines += ["暂无因主题关联、主营缺失或明确风险而排除的候选。", ""]
    else:
        lines += ["| 公司 | 概念暴露 | 原因 |", "| --- | --- | --- |"]
        for row in excluded[:6]:
            company = f"{_cell(row.get('name'))}（{_cell(row.get('code'))}）"
            reason = f"{_cell(row.get('business_status'))}；{_cell(row.get('survival_status'))}；{_cell((row.get('concept_exposure') or {}).get('reason'))}"
            lines.append(f"| {company} | {_cell(row.get('concept_exposure_tier'))} | {reason} |")
        if len(excluded) > 6:
            lines.append(f"| … | 其余 {len(excluded) - 6} 家见完整 JSON 结果。 |")
        lines.append("")
    lines += [
        "## 结论与下一步",
        "",
        "本页是全量轻筛的相对优先级，不是完整报告，也不是买卖结论。技术/工艺壁垒、客户认证、现金负债、减持比例和市场预期仍需在个股深研中核验。",
        "",
        f"- {_cell((payload.get('next_action') or {}).get('prompt'))}",
        "",
    ]
    return "\n".join(lines)
