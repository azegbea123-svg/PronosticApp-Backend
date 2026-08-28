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
import time
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

# ==== Cache mémoire simple ====
#
# ⚠️ En mémoire du processus (pas Redis/DB) — se vide à chaque redémarrage
# du serveur (fréquent sur le plan gratuit Render, qui s'endort après
# inactivité). Suffisant pour l'objectif visé : éviter de refaire toute
# la recherche de slug (jusqu'à 9 requêtes HTTP) à chaque fois que le
# MÊME utilisateur redemande un pronostic sur la même équipe dans la
# même session, ou pendant un backtest qui répète souvent les mêmes
# équipes. Pas conçu pour survivre entre déploiements.
_CACHE_STATS: Dict[str, tuple] = {}  # nom normalisé -> (valeur, expire_a)
_CACHE_ABSENT = object()  # sentinelle distincte de None (None = "confirmé aucune donnée")

_TTL_SUCCES_SECONDES = 6 * 3600  # 6h : la forme récente d'une équipe ne change pas d'heure en heure
_TTL_ECHEC_SECONDES = 1 * 3600  # 1h : plus court, pour ne pas bloquer trop longtemps un vrai correctif (ex: ajout dans _SLUGS_CONNUS)


def _cache_cle(nom_equipe: str) -> str:
    return nom_equipe.strip().lower()


def _cache_lire(nom_equipe: str):
    entree = _CACHE_STATS.get(_cache_cle(nom_equipe))
    if entree is None:
        return _CACHE_ABSENT
    valeur, expire_a = entree
    if time.time() >= expire_a:
        return _CACHE_ABSENT
    return valeur


def _cache_ecrire(nom_equipe: str, valeur: Optional[Dict[str, Any]]) -> None:
    ttl = _TTL_SUCCES_SECONDES if valeur is not None else _TTL_ECHEC_SECONDES
    _CACHE_STATS[_cache_cle(nom_equipe)] = (valeur, time.time() + ttl)


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
    "cologne": "1-fc-koln",  # ⚠️ hypothèse (nom officiel "1. FC Köln"), pas encore confirmée sur une vraie page
}


def _slugify(texte: str) -> str:
    """Convertit un nom d'équipe en slug d'URL (minuscule, sans accents, tirets)."""
    texte_normalise = unicodedata.normalize("NFKD", texte)
    texte_sans_accents = "".join(c for c in texte_normalise if not unicodedata.combining(c))
    texte_propre = re.sub(r"[^a-zA-Z0-9\s-]", "", texte_sans_accents).strip().lower()
    return re.sub(r"\s+", "-", texte_propre)


# Équivalences fréquentes entre l'anglais et l'usage interne BeSoccer
# (souvent hispanisant à l'origine). Remplacement sur des MOTS ENTIERS
# uniquement (limites de mot strictes) — sans ça, "st" matche à
# l'intérieur de "castilla", "ii" à l'intérieur d'autres mots, etc.
_EQUIVALENCES = [
    ("united", "utd"),
    ("saint", "st"),
    ("munich", "munchen"),  # BeSoccer semble utiliser l'orthographe allemande (cas réel : Bayern Munich)
    ("cologne", "koln"),
    ("nuremberg", "nurnberg"),
]


def _remplacer_mot_entier(texte: str, mot: str, remplacement: str) -> str:
    return re.sub(rf"\b{re.escape(mot)}\b", remplacement, texte)


