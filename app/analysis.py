"""
Moteur de pronostic — v2.

Remplace l'ancien score de forme simpliste (points V=3/N=1/D=0) par un
modèle de buts attendus (expected goals) combiné à une distribution de
Poisson — une approche standard et bien documentée en analyse sportive
(popularisée par le modèle de Dixon-Coles, ici dans une version simplifiée
sans le terme de corrélation basses-scores).

Principe :
  1. Calculer la "force d'attaque" et la "faiblesse défensive" de chaque
     équipe, normalisées par rapport à une moyenne générale de buts.
  2. En déduire le nombre de buts attendus (lambda) pour chaque équipe
     dans CE match précis (force d'attaque de l'une x faiblesse
     défensive de l'autre), avec un bonus pour l'équipe à domicile.
  3. Simuler toutes les combinaisons de scores plausibles (0-0, 1-0,
     2-1, etc.) via la loi de Poisson, et sommer les probabilités pour
     obtenir victoire équipe 1 / nul / victoire équipe 2.

Les indisponibilités (blessures/suspensions) réduisent la force
d'attaque et aggravent la faiblesse défensive, proportionnellement au
nombre de joueurs concernés (plafonné pour rester réaliste).
"""

import math
from typing import Optional, Dict, Any, List, Tuple

MOYENNE_BUTS_LIGUE = 1.35   # buts marqués par équipe et par match, moyenne généraliste
AVANTAGE_DOMICILE = 1.15    # multiplicateur "championnat" par défaut — voir CATEGORIES_MATCH pour les autres
MAX_BUTS_SIMULES = 6        # borne de la grille de scores simulés (au-delà, probabilité négligeable)
PENALITE_MIN_INDISPONIBLES = 0.70  # plancher : -30% de force max, même avec beaucoup d'absents
SEUIL_JOURS_REPOS_CONFORTABLE = 6.0  # au-delà, aucune pénalité de fatigue
SEUIL_JOURS_REPOS_CRITIQUE = 3.0  # en-dessous, pénalité maximale
PENALITE_MIN_FATIGUE = 0.90  # plancher : -10% de force max en cas de calendrier très chargé — volontairement modeste tant que le signal n'est pas confirmé sur de vraies données (voir _parser_jour_approximatif)
POIDS_DECROISSANCE_ANCIENNETE = 0.85  # chaque match plus ancien pèse 15% de moins que le précédent
POIDS_LISSAGE = 3  # ajusté empiriquement (voir backtest) — ⚠️ optimisé sur le même échantillon que testé, pas une validation indépendante ; réduit aussi une partie de la protection contre les petits échantillons extrêmes (cas Arsenal/Coventry) qui avait motivé la valeur initiale de 5
RHO_DIXON_COLES = -0.20  # ajusté empiriquement (voir backtest) — meilleure précision sur les nuls que -0.10
DIVISEUR_ECHELLE_ELO = 30  # ~73/27 pour un écart de 13 points, ~81/19 pour 19 points — jamais testé empiriquement au-delà de 2-3 cas manuels
COEFFICIENT_ATTENUATION_ELO = 0.5  # jusqu'à -50% d'effet ELO si forme récente très fiable — choisi arbitrairement, jamais testé

# ⚠️ Catégorisation du type de match — jusqu'ici `type_match` ne servait
# qu'à écrire une phrase dans le résumé, sans influencer le calcul. Ce
# qui suit reste une estimation raisonnée (pas des coefficients publiés
# et validés comme Dixon-Coles), à affiner avec l'usage réel :
#   - "avantage_domicile" : une finale de coupe se joue souvent sur
#     terrain neutre -> aucun avantage à annuler ; un match amical a un
#     avantage domicile réel mais atténué (enjeu réduit, ambiance moindre)
#   - "confiance_max" : plafonne la confiance accordée à la forme
#     récente avant mélange avec l'ELO — pertinent pour les amicaux
#     (compositions rotées, peu représentatives du niveau réel) et les
#     sélections nationales (les joueurs se retrouvent rarement, la
#     "forme d'équipe" au sens club n'a pas le même sens)
CATEGORIES_MATCH: Dict[str, Dict[str, float]] = {
    "championnat": {"avantage_domicile": 1.15, "confiance_max": 1.0},
    "coupe": {"avantage_domicile": 1.10, "confiance_max": 0.9},
    "finale": {"avantage_domicile": 1.00, "confiance_max": 0.9},
    "amical": {"avantage_domicile": 1.05, "confiance_max": 0.5},
    "selection_nationale": {"avantage_domicile": 1.10, "confiance_max": 0.7},
}


