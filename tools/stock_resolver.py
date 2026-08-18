"""Fast A-share stock code/name resolution.

The resolver keeps a tiny JSON index next to the pipeline caches.  Code input
is accepted directly; Chinese names are resolved locally first and fall back
to efinance only on a cache miss.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "knowledge" / "research" / "pipeline" / "cache" / "stock_name_index.json"
_INDEX: dict[str, dict[str, str]] | None = None

INVALID_STOCK_NAMES = {
    "代码", "简称", "股票代码", "股票名称", "证券代码", "证券简称",
    "市盈率", "市净率", "总市值", "行业", "指标", "数值",
}


def normalize_stock_text(value: Any) -> str:
    """Normalize names while preserving Chinese characters."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"[\s()（）\[\]【】._-]+", "", text)
    return text


def _valid_code(value: Any) -> str | None:
    match = re.fullmatch(r"\d{6}", str(value or "").strip())
    return match.group(0) if match else None


def _valid_name(value: Any, code: str = "") -> str | None:
    name = str(value or "").strip()
    if not name or name == code or name in INVALID_STOCK_NAMES or _valid_code(name):
        return None
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", name):
        return None
    # A-share display names are short labels. Long sentences here normally
    # mean a provider column shifted and returned business-scope text.
    if len(name) > 24 or re.search(r"[；;。\r\n]", name):
        return None
    if any(term in name for term in ("依法须经批准", "经营范围", "许可证管理商品", "技术咨询、技术培训")):
        return None
    return name


def _valid_aliases(value: Any, code: str = "", name: str = "") -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else re.split(r"[、,，/;；\s]+", str(value or ""))
    aliases: list[str] = []
    for raw in values:
        alias = _valid_name(raw, code)
        if not alias or alias == name or alias in aliases:
            continue
        aliases.append(alias)
    return aliases


def _read_index() -> dict[str, dict[str, str]]:
    if CACHE_PATH.exists():
        try:
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cleaned: dict[str, dict[str, str]] = {}
                for raw_code, item in payload.items():
                    code = _valid_code(raw_code)
                    if not code or not isinstance(item, dict):
                        continue
                    name = _valid_name(item.get("name"), code)
                    if name:
                        cleaned[code] = {**dict(item), "code": code, "name": name}
                        aliases = _valid_aliases(item.get("aliases"), code, name)
                        if aliases:
                            cleaned[code]["aliases"] = aliases
                return cleaned
        except (OSError, ValueError, TypeError):
            pass
    return {}


