"""
OpenLigaDB — base gratuite, communautaire, SANS clé et SANS limite de
débit documentée. Couvre le football allemand uniquement (Bundesliga,
2. Bundesliga, 3. Liga, DFB-Pokal...), mais avec un vrai luxe : les
divisions inférieures allemandes (2./3. Liga), que ni football-data.org
ni le plan gratuit de RapidAPI/TheSportsDB ne couvrent aussi bien.

Tourne EN PARALLÈLE des trois autres sources (voir matchs.py). Niche
par nature (un seul pays) — c'est un complément, pas une source
principale.

Pas d'endpoint "tous les matchs à telle date, tous championnats" sur
OpenLigaDB : on interroge la journée EN COURS de chaque championnat
suivi (GetMatchdata/{shortcut}, qui renvoie la journée actuelle sans
préciser de saison), puis on filtre côté client sur la date demandée.
"""

from datetime import timezone
from typing import Optional, Dict, Any, List

import httpx

BASE_URL = "https://api.openligadb.de"

# Championnats suivis (shortcuts OpenLigaDB). Liste volontairement
# courte — ajouter un shortcut ici suffit à en suivre un de plus.
LIGUES_SUIVIES = {
    "bl1": "Bundesliga",
    "bl2": "2. Bundesliga",
    "bl3": "3. Liga",
}


def _normaliser(m: Dict[str, Any], libelle_ligue: str) -> Optional[Dict[str, Any]]:
    """fixture_id préfixé "openliga-" pour ne jamais entrer en collision
    avec les autres fournisseurs une fois les listes fusionnées (voir matchs.py)."""
    try:
        date_brute = m.get("matchDateTimeUTC") or m["matchDateTime"]
        if not date_brute.endswith("Z"):
            date_brute += "Z"
        return {
            "fixture_id": f"openliga-{m['matchID']}",
            "date": date_brute,
            "league_id": 0,  # OpenLigaDB identifie par shortcut (texte), pas par id numérique
            "league": libelle_ligue,
            "equipe1": m["team1"]["teamName"],
            "equipe2": m["team2"]["teamName"],
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError):
        return None


async def _appeler_ligue(shortcut: str) -> Optional[List[Dict[str, Any]]]:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/getmatchdata/{shortcut}", timeout=15.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """
    Liste des matchs pour cette date (YYYY-MM-DD), déjà normalisée.
    Interroge chaque championnat suivi (journée actuelle uniquement,
    OpenLigaDB ne propose pas de filtre par date), puis garde seulement
    les matchs dont la date correspond à jour_iso.

    Renvoie None uniquement si TOUS les appels échouent (pas de
    distinction quota/clé ici, OpenLigaDB n'en a pas) — une liste vide
    veut dire "interrogé avec succès, rien ce jour-là".
    """
    tout_a_echoue = True
    matchs: List[Dict[str, Any]] = []

    for shortcut, libelle in LIGUES_SUIVIES.items():
        bruts = await _appeler_ligue(shortcut)
        if bruts is None:
            continue
        tout_a_echoue = False
        for b in bruts:
            m = _normaliser(b, libelle)
            if m and m["date"][:10] == jour_iso:
                matchs.append(m)

    if tout_a_echoue:
        return None
    return matchs


async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """🔧 Pour /debug/fixtures — réponse brute de chaque championnat suivi, non filtrée."""
    resultats = {}
    for shortcut in LIGUES_SUIVIES:
        resultats[shortcut] = await _appeler_ligue(shortcut)
    return resultats
