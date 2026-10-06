"""Connecteur SportAPI7 - PronosticApp V3.3.2.

Strategie:
1) calendrier football du mois -> dailyStages / stageIds
2) tentative du flux categories du jour
3) tentative du scheduled-events historique/documente si disponible
4) details d'evenements a la demande

Le calendrier est utilise comme source de verite pour diagnostiquer la
couverture du jour, sans supposer qu'un stageId est lui-meme un eventId.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    home = _name(event.get("homeTeam") or event.get("home_team") or event.get("home"))
    away = _name(event.get("awayTeam") or event.get("away_team") or event.get("away"))
    if not home or not away:
        return None
    tournament = event.get("tournament") or event.get("uniqueTournament") or event.get("league") or {}
    competition = _name(tournament) or (tournament.get("name") if isinstance(tournament, dict) else None)
    event_id = event.get("id") or event.get("eventId")
    ts = event.get("startTimestamp") or event.get("startTimestampUtc") or event.get("startTime") or event.get("date") or event.get("timeTS")
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
        for key in ("events", "matches", "scheduledEvents"):
            value = payload.get(key)
            if isinstance(value, list):
                for x in value:
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


def _calendar_path(jour_iso: str) -> str:
    return f"/calendar/{jour_iso[:7]}/0/football/unique-tournaments"


def _extract_daily_stages(payload: Any, jour_iso: str) -> List[int]:
    """Ancien format calendrier : dailyStages[].stageIds."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("dailyStages")
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("date") != jour_iso:
            continue
        values = row.get("stageIds") or []
        return [int(x) for x in values if str(x).isdigit()]
    return []