def _candidats_slug(nom_equipe: str) -> List[str]:
    """
    Génère un large éventail de variantes de slug à essayer, de la plus
    probable à la moins probable — volontairement exhaustif : BeSoccer
    n'offre pas de moteur de recherche fiable, donc plus on teste de
    variantes plausibles avant d'abandonner, moins on rate d'équipes qui
    existent réellement sur BeSoccer mais sous un slug inattendu.
    """
    candidats: List[str] = []

    def _ajouter(candidat: str) -> None:
        if candidat and candidat not in candidats:
            candidats.append(candidat)

    nom_sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", nom_equipe.lower()) if not unicodedata.combining(c)
    )

    # 1. Correspondances connues, vérifiées manuellement — toujours en premier.
    for mot_cle, slug_connu in _SLUGS_CONNUS.items():
        if mot_cle in nom_sans_accents:
            _ajouter(slug_connu)

    # 2. Slug direct, tel quel.
    _ajouter(_slugify(nom_equipe))

    # 3. Sans les tokens génériques (FC, CF, AFC, CD, SD, UD, RC, AC, CA, "de", "club").
    mots = nom_equipe.split()
    mots_filtres = [m for m in mots if m.lower() not in _TOKENS_A_RETIRER]
    if mots_filtres and len(mots_filtres) != len(mots):
        _ajouter(_slugify(" ".join(mots_filtres)))

    # 4. Sans espaces ni tirets du tout (certains clubs composés sont
    # collés en un seul mot sur BeSoccer).
    _ajouter(_slugify(nom_equipe).replace("-", ""))

    # 5. Seulement le premier ou le dernier mot significatif (utile pour
    # les équipes très souvent désignées par un seul mot dans l'usage
    # courant, ex: "Barcelona" plutôt que le nom complet). Filtré sur une
    # longueur minimale : un mot trop court/générique ("ii", "cp", "sc")
    # risquerait de matcher la page BeSoccer d'une TOUT AUTRE équipe —
    # un faux positif silencieux serait pire que pas de données du tout.
    if mots_filtres and len(mots_filtres) > 1:
        for mot in (mots_filtres[-1], mots_filtres[0]):
            if len(mot) >= 4:
                _ajouter(_slugify(mot))

    # 6. Équivalences lexicales connues (mots entiers uniquement, dans
    # les deux sens), appliquées sur le nom déjà nettoyé des tokens
    # génériques.
    base = " ".join(mots_filtres) if mots_filtres else nom_equipe
    base_minuscule = base.lower()
    for terme_a, terme_b in _EQUIVALENCES:
        variante_a = _remplacer_mot_entier(base_minuscule, terme_a, terme_b)
        if variante_a != base_minuscule:
            _ajouter(_slugify(variante_a))
        variante_b = _remplacer_mot_entier(base_minuscule, terme_b, terme_a)
        if variante_b != base_minuscule:
            _ajouter(_slugify(variante_b))

    # 7. Variante "espoirs"/"réserve" fréquente sur BeSoccer : équipe B /
    # équipe II souvent notée avec un tiret ("-b", "-ii") même quand le
    # nom saisi utilise un espace, et l'inverse (II <-> B).
    for suffixe, alternative in ((" ii", "-b"), (" b", "-ii")):
        if base_minuscule.endswith(suffixe):
            racine = _slugify(base_minuscule[: -len(suffixe)].strip())
            suffixe_propre = suffixe.strip()  # "ii" ou "b"
            _ajouter(f"{racine}-{suffixe_propre}")
            _ajouter(f"{racine}{alternative}")

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


_MOIS_ABBR = {
    "jan": 1, "feb": 2, "fev": 2, "mar": 3, "apr": 4, "avr": 4, "may": 5, "mai": 5,
    "jun": 6, "jui": 6, "jul": 7, "aug": 8, "aou": 8, "sep": 9, "oct": 10,
    "nov": 11, "dec": 12,
}
_JOURS_CUMULES_AVANT_MOIS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def _parser_jour_approximatif(texte: str) -> Optional[int]:
    """
    Cherche une date du type "16 Aug." ou "3 Sep" dans le texte d'un lien
    de match, et renvoie un jour-de-l'année approximatif (1-365, sans
    tenir compte des années bissextiles — précision suffisante pour
    comparer des écarts de quelques jours entre deux matchs).

    ⚠️ Format non vérifié sur une vraie page BeSoccer (mon environnement
    ne peut pas y accéder directement) — à confirmer via /debug/besoccer
    avant de faire confiance à ce signal. Si le format réel diffère,
    cette fonction renverra simplement None partout, sans rien casser
    (le signal de fatigue sera juste silencieusement inactif).
    """
    m = re.search(r"\b(\d{1,2})\s+([A-Za-zÀ-ÿ]{3,})\.?\b", texte)
    if not m:
        return None
    jour = int(m.group(1))
    mois_abbr = m.group(2)[:3].lower()
    mois = _MOIS_ABBR.get(mois_abbr)
    if mois is None or not (1 <= jour <= 31):
        return None
    return _JOURS_CUMULES_AVANT_MOIS[mois - 1] + jour


