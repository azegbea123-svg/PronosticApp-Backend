"""
Intégration TheSportsDB — remplace football-data.org comme fournisseur
de la liste de matchs du jour, pour une couverture plus large (~617
championnats de football recensés, contre ~12 sur le plan gratuit de
football-data.org).

⚠️ Clé "3" = clé de test PARTAGÉE par toute la communauté TheSportsDB,
gratuite mais sans garantie de débit dédié (elle peut être plus lente
aux heures de pointe, car tout le monde s'en sert). Une clé personnelle
gratuite/à bas coût s'obtient via patreon.com/thesportsdb si besoin de
plus de fiabilité — dans ce cas, définis THESPORTSDB_API_KEY sur Render.

⚠️ Les horaires (strTime) viennent d'une base contributive (comme
Wikipédia) : leur fuseau horaire n'est pas garanti à 100% cohérent pour
toutes les compétitions. À surveiller si des horaires affichés dans
l'appli semblent décalés.
"""

import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import httpx

from ..config import CLE_THESPORTSDB

BASE_URL = "https://www.thesportsdb.com/api/v1/json"

_MAX_APPELS_PAR_MINUTE = 20  # marge sous les ~30/min généralement documentées

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
    if not _peut_appeler():
        return None
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/{CLE_THESPORTSDB}{endpoint}", params=params or {}, timeout=15.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


def _normaliser(evt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        fixture_id = f"tsdb-{evt['idEvent']}"
        jour = evt["dateEvent"]
        heure = evt.get("strTime") or "00:00:00"
        date_iso = f"{jour}T{heure[:8]}Z"  # voir avertissement fuseau horaire en tête de fichier
        try:
            league_id = int(evt.get("idLeague") or 0)
        except (TypeError, ValueError):
            league_id = 0

        return {
            "fixture_id": fixture_id,
            "date": date_iso,
            "league_id": league_id,
            "league": evt.get("strLeague") or "Inconnu",
            "equipe1": evt.get("strHomeTeam") or "?",
            "equipe2": evt.get("strAwayTeam") or "?",
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError, ValueError):
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """
    Liste BRUTE (déjà passée par _normaliser, contrairement aux autres
    modules sources/ — le format TheSportsDB est trop différent pour
    partager la même fonction _normaliser que matchs.py utilisait pour
    football-data.org/API-Football) des matchs pour cette date
    (YYYY-MM-DD), tous championnats de football confondus.
    """
    data = await _appeler("/eventsday.php", {"d": jour_iso, "s": "Soccer"})
    if data is None:
        return None
    evenements = data.get("events") or []  # l'API renvoie {"events": null} quand il n'y a rien ce jour-là
    matchs = [m for m in (_normaliser(e) for e in evenements) if m is not None]
    return matchs


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — réponse brute, non filtrée."""
    if not _peut_appeler():
        return {"erreur": "limite de débit locale atteinte, réessaie dans un instant"}
    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}/{CLE_THESPORTSDB}/eventsday.php",
                params={"d": jour_iso, "s": "Soccer"},
                timeout=15.0,
            )
            corps = r.json()
            corps["_statut_http"] = r.status_code
            return corps
    except Exception as e:
        return {"erreur": str(e)}
