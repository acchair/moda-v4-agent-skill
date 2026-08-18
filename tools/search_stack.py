#!/usr/bin/env python3
"""Cross-platform search-stack health checks and optional DDG MCP bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    "SEARXNG_URL": "http://127.0.0.1:8888",
    "DDG_MCP_URL": "http://127.0.0.1:7070/mcp",
    "DDG_HTML_URL": "https://html.duckduckgo.com/html/",
    "DDG_LITE_URL": "https://lite.duckduckgo.com/lite/",
    "SO360_SEARCH_URL": "https://www.so.com/s",
}


def load_local_env(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {
            "SEARXNG_URL", "DDG_MCP_URL", "DDG_HTML_URL", "DDG_LITE_URL", "SO360_SEARCH_URL",
            "BRAVE_SEARCH_URL", "BRAVE_SEARCH_API_KEY",
            "MODA_MODEL_SEARCH_PROVIDER", "MODA_MODEL_SEARCH_URL", "MODA_MODEL_SEARCH_MODEL",
            "MODA_MODEL_SEARCH_API_KEY", "OPENAI_API_KEY", "OPENAI_WEB_SEARCH_MODEL",
        }:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def configured(key: str) -> str:
    return os.getenv(key, DEFAULTS[key]).strip() or DEFAULTS[key]


def _tcp_probe(url: str, timeout: float) -> tuple[bool, str]:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False, "invalid_url"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True, "tcp_ok"
    except OSError as exc:
        return False, type(exc).__name__


def _http_probe(url: str, timeout: float) -> tuple[bool, str]:
    try:
        response = requests.get(
            url,
            params={"q": "moda-v4", "format": "json", "language": "zh-CN"},
            headers={"User-Agent": "moda-v4-search-stack/1.0"},
            timeout=timeout,
        )
        if response.status_code < 500:
            return True, f"http_{response.status_code}"
        return False, f"http_{response.status_code}"
    except requests.RequestException as exc:
        return False, type(exc).__name__


def _public_search_probe(url: str, timeout: float, marker: str) -> tuple[bool, str]:
    """Probe a result page, not merely its TCP port, to catch anti-bot pages."""
    try:
        response = requests.get(
            url,
            params={"q": "moda-v4 research"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        full_text = response.text.lower()
        if response.status_code == 202 or "anomaly" in full_text[:20_000] or "unusual traffic" in full_text[:20_000]:
            return False, "anti_bot"
        if response.status_code >= 400:
            return False, f"http_{response.status_code}"
        return (marker in full_text, "search_ok" if marker in full_text else "no_result_markup")
    except requests.RequestException as exc:
        return False, type(exc).__name__


def _mcp_search_probe(url: str, timeout: float) -> tuple[bool, str]:
    """Verify that a streamable MCP endpoint can complete a real search call."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "moda-v4-search-stack/1.0",
    }
    session = requests.Session()
    try:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "moda-v4", "version": "1.0"},
            },
        }
        response = session.post(url, json=initialize, headers=headers, timeout=timeout)
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        response = session.post(
            url,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
            timeout=timeout,
        )
        if response.status_code >= 400:
            return False, f"http_{response.status_code}"
        response = session.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "moda-v4 research", "max_results": 1}},
            },
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return ("\"result\"" in response.text, "tool_ok" if "\"result\"" in response.text else "invalid_tool_response")
    except requests.RequestException as exc:
        return False, type(exc).__name__
    finally:
        session.close()


