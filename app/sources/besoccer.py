"""
Source : BeSoccer.

BeSoccer rend son contenu majoritairement côté serveur (HTML classique),
ce qui le rend scrapable avec httpx + BeautifulSoup — contrairement à
Flashscore. En contrepartie, contrairement à l'API JSON de Sofascore,
ce module dépend directement de la structure HTML de leurs pages, qui
peut changer sans préavis.

⚠️ IMPORTANT : les sélecteurs CSS ci-dessous (`select_one`, `select`)
sont une hypothèse raisonnable basée sur des structures de sites de
stats sportives classiques, mais n'ont PAS pu être vérifiés contre le
HTML réel de BeSoccer depuis cet environnement (pas d'accès réseau
sortant vers ce domaine ici). Avant la mise en prod :
  1. Fais une requête réelle vers une page de recherche BeSoccer
  2. Inspecte le HTML retourné (clic droit > Inspecter dans le navigateur)
  3. Ajuste les sélecteurs ci-dessous en conséquence
"""

from typing import Optional, Dict, Any, List
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

SEARCH_URL = "https://www.besoccer.com/search?q={query}"


async def _fetch_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, headers=HEADERS, timeout=8.0, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        pass
    return None


async def find_team_url(client: httpx.AsyncClient, nom_equipe: str) -> Optional[str]:
    html = await _fetch_html(client, SEARCH_URL.format(query=nom_equipe))
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    # ⚠️ Sélecteur à vérifier/ajuster contre le HTML réel (voir docstring du module)
    lien = soup.select_one("a.team-name, a[href*='/equipo/'], a[href*='/team/']")
    if lien and lien.get("href"):
        href = lien["href"]
        return href if href.startswith("http") else f"https://www.besoccer.com{href}"
    return None


async def get_team_recent_results(
    client: httpx.AsyncClient, team_url: str
) -> Optional[Dict[str, Any]]:
    html = await _fetch_html(client, team_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    # ⚠️ Sélecteur à vérifier/ajuster contre le HTML réel (voir docstring du module)
    lignes = soup.select(".match-result, .panel-match")[:5]
    if not lignes:
        return None

    resultats: List[str] = []
    for ligne in lignes:
        classes = ligne.get("class", [])
        if any("win" in c for c in classes):
            resultats.append("V")
        elif any("draw" in c for c in classes):
            resultats.append("N")
        elif any("loss" in c for c in classes):
            resultats.append("D")

    if not resultats:
        return None

    return {"forme": resultats, "matchs_analyses": len(resultats)}


async def get_team_stats(nom_equipe: str) -> Optional[Dict[str, Any]]:
    """Point d'entrée utilisé par le reste de l'app. Renvoie None si indisponible."""
    async with httpx.AsyncClient() as client:
        url = await find_team_url(client, nom_equipe)
        if not url:
            return None
        stats = await get_team_recent_results(client, url)
        if stats is None:
            return None
        stats["source"] = "BeSoccer"
        return stats
