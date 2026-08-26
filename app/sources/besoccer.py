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

# ⚠️ Certains clubs ont un slug BeSoccer qui ne suit AUCUNE règle de
# slugification prévisible — BeSoccer est à l'origine une plateforme
# espagnole, et garde parfois en interne le nom espagnol d'un club
# international plutôt que son nom français/anglais usuel. Impossible à
# deviner par une règle générique : table de correspondances connues,
# à étoffer au fil des cas rencontrés (clé = mot-clé cherché dans le nom
# saisi, en minuscule et sans accents).
_SLUGS_CONNUS: dict = {
    "marseille": "olympique-marsella",  # PAS "olympique-marseille" — vérifié
}


def _slugify(texte: str) -> str:
    """Convertit un nom d'équipe en slug d'URL (minuscule, sans accents, tirets)."""
    texte_normalise = unicodedata.normalize("NFKD", texte)
    texte_sans_accents = "".join(c for c in texte_normalise if not unicodedata.combining(c))
    texte_propre = re.sub(r"[^a-zA-Z0-9\s-]", "", texte_sans_accents).strip().lower()
    return re.sub(r"\s+", "-", texte_propre)


def _candidats_slug(nom_equipe: str) -> List[str]:
    """Génère plusieurs variantes de slug à essayer, de la plus probable à la moins probable."""
    candidats: List[str] = []

    # Les correspondances connues passent en premier — plus fiables
    # qu'une règle générique puisque vérifiées manuellement.
    nom_sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", nom_equipe.lower()) if not unicodedata.combining(c)
    )
    for mot_cle, slug_connu in _SLUGS_CONNUS.items():
        if mot_cle in nom_sans_accents and slug_connu not in candidats:
            candidats.append(slug_connu)

    candidats.append(_slugify(nom_equipe))

    mots = nom_equipe.split()
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


async def _trouver_page_equipe(client: httpx.AsyncClient, nom_equipe: str) -> Optional[tuple]:
    """Renvoie (html, slug_utilise) pour le premier slug qui fonctionne, ou None."""
    for slug in _candidats_slug(nom_equipe):
        html = await _get_html(client, TEAM_URL.format(slug=slug))
        if html:
            return html, slug
    return None


def _extraire_forme_recente(html: str, slug_equipe: str, n: int = 5) -> Optional[Dict[str, Any]]:
    """
    Parcourt tous les liens /match/... de la page et identifie ceux qui
    contiennent un score avec une partie en gras (= score de l'équipe de
    la page). Dédoublonne par URL de match pour éviter de compter deux
    fois le même match s'il apparaît dans plusieurs sections de la page
    (ex: "Last match" ET "Form in last matches").

    Distingue aussi domicile/extérieur pour chaque match : l'URL suit le
    format /match/{domicile}/{exterieur}/{id} — l'équipe listée en
    PREMIER est toujours celle qui recevait (confirmé par inspection
    réelle de page BeSoccer).

    Garde aussi le détail MATCH PAR MATCH (matchs_detail), dans l'ordre
    où BeSoccer les liste (le plus récent en premier) — nécessaire pour
    pondérer la forme récente par ancienneté plutôt que de traiter un
    match d'il y a 5 rencontres exactement comme celui d'hier.
    """
    soup = BeautifulSoup(html, "html.parser")

    vus: set = set()
    resultats: List[str] = []
    matchs_detail: List[Dict[str, Any]] = []
    buts_marques = 0
    buts_encaisses = 0

    dom_buts_marques = dom_buts_encaisses = dom_matchs = 0
    ext_buts_marques = ext_buts_encaisses = ext_matchs = 0

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

        # Domicile/extérieur pour CE match précis
        parties = [p for p in href.split("/") if p]
        etait_domicile: Optional[bool] = None
        try:
            i = parties.index("match")
            etait_domicile = parties[i + 1] == slug_equipe
            if etait_domicile:
                dom_matchs += 1
                dom_buts_marques += score_propre
                dom_buts_encaisses += score_adverse
            else:
                ext_matchs += 1
                ext_buts_marques += score_propre
                ext_buts_encaisses += score_adverse
        except (ValueError, IndexError):
            pass  # format d'URL inattendu : on garde quand même le score global

        matchs_detail.append(
            {
                "buts_pour": score_propre,
                "buts_contre": score_adverse,
                "domicile": etait_domicile,  # None si format d'URL inattendu
            }
        )

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

    resultat: Dict[str, Any] = {
        "forme": resultats,
        "buts_marques": buts_marques,
        "buts_encaisses": buts_encaisses,
        "matchs_analyses": len(resultats),
        "matchs_detail": matchs_detail,  # plus récent en premier
    }

    if dom_matchs > 0:
        resultat["domicile"] = {
            "buts_marques": dom_buts_marques,
            "buts_encaisses": dom_buts_encaisses,
            "matchs_analyses": dom_matchs,
        }
    if ext_matchs > 0:
        resultat["exterieur"] = {
            "buts_marques": ext_buts_marques,
            "buts_encaisses": ext_buts_encaisses,
            "matchs_analyses": ext_matchs,
        }

    return resultat


