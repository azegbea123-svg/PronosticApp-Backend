"""
free-api-live-football-data (via RapidAPI) — remplace le module
API-Football/RapidAPI officiel (abandonné : nécessitait une carte
bancaire pour la v\u00e9rification anti-abus, m\u00eame sur le plan gratuit).

Tr\u00e8s large couverture constat\u00e9e en pratique (Ghana, Isra\u00ebl, Qatar,
P\u00e9rou, \u00c9mirats...), comparable \u00e0 l'API-Football officielle.

\u26a0\ufe0f Pas de nom de championnat dans la r\u00e9ponse de /football-get-matches-by-date
(seulement un leagueId num\u00e9rique) — on affiche donc "Ligue <id>" faute
de mieux. /football-popular-leagues pourrait donner les noms des plus
grands championnats, mais pas de ceux, justement les plus nombreux ici,
qui sortent des grands championnats.

\u26a0\ufe0f M\u00eame cl\u00e9 RapidAPI (CLE_RAPIDAPI, variable RAPIDAPI_KEY) que pour
toute autre API RapidAPI \u00e9ventuellement ajout\u00e9e plus tard — RapidAPI
attribue UNE cl\u00e9 par compte, partag\u00e9e entre toutes les API auxquelles
on s'abonne, pas une cl\u00e9 par API.
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

_MAX_APPELS_PAR_MINUTE = 8  # pas de limite officiellement documentée pour ce wrapper, marge prudente par défaut

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


def _normaliser(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """fixture_id préfixé "live-" pour ne jamais entrer en collision avec
    les autres fournisseurs une fois les listes fusionnées (voir matchs.py)."""
    try:
        date_iso = m["status"]["utcTime"]  # ex: "2026-10-01T18:45:00.000Z"
        date_iso = date_iso.split(".")[0] + "Z"  # retire les millisecondes, garde le format commun
        return {
            "fixture_id": f"live-{m['id']}",
            "date": date_iso,
            "league_id": m.get("leagueId") or 0,
            "league": f"Ligue {m.get('leagueId')}",  # pas de nom de championnat dans cette réponse
            "equipe1": m["home"]["name"],
            "equipe2": m["away"]["name"],
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError):
        return None


async def _appeler(jour_aaaammjj: str) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI:
        return None
    if not _peut_appeler():
        return None
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}/football-get-matches-by-date",
                headers=HEADERS,
                params={"date": jour_aaaammjj},
                timeout=15.0,
            )
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """Liste des matchs pour cette date (YYYY-MM-DD), déjà normalisée au format interne commun."""
    jour_aaaammjj = jour_iso.replace("-", "")  # l'API attend YYYYMMDD, sans tiret
    data = await _appeler(jour_aaaammjj)
    if data is None:
        return None
    bruts = (data.get("response") or {}).get("matches", [])
    matchs = [m for m in (_normaliser(b) for b in bruts) if m is not None]
    # Filtre défensif : on ne garde que les matchs dont la date UTC
    # correspond vraiment au jour demandé (l'API peut renvoyer un match
    # dont l'heure locale affichée "time" déborde légèrement sur le jour
    # suivant/précédent par rapport à utcTime).
    return [m for m in matchs if m["date"][:10] == jour_iso]


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — réponse brute, non filtrée."""
    if not CLE_RAPIDAPI:
        return {"erreur": "RAPIDAPI_KEY non configurée"}
    if not _peut_appeler():
        return {"erreur": "limite de débit locale atteinte, réessaie dans un instant"}
    jour_aaaammjj = jour_iso.replace("-", "")
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}/football-get-matches-by-date",
                headers=HEADERS,
                params={"date": jour_aaaammjj},
                timeout=15.0,
            )
            corps = r.json()
            corps["_statut_http"] = r.status_code
            return corps
    except Exception as e:
        return {"erreur": str(e)}