def _categoriser_match(type_match: str) -> str:
    """
    Classe le type de match saisi (texte libre) dans une des catégories
    de CATEGORIES_MATCH, par simple recherche de mots-clés. Par défaut,
    tout ce qui n'est pas reconnu est traité comme un "championnat"
    classique — l'hypothèse la plus courante et la plus sûre.
    """
    texte = type_match.lower()

    if any(mot in texte for mot in ("amical", "friendly", "friendlies")):
        return "amical"
    if any(mot in texte for mot in ("finale", "final")):
        return "finale"
    if any(mot in texte for mot in ("coupe", "cup", "copa", "pokal", "trophy", "trophee")):
        return "coupe"
    if any(
        mot in texte
        for mot in ("sélection", "selection", "national team", "u17", "u18", "u19", "u20", "u21", "u23", "olympique", "olympic")
    ):
        return "selection_nationale"
    return "championnat"


def _fusionner_sous_bloc(stats_sources: List[Dict[str, Any]], cle: str) -> Optional[Dict[str, Any]]:
    """Fusionne le sous-bloc 'domicile' ou 'exterieur' de plusieurs sources, même logique que _fusionner_stats."""
    blocs = [s[cle] for s in stats_sources if s.get(cle)]
    if not blocs:
        return None
    return {
        "matchs_analyses": sum(b["matchs_analyses"] for b in blocs),
        "buts_marques": sum(b["buts_marques"] for b in blocs),
        "buts_encaisses": sum(b["buts_encaisses"] for b in blocs),
    }