def _extraire_indisponibles(html: str) -> Optional[int]:
    """
    Compte approximatif des joueurs blessés/suspendus, en isolant la
    portion de page entre le titre "Injuries / Suspensions" et le titre
    suivant, puis en comptant les liens uniques vers des fiches joueur
    (/player/...) dans cette zone.

    ⚠️ Heuristique textuelle (recherche de sous-chaîne) plutôt que
    sélecteur CSS précis — la structure DOM exacte de cette section n'a
    pas pu être vérifiée depuis cet environnement (accès réseau limité).
    Donne un ordre de grandeur fiable dans la plupart des cas, pas un
    chiffre garanti exact à l'unité près.
    """
    # ⚠️ Le mot "Injuries" apparaît aussi dans le menu de navigation en haut
    # de page, mais sous la forme "Injuries/Suspensions" (SANS espaces, comme
    # lien d'onglet) — alors que le vrai titre de section s'écrit avec des
    # espaces : "Injuries / Suspensions". On cible cette forme précise pour
    # ne pas tomber sur le lien de menu.
    debut = html.find("Injuries / Suspensions")
    if debut == -1:
        return None

    bornes_fin = [
        html.find(marqueur, debut)
        for marqueur in ("Last seasons", "Honours", "Stadium", "Historical")
    ]
    bornes_fin = [b for b in bornes_fin if b != -1]
    fin = min(bornes_fin) if bornes_fin else debut + 4000  # borne de sécurité

    zone = html[debut:fin]
    joueurs_uniques = set(re.findall(r"/player/[a-z0-9-]+", zone, re.I))
    return len(joueurs_uniques)


async def get_team_stats(nom_equipe: str) -> Optional[Dict[str, Any]]:
    """Point d'entrée utilisé par le reste de l'app. Renvoie None si indisponible."""
    async with httpx.AsyncClient() as client:
        trouve = await _trouver_page_equipe(client, nom_equipe)
        if not trouve:
            return None
        html, slug = trouve
        stats = _extraire_forme_recente(html, slug)
        if stats is None:
            return None
        stats["source"] = "BeSoccer"
        stats["indisponibles"] = _extraire_indisponibles(html)
        return stats


ANALYSIS_URL = "https://www.besoccer.com/match/{slug1}/{slug2}/{match_id}/analysis"


