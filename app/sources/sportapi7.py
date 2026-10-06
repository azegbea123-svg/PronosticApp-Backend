"""Connecteur SportAPI7 pour PronosticApp V3.3.

Flux principal:
  /sport/football/scheduled-events/{date} -> liste des matchs du jour
  /event/{id} -> détail d'un match sélectionné

Le connecteur ne fait pas de boucle d'enrichissement automatique sur tous les
matchs : les détails sont récupérés à la demande afin de préserver le quota.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ..config import CLE_RAPIDAPI

HOST = "sportapi7.p.rapidapi.com"
BASE = "https://sportapi7.p.rapidapi.com/api/v1"
TIMEOUT = 12.0


def _headers() -> Dict[str, str]:
    return {
        "x-rapidapi-key": CLE_RAPIDAPI,
        "x-rapidapi-host": HOST,
        "accept": "application/json",
    }


def _name(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("name", "fullName", "shortName", "mediumName"):
            if v.get(k):
                return str(v[k]).strip()
    return None


def _iso_timestamp(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
        except Exception:
            return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (10, 13):
        try:
            ts = int(s) / (1000 if len(s) == 13 else 1)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            return None
    return s


def _event_from_obj(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event = obj.get("event") if isinstance(obj.get("event"), dict) else obj
    home = _name(event.get("homeTeam") or event.get("home_team"))
    away = _name(event.get("awayTeam") or event.get("away_team"))
    if not home or not away:
        return None
    tournament = event.get("tournament") or event.get("uniqueTournament") or event.get("league") or {}
    competition = _name(tournament) or (tournament.get("name") if isinstance(tournament, dict) else None)
    event_id = event.get("id") or event.get("eventId")
    ts = event.get("startTimestamp") or event.get("startTimestampUtc") or event.get("startTime") or event.get("date")
    dt = _iso_timestamp(ts)
    return {
        "fixture_id": f"sportapi7-{event_id}" if event_id is not None else None,
        "event_id": event_id,
        "date": dt,
        "league": competition or "SportAPI7",
        "equipe1": home,
        "equipe2": away,
        "status": event.get("status"),
        "source": "SportAPI7",
        "donneesDisponibles": None,
        "sportapi7": True,
        "raw_event": event,
    }


def _walk_events(payload: Any):
    if isinstance(payload, dict):
        if isinstance(payload.get("event"), dict):
            yield payload["event"]
        events = payload.get("events")
        if isinstance(events, list):
            for x in events:
                if isinstance(x, dict):
                    yield x
        for v in payload.values():
            if isinstance(v, (dict, list)):
                yield from _walk_events(v)
    elif isinstance(payload, list):
        for v in payload:
            if isinstance(v, (dict, list)):
                yield from _walk_events(v)


def normalize_events(payload: Any, target_date: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for obj in _walk_events(payload):
        m = _event_from_obj(obj)
        if not m:
            continue
        if target_date and m.get("date") and str(m["date"])[:10] != target_date:
            continue
        key = m.get("event_id") or (m.get("date"), m.get("equipe1"), m.get("equipe2"))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


async def get_fixtures_du_jour(jour_iso: str) -> List[Dict[str, Any]]:
    """Récupère les matchs du jour.

    Le premier appel est le flux officiel scheduled-events. Si celui-ci répond
    correctement mais sans événements exploitables, on tente le flux catégories
    avec timezone UTC+0. Les réponses non-200 ne sont plus silencieusement
    transformées en succès côté diagnostic.
    """
    if not CLE_RAPIDAPI:
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(f"{BASE}/sport/football/scheduled-events/{jour_iso}", headers=_headers())
            if r.status_code == 200:
                matches = normalize_events(r.json(), target_date=jour_iso)
                if matches:
                    return matches

            # Fallback documenté : catégories ayant des événements à cette date.
            c = await client.get(f"{BASE}/sport/football/{jour_iso}/0/categories", headers=_headers())
            if c.status_code != 200:
                return []
            payload = c.json()
            categories = payload.get("categories", []) if isinstance(payload, dict) else []
            out: List[Dict[str, Any]] = []
            seen = set()
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                cid = cat.get("id")
                if cid is None:
                    continue
                cr = await client.get(f"{BASE}/category/{cid}/scheduled-events/{jour_iso}", headers=_headers())
                if cr.status_code != 200:
                    continue
                for m in normalize_events(cr.json(), target_date=jour_iso):
                    key = m.get("event_id") or (m.get("date"), m.get("equipe1"), m.get("equipe2"))
                    if key not in seen:
                        seen.add(key)
                        out.append(m)
            return out
    except Exception:
        return []


async def get_event_detail(event_id: int | str) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI:
        return None
    url = f"{BASE}/event/{event_id}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers=_headers())
        if r.status_code != 200:
            return None
        payload = r.json()
        event = payload.get("event") if isinstance(payload, dict) else None
        return event if isinstance(event, dict) else payload
    except Exception:
        return None


async def get_scheduled_events_raw(jour_iso: Optional[str] = None) -> Dict[str, Any]:
    jour_iso = jour_iso or date.today().isoformat()
    url = f"{BASE}/sport/football/scheduled-events/{jour_iso}"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url, headers=_headers())
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:4000]
    summary = {}
    if isinstance(payload, dict):
        summary = {"root_keys": list(payload.keys())[:50]}
        for k in ("events", "matches", "scheduledEvents", "data", "categories"):
            if isinstance(payload.get(k), list):
                summary[k + "_count"] = len(payload[k])
    elif isinstance(payload, list):
        summary = {"root_shape": "array", "root_count": len(payload)}
    return {
        "http_status": r.status_code,
        "date": jour_iso,
        "url_path": f"/sport/football/scheduled-events/{jour_iso}",
        "content_type": r.headers.get("content-type"),
        "rate_remaining": r.headers.get("x-ratelimit-requests-remaining"),
        "rate_limit": r.headers.get("x-ratelimit-requests-limit"),
        "summary": summary,
        "payload": payload,
    }
