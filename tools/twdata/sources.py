"""Raw data sources for Taiwan stocks (free, no API key required).

- FinMind v4 API (per-stock history). Optional env FINMIND_API_TOKEN raises the
  rate limit from ~300 to ~600 requests/hour. Never commit the token.
- TWSE / TPEx OpenAPI (whole-market snapshots for screening).

Responses are cached on disk (default 6h) under <repo>/.cache/twdata so repeated
skill runs don't burn the rate limit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
CACHE_DIR = Path(os.environ.get("TWDATA_CACHE", Path(__file__).resolve().parents[2] / ".cache" / "twdata"))
DEFAULT_TTL = 6 * 3600
HEADERS = {"User-Agent": "twdata/0.1 (personal research tool)"}
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep MCP stderr quiet


class DataSourceError(RuntimeError):
    pass


def _cached_get(url: str, params: dict | None, ttl: int) -> object:
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.json"
    if ttl and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text(encoding="utf-8"))
    headers = dict(HEADERS)
    token = os.environ.get("FINMIND_API_TOKEN")
    if token and url.startswith(FINMIND_URL):
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=30, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise DataSourceError(f"request failed: {url} ({e})") from e
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def finmind(dataset: str, data_id: str | None = None, start_date: str | None = None,
            end_date: str | None = None, ttl: int = DEFAULT_TTL) -> list[dict]:
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    data = _cached_get(FINMIND_URL, params, ttl)
    if not isinstance(data, dict) or data.get("status") != 200:
        msg = data.get("msg") if isinstance(data, dict) else data
        raise DataSourceError(f"FinMind {dataset}: {msg} (rate limit? set FINMIND_API_TOKEN)")
    return data.get("data", [])


def twse(path: str, ttl: int = DEFAULT_TTL) -> list[dict]:
    return _cached_get(f"{TWSE_OPENAPI}/{path}", None, ttl)  # type: ignore[return-value]


def tpex(path: str, ttl: int = DEFAULT_TTL) -> list[dict]:
    return _cached_get(f"{TPEX_OPENAPI}/{path}", None, ttl)  # type: ignore[return-value]
