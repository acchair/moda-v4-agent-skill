"""Shared, cross-process transport for direct Eastmoney requests.

Eastmoney is a useful source but reacts badly to concurrent bursts.  AKShare
wrappers cannot be fully controlled here; all direct V4 endpoints should use
this module so independent collector processes still share one request clock.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Iterator
from urllib.parse import urlparse

import requests


DEFAULT_MIN_INTERVAL_SECONDS = 1.1
DEFAULT_JITTER_SECONDS = 0.15
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
}
_SESSION = requests.Session()


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _state_path() -> Path:
    configured = os.getenv("MODA_EASTMONEY_RATE_STATE", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "moda-v4-eastmoney-rate.state"


def _is_eastmoney_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "eastmoney.com" or host.endswith(".eastmoney.com")


def _lock(handle: Any) -> None:
    handle.seek(0)
    if handle.read(1) == b"":
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_state() -> Iterator[Any]:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        while True:
            try:
                _lock(handle)
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield handle
        finally:
            _unlock(handle)


def _read_timestamp(handle: Any) -> float:
    handle.seek(0)
    try:
        return float(handle.read().decode("ascii").strip() or "0")
    except (OSError, UnicodeDecodeError, ValueError):
        return 0.0


def _write_timestamp(handle: Any, value: float) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(f"{value:.6f}".encode("ascii"))
    handle.flush()
    os.fsync(handle.fileno())


def wait_turn(
    min_interval: float | None = None,
    jitter: float | None = None,
) -> float:
    """Reserve the next Eastmoney request slot across all local processes."""
    interval = _float_env("MODA_EASTMONEY_MIN_INTERVAL", DEFAULT_MIN_INTERVAL_SECONDS) if min_interval is None else max(0.0, float(min_interval))
    spread = _float_env("MODA_EASTMONEY_JITTER", DEFAULT_JITTER_SECONDS) if jitter is None else max(0.0, float(jitter))
    with _locked_state() as handle:
        wait = max(0.0, _read_timestamp(handle) + interval - time.time())
        if wait:
            time.sleep(wait)
        extra = random.uniform(0.0, spread) if spread else 0.0
        if extra:
            time.sleep(extra)
        _write_timestamp(handle, time.time())
    return wait + extra


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Make one paced direct request to an Eastmoney-owned host."""
    if not _is_eastmoney_url(url):
        raise ValueError(f"eastmoney_transport rejects non-Eastmoney host: {urlparse(url).hostname or ''}")
    wait_turn()
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
    client = session or _SESSION
    return client.request(
        method.upper(),
        url,
        params=params,
        json=json,
        headers=merged_headers,
        timeout=timeout,
        **kwargs,
    )


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)