def _extract_daily_unique_tournaments(payload: Any, jour_iso: str) -> List[int]:
    """Format actuellement renvoyé : dailyUniqueTournaments[].uniqueTournamentIds."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("dailyUniqueTournaments")
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("date") != jour_iso:
            continue
        values = row.get("uniqueTournamentIds") or []
        out: List[int] = []
        for x in values:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    return []


def _extract_categories(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("categories", "data", "groups"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


async def get_calendar_raw(jour_iso: str) -> Dict[str, Any]:
    """Interroge le calendrier SportAPI7 et expose les deux formats rencontrés."""
    url_path = _calendar_path(jour_iso)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(BASE + url_path, headers=_headers())
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:4000]

    stage_ids = _extract_daily_stages(payload, jour_iso)
    tournament_ids = _extract_daily_unique_tournaments(payload, jour_iso)
    return {
        "http_status": r.status_code,
        "date": jour_iso,
        "url_path": url_path,
        "content_type": r.headers.get("content-type"),
        "rate_remaining": r.headers.get("x-ratelimit-requests-remaining"),
        "rate_limit": r.headers.get("x-ratelimit-requests-limit"),
        "stage_ids_for_date": stage_ids,
        "unique_tournament_ids_for_date": tournament_ids,
        "summary": {
            "root_keys": list(payload.keys())[:50] if isinstance(payload, dict) else [],
            "dailyStages_count": len(payload.get("dailyStages", [])) if isinstance(payload, dict) and isinstance(payload.get("dailyStages"), list) else 0,
            "dailyUniqueTournaments_count": len(payload.get("dailyUniqueTournaments", [])) if isinstance(payload, dict) and isinstance(payload.get("dailyUniqueTournaments"), list) else 0,
            "stage_ids_count": len(stage_ids),
            "unique_tournament_ids_count": len(tournament_ids),
        },
        "payload": payload,
    }


async def _get_json(client: httpx.AsyncClient, path: str) -> Tuple[int, Any, Dict[str, str]]:
    r = await client.get(BASE + path, headers=_headers())
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:4000]
    return r.status_code, payload, {
        "rate_remaining": r.headers.get("x-ratelimit-requests-remaining"),
        "rate_limit": r.headers.get("x-ratelimit-requests-limit"),
    }


async def diagnostic_fixtures_du_jour(jour_iso: str) -> Dict[str, Any]:
    """Diagnostic des trois voies SportAPI7 sans masquer les statuts HTTP."""
    result: Dict[str, Any] = {"source": "SportAPI7", "date": jour_iso, "tests": []}
    if not CLE_RAPIDAPI:
        result["erreur"] = "RAPIDAPI_KEY non configurée"
        return result

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        paths = [
            ("categories", f"/sport/football/{jour_iso}/0/categories"),
            ("scheduled-events", f"/sport/football/scheduled-events/{jour_iso}"),
            ("calendar", _calendar_path(jour_iso)),
        ]
        for label, path in paths:
            status, payload, rate = await _get_json(client, path)
            item: Dict[str, Any] = {
                "test": label,
                "http_status": status,
                "url_path": path,
                **rate,
            }
            if isinstance(payload, dict):
                item["root_keys"] = list(payload.keys())[:30]
                for key in ("categories", "data", "groups", "events", "matches", "scheduledEvents", "dailyStages", "dailyUniqueTournaments"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        item[f"{key}_count"] = len(value)
            elif isinstance(payload, list):
                item["root_shape"] = "array"
                item["root_count"] = len(payload)
            result["tests"].append(item)

    cal = await get_calendar_raw(jour_iso)
    result["calendar_interpretation"] = {
        "stage_ids": cal.get("stage_ids_for_date", []),
        "unique_tournament_ids": cal.get("unique_tournament_ids_for_date", []),
        "unique_tournament_ids_count": len(cal.get("unique_tournament_ids_for_date", [])),
    }
    return result


async def get_fixtures_du_jour(jour_iso: str) -> List[Dict[str, Any]]:
    if not CLE_RAPIDAPI:
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            # 1) Catégories du jour puis matchs par catégorie.
            status, payload, _ = await _get_json(
                client, f"/sport/football/{jour_iso}/0/categories"
            )
            if status == 200:
                out: List[Dict[str, Any]] = []
                seen = set()
                for cat in _extract_categories(payload):
                    cid = cat.get("id") or cat.get("categoryId")
                    if cid is None:
                        continue
                    cr_status, cr_payload, _ = await _get_json(
                        client, f"/category/{cid}/scheduled-events/{jour_iso}"
                    )
                    if cr_status != 200:
                        continue
                    for m in normalize_events(cr_payload, target_date=jour_iso):
                        key = m.get("event_id") or (
                            m.get("date"), m.get("equipe1"), m.get("equipe2")
                        )
                        if key not in seen:
                            seen.add(key)
                            out.append(m)
                if out:
                    return out

            # 2) Endpoint global documenté, conservé comme fallback.
            status, payload, _ = await _get_json(
                client, f"/sport/football/scheduled-events/{jour_iso}"
            )
            if status == 200:
                out = normalize_events(payload, target_date=jour_iso)
                if out:
                    return out

            # 3) Le calendrier actuel fournit les compétitions du jour via
            # dailyUniqueTournaments[].uniqueTournamentIds. Ces IDs ne sont
            # jamais convertis artificiellement en eventIds.
            return []
    except Exception:
        return []


async def get_event_detail(event_id: int | str) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            r = await client.get(f"{BASE}/event/{event_id}", headers=_headers())
        if r.status_code != 200:
            return None
        payload = r.json()
        event = payload.get("event") if isinstance(payload, dict) else None
        return event if isinstance(event, dict) else payload
    except Exception:
        return None


async def get_scheduled_events_raw(jour_iso: Optional[str] = None) -> Dict[str, Any]:
    jour_iso = jour_iso or date.today().isoformat()
    # Conserve l'ancien endpoint de diagnostic afin de documenter explicitement
    # le 404 observe, sans le masquer.
    url_path = f"/sport/football/scheduled-events/{jour_iso}"
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(BASE + url_path, headers=_headers())
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:4000]
    summary = {"root_keys": list(payload.keys())[:50] if isinstance(payload, dict) else []}
    if isinstance(payload, dict):
        for k in ("events", "matches", "scheduledEvents", "data", "categories"):
            if isinstance(payload.get(k), list):
                summary[k + "_count"] = len(payload[k])
    elif isinstance(payload, list):
        summary = {"root_shape": "array", "root_count": len(payload)}
    return {
        "http_status": r.status_code,
        "date": jour_iso,
        "url_path": url_path,
        "content_type": r.headers.get("content-type"),
        "rate_remaining": r.headers.get("x-ratelimit-requests-remaining"),
        "rate_limit": r.headers.get("x-ratelimit-requests-limit"),
        "summary": summary,
        "payload": payload,
    }
