#!/usr/bin/env python3
"""Health check for the retained, URL-citable search backends."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {"DDG_LITE_URL": "https://lite.duckduckgo.com/lite/"}
SECRET_PLACEHOLDERS = {"", "YOUR_API_KEY_HERE", "***", "<REDACTED>"}


def load_local_env(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    allowed = {
        "DDG_LITE_URL", "BRAVE_SEARCH_URL", "BRAVE_SEARCH_API_KEY",
        "DEEPSEEK_API_KEY", "DEEPSEEK_ANTHROPIC_BASE_URL", "DEEPSEEK_WEB_SEARCH_MODEL",
        "OPENAI_API_KEY", "OPENAI_RESPONSES_URL", "OPENAI_WEB_SEARCH_MODEL",
        "MODA_MODEL_SEARCH_URL", "MODA_MODEL_SEARCH_MODEL", "MODA_MODEL_SEARCH_API_KEY",
    }
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configured(key: str) -> str:
    return os.getenv(key, DEFAULTS[key]).strip() or DEFAULTS[key]


def _configured_secret(name: str) -> bool:
    return os.getenv(name, "").strip() not in SECRET_PLACEHOLDERS


def _public_search_probe(url: str, timeout: float) -> tuple[bool, str]:
    try:
        response = requests.get(
            url,
            params={"q": "moda-v4 research"},
            headers={"User-Agent": "moda-v4-search-stack/1.0"},
            timeout=timeout,
        )
        text = response.text.lower()
        if response.status_code >= 400:
            return False, f"http_{response.status_code}"
        if "captcha" in text[:20_000] or "unusual traffic" in text[:20_000]:
            return False, "anti_bot"
        return ("result-link" in text, "search_ok" if "result-link" in text else "no_result_markup")
    except requests.RequestException as exc:
        return False, type(exc).__name__


def check_stack(timeout: float = 3.0) -> dict[str, Any]:
    """Do not spend paid-model quota in a health check."""
    load_local_env()
    lite_url = configured("DDG_LITE_URL")
    lite_ok, lite_detail = _public_search_probe(lite_url, timeout)
    endpoints = {
        "duckduckgo_lite": {"url": lite_url, "ok": lite_ok, "detail": lite_detail},
        "deepseek_web_search": {
            "url": os.getenv("DEEPSEEK_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").strip(),
            "ok": _configured_secret("DEEPSEEK_API_KEY"),
            "detail": "configured_not_probed" if _configured_secret("DEEPSEEK_API_KEY") else "not_configured",
        },
        "openai_web_search": {
            "url": os.getenv("OPENAI_RESPONSES_URL", "https://api.openai.com/v1/responses").strip(),
            "ok": _configured_secret("OPENAI_API_KEY"),
            "detail": "configured_not_probed" if _configured_secret("OPENAI_API_KEY") else "not_configured",
        },
        "brave_search": {
            "url": os.getenv("BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search").strip(),
            "ok": _configured_secret("BRAVE_SEARCH_API_KEY"),
            "detail": "configured_not_probed" if _configured_secret("BRAVE_SEARCH_API_KEY") else "not_configured",
        },
    }
    recommended = next((name for name in ("duckduckgo_lite", "deepseek_web_search", "openai_web_search", "brave_search") if endpoints[name]["ok"]), "none")
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "endpoints": endpoints,
        "recommended": recommended,
        "ok": recommended != "none",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check moda-v4 search backends")
    parser.add_argument("command", choices=["check"], nargs="?", default="check")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = check_stack(timeout=args.timeout)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