def _write_index(index: dict[str, dict[str, str]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _seed_from_reports(index: dict[str, dict[str, str]]) -> None:
    """Best-effort seed from already collected finance reports."""
    report_dir = ROOT / "knowledge" / "research" / "finance_data"
    if not report_dir.exists():
        return
    patterns = (
        re.compile(r"股票名称\s*[：:]\s*([^|\n]+)"),
        re.compile(r"^#\s*基本面\+行情报告\s*:\s*([^（(\n]+)[（(](\d{6})[）)]", re.MULTILINE),
    )
    for path in report_dir.glob("*.md"):
        code = _valid_code(path.stem)
        if not code or code in index:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        name = ""
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            candidate = _valid_name(match.group(1), code)
            if candidate:
                name = candidate
                break
        if name:
            index[code] = {"code": code, "name": name, "source": "local_report"}


def load_index() -> dict[str, dict[str, str]]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _read_index()
        before = len(_INDEX)
        _seed_from_reports(_INDEX)
        if len(_INDEX) != before or CACHE_PATH.exists():
            try:
                _write_index(_INDEX)
            except OSError:
                pass
    return _INDEX


def _remember(rows: list[dict[str, Any]]) -> None:
    index = load_index()
    changed = False
    for row in rows:
        code = _valid_code(row.get("code"))
        name = _valid_name(row.get("name"), code or "")
        if code and name:
            item = {"code": code, "name": name, "source": str(row.get("source") or "efinance")}
            aliases = _valid_aliases(row.get("aliases"), code, name)
            if aliases:
                item["aliases"] = aliases
            if index.get(code) != item:
                index[code] = item
                changed = True
    if changed:
        try:
            _write_index(index)
        except OSError:
            pass


def lookup_local(keyword: str, limit: int = 20) -> list[dict[str, str]]:
    query = normalize_stock_text(keyword)
    if not query:
        return []
    index = load_index()
    invalid_codes = [
        code for code, row in index.items()
        if not _valid_name(row.get("name"), code)
    ]
    for code in invalid_codes:
        index.pop(code, None)
    rows = list(index.values())
    exact = [
        row for row in rows
        if normalize_stock_text(row.get("code")) == query
        or normalize_stock_text(row.get("name")) == query
        or any(normalize_stock_text(alias) == query for alias in row.get("aliases", []))
    ]
    if exact:
        return exact[:limit]
    return [
        row for row in rows
        if query in normalize_stock_text(row.get("name"))
        or query in normalize_stock_text(row.get("code"))
        or any(query in normalize_stock_text(alias) for alias in row.get("aliases", []))
    ][:limit]


def _cninfo_profile_lookup(code: str) -> list[dict[str, Any]]:
    """Resolve a code through CNINFO's current issuer profile as a fallback."""
    try:
        import akshare as ak

        profile = ak.stock_profile_cninfo(symbol=code)
        if profile is None or profile.empty:
            return []
        row = profile.iloc[0].to_dict()
        name = _valid_name(row.get("A股简称") or row.get("证券简称"), code)
        if not name:
            return []
        return [{
            "code": code,
            "name": name,
            "aliases": _valid_aliases(row.get("曾用简称"), code, name),
            "source": "AKShare/CNINFO profile",
        }]
    except Exception:
        return []


def _classification_name_lookup(code: str) -> list[dict[str, Any]]:
    """Use the local classification list only after live name sources fail."""
    try:
        from tools.scoring.classification_db import lookup

        match = lookup(code)
        name = _valid_name(match.get("name"), code) if match.get("found") else None
        return [{"code": code, "name": name, "source": str(match.get("source") or "classification_db")}] if name else []
    except Exception:
        return []


def resolve_stock_input(value: str, name: str = "") -> tuple[str, str]:
    """Return ``(code, display_name)`` for a six-digit code or Chinese name."""
    raw = str(value or "").strip()
    code = _valid_code(raw)
    if code:
        requested_name = _valid_name(name, code)
        if requested_name:
            return code, requested_name
        hits = lookup_local(code, limit=1)
        if not hits:
            try:
                from tools.efinance.provider import search_stock

                remote = search_stock(code, limit=20)
                exact = [row for row in remote if _valid_code(row.get("code")) == code and _valid_name(row.get("name"), code)]
                _remember(exact)
                hits = exact[:1]
            except Exception:
                hits = []
        if not hits:
            hits = _cninfo_profile_lookup(code)
            _remember(hits)
        if not hits:
            hits = _classification_name_lookup(code)
            _remember(hits)
        return code, (hits[0].get("name") or code) if hits else code

    hits = lookup_local(raw)
    if not hits:
        try:
            from tools.efinance.provider import search_stock

            hits = search_stock(raw, limit=20)
            _remember(hits)
        except Exception:
            hits = []
    if not hits:
        raise ValueError(f"未找到股票：{raw}")
    exact = [row for row in hits if normalize_stock_text(row.get("name")) == normalize_stock_text(raw)]
    candidates = exact or hits
    if len(candidates) > 1 and not exact:
        labels = "、".join(f"{row.get('name', '')}({row.get('code', '')})" for row in candidates[:8])
        raise ValueError(f"股票名称不唯一，请改用代码：{labels}")
    selected = candidates[0]
    return str(selected["code"]), str(selected.get("name") or raw)


def clear_memory_cache() -> None:
    global _INDEX
    _INDEX = None
