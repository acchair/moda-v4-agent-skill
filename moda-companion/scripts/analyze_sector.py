#!/usr/bin/env python3
"""Run a Moda full-universe quick screen or a deep sector judgment.

The default screen works from available industry constituents, market
snapshots, and F10 business composition.  It returns a short research list
without invoking the individual-stock pipeline.  ``research`` mode preserves
the existing evidence-first sector card for user-confirmed deep work.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
ANALYZE_A_SHARE = ROOT / "scripts" / "analyze_a_share.py"


def _load_a_share_module():
    spec = importlib.util.spec_from_file_location("moda_companion_a_share", ANALYZE_A_SHARE)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 moda-companion 个股采集器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s\-_/，、,;；()（）【】\[\]]+", "", text)
    return text


def _sector_variants(sector: str) -> list[str]:
    raw = _normalize(sector)
    variants = [raw]
    for suffix in ("板块选股", "产业链", "概念股", "概念", "板块", "行业", "股票", "选股"):
        if raw.endswith(suffix):
            variants.append(raw[: -len(suffix)])
    return list(dict.fromkeys(item for item in variants if len(item) >= 2))


def _market_cap(value: Any) -> float:
    try:
        return float(str(value or "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def discover_sector_candidates(root: Path, sector: str, limit: int = 5) -> dict[str, Any]:
    """Find a bounded local research universe; no market or web request is made."""
    if limit < 1 or limit > 8:
        raise ValueError("limit 必须在 1 到 8 之间")
    variants = _sector_variants(sector)
    csv_path = root / "tools" / "scoring" / "专精特新_行业龙头_核心供应商_A股名单_完整版.csv"
    if not csv_path.is_file():
        return {
            "sector": sector,
            "source": "本地名单数据库不可用",
            "selection_note": "未能建立候选研究池，需人工指定候选公司。",
            "candidates": [],
        }

    matched: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            industry = _normalize(row.get("行业名称"))
            concepts = _normalize(row.get("概念名称"))
            name = _normalize(row.get("证券简称"))
            match_field = ""
            match_score = 0
            for variant in variants:
                if variant and variant in industry:
                    match_field, match_score = "行业名称", max(match_score, 3)
                if variant and variant in concepts and match_score < 3:
                    match_field, match_score = "概念名称", max(match_score, 2)
                if variant and variant in name and match_score < 2:
                    match_field, match_score = "证券简称", max(match_score, 1)
            code = str(row.get("证券代码") or "").strip().zfill(6)
            name_value = str(row.get("证券简称") or "").strip()
            if not match_score or not re.fullmatch(r"\d{6}", code) or not name_value:
                continue
            category = str(row.get("分类") or "").strip()
            category_rank = 0 if "行业龙头" in category else 1 if "核心供应商" in category else 2 if "专精特新" in category else 3
            matched.append({
                "code": code,
                "name": name_value,
                "industry": str(row.get("行业名称") or "").strip(),
                "category": category,
                "match_field": match_field,
                "match_status": "本地候选池命中",
                "source": csv_path.name,
                "_sort": (-match_score, category_rank, -_market_cap(row.get("总市值(亿元)")), code),
            })

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(matched, key=lambda item: item["_sort"]):
        if row["code"] in seen:
            continue
        seen.add(row["code"])
        row.pop("_sort", None)
        selected.append(row)
        if len(selected) >= limit:
            break
    return {
        "sector": sector,
        "source": csv_path.name,
        "selection_note": "候选仅按本地行业/概念字段和公开分类建立研究池，不是行业全样本，也不是投资排序。",
        "candidates": selected,
    }


def _load_sector_module(root: Path):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools import sector_analysis

    return sector_analysis


def _load_screening_module(root: Path):
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from tools import sector_screening

    return sector_screening


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _load_sector_evidence(path_text: str) -> dict[str, Any] | None:
    if not path_text:
        return None
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取行业级事实 JSON: {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("行业级事实 JSON 必须是对象")
    return dict(payload)


def _sector_filename(sector: str) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", str(sector or "板块")).strip("_")
    return (text or "板块")[:48]


def screen_sector(
    sector: str,
    candidates: Sequence[str | Mapping[str, Any]] | None = None,
    *,
    refresh: bool = False,
    save: bool = False,
    moda_root: str | Path | None = None,
    universe_rows: Sequence[Mapping[str, Any]] | None = None,
    use_live_universe: bool = True,
    fetch_business: bool = True,
    business_timeout: int = 4,
    business_workers: int = 1,
    fetch_peer_snapshot: bool = False,
    shortlist_limit: int = 6,
    query_kind: str = "auto",
) -> dict[str, Any]:
    """Run the full-universe lightweight screen without a stock pipeline.

    ``refresh`` is accepted for a stable public entrypoint.  The initial
    implementation has no persistent screening cache, so every invocation
    uses the current provider response where available.
    """
    del refresh
    a_share = _load_a_share_module()
    root = a_share.find_moda_root(moda_root)
    screening = _load_screening_module(root)
    result = screening.screen_sector(
        sector,
        root=root,
        candidates=candidates,
        universe_rows=universe_rows,
        use_live_universe=use_live_universe,
        fetch_business=fetch_business,
        business_timeout=business_timeout,
        business_workers=business_workers,
        fetch_peer_snapshot=fetch_peer_snapshot,
        shortlist_limit=shortlist_limit,
        query_kind=query_kind,
    )
    markdown = screening.render_quick_screen(result)
    result = {**result, "markdown": markdown}
    if save:
        output_dir = root / "knowledge" / "research" / "sectors"
        filename = _sector_filename(sector)
        markdown_path = output_dir / f"{filename}_screening.md"
        json_path = output_dir / f"{filename}_screening.json"
        _write_text(markdown_path, markdown)
        _write_text(json_path, json.dumps(result, ensure_ascii=False, indent=2))
        result["saved_path"] = str(markdown_path)
        result["json_path"] = str(json_path)
    return result


def analyze_sector(
    sector: str,
    candidates: Sequence[str] | None = None,
    *,
    refresh: bool = False,
    save: bool = False,
    collect: bool = False,
    max_candidates: int = 3,
    moda_root: str | Path | None = None,
    sector_evidence: Mapping[str, Any] | None = None,
    collect_sector: bool = False,
    sector_context: str = "",
    sector_provider: str | None = None,
    sector_timeout: int = 12,
) -> dict[str, Any]:
    """Return a sector card from bounded candidate research artifacts.

    ``collect`` is intentionally opt-in because it may run several full stock
    pipelines. ``collect_sector`` is a separate, explicit opt-in for bounded
    industry web evidence; it never treats candidate aggregation as an
    industry-level conclusion.
    """
    a_share = _load_a_share_module()
    root = a_share.find_moda_root(moda_root)
    requested = [str(item).strip() for item in (candidates or []) if str(item).strip()]
    universe = discover_sector_candidates(root, sector, max_candidates)
    if not requested:
        requested = [item["code"] for item in universe["candidates"]]
    else:
        universe = {
            "sector": sector,
            "source": "用户指定候选",
            "selection_note": "候选由用户指定；仍按事实包比较，不把输入顺序当成投资排序。",
            "candidates": [],
        }
    requested = list(dict.fromkeys(requested))[:max_candidates]

    artifacts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for query in requested:
        try:
            code, name = a_share._resolve_stock(root, query)
            result = (
                a_share.analyze_a_share(query, refresh=refresh, save=False, moda_root=root, run_pipeline=True)
                if collect
                else a_share.load_analysis(root, code, name)
            )
            if result.get("collector_status") != "ready":
                errors.append({"candidate": f"{name}({code})", "reason": "事实包过期，需重新采集。"})
                continue
            artifacts.append(json.loads(Path(result["scorecard_path"]).read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError, RuntimeError, OSError, json.JSONDecodeError):
            errors.append({"candidate": query, "reason": "未生成可用事实包，需人工确认。"})

    sector_module = _load_sector_module(root)
    judgment = sector_module.build_sector_judgment(
        sector,
        artifacts,
        sector_evidence=sector_evidence,
        collect_sector=collect_sector,
        sector_context=sector_context,
        sector_provider=sector_provider,
        sector_timeout=sector_timeout,
    )
    markdown = sector_module.render_sector_judgment(judgment)
    result: dict[str, Any] = {
        "sector": sector,
        "candidate_universe": universe,
        "candidate_errors": errors,
        "judgment": judgment,
        "markdown": markdown,
        "not_stock_decision": True,
        "sector_evidence_collection": judgment.get("sector_evidence_collection", {}),
    }
    if save:
        output_dir = root / "knowledge" / "research" / "sectors"
        filename = _sector_filename(sector)
        markdown_path = output_dir / f"{filename}.md"
        json_path = output_dir / f"{filename}.json"
        _write_text(markdown_path, markdown)
        _write_text(json_path, json.dumps(result, ensure_ascii=False, indent=2))
        result["saved_path"] = str(markdown_path)
        result["json_path"] = str(json_path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen a full sector, or build a deep sector judgment from candidate facts")
    parser.add_argument("sector", help="板块、产业链或概念名称")
    parser.add_argument("--mode", choices=("screen", "research"), default="screen", help="screen 为全量轻筛（默认）；research 为已有事实包的深度板块判断")
    parser.add_argument("--candidate", action="append", default=[], help="候选股票代码或名称，可重复；screen 模式下会缩小候选池，不代表行业全样本")
    parser.add_argument("--universe-file", default="", help="全量候选池 JSON/CSV；screen 模式可用于可复现筛选")
    parser.add_argument("--screen-limit", type=int, default=6, help="screen 模式输出的优先深研候选数，默认 6，最大 12")
    parser.add_argument("--kind", choices=("auto", "sector", "concept"), default="auto", help="概念模式优先概念成分股，并要求 F10 分部收入可归因")
    parser.add_argument("--no-live-universe", action="store_true", help="screen 模式不请求实时行业成分股，只使用用户候选或本地回退名单")
    parser.add_argument("--no-business-fetch", action="store_true", help="screen 模式不抓取 F10 主营构成；会降低主营与壁垒判断的覆盖度")
    parser.add_argument("--no-peer-snapshot", action="store_true", help="screen 模式不抓取 AKShare/东财同行规模排名；默认只作轻筛末级排序")
    parser.add_argument("--business-timeout", type=int, default=4, help="screen 模式单家公司 F10 采集超时秒数，默认 4")
    parser.add_argument("--business-workers", type=int, default=1, help="screen 模式并行 F10 采集数，默认 1；东方财富 F10 按单线程节流")
    parser.add_argument("--max-candidates", type=int, default=3, help="最多读取或采集的候选数，默认 3，最大 8")
    parser.add_argument("--collect", action="store_true", help="research 模式下为候选运行完整个股采集；screen 模式禁止触发")
    parser.add_argument("--sector-evidence", default="", help="行业级事实 JSON；可直接传 collector 返回的完整 JSON 或 sections 对象")
    parser.add_argument("--collect-sector", action="store_true", help="显式采集行业级网页证据；默认不联网")
    parser.add_argument("--sector-context", default="", help="行业采集上下文，仅在 --collect-sector 时使用")
    parser.add_argument("--sector-provider", default="", help="行业网页证据提供方，仅在 --collect-sector 时使用")
    parser.add_argument("--sector-timeout", type=int, default=12, help="行业网页证据采集超时秒数，默认 12")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--save", action="store_true")
    parser.add_argument("--moda-root")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    try:
        if args.mode == "screen":
            if args.collect:
                raise ValueError("screen 模式不运行完整个股采集；请先确认前六，再使用 research 模式深研。")
            if args.collect_sector:
                raise ValueError("screen 模式不采集行业网页证据；行业深度判断请使用 --mode research。")
            moda_root = _load_a_share_module().find_moda_root(args.moda_root)
            screening = _load_screening_module(moda_root)
            universe_rows = screening.load_universe_file(args.universe_file) if args.universe_file else None
            result = screen_sector(
                args.sector,
                args.candidate,
                refresh=args.refresh,
                save=args.save,
                moda_root=args.moda_root,
                universe_rows=universe_rows,
                use_live_universe=not args.no_live_universe,
                fetch_business=not args.no_business_fetch,
                business_timeout=args.business_timeout,
                business_workers=args.business_workers,
                fetch_peer_snapshot=not args.no_peer_snapshot,
                shortlist_limit=args.screen_limit,
                query_kind=args.kind,
            )
        else:
            sector_evidence = _load_sector_evidence(args.sector_evidence)
            result = analyze_sector(
                args.sector,
                args.candidate,
                refresh=args.refresh,
                save=args.save,
                collect=args.collect,
                max_candidates=args.max_candidates,
                moda_root=args.moda_root,
                sector_evidence=sector_evidence,
                collect_sector=args.collect_sector,
                sector_context=args.sector_context,
                sector_provider=args.sector_provider or None,
                sector_timeout=args.sector_timeout,
            )
    except (TypeError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["markdown"], end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
