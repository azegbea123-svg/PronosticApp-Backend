"""
SportAPI7 (RapidAPI, hôte sportapi7.p.rapidapi.com) — données au format
Sofascore. Cinquième source de la liste de matchs.

⚠️ Ce qui est VÉRIFIÉ et ce qui ne l'est pas :
  - VÉRIFIÉ : la structure d'un événement (/api/v1/event/{id}) — équipes,
    tournoi, statut, score, startTimestamp — d'après la capture fournie
    (tests/fixtures/sportapi7_event.json). Le parseur `_normaliser` est testé dessus.
  - À VÉRIFIER : le chemin de la LISTE par date (CHEMIN_PROGRAMME). La capture
    ne contenait que le détail d'un événement. Le chemin ci-dessous est celui
    de l'API Sofascore dont SportAPI7 est le miroir ; teste-le avec
    GET /debug/sportapi7?date=AAAA-MM-JJ (admin). S'il est faux ou si ton
    abonnement ne le couvre pas, la source renvoie simplement None : le
    disjoncteur (sources/sante.py) la met en pause et le reste fonctionne.

Choix volontaire : le statut "prevu"/"en_direct" n'est PAS exposé — cette
source n'est pas rafraîchie en direct, un statut périmé serait pire qu'un
statut inconnu (l'appli affiche alors l'heure). Seuls terminé / reporté /
annulé sont repris, avec le score des matchs terminés.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ..config import CLE_RAPIDAPI

HOTE = "sportapi7.p.rapidapi.com"
BASE_URL = f"https://{HOTE}"
CHEMIN_PROGRAMME = "/api/v1/sport/football/scheduled-events/{date}"  # À VÉRIFIER (voir en-tête)

_MAX_APPELS_PAR_MINUTE = 5
_horodatages_appels: List[float] = []
_cache: Dict[str, Any] = {}
TTL_PROGRAMME = 30 * 60

_STATUTS_REPRIS = {"finished": "termine", "postponed": "reporte", "canceled": "annule", "cancelled": "annule"}


def _peut_appeler() -> bool:
    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    return len(_horodatages_appels) < _MAX_APPELS_PAR_MINUTE


async def _get(chemin: str) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI or not _peut_appeler():
        return None
    _horodatages_appels.append(time.time())
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}{chemin}",
                headers={"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": HOTE},
                timeout=15.0,
            )
        if r.status_code != 200:
            return None
        corps = r.json()
        return corps if isinstance(corps, dict) else None
    except Exception:
        return None


def _normaliser(evenement: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Un événement Sofascore -> format interne commun. None si incomplet."""
    try:
        debut = datetime.fromtimestamp(int(evenement["startTimestamp"]), tz=timezone.utc)
        tournoi = evenement.get("tournament") or {}
        pays = ((tournoi.get("category") or {}).get("country") or {}).get("alpha3")
        statut = _STATUTS_REPRIS.get(((evenement.get("status") or {}).get("type") or "").lower())
        score1 = (evenement.get("homeScore") or {}).get("current") if statut == "termine" else None
        score2 = (evenement.get("awayScore") or {}).get("current") if statut == "termine" else None
        sortie = {
            "fixture_id": f"sofa-{evenement['id']}",
            "date": debut.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "league_id": (tournoi.get("uniqueTournament") or {}).get("id") or tournoi.get("id") or 0,
            "league": tournoi.get("name") or "",
            "equipe1": evenement["homeTeam"]["name"],
            "equipe2": evenement["awayTeam"]["name"],
            "donneesDisponibles": None,
            "sofa_id": evenement["id"],
        }
        if pays:
            sortie["league_pays"] = pays
        if statut:
            sortie.update(statut=statut, score1=score1, score2=score2)
        return sortie
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """Matchs du jour (YYYY-MM-DD). None = échec (clé, abonnement, chemin invalide, quota)."""
    hit = _cache.get(jour_iso)
    if hit and hit[0] > time.time():
        return hit[1]
    data = await _get(CHEMIN_PROGRAMME.format(date=jour_iso))
    if data is None:
        return None
    bruts = data.get("events")
    if not isinstance(bruts, list):
        return None  # forme inattendue = échec, pas "aucun match"
    matchs = [m for m in (_normaliser(b) for b in bruts) if m is not None]
    matchs = [m for m in matchs if m["date"][:10] == jour_iso]
    _cache[jour_iso] = (time.time() + TTL_PROGRAMME, matchs)
    return matchs


async def appel_diagnostic(jour_iso: str) -> Dict[str, Any]:
    if not CLE_RAPIDAPI:
        return {"erreur": "RAPIDAPI_KEY non configurée"}
    data = await _get(CHEMIN_PROGRAMME.format(date=jour_iso))
    if data is None:
        return {"erreur": "appel en échec (chemin à vérifier, abonnement SportAPI7, quota ou réseau)",
                "chemin_teste": CHEMIN_PROGRAMME.format(date=jour_iso)}
    return {"chemin_teste": CHEMIN_PROGRAMME.format(date=jour_iso), "cles": list(data.keys()),
            "nb_evenements": len(data.get("events") or [])}
