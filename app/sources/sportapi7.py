"""Connecteur SportAPI7 - PronosticApp V3.3.3.

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
# Le plan observe dispose de 50 requetes/jour pour cet endpoint.
# Une synchronisation utilise 2 appels de pilotage (calendrier + categories)
# puis au maximum 35 categories, laissant une marge de securite.
MAX_CATEGORY_CALLS_PER_SYNC = 35
MIN_RATE_REMAINING = 5


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
    if not isinstance(payload, dict):
        return []
    rows = payload.get("dailyUniqueTournaments")
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict) or row.get("date") != jour_iso:
            continue
        values = row.get("uniqueTournamentIds") or []
        return [int(x) for x in values if str(x).isdigit()]
    return []


def _category_id(cat: Dict[str, Any]) -> Optional[int]:
    value = cat.get("id") or cat.get("categoryId")
    nested = cat.get("category")
    if value is None and isinstance(nested, dict):
        value = nested.get("id") or nested.get("categoryId")
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


def _category_events_count(cat: Dict[str, Any]) -> int:
    try:
        return int(cat.get("totalEvents") or 0)
    except Exception:
        return 0


def _category_tournament_ids(cat: Dict[str, Any]) -> set[int]:
    values = cat.get("uniqueTournamentIds") or []
    out = set()
    for value in values:
        try:
            out.add(int(value))
        except Exception:
            pass
    return out


def _category_label(cat: Dict[str, Any]) -> str:
    nested = cat.get("category") if isinstance(cat.get("category"), dict) else {}
    return str(nested.get("name") or cat.get("name") or f"category-{_category_id(cat) or '?'}")


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
    """Interroge la route calendrier prouvee dans le fichier SportAPI7 fourni."""
    url_path = _calendar_path(jour_iso)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(BASE + url_path, headers=_headers())
    try:
        payload = r.json()
    except Exception:
        payload = r.text[:4000]
    return {
        "http_status": r.status_code,
        "date": jour_iso,
        "url_path": url_path,
        "content_type": r.headers.get("content-type"),
        "rate_remaining": r.headers.get("x-ratelimit-requests-remaining"),
        "rate_limit": r.headers.get("x-ratelimit-requests-limit"),
        "stage_ids_for_date": _extract_daily_stages(payload, jour_iso),
        "unique_tournament_ids_for_date": _extract_daily_unique_tournaments(payload, jour_iso),
        "summary": {
            "root_keys": list(payload.keys())[:50] if isinstance(payload, dict) else [],
            "dailyStages_count": len(payload.get("dailyStages", [])) if isinstance(payload, dict) and isinstance(payload.get("dailyStages"), list) else 0,
            "dailyUniqueTournaments_count": len(payload.get("dailyUniqueTournaments", [])) if isinstance(payload, dict) and isinstance(payload.get("dailyUniqueTournaments"), list) else 0,
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


async def get_fixtures_du_jour(jour_iso: str) -> List[Dict[str, Any]]:
    """Synchronise une journee avec un minimum d'appels API.

    Strategie V3.3.3:
      1. calendrier du mois -> tournois actifs du jour;
      2. categories du jour -> pays/categories et totalEvents;
      3. selection intelligente des categories (tournois du calendrier
         + volume d'evenements), plafonnee pour proteger le quota;
      4. chaque appel category/scheduled-events retourne plusieurs matchs;
      5. normalisation + deduplication par event_id puis equipes/date.

    Aucun appel /event/{id} n'est effectue ici: le detail est a la demande.
    """
    if not CLE_RAPIDAPI:
        return []

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            calendar_status, calendar_payload, calendar_headers = await _get_json(
                client, _calendar_path(jour_iso)
            )
            tournament_ids = (
                _extract_daily_unique_tournaments(calendar_payload, jour_iso)
                if calendar_status == 200 else []
            )

            categories_status, categories_payload, categories_headers = await _get_json(
                client, f"/sport/football/{jour_iso}/0/categories"
            )

            if categories_status == 200:
                categories = _extract_categories(categories_payload)
                tournament_set = set(tournament_ids)

                # Priorite aux categories qui portent au moins un tournoi
                # du calendrier du jour, puis aux categories ayant le plus
                # de matchs. Cela maximise la couverture sans depasser le quota.
                def score(cat: Dict[str, Any]):
                    overlap = len(_category_tournament_ids(cat) & tournament_set)
                    events = _category_events_count(cat)
                    return (1 if overlap else 0, overlap, events)

                candidates = []
                seen_category_ids = set()
                for cat in sorted(categories, key=score, reverse=True):
                    cid = _category_id(cat)
                    if cid is None or cid in seen_category_ids:
                        continue
                    if _category_events_count(cat) <= 0:
                        continue
                    seen_category_ids.add(cid)
                    candidates.append(cat)

                # Ne jamais consommer la marge de securite du quota.
                selected = candidates[:MAX_CATEGORY_CALLS_PER_SYNC]
                out: List[Dict[str, Any]] = []
                seen = set()
                category_diagnostics = []

                for cat in selected:
                    cid = _category_id(cat)
                    status, payload, headers = await _get_json(
                        client, f"/category/{cid}/scheduled-events/{jour_iso}"
                    )
                    count = 0
                    if status == 200:
                        normalized = normalize_events(payload, target_date=jour_iso)
                        for m in normalized:
                            key = m.get("event_id") or (
                                m.get("date"), m.get("equipe1"), m.get("equipe2")
                            )
                            if key not in seen:
                                seen.add(key)
                                out.append(m)
                        count = len(normalized)
                    category_diagnostics.append({
                        "category_id": cid,
                        "category": _category_label(cat),
                        "expected_events": _category_events_count(cat),
                        "http_status": status,
                        "returned_events": count,
                        "rate_remaining": headers.get("rate_remaining"),
                    })

                    remaining = headers.get("rate_remaining")
                    try:
                        if remaining is not None and int(remaining) <= MIN_RATE_REMAINING:
                            break
                    except Exception:
                        pass

                # Expose diagnostics for logs without requiring extra API calls.
                get_fixtures_du_jour.last_diagnostic = {
                    "date": jour_iso,
                    "calendar_status": calendar_status,
                    "calendar_tournament_count": len(tournament_ids),
                    "categories_status": categories_status,
                    "categories_total": len(categories),
                    "categories_selected": len(selected),
                    "matches_found": len(out),
                    "category_results": category_diagnostics,
                    "rate_remaining_after_calendar": calendar_headers.get("rate_remaining"),
                    "rate_remaining_after_categories": categories_headers.get("rate_remaining"),
                }
                if out:
                    return out

            # Fallback documente, si expose par l'abonnement.
            status, payload, _ = await _get_json(
                client, f"/sport/football/scheduled-events/{jour_iso}"
            )
            if status == 200:
                out = normalize_events(payload, target_date=jour_iso)
                if out:
                    return out

            get_fixtures_du_jour.last_diagnostic = {
                "date": jour_iso,
                "calendar_status": calendar_status,
                "calendar_tournament_count": len(tournament_ids),
                "categories_status": categories_status,
                "categories_total": len(_extract_categories(categories_payload)) if categories_status == 200 else 0,
                "categories_selected": 0,
                "matches_found": 0,
                "fallback_scheduled_events_status": status,
            }
            return []
    except Exception as exc:
        get_fixtures_du_jour.last_diagnostic = {"date": jour_iso, "error": str(exc)}
        return []


get_fixtures_du_jour.last_diagnostic = {}


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
