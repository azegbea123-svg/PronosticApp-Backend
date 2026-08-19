"""
Source : Sofascore.

Sofascore charge ses données via une API JSON interne (api.sofascore.com),
non officiellement documentée mais largement utilisée par des projets open
source pour cette raison. C'est la source la plus stable des trois car on
ne dépend pas du parsing HTML.

⚠️ Cette API n'étant pas officielle, elle peut changer sans préavis.
Si un champ n'est plus trouvé, la fonction renvoie None plutôt que de
planter — à surveiller en prod (logs) pour détecter une cassure.
"""

from typing import Optional, Dict, Any, List
import httpx

BASE = "https://api.sofascore.com/api/v1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


async def _get(client: httpx.AsyncClient, url: str) -> Optional[dict]:
    try:
        r = await client.get(url, headers=HEADERS, timeout=8.0)
        if r.status_code == 200:
            return r.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


async def find_team_id(client: httpx.AsyncClient, nom_equipe: str) -> Optional[int]:
    data = await _get(client, f"{BASE}/search/all?q={nom_equipe}")
    if not data:
        return None
    for res in data.get("results", []):
        if res.get("type") == "team":
            entity = res.get("entity", {})
            if entity.get("id"):
                return entity["id"]
    return None


async def get_recent_form(
    client: httpx.AsyncClient, team_id: int, n: int = 5
) -> Optional[Dict[str, Any]]:
    data = await _get(client, f"{BASE}/team/{team_id}/events/last/0")
    if not data:
        return None

    events = data.get("events", [])[:n]
    if not events:
        return None

    resultats: List[str] = []
    buts_marques = 0
    buts_encaisses = 0

    for e in events:
        home = e.get("homeTeam", {})
        away = e.get("awayTeam", {})
        home_score = e.get("homeScore", {}).get("current")
        away_score = e.get("awayScore", {}).get("current")

        if home_score is None or away_score is None:
            continue

        est_domicile = home.get("id") == team_id
        buts_pour = home_score if est_domicile else away_score
        buts_contre = away_score if est_domicile else home_score

        buts_marques += buts_pour
        buts_encaisses += buts_contre

        if buts_pour > buts_contre:
            resultats.append("V")
        elif buts_pour == buts_contre:
            resultats.append("N")
        else:
            resultats.append("D")

    if not resultats:
        return None

    return {
        "forme": resultats,  # ex: ["V", "V", "N", "D", "V"] (plus récent en premier)
        "buts_marques": buts_marques,
        "buts_encaisses": buts_encaisses,
        "matchs_analyses": len(resultats),
    }


async def get_team_stats(nom_equipe: str) -> Optional[Dict[str, Any]]:
    """Point d'entrée utilisé par le reste de l'app. Renvoie None si indisponible."""
    async with httpx.AsyncClient() as client:
        team_id = await find_team_id(client, nom_equipe)
        if team_id is None:
            return None
        forme = await get_recent_form(client, team_id)
        if forme is None:
            return None
        forme["team_id"] = team_id
        forme["source"] = "Sofascore"
        return forme
