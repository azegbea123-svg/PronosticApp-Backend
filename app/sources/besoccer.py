"""
Source : BeSoccer.

Contrairement à la première version, ce module ne suppose plus une URL de
recherche générique (`/search?q=...`), qui n'existe pas sur BeSoccer et
renvoyait un 404 (confirmé en prod). Les pages d'équipe suivent en réalité
le format `besoccer.com/team/{slug}` — vérifié en direct pour plusieurs
équipes (real-madrid, barcelona, malaga).

Chaque page équipe contient une section "Form in last matches" listant les
5 derniers résultats. Le score de L'ÉQUIPE DE LA PAGE est systématiquement
mis en gras (balise <b>/<strong>), qu'elle ait joué à domicile ou à
l'extérieur — c'est ce signal qu'on utilise pour identifier son propre
score sans dépendre de l'ordre domicile/extérieur.

⚠️ Le slug d'une équipe n'est pas toujours son nom tel quel : "FC Barcelona"
devient "barcelona" (le "FC" est retiré), "Real Madrid" reste "real-madrid".
Comme il n'existe pas de règle universelle vérifiable sans base de données
BeSoccer complète, plusieurs variantes de slug sont essayées dans l'ordre
avant d'abandonner.
"""

import re
import unicodedata
from typing import Optional, Dict, Any, List
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

TEAM_URL = "https://www.besoccer.com/team/{slug}"

# Tokens fréquemment absents du slug BeSoccer (préfixes/mots de liaison
# courants dans les noms de clubs) — retirés pour générer des variantes.
_TOKENS_A_RETIRER = {"fc", "cf", "afc", "cd", "sd", "ud", "rc", "ac", "ca", "de", "club"}


def _slugify(texte: str) -> str:
    """Convertit un nom d'équipe en slug d'URL (minuscule, sans accents, tirets)."""
    texte_normalise = unicodedata.normalize("NFKD", texte)
    texte_sans_accents = "".join(c for c in texte_normalise if not unicodedata.combining(c))
    texte_propre = re.sub(r"[^a-zA-Z0-9\s-]", "", texte_sans_accents).strip().lower()
    return re.sub(r"\s+", "-", texte_propre)


def _candidats_slug(nom_equipe: str) -> List[str]:
    """Génère plusieurs variantes de slug à essayer, de la plus probable à la moins probable."""
    mots = nom_equipe.split()
    candidats = [_slugify(nom_equipe)]

    mots_filtres = [m for m in mots if m.lower() not in _TOKENS_A_RETIRER]
    if mots_filtres and len(mots_filtres) != len(mots):
        candidat_filtre = _slugify(" ".join(mots_filtres))
        if candidat_filtre not in candidats:
            candidats.append(candidat_filtre)

    return candidats


async def _get_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(url, headers=HEADERS, timeout=10.0, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        pass
    return None


async def _trouver_page_equipe(client: httpx.AsyncClient, nom_equipe: str) -> Optional[str]:
    for slug in _candidats_slug(nom_equipe):
        html = await _get_html(client, TEAM_URL.format(slug=slug))
        if html:
            return html
    return None


def _extraire_forme_recente(html: str, n: int = 5) -> Optional[Dict[str, Any]]:
    """
    Parcourt tous les liens /match/... de la page et identifie ceux qui
    contiennent un score avec une partie en gras (= score de l'équipe de
    la page). Dédoublonne par URL de match pour éviter de compter deux
    fois le même match s'il apparaît dans plusieurs sections de la page
    (ex: "Last match" ET "Form in last matches").
    """
    soup = BeautifulSoup(html, "html.parser")

    vus: set = set()
    resultats: List[str] = []
    buts_marques = 0
    buts_encaisses = 0

    for lien in soup.find_all("a", href=re.compile(r"/match/")):
        href = lien.get("href", "")
        if href in vus:
            continue

        gras = lien.find(["b", "strong"])
        if not gras:
            continue

        texte_complet = lien.get_text(" ", strip=True)
        match_score = re.search(r"(\d+)\s*-\s*(\d+)", texte_complet)
        if not match_score:
            continue

        score_gauche, score_droite = int(match_score.group(1)), int(match_score.group(2))

        try:
            score_propre = int(gras.get_text(strip=True))
        except ValueError:
            continue

        if score_propre == score_gauche:
            score_adverse = score_droite
        elif score_propre == score_droite:
            score_adverse = score_gauche
        else:
            continue  # score en gras qui ne correspond à aucun des deux nombres trouvés

        vus.add(href)
        buts_marques += score_propre
        buts_encaisses += score_adverse

        if score_propre > score_adverse:
            resultats.append("V")
        elif score_propre == score_adverse:
            resultats.append("N")
        else:
            resultats.append("D")

        if len(resultats) >= n:
            break

    if not resultats:
        return None

    return {
        "forme": resultats,
        "buts_marques": buts_marques,
        "buts_encaisses": buts_encaisses,
        "matchs_analyses": len(resultats),
    }


async def get_team_stats(nom_equipe: str) -> Optional[Dict[str, Any]]:
    """Point d'entrée utilisé par le reste de l'app. Renvoie None si indisponible."""
    async with httpx.AsyncClient() as client:
        html = await _trouver_page_equipe(client, nom_equipe)
        if not html:
            return None
        stats = _extraire_forme_recente(html)
        if stats is None:
            return None
        stats["source"] = "BeSoccer"
        return stats
