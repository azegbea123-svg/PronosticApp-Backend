"""
Intégration football-data.org (v4) — remplace API-Football, dont le
compte a été suspendu (voir historique). Même rôle : liste brute des
matchs du jour, utilisée par matchs.py pour construire /matchs.

⚠️ Limite du plan gratuit : seule une douzaine de grandes compétitions
sont couvertes (Premier League, Liga, Serie A, Bundesliga, Ligue 1,
Ligue des Champions, Championship, Eredivisie, Primeira Liga,
Brasileirão, Coupe du Monde, Euro...). PAS de divisions inférieures, PAS
de championnat togolais ni de compétitions CAF sur ce plan. "Tous les
matchs" ici veut dire "tous les matchs des compétitions couvertes par
le plan gratuit" — une vraie limite du fournisseur, pas un filtre qu'on
choisit.

Limite de débit documentée : 10 requêtes/minute sur le plan gratuit,
pas de plafond journalier officiel connu. Le limiteur ci-dessous garde
quand même une marge de sécurité, par principe.
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


async def _appeler(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Renvoie None si la clé n'est pas configurée, si le débit local est
    dépassé, ou en cas d'erreur HTTP/réseau. Le corps d'erreur exact de
    football-data.org (403 suspendu, 429 rate-limit, etc.) est
    disponible via debug_football_data / debug_fixtures côté main.py en
    cas de besoin de diagnostic — ici on reste simple : ça marche ou pas.
    """
    if not CLE_FOOTBALL_DATA:
        return None
    if not _peut_appeler():
        return None

    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params or {}, timeout=15.0)
            data = r.json()
            if r.status_code != 200:
                # On renvoie quand même le corps (contient souvent "message"
                # expliquant pourquoi) pour que l'appelant debug puisse
                # l'inspecter — mais get_fixtures_du_jour traite ça comme
                # un échec (None) pour ne pas planter la liste.
                data["_statut_http"] = r.status_code
                return data if endpoint == "__debug__" else None
            return data
    except Exception:
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """
    Liste BRUTE des matchs programmés à cette date (YYYY-MM-DD), au
    format natif football-data.org (voir matchs.py pour la
    normalisation). Une seule requête par jour interrogé.

    Renvoie None si le quota est épuisé, la clé non configurée, ou en
    cas d'erreur — l'appelant garde alors la dernière liste connue.
    """
    data = await _appeler("/matches", {"dateFrom": jour_iso, "dateTo": jour_iso})
    if data is None:
        return None
    return data.get("matches", [])


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — renvoie la réponse brute (succès ou erreur) sans la filtrer."""
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
