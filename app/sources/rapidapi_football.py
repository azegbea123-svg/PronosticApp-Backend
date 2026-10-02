"""
API-Football via RapidAPI — MÊME base de données qu'api_football.py
(1 236+ championnats, Afrique/Asie/Amérique du Sud comprises), mais via
un compte RapidAPI séparé, gratuit (100 requêtes/jour, 10/min). Comme
c'est un compte distinct de celui suspendu sur api-sports.io direct, la
suspension ne s'applique pas ici.

Tourne EN PARALLÈLE des trois autres sources (voir matchs.py) — sert
surtout à combler les championnats que football-data.org et TheSportsDB
ne couvrent pas (divisions inférieures, championnats africains/asiatiques).
"""

import time
from typing import Optional, Dict, Any, List

import httpx

from ..config import CLE_RAPIDAPI_FOOTBALL

BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"
HEADERS = {
    "x-rapidapi-key": CLE_RAPIDAPI_FOOTBALL,
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
}

_MAX_APPELS_PAR_MINUTE = 8
_MAX_APPELS_PAR_JOUR = 85

_horodatages_appels: List[float] = []
_compteur_jour = 0
_jour_compteur_reinitialise: Optional[str] = None


def _jour_utc_actuel() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _peut_appeler() -> bool:
    global _compteur_jour, _jour_compteur_reinitialise
    aujourd_hui = _jour_utc_actuel()
    if _jour_compteur_reinitialise != aujourd_hui:
        _compteur_jour = 0
        _jour_compteur_reinitialise = aujourd_hui
    if _compteur_jour >= _MAX_APPELS_PAR_JOUR:
        return False
    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    return len(_horodatages_appels) < _MAX_APPELS_PAR_MINUTE


def _enregistrer_appel() -> None:
    global _compteur_jour
    _horodatages_appels.append(time.time())
    _compteur_jour += 1


def quota_restant() -> Dict[str, int]:
    maintenant = time.time()
    appels_recents = len([t for t in _horodatages_appels if maintenant - t < 60])
    return {
        "appels_derniere_minute": appels_recents,
        "restant_cette_minute": max(0, _MAX_APPELS_PAR_MINUTE - appels_recents),
        "appels_aujourd_hui": _compteur_jour,
        "restant_aujourd_hui": max(0, _MAX_APPELS_PAR_JOUR - _compteur_jour),
    }


def _normaliser(fixture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """fixture_id préfixé "rapid-" pour ne jamais entrer en collision
    avec les autres fournisseurs une fois les listes fusionnées (voir matchs.py)."""
    try:
        return {
            "fixture_id": f"rapid-{fixture['fixture']['id']}",
            "date": fixture["fixture"]["date"],
            "league_id": fixture["league"]["id"],
            "league": fixture["league"]["name"],
            "equipe1": fixture["teams"]["home"]["name"],
            "equipe2": fixture["teams"]["away"]["name"],
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError):
        return None


async def _appeler(endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI_FOOTBALL:
        return None
    if not _peut_appeler():
        return None
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params, timeout=15.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """Liste des matchs pour cette date (YYYY-MM-DD), déjà normalisée au format interne commun."""
    data = await _appeler("/fixtures", {"date": jour_iso})
    if data is None:
        return None
    bruts = data.get("response", [])
    return [m for m in (_normaliser(b) for b in bruts) if m is not None]


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — réponse brute, non filtrée."""
    if not CLE_RAPIDAPI_FOOTBALL:
        return {"erreur": "RAPIDAPI_FOOTBALL_KEY non configurée"}
    if not _peut_appeler():
        return {"erreur": "limite de débit locale atteinte, réessaie dans un instant"}
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/fixtures", headers=HEADERS, params={"date": jour_iso}, timeout=15.0)
            corps = r.json()
            corps["_statut_http"] = r.status_code
            return corps
    except Exception as e:
        return {"erreur": str(e)}