def _jours_moyens_entre_matchs(matchs_detail: List[Dict[str, Any]]) -> Optional[float]:
    """
    Écart moyen (en jours) entre les matchs listés, du plus récent au
    plus ancien. Sert de signal de "congestion du calendrier" — une
    équipe qui enchaîne les matchs tous les 3 jours est généralement
    plus sujette à la fatigue qu'une équipe qui en joue un tous les 7-10
    jours.

    Renvoie None si moins de 2 dates exploitables (rien à comparer), ou
    si le format de date n'a pas pu être reconnu par
    _parser_jour_approximatif (voir sa docstring — non vérifié sur une
    vraie page BeSoccer).

    Gère le passage d'une année à l'autre de façon approximative : si un
    match plus ancien semble "après" dans l'année civile un match plus
    récent (ex: match récent en janvier, précédent en décembre), on
    ajoute 365 jours pour obtenir un écart positif cohérent.
    """
    jours = [m["jour_annee_approx"] for m in matchs_detail if m.get("jour_annee_approx") is not None]
    if len(jours) < 2:
        return None

    ecarts = []
    for plus_recent, plus_ancien in zip(jours, jours[1:]):
        ecart = plus_recent - plus_ancien
        if ecart < 0:
            ecart += 365  # passage d'année
        if ecart > 0:  # ignore les doublons/dates identiques (rien à en tirer)
            ecarts.append(ecart)

    if not ecarts:
        return None
    return sum(ecarts) / len(ecarts)


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

        # Domicile/extérieur pour CE match précis, + slug de l'adversaire
        # (nécessaire pour retrouver ce match précis plus tard, quand on
        # cherche à vérifier une prédiction enregistrée au préalable).
        parties = [p for p in href.split("/") if p]
        etait_domicile: Optional[bool] = None
        adversaire_slug: Optional[str] = None
        try:
            i = parties.index("match")
            slug_domicile, slug_exterieur = parties[i + 1], parties[i + 2]
            etait_domicile = slug_domicile == slug_equipe
            adversaire_slug = slug_exterieur if etait_domicile else slug_domicile
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
                "adversaire_slug": adversaire_slug,
                "jour_annee_approx": _parser_jour_approximatif(texte_complet),  # None si non reconnu
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
        "jours_moyens_entre_matchs": _jours_moyens_entre_matchs(matchs_detail),  # None si dates non exploitables
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
    """
    Point d'entrée utilisé par le reste de l'app. Renvoie None si
    vraiment aucune variante de slug ne donne de données exploitables.

    ⚠️ Essaie TOUTES les variantes de slug candidates avant d'abandonner
    — pas seulement jusqu'à la première page qui répond HTTP 200. Une
    page peut exister (bon slug, statut 200) mais ne rien donner
    d'exploitable (structure différente, page vide, saison sans matchs
    récents...) ; dans ce cas, une autre variante de slug peut mener à
    la VRAIE bonne page. S'arrêter au premier 200 sans vérifier que
    l'extraction réussit faisait rater des équipes qui existaient
    pourtant bien sur BeSoccer sous un autre slug (cas réel : Celta Vigo).

    ⚠️ Mis en cache en mémoire (voir _CACHE_STATS ci-dessous) — la
    recherche élargie (jusqu'à 9 variantes de slug) fait grimper le
    nombre de requêtes BeSoccer par équipe ; sans cache, chaque
    pronostic redemandé refait toute la recherche depuis zéro, ce qui
    ralentit l'appli et rapproche du seuil qui avait déjà causé un
    blocage BeSoccer lors d'un backtest précédent.
    """
    resultat = await _get_team_stats_sans_cache(nom_equipe)
    _cache_ecrire(nom_equipe, resultat)
    return resultat


async def _get_team_stats_sans_cache(nom_equipe: str) -> Optional[Dict[str, Any]]:
    en_cache = _cache_lire(nom_equipe)
    if en_cache is not _CACHE_ABSENT:
        return en_cache

    async with httpx.AsyncClient() as client:
        for slug in _candidats_slug(nom_equipe):
            html = await _get_html(client, TEAM_URL.format(slug=slug))
            if not html:
                continue
            stats = _extraire_forme_recente(html, slug)
            if stats is None:
                continue
            stats["source"] = "BeSoccer"
            stats["indisponibles"] = _extraire_indisponibles(html)
            stats["slug"] = slug
            return stats
    return None


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
