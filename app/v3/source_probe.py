"""Tests réels et activation contrôlée des sources RapidAPI V3.1.

Cette couche est volontairement séparée du moteur de pronostic :
- un test explicite peut appeler une source et inspecter son JSON ;
- aucune clé/API n'est renvoyée ;
- une source peut être activée après validation ;
- seules les sources marquées fixture-capable et réellement activées peuvent
  alimenter /matchs.

L'état d'activation est en mémoire du processus Render. Il est donc réinitialisé
après un redéploiement/restart ; cela évite d'écrire une configuration sensible
sur le disque éphémère de Render.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import CLE_RAPIDAPI


# Requêtes issues des endpoints fournis par l'utilisateur.
# Elles sont conservées ici comme tests de contrat, pas comme preuve qu'un
# endpoint est sémantiquement correct.
ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "sofascore": {
        "name": "SofaScore RapidAPI", "host": "sofascore.p.rapidapi.com",
        "url": "https://sofascore.p.rapidapi.com/teams/detail?teamId=38",
        "role": "team/form/enrichment", "fixture_capable": False,
    },
    "sportapi7": {
        "name": "SportAPI7", "host": "sportapi7.p.rapidapi.com",
        "url": "https://sportapi7.p.rapidapi.com/api/v1/sport/football/scheduled-events/{date}",
        "detail_url": "https://sportapi7.p.rapidapi.com/api/v1/event/{event_id}",
        "role": "fixtures + event detail", "fixture_capable": True,
    },
    "allsportsapi2": {
        "name": "AllSportsAPI2", "host": "allsportsapi2.p.rapidapi.com",
        "url": "https://allsportsapi2.p.rapidapi.com/api/tennis/rankings/atp",
        "role": "historical/fixtures", "fixture_capable": True,
    },
    "odds-feed": {
        "name": "Odds Feed", "host": "odds-feed.p.rapidapi.com",
        "url": "https://odds-feed.p.rapidapi.com/api/v1/events?event_ids=845%2C123%2C435%2C22%2C842%2C844%2C845&status=FINISHED&page=0",
        "role": "odds", "fixture_capable": False,
    },
    "all-sport-live-stream": {
        "name": "All Sport Live Stream", "host": "all-sport-live-stream.p.rapidapi.com",
        "url": "https://all-sport-live-stream.p.rapidapi.com/allSportid",
        "role": "live/events", "fixture_capable": True,
    },
    "flashlive": {
        "name": "FlashLive Sports", "host": "flashlive-sports.p.rapidapi.com",
        "url": "https://flashlive-sports.p.rapidapi.com/v1/teams/transfers?page=1&team_id=Wtn9Stg0&locale=en_INT",
        "role": "events/transfers", "fixture_capable": True,
    },
    "football-data1": {
        "name": "FootballData1", "host": "football-data1.p.rapidapi.com",
        "url": "https://football-data1.p.rapidapi.com/match/list/live?date=06%2F10%2F2020",
        "role": "fixtures/live", "fixture_capable": True,
    },
    "soccer-data": {
        "name": "SoccerData", "host": "soccer-data.p.rapidapi.com",
        "url": "https://soccer-data.p.rapidapi.com/tournament/leaderboard/red?tournamentId=14",
        "role": "discipline", "fixture_capable": False,
    },
    "football-live-score2": {
        "name": "Football Live Score 2", "host": "football-live-score2.p.rapidapi.com",
        "url": "https://football-live-score2.p.rapidapi.com/refresh?edition=en&date=2023-03-18&tzoffset=60",
        "role": "fixtures/live", "fixture_capable": True,
    },
}

_ACTIVE: Dict[str, bool] = {}
_LAST_TEST: Dict[str, Dict[str, Any]] = {}


def catalogue_ids() -> List[str]:
    return list(ENDPOINTS)


def _headers(host: str) -> Dict[str, str]:
    return {"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": host, "accept": "application/json"}


def _is_football(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"football", "soccer", "1", "association football"}
    if isinstance(value, dict):
        return _is_football(value.get("name") or value.get("slug") or value.get("sport") or value.get("sportName"))
    return False


def _team_name(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for k in ("name", "fullName", "shortName", "mediumName", "teamName"):
            if v.get(k):
                return str(v[k]).strip()
    return None


def _date_value(v: Any) -> Optional[str]:
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
    if re.fullmatch(r"\d{10,13}", s):
        try:
            ts = int(s) / (1000 if len(s) == 13 else 1)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except Exception:
            return None
    return s


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        yield obj, path
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _extract_match(obj: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    # Formes courantes des fournisseurs RapidAPI.
    base = obj.get("event") if isinstance(obj.get("event"), dict) else obj
    home = _team_name(base.get("homeTeam")) or _team_name(base.get("home_team")) or _team_name(base.get("team_home")) or _team_name(base.get("home"))
    away = _team_name(base.get("awayTeam")) or _team_name(base.get("away_team")) or _team_name(base.get("team_away")) or _team_name(base.get("away"))
    if not home or not away:
        return None

    sport = base.get("sport") or base.get("sportName") or base.get("sportType")
    tournament = base.get("tournament") or base.get("uniqueTournament") or base.get("competition") or base.get("league")
    # Si un sport explicite existe et n'est pas football, on rejette.
    if sport is not None and not _is_football(sport):
        return None
    context = " ".join(str(x) for x in (sport, tournament, base.get("category"), base.get("name")) if x)
    if any(x in context.lower() for x in ("tennis", "basketball", "nfl", "american football", "baseball", "cricket", "hockey")) and not _is_football(sport):
        return None

    date = None
    for k in ("date", "start_date", "startDate", "startTimestamp", "startTimestampUtc", "utcTime", "scheduledAt", "start_at"):
        if base.get(k) is not None:
            date = _date_value(base.get(k))
            if date:
                break
    if not date:
        # Un match peut être identifié sans date, mais il ne peut pas être
        # injecté dans /matchs ; on le garde néanmoins pour le diagnostic.
        date = None

    event_id = base.get("id") or base.get("eventId") or base.get("event_id")
    return {
        "event_id": event_id,
        "date": date,
        "equipe1": home,
        "equipe2": away,
        "competition": _team_name(tournament) or (str(tournament) if tournament else None),
        "path": path,
        "football": True,
    }


def extraire_matchs(payload: Any, limit: int = 20) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for obj, path in _walk(payload):
        m = _extract_match(obj, path)
        if not m:
            continue
        key = (m.get("date"), m.get("equipe1", "").lower(), m.get("equipe2", "").lower(), str(m.get("event_id")))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _response_shape(payload: Any) -> str:
    if isinstance(payload, list):
        return "array"
    if isinstance(payload, dict):
        return "object"
    return type(payload).__name__


def _contract_warning(source_id: str, matches: List[Dict[str, Any]], payload: Any) -> Optional[str]:
    cfg = ENDPOINTS[source_id]
    url = cfg["url"].lower()
    text = str(payload)[:20000].lower()
    if "tennis" in url:
        return "L'URL testée contient 'tennis' : validation manuelle requise même si le payload contient du football."
    if source_id == "sofascore" and matches:
        return "L'endpoint teams/detail contient un événement dans la réponse : source exploitable possiblement, mais contrat non standard."
    if source_id == "flashlive" and "nfl" in text:
        return "Le payload observé contient NFL : pas de validation football."
    return None


async def tester_source(source_id: str, timeout: float = 12.0) -> Dict[str, Any]:
    if source_id not in ENDPOINTS:
        raise KeyError(source_id)
    cfg = ENDPOINTS[source_id]
    if not CLE_RAPIDAPI:
        result = {"source_id": source_id, "source": cfg["name"], "status": "NO_RAPIDAPI_KEY", "http_status": None}
        _LAST_TEST[source_id] = result
        return result

    t0 = time.perf_counter()
    try:
        request_url = cfg["url"]
        if source_id == "sportapi7":
            request_url = request_url.replace("{date}", datetime.now(timezone.utc).date().isoformat())
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(request_url, headers=_headers(cfg["host"]))
        latency = round((time.perf_counter() - t0) * 1000)
        item: Dict[str, Any] = {
            "source_id": source_id, "source": cfg["name"], "host": cfg["host"],
            "role": cfg["role"], "fixture_capable": cfg["fixture_capable"],
            "http_status": response.status_code, "latency_ms": latency,
            "active": bool(_ACTIVE.get(source_id)),
        }
        for h in ("x-ratelimit-requests-remaining", "x-ratelimit-requests-limit", "x-ratelimit-requests-reset"):
            if response.headers.get(h) is not None:
                item[h] = response.headers.get(h)
        if response.status_code == 429:
            item["status"] = "RATE_LIMITED"
            _LAST_TEST[source_id] = item
            return item
        if response.status_code in (401, 403):
            item["status"] = "AUTH_OR_SUBSCRIPTION_ERROR"
            _LAST_TEST[source_id] = item
            return item
        if response.status_code != 200:
            item["status"] = "HTTP_ERROR"
            item["response_preview"] = response.text[:500]
            _LAST_TEST[source_id] = item
            return item
        try:
            payload = response.json()
        except Exception:
            item["status"] = "INVALID_JSON"
            item["response_preview"] = response.text[:500]
            _LAST_TEST[source_id] = item
            return item

        matches = extraire_matchs(payload)
        warning = _contract_warning(source_id, matches, payload)
        item.update({
            "status": "VALIDATED" if matches and not warning else ("FOOTBALL_DATA_BUT_CONTRACT_MISMATCH" if matches else "NO_FOOTBALL_MATCH_DETECTED"),
            "response_shape": _response_shape(payload),
            "football_match_count": len(matches),
            "sample_matches": matches,
            "contract_warning": warning,
        })
        # Diagnostic compact : clés racine uniquement, jamais la clé RapidAPI.
        if isinstance(payload, dict):
            item["root_keys"] = list(payload.keys())[:40]
        elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
            item["root_item_keys"] = list(payload[0].keys())[:40]
        _LAST_TEST[source_id] = item
        return item
    except Exception as exc:
        item = {"source_id": source_id, "source": cfg["name"], "status": "NETWORK_ERROR", "http_status": None, "error": f"{type(exc).__name__}: {exc}", "latency_ms": round((time.perf_counter()-t0)*1000)}
        _LAST_TEST[source_id] = item
        return item


async def tester_toutes_sources() -> Dict[str, Any]:
    # Séquentiel volontairement : on évite une rafale de 9 requêtes RapidAPI.
    results = []
    for source_id in ENDPOINTS:
        results.append(await tester_source(source_id))
        await asyncio.sleep(0.15)
    return {
        "tested_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "results": results,
        "warning": "Le test de toutes les sources consomme des requêtes RapidAPI. Utiliser de préférence le test individuel.",
    }


def etat_sources() -> Dict[str, Any]:
    return {
        "sources": [
            {"source_id": sid, "name": cfg["name"], "host": cfg["host"], "role": cfg["role"], "fixture_capable": cfg["fixture_capable"], "active": bool(_ACTIVE.get(sid)), "last_test": _LAST_TEST.get(sid)}
            for sid, cfg in ENDPOINTS.items()
        ]
    }


def activer(source_id: str, force: bool = False) -> Dict[str, Any]:
    if source_id not in ENDPOINTS:
        raise KeyError(source_id)
    last = _LAST_TEST.get(source_id)
    if not last:
        return {"ok": False, "source_id": source_id, "reason": "TEST_REQUIRED", "message": "Teste d'abord la source avec /debug/v3/source/{source_id}/test."}
    if last.get("status") != "VALIDATED" and not force:
        return {"ok": False, "source_id": source_id, "reason": "VALIDATION_REQUIRED", "message": "La source n'est pas validée. Utilise force=true uniquement après inspection du payload."}
    _ACTIVE[source_id] = True
    return {"ok": True, "source_id": source_id, "active": True, "force": force, "fixture_capable": ENDPOINTS[source_id]["fixture_capable"]}


def desactiver(source_id: str) -> Dict[str, Any]:
    if source_id not in ENDPOINTS:
        raise KeyError(source_id)
    _ACTIVE[source_id] = False
    return {"ok": True, "source_id": source_id, "active": False}


def sources_actives_fixture() -> List[str]:
    return [sid for sid, cfg in ENDPOINTS.items() if cfg["fixture_capable"] and _ACTIVE.get(sid)]


def derniere_source(source_id: str) -> Optional[Dict[str, Any]]:
    return _LAST_TEST.get(source_id)


async def fetch_active_fixture_matches(jour_iso: str) -> List[List[Dict[str, Any]]]:
    """Récupère les matchs du jour depuis les sources fixture activées.

    Les endpoints fournis initialement contiennent parfois des dates de
    démonstration anciennes. On ne les injecte donc dans /matchs que si la
    date retournée correspond réellement à jour_iso.
    """
    groups: List[List[Dict[str, Any]]] = []
    for source_id in sources_actives_fixture():
        cfg = ENDPOINTS[source_id]
        try:
            if source_id == "sportapi7":
                from ..sources import sportapi7
                groups.append(await sportapi7.get_fixtures_du_jour(jour_iso))
                continue
            url = cfg["url"]
            # Les deux endpoints dont la requête fournie possède explicitement
            # une date reçoivent la date demandée par /matchs.
            if source_id == "football-data1":
                url = re.sub(r"date=\d{2}%2F\d{2}%2F\d{4}", f"date={datetime.fromisoformat(jour_iso).strftime('%d%%2F%m%%2F%Y')}", url)
            elif source_id == "football-live-score2":
                url = re.sub(r"date=\d{4}-\d{2}-\d{2}", f"date={jour_iso}", url)

            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                response = await client.get(url, headers=_headers(cfg["host"]))
            if response.status_code != 200:
                groups.append([])
                continue
            payload = response.json()
            extracted = extraire_matchs(payload, limit=200)
            normalized = []
            for m in extracted:
                if m.get("date") and str(m["date"])[:10] != jour_iso:
                    continue
                if not m.get("date"):
                    continue
                normalized.append({
                    "fixture_id": f"{source_id}-{m.get('event_id') or abs(hash((jour_iso, m['equipe1'], m['equipe2'])))}",
                    "event_id": m.get("event_id"),
                    "date": str(m["date"]),
                    "league": m.get("competition") or cfg["name"],
                    "equipe1": m["equipe1"],
                    "equipe2": m["equipe2"],
                    "donneesDisponibles": None,
                    "source": cfg["name"],
                })
            groups.append(normalized)
        except Exception:
            groups.append([])
    return groups
