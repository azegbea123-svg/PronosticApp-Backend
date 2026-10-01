"""
Intégration football-data.org (v4) — utilisée EN PARALLÈLE de TheSportsDB
(voir sources/thesportsdb.py et matchs.py) pour la liste de matchs du
jour. football-data.org est plus fiable/structuré sur les grandes
compétitions qu'il couvre (~12, voir plus bas) ; TheSportsDB couvre
beaucoup plus large (~617) mais avec une fiabilité de données un peu
plus variable (base contributive). Les deux listes sont fusionnées et
dédoublonnées dans matchs.py.

⚠️ Limite du plan gratuit : seule une douzaine de grandes compétitions
sont couvertes (Premier League, Liga, Serie A, Bundesliga, Ligue 1,
Ligue des Champions, Championship, Eredivisie, Primeira Liga,
Brasileirão, Coupe du Monde, Euro...). PAS de divisions inférieures, PAS
de championnat togolais ni de compétitions CAF sur ce plan.

Limite de débit documentée : 10 requêtes/minute sur le plan gratuit.
"""

import time
from typing import Optional, Dict, Any, List

import httpx

from ..config import CLE_FOOTBALL_DATA

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": CLE_FOOTBALL_DATA}

_MAX_APPELS_PAR_MINUTE = 8  # marge sous les 10/min documentées

_horodatages_appels: List[float] = []


def _peut_appeler() -> bool:
    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    return len(_horodatages_appels) < _MAX_APPELS_PAR_MINUTE


def _enregistrer_appel() -> None:
    _horodatages_appels.append(time.time())


def quota_restant() -> Dict[str, int]:
    maintenant = time.time()
    appels_recents = len([t for t in _horodatages_appels if maintenant - t < 60])
    return {
        "appels_derniere_minute": appels_recents,
        "restant_cette_minute": max(0, _MAX_APPELS_PAR_MINUTE - appels_recents),
    }


def _normaliser(match_brut: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """fixture_id préfixé "fd-" pour ne jamais entrer en collision avec
    les identifiants d'un autre fournisseur (voir thesportsdb.py, préfixe
    "tsdb-") une fois les deux listes fusionnées dans matchs.py."""
    try:
        return {
            "fixture_id": f"fd-{match_brut['id']}",
            "date": match_brut["utcDate"],
            "league_id": match_brut["competition"]["id"],
            "league": match_brut["competition"]["name"],
            "equipe1": match_brut["homeTeam"]["name"],
            "equipe2": match_brut["awayTeam"]["name"],
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError):
        return None


async def _appeler(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    if not CLE_FOOTBALL_DATA:
        return None
    if not _peut_appeler():
        return None

    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params or {}, timeout=15.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """
    Liste des matchs pour cette date (YYYY-MM-DD), déjà normalisée au
    format interne commun (voir _normaliser) — contrairement à
    api_football.py, ce module normalise lui-même car son format brut
    est trop différent pour partager une fonction avec un autre
    fournisseur une fois les listes fusionnées dans matchs.py.
    """
    data = await _appeler("/matches", {"dateFrom": jour_iso, "dateTo": jour_iso})
    if data is None:
        return None
    bruts = data.get("matches", [])
    return [m for m in (_normaliser(b) for b in bruts) if m is not None]


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — réponse brute (succès ou erreur), non filtrée."""
    if not CLE_FOOTBALL_DATA:
        return {"erreur": "FOOTBALL_DATA_API_KEY non configurée"}
    if not _peut_appeler():
        return {"erreur": "limite de débit locale atteinte, réessaie dans un instant"}

    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}/matches",
                headers=HEADERS,
                params={"dateFrom": jour_iso, "dateTo": jour_iso},
                timeout=15.0,
            )
            corps = r.json()
            corps["_statut_http"] = r.status_code
            return corps
    except Exception as e:
        return {"erreur": str(e)}
