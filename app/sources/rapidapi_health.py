"""Diagnostic léger des fournisseurs RapidAPI déjà configurés sur Render.

Ce module ne crée aucune clé et ne modifie aucune configuration RapidAPI.
Il utilise uniquement RAPIDAPI_KEY et teste un endpoint peu coûteux par fournisseur.
"""
import time
from datetime import datetime, timezone
from typing import Any, Dict
import httpx
from ..config import CLE_RAPIDAPI

CACHE_TTL = 60
_cache = None
_cache_at = 0.0

async def _probe(name: str, host: str, url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not CLE_RAPIDAPI:
        return {"provider": name, "status": "NOT_CONFIGURED", "http_status": None, "latency_ms": None, "message": "RAPIDAPI_KEY absente"}
    headers = {"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": host}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(url, headers=headers, params=params or {})
        latency = round((time.perf_counter() - started) * 1000)
        code = response.status_code
        if code == 200:
            status = "OK"
        elif code == 401:
            status = "AUTH_ERROR"
        elif code == 403:
            status = "ACCESS_FORBIDDEN"
        elif code == 404:
            status = "ENDPOINT_NOT_FOUND"
        elif code == 429:
            status = "RATE_OR_QUOTA_LIMIT"
        elif 500 <= code <= 599:
            status = "PROVIDER_ERROR"
        else:
            status = "HTTP_ERROR"
        result = {"provider": name, "status": status, "http_status": code, "latency_ms": latency}
        for h in ("x-ratelimit-requests-remaining", "x-ratelimit-requests-limit", "x-ratelimit-requests-reset"):
            if h in response.headers:
                result[h.replace("-", "_")] = response.headers[h]
        return result
    except httpx.TimeoutException:
        return {"provider": name, "status": "TIMEOUT", "http_status": None, "latency_ms": round((time.perf_counter()-started)*1000)}
    except httpx.HTTPError as exc:
        return {"provider": name, "status": "NETWORK_ERROR", "http_status": None, "latency_ms": round((time.perf_counter()-started)*1000), "message": type(exc).__name__}

async def check(force: bool = False) -> Dict[str, Any]:
    global _cache, _cache_at
    now = time.time()
    if not force and _cache is not None and now - _cache_at < CACHE_TTL:
        return {**_cache, "cached": True}
    live = await _probe(
        "free-api-live-football-data",
        "free-api-live-football-data.p.rapidapi.com",
        "https://free-api-live-football-data.p.rapidapi.com/football-popular-leagues",
    )
    prediction = await _probe(
        "football-prediction-api",
        "football-prediction-api.p.rapidapi.com",
        "https://football-prediction-api.p.rapidapi.com/api/v2/list-markets",
    )
    statuses = [live["status"], prediction["status"]]
    overall = "OK" if all(x == "OK" for x in statuses) else ("DEGRADED" if any(x == "OK" for x in statuses) else "DOWN")
    result = {"overall": overall, "checked_at": datetime.now(timezone.utc).isoformat(), "providers": {"live_football": live, "football_prediction": prediction}, "cache_ttl_seconds": CACHE_TTL, "cached": False}
    _cache, _cache_at = result, now
    return result