def _fusionner_stats(stats_sources: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Combine les stats de plusieurs sources pour une même équipe.
    Additionner les compteurs bruts (buts, matchs) reste mathématiquement
    correct même si les sources se recoupent sur les mêmes matchs réels :
    ça ne biaise pas le TAUX (buts/match), seulement l'échantillon apparent.
    """
    if not stats_sources:
        return None

    matchs = sum(s.get("matchs_analyses", 0) for s in stats_sources)
    buts_marques = sum(s.get("buts_marques", 0) for s in stats_sources)
    buts_encaisses = sum(s.get("buts_encaisses", 0) for s in stats_sources)

    if matchs == 0:
        return None

    indisponibles_connus = [
        s["indisponibles"] for s in stats_sources if s.get("indisponibles") is not None
    ]
    indisponibles = max(indisponibles_connus) if indisponibles_connus else None

    # Fusionne le détail match par match de toutes les sources, trié pour
    # garder un ordre chronologique cohérent même si plusieurs sources
    # sont combinées (sans vraie date, on garde l'ordre d'apparition par
    # source — approximation raisonnable, les sources listent déjà du
    # plus récent au plus ancien).
    matchs_detail: List[Dict[str, Any]] = []
    for s in stats_sources:
        matchs_detail.extend(s.get("matchs_detail", []))

    return {
        "matchs_analyses": matchs,
        "buts_marques": buts_marques,
        "buts_encaisses": buts_encaisses,
        "indisponibles": indisponibles,
        "sources": [s.get("source", "source inconnue") for s in stats_sources],
        "slugs": [s["slug"] for s in stats_sources if s.get("slug")],
        "formes_par_source": [
            (s.get("source", "?"), s.get("forme", [])) for s in stats_sources if s.get("forme")
        ],
        "domicile": _fusionner_sous_bloc(stats_sources, "domicile"),
        "exterieur": _fusionner_sous_bloc(stats_sources, "exterieur"),
        "matchs_detail": matchs_detail,
    }


def _stats_ponderees_depuis_detail(matchs_detail: List[Dict[str, Any]]) -> Tuple[float, float, float]:
    """
    Calcule les buts marqués/encaissés en pondérant chaque match par son
    ancienneté : le plus récent pèse plein pot (poids 1.0), chaque match
    plus ancien un peu moins (x0.85 à chaque cran) — un match d'il y a 5
    rencontres ne doit pas peser autant que celui d'hier, la forme d'une
    équipe pouvant changer vite (nouvel entraîneur, retour de blessés...).

    Renvoie aussi le poids total, qui sert de "taille d'échantillon
    effective" — à la fois pour le lissage ci-dessous et pour doser la
    confiance à accorder à la forme récente face à l'ELO (voir plus bas).
    """
    poids_total = 0.0
    marques_pond = 0.0
    encaisses_pond = 0.0
    for i, m in enumerate(matchs_detail):
        poids = POIDS_DECROISSANCE_ANCIENNETE ** i
        marques_pond += m["buts_pour"] * poids
        encaisses_pond += m["buts_contre"] * poids
        poids_total += poids
    return marques_pond, encaisses_pond, poids_total


def _jours_moyens_entre_matchs_locale(matchs_detail: List[Dict[str, Any]]) -> Optional[float]:
    """
    Version locale à analysis.py du calcul de congestion du calendrier
    (voir besoccer._jours_moyens_entre_matchs pour la logique détaillée
    et les mêmes réserves sur la fiabilité du parsing de date) — dupliquée
    ici plutôt qu'importée pour que analysis.py reste indépendant d'une
    source de données précise (il ne connaît que des dicts génériques).
    """
    jours = [m["jour_annee_approx"] for m in matchs_detail if m.get("jour_annee_approx") is not None]
    if len(jours) < 2:
        return None
    ecarts = []
    for plus_recent, plus_ancien in zip(jours, jours[1:]):
        ecart = plus_recent - plus_ancien
        if ecart < 0:
            ecart += 365
        if ecart > 0:
            ecarts.append(ecart)
    if not ecarts:
        return None
    return sum(ecarts) / len(ecarts)


def _force_attaque_defense(
    stats: Optional[Dict[str, Any]], poids_lissage: float = POIDS_LISSAGE
) -> Tuple[float, float, float]:
    """
    Renvoie (force d'attaque, faiblesse défensive, confiance) — les deux
    premiers normalisés à 1.0 = dans la moyenne, le troisième entre 0 et
    1 reflétant la fiabilité de l'échantillon (peu de matchs récents
    pondérés = confiance faible, utilisé ensuite pour doser l'ELO).

    ⚠️ Lissage statistique appliqué (shrinkage) : sur un petit échantillon
    pondéré, un taux brut peut être extrême par pur hasard — ex: une
    équipe qui n'a encaissé qu'1 but en 4 matchs (contre des adversaires
    faibles, ou juste par chance) ressort comme "défensivement excellente"
    alors que ce n'est pas forcément représentatif. On mélange le taux
    observé avec la moyenne générale, pondéré par la taille réelle de
    l'échantillon — plus il y a de matchs, plus on fait confiance au taux
    observé ; sur peu de matchs, on reste proche de la moyenne par
    prudence. Correction directement motivée par un cas réel remonté
    (Arsenal donné perdant face à Coventry City à cause d'un échantillon
    trop petit et non représentatif).

    poids_lissage est paramétrable (défaut = POIDS_LISSAGE) uniquement
    pour permettre au backtest de tester différentes valeurs sans jamais
    affecter le comportement réel de l'appli pour les vrais utilisateurs.
    """
    if not stats or stats["matchs_analyses"] == 0:
        return 1.0, 1.0, 0.0

    matchs_detail = stats.get("matchs_detail")
    if matchs_detail:
        marques_pond, encaisses_pond, poids_total = _stats_ponderees_depuis_detail(matchs_detail)
    else:
        # Repli si jamais une source ne fournit pas le détail par match
        # (ne devrait plus arriver avec BeSoccer, gardé par sécurité).
        poids_total = float(stats["matchs_analyses"])
        marques_pond = float(stats["buts_marques"])
        encaisses_pond = float(stats["buts_encaisses"])

    taux_marques = (marques_pond + poids_lissage * MOYENNE_BUTS_LIGUE) / (poids_total + poids_lissage)
    taux_encaisses = (encaisses_pond + poids_lissage * MOYENNE_BUTS_LIGUE) / (poids_total + poids_lissage)

    force_attaque = taux_marques / MOYENNE_BUTS_LIGUE
    faiblesse_defense = taux_encaisses / MOYENNE_BUTS_LIGUE

    indisponibles = stats.get("indisponibles")
    if indisponibles:
        penalite = max(PENALITE_MIN_INDISPONIBLES, 1 - 0.03 * indisponibles)
        force_attaque *= penalite
        faiblesse_defense /= penalite

    # Pénalité de fatigue si le calendrier récent est chargé (peu de
    # jours entre les matchs). Recalculée ici depuis matchs_detail
    # (déjà filtré domicile/extérieur à ce stade) plutôt que lue depuis
    # un champ pré-calculé — sinon le nombre porterait sur l'ensemble des
    # matchs, pas sur le sous-ensemble pertinent pour CE match précis.
    # Dégressif linéairement entre les deux seuils. Reste silencieusement
    # inactif si la date n'a pas pu être extraite (voir
    # _parser_jour_approximatif côté besoccer.py — signal encore à
    # confirmer sur de vraies données).
    jours_repos = _jours_moyens_entre_matchs_locale(matchs_detail) if matchs_detail else None
    if jours_repos is not None and jours_repos < SEUIL_JOURS_REPOS_CONFORTABLE:
        avancement = (SEUIL_JOURS_REPOS_CONFORTABLE - jours_repos) / (
            SEUIL_JOURS_REPOS_CONFORTABLE - SEUIL_JOURS_REPOS_CRITIQUE
        )
        avancement = min(1.0, max(0.0, avancement))
        penalite_fatigue = 1 - avancement * (1 - PENALITE_MIN_FATIGUE)
        force_attaque *= penalite_fatigue
        faiblesse_defense /= penalite_fatigue

    confiance = min(1.0, poids_total / 3.0)  # ~3 matchs pondérés pleins = confiance max

    return force_attaque, faiblesse_defense, confiance


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _tau_dixon_coles(x: int, y: int, lambda_dom: float, lambda_ext: float, rho: float) -> float:
    """
    Correction de corrélation sur les scores bas (0-0, 1-0, 0-1, 1-1) —
    le modèle de Poisson pur suppose les deux scores totalement
    indépendants, ce qui n'est pas exactement vrai en football (Dixon &
    Coles, 1997) : dans les matchs à faible score, une dynamique
    légèrement différente s'observe. Correction standard et bien
    documentée, appliquée UNIQUEMENT à ces 4 cases précises — le reste
    de la grille n'est pas affecté.
    """
    if x == 0 and y == 0:
        return 1 - (lambda_dom * lambda_ext * rho)
    elif x == 0 and y == 1:
        return 1 + (lambda_dom * rho)
    elif x == 1 and y == 0:
        return 1 + (lambda_ext * rho)
    elif x == 1 and y == 1:
        return 1 - rho
    return 1.0


def _grille_scores(
    lambda_dom: float, lambda_ext: float, rho: float = RHO_DIXON_COLES
) -> Dict[Tuple[int, int], float]:
    """
    Probabilité de chaque score exact possible (0-0, 1-0, ..., 6-6),
    normalisée à 1.0 au total. Inclut la correction de Dixon-Coles sur
    les scores bas (voir _tau_dixon_coles).

    rho est paramétrable (défaut = RHO_DIXON_COLES) uniquement pour
    permettre au backtest de tester différentes valeurs sans jamais
    affecter le comportement réel de l'appli pour les vrais utilisateurs.
    """
    grille: Dict[Tuple[int, int], float] = {}
    for i in range(MAX_BUTS_SIMULES + 1):
        for j in range(MAX_BUTS_SIMULES + 1):
            p = _poisson(i, lambda_dom) * _poisson(j, lambda_ext)
            p *= _tau_dixon_coles(i, j, lambda_dom, lambda_ext, rho)
            grille[(i, j)] = max(0.0, p)  # sécurité : jamais négatif après correction

    total = sum(grille.values())
    if total == 0:
        return grille
    return {score: p / total for score, p in grille.items()}


def _probabilites_1x2(grille: Dict[Tuple[int, int], float]) -> Tuple[float, float, float]:
    p_dom = sum(p for (i, j), p in grille.items() if i > j)
    p_nul = sum(p for (i, j), p in grille.items() if i == j)
    p_ext = sum(p for (i, j), p in grille.items() if i < j)
    return p_dom, p_nul, p_ext


def _marches_supplementaires(grille: Dict[Tuple[int, int], float]) -> Dict[str, Any]:
    """
    Dérive les marchés de paris courants (over/under, BTTS, scores les
    plus probables) de la MÊME grille de scores déjà calculée pour le
    1X2 — aucun calcul supplémentaire lourd, juste une autre lecture des
    mêmes probabilités.
    """
    btts = sum(p for (i, j), p in grille.items() if i >= 1 and j >= 1)
    over_05 = sum(p for (i, j), p in grille.items() if i + j > 0.5)
    over_15 = sum(p for (i, j), p in grille.items() if i + j > 1.5)
    over_25 = sum(p for (i, j), p in grille.items() if i + j > 2.5)

    top_scores = sorted(grille.items(), key=lambda item: item[1], reverse=True)[:3]
    scores_probables = [
        {"score": f"{i}-{j}", "probabilite": round(p, 3)} for (i, j), p in top_scores
    ]

    return {
        "probabiliteBTTS": round(btts, 3),
        "probabiliteOver05": round(over_05, 3),
        "probabiliteOver15": round(over_15, 3),
        "probabiliteOver25": round(over_25, 3),
        "scoresProbables": scores_probables,
    }


def _facteur_contexte_domicile_exterieur(
    nom: str, stats_globales: Optional[Dict[str, Any]], stats_utilisees: Optional[Dict[str, Any]], label: str
) -> Optional[str]:
    """
    Si les stats effectivement utilisées pour le calcul (domicile ou
    extérieur) sont différentes des stats globales (mélangées), génère
    une ligne de transparence avec les VRAIS chiffres utilisés — jamais
    juste une affirmation sans les chiffres derrière.
    """
    if not stats_globales or not stats_utilisees:
        return None
    if stats_utilisees is stats_globales:
        return None  # pas de sous-bloc disponible, repli déjà sur le global — rien à signaler

    return (
        f"{nom} — {stats_utilisees['buts_marques']} buts marqués / {stats_utilisees['buts_encaisses']} "
        f"encaissés sur ses {stats_utilisees['matchs_analyses']} derniers matchs {label} uniquement "
        f"(plus précis que ses stats générales)"
    )


def _confrontation_recente(equipe1: str, stats1: Optional[Dict[str, Any]], equipe2: str, stats2: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Cherche si les deux équipes se sont affrontées parmi les quelques
    derniers matchs déjà récupérés pour l'une OU l'autre (pas une
    recherche dédiée — juste un coup d'œil dans des données qu'on a de
    toute façon déjà sous la main).

    ⚠️ Ce n'est PAS un historique complet des confrontations directes —
    seulement une fenêtre de quelques matchs récents par équipe. Si les
    deux équipes ne se sont pas rencontrées dans cette fenêtre, ça ne
    veut pas dire qu'elles n'ont jamais joué l'une contre l'autre,
    seulement qu'on n'a pas l'info sous la main sans requête dédiée.

    Renvoie une phrase informative si un match commun est trouvé, sinon
    None (n'affecte jamais le calcul des probabilités — un seul match ne
    doit pas biaiser le modèle, ceci est purement informatif pour
    l'utilisateur).
    """
    if not stats1 or not stats2:
        return None

    slugs2 = set(stats2.get("slugs", []))
    for m in stats1.get("matchs_detail", []):
        if m.get("adversaire_slug") and m["adversaire_slug"] in slugs2:
            if m.get("domicile") is True:
                return f"Confrontation directe récente : {equipe1} {m['buts_pour']}-{m['buts_contre']} {equipe2}"
            elif m.get("domicile") is False:
                return f"Confrontation directe récente : {equipe2} {m['buts_contre']}-{m['buts_pour']} {equipe1}"
            return f"Confrontation directe récente trouvée : score {m['buts_pour']}-{m['buts_contre']} (sens non déterminé)"

    return None


def _facteurs(nom: str, stats: Optional[Dict[str, Any]]) -> List[str]:
    if not stats:
        return []

    facteurs: List[str] = []
    for source, forme in stats["formes_par_source"]:
        facteurs.append(f"{nom} — forme sur {len(forme)} matchs : {''.join(forme)} ({source})")

    facteurs.append(
        f"{nom} — {stats['buts_marques']} buts marqués / {stats['buts_encaisses']} encaissés "
        f"sur {stats['matchs_analyses']} matchs récents cumulés ({', '.join(stats['sources'])})"
    )

    if stats.get("indisponibles") is not None:
        if stats["indisponibles"] > 0:
            facteurs.append(f"{nom} — {stats['indisponibles']} joueur(s) blessé(s)/suspendu(s) actuellement")
        else:
            facteurs.append(f"{nom} — aucun blessé/suspendu recensé actuellement")

    return facteurs


def _facteur_elo(
    elo_equipe1: Optional[float], elo_equipe2: Optional[float], diviseur_echelle: float = DIVISEUR_ECHELLE_ELO
) -> Tuple[float, float]:
    """
    Convertit un écart d'ELO en multiplicateurs à appliquer aux buts
    attendus de chaque équipe, via la formule logistique standard des
    systèmes ELO (la même famille de formule que les échecs) —
    RECALIBRÉE pour l'échelle réelle observée sur BeSoccer (des valeurs
    du type 73-96, donc une échelle bien plus resserrée que le 1000-2500
    des échecs). Avec le diviseur d'origine (400, calibré échecs), un
    écart de 13 points (ex: Arsenal 96 vs Coventry City 83, pourtant un
    vrai gouffre de niveau) ne produisait quasiment aucun effet — bug
    identifié et corrigé après un vrai cas remonté (Arsenal donné quasi
    à égalité face à un club de division inférieure).

    diviseur_echelle est paramétrable (défaut = DIVISEUR_ECHELLE_ELO)
    uniquement pour permettre au backtest de tester différentes valeurs
    sans jamais affecter le comportement réel de l'appli.

    Sans ELO disponible pour les deux équipes : (1.0, 1.0), neutre —
    n'affecte pas le calcul basé sur les buts récents.
    """
    if elo_equipe1 is None or elo_equipe2 is None:
        return 1.0, 1.0

    force1 = 10 ** (elo_equipe1 / diviseur_echelle)
    force2 = 10 ** (elo_equipe2 / diviseur_echelle)
    part1 = force1 / (force1 + force2)  # entre 0 et 1

    # part1 = 0.5 (équipes égales) -> multiplicateur neutre (1.0, 1.0)
    # part1 > 0.5 -> boost équipe1, réduction équipe2, et inversement
    return 2 * part1, 2 * (1 - part1)


def _choisir_stats_contexte(stats_globales: Optional[Dict[str, Any]], domicile: bool) -> Optional[Dict[str, Any]]:
    """
    Filtre le détail match par match pour ne garder QUE les matchs à
    domicile (ou à l'extérieur) de l'équipe pour CE match précis, si
    suffisamment de données existent — sinon retombe sur l'ensemble des
    matchs récents (mieux vaut une donnée générale que rien).

    Une équipe peut être solide à domicile et fébrile en déplacement (ou
    l'inverse) — mélanger les deux masque cet écart, pourtant fréquent
    en football. Les indisponibilités, elles, ne sont PAS filtrées (une
    blessure compte pareil, à domicile ou à l'extérieur).
    """
    if not stats_globales:
        return None

    matchs_detail = stats_globales.get("matchs_detail") or []
    filtres = [m for m in matchs_detail if m.get("domicile") == domicile]

    if not filtres:
        return stats_globales  # repli : pas assez de données contextuelles

    return {
        **stats_globales,
        "matchs_detail": filtres,
        "matchs_analyses": len(filtres),
        "buts_marques": sum(m["buts_pour"] for m in filtres),
        "buts_encaisses": sum(m["buts_contre"] for m in filtres),
    }


def _ponderer_elo_par_confiance(
    facteur_elo: float, confiance_forme: float, coefficient_attenuation: float = COEFFICIENT_ATTENUATION_ELO
) -> float:
    """
    Réduit l'influence de l'ELO quand on dispose déjà d'une forme récente
    fiable (beaucoup de matchs pondérés disponibles), et la laisse
    pleinement agir quand la forme récente est peu fiable (échantillon
    faible) — l'ELO résume toute la saison de chaque équipe, donc plus
    utile précisément quand les derniers matchs ne suffisent pas à eux
    seuls à juger correctement le niveau actuel.

    coefficient_attenuation est paramétrable (défaut =
    COEFFICIENT_ATTENUATION_ELO) uniquement pour permettre au backtest
    de tester différentes valeurs sans jamais affecter le comportement
    réel de l'appli.
    """
    attenuation = 1 - (confiance_forme * coefficient_attenuation)
    return 1.0 + (facteur_elo - 1.0) * attenuation


def _avis_fiabilite_v1_v2(probabilite: float) -> str:
    """
    Avis basé sur les seuils de calibration mesurés empiriquement sur un
    backtest de 126 matchs réels pour les victoires (domicile ou
    extérieur) : au-delà de 70%, le favori s'est toujours confirmé sur
    cet échantillon ; entre 50 et 70%, environ 4 fois sur 5. En dessous,
    le signal existe mais reste trop incertain pour être qualifié de
    fiable.

    ⚠️ Basé sur un échantillon encore modeste — à considérer comme une
    indication, pas une garantie, et amené à se préciser avec le temps
    à mesure que le jeu de backtest grossit.
    """
    if probabilite >= 0.70:
        return "Fiabilité élevée sur historique récent"
    if probabilite >= 0.50:
        return "Fiabilité correcte, reste un pari avec de l'incertitude"
    if probabilite >= 0.35:
        return "Signal présent mais incertain"
    return "Peu probable sur la base de l'analyse"


def _avis_fiabilite_nul(probabilite: float) -> str:
    """
    Avis spécifique au nul — sur notre backtest, le nul n'a JAMAIS atteint
    un niveau de fiabilité comparable à V1/V2, même dans ses meilleures
    tranches de probabilité (jamais au-delà d'environ 42% de réussite
    réelle, y compris quand le modèle l'annonçait comme deuxième issue la
    plus probable). Le signal existe et progresse avec la probabilité
    affichée, mais reste structurellement le pari le plus incertain des
    trois — c'est une caractéristique connue de ce type de modèle
    statistique, pas une limite propre à notre calibration.
    """
    if probabilite >= 0.30:
        return "Signal notable, mais le nul reste statistiquement le pari le plus incertain des trois"
    if probabilite >= 0.20:
        return "Possibilité à surveiller, sans plus"
    return "Peu probable sur la base de l'analyse"


def generer_pronostic(
    equipe1: str,
    equipe2: str,
    type_match: str,
    stats1_sources: List[Dict[str, Any]],
    stats2_sources: List[Dict[str, Any]],
    elo_confrontation: Optional[Dict[str, Any]] = None,
    rho_dixon_coles: float = RHO_DIXON_COLES,
    poids_lissage: float = POIDS_LISSAGE,
    diviseur_echelle_elo: float = DIVISEUR_ECHELLE_ELO,
    coefficient_attenuation_elo: float = COEFFICIENT_ATTENUATION_ELO,
) -> Dict[str, Any]:
    """
    rho_dixon_coles, poids_lissage, diviseur_echelle_elo et
    coefficient_attenuation_elo sont paramétrables uniquement pour
    permettre au backtest de tester différents réglages sans jamais
    affecter /match/analyse pour les vrais utilisateurs (qui appelle
    toujours cette fonction avec les valeurs par défaut).
    """
    stats1 = _fusionner_stats(stats1_sources)
    stats2 = _fusionner_stats(stats2_sources)

    categorie = _categoriser_match(type_match)
    parametres_categorie = CATEGORIES_MATCH[categorie]

    # equipe1 reçoit dans CE match -> ses stats "à domicile" si connues ;
    # equipe2 se déplace -> ses stats "à l'extérieur" si connues.
    stats1_contexte = _choisir_stats_contexte(stats1, domicile=True)
    stats2_contexte = _choisir_stats_contexte(stats2, domicile=False)

    force_att1, faib_def1, confiance1 = _force_attaque_defense(stats1_contexte, poids_lissage)
    force_att2, faib_def2, confiance2 = _force_attaque_defense(stats2_contexte, poids_lissage)

    # Confiance plafonnée selon la catégorie détectée (ex: un amical ne
    # peut jamais atteindre une confiance de 1.0, même avec 5 matchs
    # récents pondérés — les compositions y sont trop souvent rotées
    # pour que la forme récente soit pleinement représentative).
    confiance1 = min(confiance1, parametres_categorie["confiance_max"])
    confiance2 = min(confiance2, parametres_categorie["confiance_max"])

    lambda1 = force_att1 * faib_def2 * MOYENNE_BUTS_LIGUE * parametres_categorie["avantage_domicile"]
    lambda2 = force_att2 * faib_def1 * MOYENNE_BUTS_LIGUE

    elo1 = elo_confrontation.get("elo_equipe1") if elo_confrontation else None
    elo2 = elo_confrontation.get("elo_equipe2") if elo_confrontation else None
    facteur_elo1, facteur_elo2 = _facteur_elo(elo1, elo2, diviseur_echelle_elo)

    # L'ELO pèse plus lourd quand la forme récente est peu fiable (peu de
    # matchs pondérés disponibles), et moins quand elle est déjà solide.
    confiance_moyenne = (confiance1 + confiance2) / 2
    facteur_elo1 = _ponderer_elo_par_confiance(facteur_elo1, confiance_moyenne, coefficient_attenuation_elo)
    facteur_elo2 = _ponderer_elo_par_confiance(facteur_elo2, confiance_moyenne, coefficient_attenuation_elo)

    lambda1 *= facteur_elo1
    lambda2 *= facteur_elo2

    grille = _grille_scores(lambda1, lambda2, rho_dixon_coles)
    p1, p_nul, p2 = _probabilites_1x2(grille)
    marches = _marches_supplementaires(grille)

    facteurs = _facteurs(equipe1, stats1) + _facteurs(equipe2, stats2)
    confrontation = _confrontation_recente(equipe1, stats1, equipe2, stats2)
    if confrontation:
        facteurs.append(confrontation)

    facteur_dom = _facteur_contexte_domicile_exterieur(equipe1, stats1, stats1_contexte, "à domicile")
    if facteur_dom:
        facteurs.append(facteur_dom)
    facteur_ext = _facteur_contexte_domicile_exterieur(equipe2, stats2, stats2_contexte, "à l'extérieur")
    if facteur_ext:
        facteurs.append(facteur_ext)

    if categorie != "championnat":
        libelles = {
            "coupe": "match de coupe",
            "finale": "finale (avantage du terrain neutralisé)",
            "amical": "match amical (forme récente moins déterminante)",
            "selection_nationale": "sélection nationale",
        }
        facteurs.append(f"Type de rencontre détecté : {libelles[categorie]}")

    if elo_confrontation:
        facteurs.append(
            f"Confrontation directe identifiée — ELO {equipe1} : {elo1:.0f}, "
            f"{equipe2} : {elo2:.0f} (BeSoccer, intègre déjà la qualité des "
            f"adversaires affrontés par chaque équipe)"
        )

    if p1 > p2 and p1 > p_nul:
        favori: Optional[str] = equipe1
    elif p2 > p1 and p2 > p_nul:
        favori = equipe2
    else:
        favori = None

    if favori:
        resume = (
            f"{favori} part favori pour ce {type_match} (buts attendus : "
            f"{lambda1:.1f} - {lambda2:.1f}). Les facteurs clés ci-dessous méritent "
            f"d'être suivis avant le coup d'envoi."
        )
    else:
        resume = (
            f"Match équilibré entre {equipe1} et {equipe2} pour ce {type_match} "
            f"(buts attendus : {lambda1:.1f} - {lambda2:.1f}), sans favori net."
        )

    return {
        "equipe1": equipe1,
        "equipe2": equipe2,
        "probabiliteVictoireEquipe1": round(p1, 3),
        "probabiliteMatchNul": round(p_nul, 3),
        "probabiliteVictoireEquipe2": round(p2, 3),
        "avisFiabiliteV1": _avis_fiabilite_v1_v2(p1),
        "avisFiabiliteNul": _avis_fiabilite_nul(p_nul),
        "avisFiabiliteV2": _avis_fiabilite_v1_v2(p2),
        "facteursCles": facteurs,
        "resumeAnalyse": resume,
        "butsAttendusEquipe1": round(lambda1, 2),
        "butsAttendusEquipe2": round(lambda2, 2),
        **marches,
    }
