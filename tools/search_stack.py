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
        if key in {"SEARXNG_URL", "DDG_MCP_URL", "DDG_HTML_URL", "DDG_LITE_URL"}:
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
    ok, detail = _tcp_probe(ddg_mcp, timeout)
    result["endpoints"]["duckduckgo_mcp"] = {"url": ddg_mcp, "ok": ok, "detail": detail}
    for key in ("DDG_HTML_URL", "DDG_LITE_URL"):
        url = configured(key)
        ok, detail = _tcp_probe(url, timeout)
        result["endpoints"][key.lower()] = {"url": url, "ok": ok, "detail": detail}
    if not result["endpoints"]["searxng"]["ok"]:
        result["recommended"] = "duckduckgo_mcp" if result["endpoints"]["duckduckgo_mcp"]["ok"] else "public_fallback"
    return result


def start_ddg_mcp(host: str = "127.0.0.1", port: int = 7070, wait_seconds: int = 60) -> dict[str, Any]:
    endpoint = f"http://{host}:{port}/mcp"
    ok, detail = _tcp_probe(endpoint, 0.5)
    if ok:
        return {"started": False, "ok": True, "detail": "already_running", "url": endpoint}
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
