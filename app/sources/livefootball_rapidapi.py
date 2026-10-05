"""
free-api-live-football-data via RapidAPI.

Couche d'accès centralisée pour les données riches utilisées par PronosticApp:
- matchs du jour
- détail/statut/score
- statistiques du match
- confrontations directes
- compositions
- classements
- ligues populaires
- recherche d'équipes

Toutes les fonctions échouent proprement (None/[]) afin qu'une source
optionnelle ne bloque jamais le moteur principal.
"""
import time
from typing import Optional, Dict, Any, List
import httpx
from ..config import CLE_RAPIDAPI

BASE_URL = "https://free-api-live-football-data.p.rapidapi.com"
HEADERS = {
    "x-rapidapi-key": CLE_RAPIDAPI,
    "x-rapidapi-host": "free-api-live-football-data.p.rapidapi.com",
}

_MAX_APPELS_PAR_MINUTE = 8
_horodatages_appels: List[float] = []

def _peut_appeler() -> bool:
    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    return len(_horodatages_appels) < _MAX_APPELS_PAR_MINUTE

def _enregistrer_appel() -> None:
    _horodatages_appels.append(time.time())

def quota_restant() -> Dict[str, int]:
    maintenant = time.time()
    appels = len([t for t in _horodatages_appels if maintenant - t < 60])
    return {
        "appels_derniere_minute": appels,
        "restant_cette_minute": max(0, _MAX_APPELS_PAR_MINUTE - appels),
    }

async def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI or not _peut_appeler():
        return None
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{path}", headers=HEADERS,
                                 params=params or {}, timeout=15.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None

def _normaliser(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        date_iso = m["status"]["utcTime"]
        date_iso = date_iso.split(".")[0] + "Z"
        return {
            "fixture_id": f"live-{m['id']}",
            "event_id": int(m["id"]),
            "date": date_iso,
            "league_id": int(m.get("leagueId") or 0),
            "league": f"Ligue {m.get('leagueId')}",
            "equipe1": m["home"]["name"],
            "equipe2": m["away"]["name"],
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError, ValueError):
        return None

async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    data = await _get("/football-get-matches-by-date", {"date": jour_iso.replace("-", "")})
    if data is None:
        return None
    matchs = [_normaliser(m) for m in ((data.get("response") or {}).get("matches") or [])]
    return [m for m in matchs if m and m["date"][:10] == jour_iso]

async def get_match_detail(event_id: int) -> Optional[Dict[str, Any]]:
    data = await _get("/football-get-match-detail", {"eventid": event_id})
    return (data or {}).get("response")

async def get_match_status(event_id: int) -> Optional[Dict[str, Any]]:
    data = await _get("/football-get-match-status", {"eventid": event_id})
    return ((data or {}).get("response") or {}).get("status")

async def get_match_score(event_id: int) -> Optional[List[Dict[str, Any]]]:
    data = await _get("/football-get-match-score", {"eventid": event_id})
    return ((data or {}).get("response") or {}).get("scores") or []

async def get_match_stats(event_id: int) -> Optional[List[Dict[str, Any]]]:
    data = await _get("/football-get-match-all-stats", {"eventid": event_id})
    return ((data or {}).get("response") or {}).get("stats")

async def get_head_to_head(event_id: int) -> Optional[Dict[str, Any]]:
    data = await _get("/football-get-head-to-head", {"eventid": event_id})
    return ((data or {}).get("response") or {}).get("lineup")

async def get_lineups(event_id: int) -> Dict[str, Any]:
    home, away = await __import__("asyncio").gather(
        _get("/football-get-hometeam-lineup", {"eventid": event_id}),
        _get("/football-get-awayteam-lineup", {"eventid": event_id}),
    )
    return {
        "home": ((home or {}).get("response") or {}).get("lineup"),
        "away": ((away or {}).get("response") or {}).get("lineup"),
    }

async def get_popular_leagues() -> List[Dict[str, Any]]:
    data = await _get("/football-popular-leagues")
    return ((data or {}).get("response") or {}).get("popular") or []

async def get_standing(league_id: int, mode: str = "all") -> List[Dict[str, Any]]:
    if mode not in ("all", "home", "away"):
        mode = "all"
    data = await _get(f"/football-get-standing-{mode}", {"leagueid": league_id})
    return ((data or {}).get("response") or {}).get("standing") or []

async def search_teams(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    if len(query.strip()) < 2:
        return []
    data = await _get("/football-teams-search", {"search": query})
    result = ((data or {}).get("response") or {})
    teams = result.get("teams") or result.get("results") or result.get("list") or []
    out = []
    for t in teams:
        if not isinstance(t, dict):
            continue
        nom = t.get("name") or t.get("teamName")
        if nom:
            out.append({
                "id": t.get("id") or t.get("teamId"),
                "nom": nom,
                "pays": t.get("ccode") or t.get("country"),
                "logo": t.get("logo") or t.get("imageUrl"),
            })
        if len(out) >= limit:
            break
    return out

async def get_team_logo(team_id: int) -> Optional[str]:
    data = await _get("/football-team-logo", {"teamid": team_id})
    return ((data or {}).get("response") or {}).get("url")

async def get_league_detail(league_id: int) -> Optional[Dict[str, Any]]:
    data = await _get("/football-get-league-detail", {"leagueid": league_id})
    return ((data or {}).get("response") or {}).get("leagues")

async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    data = await _get("/football-get-matches-by-date", {"date": jour_iso.replace("-", "")})
    return data