def check_stack(timeout: float = 3.0) -> dict[str, Any]:
    load_local_env()
    searxng = configured("SEARXNG_URL")
    ddg_mcp = configured("DDG_MCP_URL")
    result: dict[str, Any] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "endpoints": {},
        "recommended": "searxng",
    }
    ok, detail = _http_probe(searxng.rstrip("/") + "/search", timeout)
    result["endpoints"]["searxng"] = {"url": searxng, "ok": ok, "detail": detail}
    ok, detail = _mcp_search_probe(ddg_mcp, timeout)
    result["endpoints"]["duckduckgo_mcp"] = {"url": ddg_mcp, "ok": ok, "detail": detail}
    for key, marker in (("SO360_SEARCH_URL", "res-list"), ("DDG_HTML_URL", "result__a"), ("DDG_LITE_URL", "result-link")):
        url = configured(key)
        ok, detail = _public_search_probe(url, timeout, marker)
        result["endpoints"][key.lower()] = {"url": url, "ok": ok, "detail": detail}
    model_bridge = os.getenv("MODA_MODEL_SEARCH_URL", "").strip()
    if model_bridge:
        ok, detail = _tcp_probe(model_bridge, timeout)
        result["endpoints"]["model_search_bridge"] = {"url": model_bridge, "ok": ok, "detail": detail}
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_configured = bool(openai_key and openai_key not in {"YOUR_API_KEY_HERE", "***", "<REDACTED>"})
    result["endpoints"]["openai_web_search"] = {
        "url": "https://api.openai.com/v1/responses",
        "ok": openai_configured,
        "detail": "configured_not_probed" if openai_configured else "not_configured",
    }
    brave_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    brave_configured = bool(brave_key and brave_key not in {"YOUR_API_KEY_HERE", "***", "<REDACTED>"})
    result["endpoints"]["brave_search"] = {
        "url": os.getenv("BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search").strip(),
        "ok": brave_configured,
        "detail": "configured_not_probed" if brave_configured else "not_configured",
    }
    if not result["endpoints"]["searxng"]["ok"]:
        if result["endpoints"]["brave_search"]["ok"]:
            result["recommended"] = "brave"
        elif result["endpoints"]["so360_search_url"]["ok"]:
            result["recommended"] = "so360"
        elif result["endpoints"]["duckduckgo_mcp"]["ok"]:
            result["recommended"] = "duckduckgo_mcp"
        elif any(result["endpoints"][key]["ok"] for key in ("ddg_html_url", "ddg_lite_url")):
            result["recommended"] = "public_fallback"
        elif result["endpoints"].get("model_search_bridge", {}).get("ok"):
            result["recommended"] = "model_search_bridge"
        elif result["endpoints"]["openai_web_search"]["ok"]:
            result["recommended"] = "openai_web_search"
        else:
            result["recommended"] = "none"
    result["ok"] = any(status.get("ok") for status in result["endpoints"].values())
    return result


def start_ddg_mcp(host: str = "127.0.0.1", port: int = 7070, wait_seconds: int = 60) -> dict[str, Any]:
    endpoint = f"http://{host}:{port}/mcp"
    ok, detail = _tcp_probe(endpoint, 0.5)
    if ok:
        healthy, health_detail = _mcp_search_probe(endpoint, min(5.0, max(1.0, float(wait_seconds))))
        return {
            "started": False,
            "ok": healthy,
            "detail": "already_running" if healthy else f"running_but_unhealthy:{health_detail}",
            "url": endpoint,
        }
    uvx = shutil.which("uvx")
    if not uvx:
        return {"started": False, "ok": False, "detail": "uvx_not_found", "url": endpoint}
    command = [
        uvx, "--with", "duckduckgo-mcp-server[browser]", "duckduckgo-mcp-server",
        "--transport", "streamable-http", "--host", host, "--port", str(port),
    ]
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + max(1, wait_seconds)
    while time.monotonic() < deadline:
        ok, _ = _tcp_probe(endpoint, 0.5)
        if ok:
            return {"started": True, "ok": True, "detail": "started", "url": endpoint}
        time.sleep(1)
    return {"started": True, "ok": False, "detail": "startup_timeout", "url": endpoint}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or bootstrap the moda-v4 search stack")
    parser.add_argument("command", choices=["check", "start-ddg"], nargs="?", default="check")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--wait-seconds", type=int, default=60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = (
        start_ddg_mcp(wait_seconds=args.wait_seconds)
        if args.command == "start-ddg"
        else check_stack(timeout=args.timeout)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