def _chercher_match_id(html: str, slug_propre: str, slugs_adversaire: List[str]) -> Optional[tuple]:
    """
    Cherche sur la page un lien /match/{a}/{b}/{id} où l'une des deux
    équipes est bien la nôtre (slug_propre) et l'autre est l'adversaire
    recherché. Renvoie (slug_a, slug_b, id) dans leur ordre RÉEL tel que
    trouvé dans l'URL (important pour savoir plus tard quel ELO
    correspond à qui).

    Vérifier que NOTRE slug apparaît aussi dans le lien évite de tomber
    par erreur sur un match sans rapport affiché ailleurs sur la page
    (ex: bandeau "Most viewed matches").
    """
    for m in re.finditer(r"/match/([a-z0-9-]+)/([a-z0-9-]+)/(\d+)", html):
        a, b, match_id = m.group(1), m.group(2), m.group(3)
        if a == slug_propre and b in slugs_adversaire:
            return a, b, match_id
        if b == slug_propre and a in slugs_adversaire:
            return a, b, match_id
    return None


def _extraire_elo(html: str) -> Optional[tuple]:
    """
    Cherche dans les tableaux de la page une ligne à 3 cellules dont la
    cellule du milieu vaut exactement "ELO" — structure confirmée par
    récupération réelle d'une page d'analyse BeSoccer (ex: "92 | ELO | 73").
    """
    soup = BeautifulSoup(html, "html.parser")
    for ligne in soup.find_all("tr"):
        cellules = ligne.find_all(["td", "th"])
        textes = [c.get_text(strip=True) for c in cellules]
        if len(textes) == 3 and textes[1] == "ELO":
            try:
                return float(textes[0]), float(textes[2])
            except ValueError:
                continue
    return None


async def get_elo_confrontation(equipe1: str, equipe2: str) -> Optional[Dict[str, Any]]:
    """
    Cherche si ces deux équipes ont un match programmé ou récent l'une
    contre l'autre (via les liens "Last match" / "Next match" / "Form in
    last matches" déjà présents sur leurs pages), et si oui, récupère
    l'ELO des deux équipes depuis la page d'analyse BeSoccer de ce match
    — un signal de force qui intègre déjà la qualité des adversaires
    affrontés par chaque équipe au fil du temps.

    Renvoie None si aucun match commun n'est trouvé (les deux équipes ne
    se sont pas croisées récemment/prochainement) — dans ce cas, le
    modèle continue de fonctionner sans ce signal supplémentaire.
    """
    candidats1 = _candidats_slug(equipe1)
    candidats2 = _candidats_slug(equipe2)

    async with httpx.AsyncClient() as client:
        page1 = await _trouver_page_equipe(client, equipe1)
        trouve = None
        slug_propre = None
        trouve_pour = None  # "equipe1" ou "equipe2" : pour qui la recherche a abouti

        if page1:
            html1, slug1 = page1
            trouve = _chercher_match_id(html1, slug1, candidats2)
            if trouve:
                slug_propre = slug1
                trouve_pour = "equipe1"

        if not trouve:
            page2 = await _trouver_page_equipe(client, equipe2)
            if page2:
                html2, slug2 = page2
                trouve = _chercher_match_id(html2, slug2, candidats1)
                if trouve:
                    slug_propre = slug2
                    trouve_pour = "equipe2"

        if not trouve:
            return None

        slug_a, slug_b, match_id = trouve
        url_analyse = ANALYSIS_URL.format(slug1=slug_a, slug2=slug_b, match_id=match_id)
        html_analyse = await _get_html(client, url_analyse)
        if not html_analyse:
            return None

        elo = _extraire_elo(html_analyse)
        if not elo:
            return None

        elo_a, elo_b = elo
        elo_propre, elo_adversaire = (elo_a, elo_b) if slug_a == slug_propre else (elo_b, elo_a)

        if trouve_pour == "equipe1":
            elo_equipe1, elo_equipe2 = elo_propre, elo_adversaire
        else:
            elo_equipe1, elo_equipe2 = elo_adversaire, elo_propre

        return {
            "elo_equipe1": elo_equipe1,
            "elo_equipe2": elo_equipe2,
            "match_id": match_id,
            "source": "BeSoccer (analyse ELO)",
        }
