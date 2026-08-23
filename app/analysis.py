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
AVANTAGE_DOMICILE = 1.15    # multiplicateur appliqué à l'attaque de l'équipe qui reçoit
MAX_BUTS_SIMULES = 6        # borne de la grille de scores simulés (au-delà, probabilité négligeable)
PENALITE_MIN_INDISPONIBLES = 0.70  # plancher : -30% de force max, même avec beaucoup d'absents


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

    return {
        "matchs_analyses": matchs,
        "buts_marques": buts_marques,
        "buts_encaisses": buts_encaisses,
        "indisponibles": indisponibles,
        "sources": [s.get("source", "source inconnue") for s in stats_sources],
        "formes_par_source": [
            (s.get("source", "?"), s.get("forme", [])) for s in stats_sources if s.get("forme")
        ],
        "domicile": _fusionner_sous_bloc(stats_sources, "domicile"),
        "exterieur": _fusionner_sous_bloc(stats_sources, "exterieur"),
    }


def _force_attaque_defense(stats: Optional[Dict[str, Any]]) -> Tuple[float, float]:
    """
    Renvoie (force d'attaque, faiblesse défensive), normalisées à 1.0 =
    dans la moyenne. Sans donnée : (1.0, 1.0), hypothèse neutre plutôt
    qu'un biais arbitraire dans un sens ou l'autre.

    ⚠️ Lissage statistique appliqué (shrinkage) : sur un petit échantillon
    (3-5 matchs), un taux brut peut être extrême par pur hasard — ex: une
    équipe qui n'a encaissé qu'1 but en 4 matchs (contre des adversaires
    faibles, ou juste par chance) ressort comme "défensivement excellente"
    alors que ce n'est pas forcément représentatif. On mélange le taux
    observé avec la moyenne générale, pondéré par la taille réelle de
    l'échantillon — plus il y a de matchs, plus on fait confiance au taux
    observé ; sur peu de matchs, on reste proche de la moyenne par
    prudence. Correction directement motivée par un cas réel remonté
    (Arsenal donné perdant face à Coventry City à cause d'un échantillon
    trop petit et non représentatif).
    """
    if not stats or stats["matchs_analyses"] == 0:
        return 1.0, 1.0

    matchs = stats["matchs_analyses"]
    POIDS_LISSAGE = 5  # équivaut à "ajouter" 5 matchs fictifs à la moyenne

    taux_marques = (stats["buts_marques"] + POIDS_LISSAGE * MOYENNE_BUTS_LIGUE) / (matchs + POIDS_LISSAGE)
    taux_encaisses = (stats["buts_encaisses"] + POIDS_LISSAGE * MOYENNE_BUTS_LIGUE) / (matchs + POIDS_LISSAGE)

    force_attaque = taux_marques / MOYENNE_BUTS_LIGUE
    faiblesse_defense = taux_encaisses / MOYENNE_BUTS_LIGUE

    indisponibles = stats.get("indisponibles")
    if indisponibles:
        penalite = max(PENALITE_MIN_INDISPONIBLES, 1 - 0.03 * indisponibles)
        force_attaque *= penalite
        faiblesse_defense /= penalite

    return force_attaque, faiblesse_defense


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _grille_scores(lambda_dom: float, lambda_ext: float) -> Dict[Tuple[int, int], float]:
    """Probabilité de chaque score exact possible (0-0, 1-0, ..., 6-6), normalisée à 1.0 au total."""
    grille: Dict[Tuple[int, int], float] = {}
    for i in range(MAX_BUTS_SIMULES + 1):
        for j in range(MAX_BUTS_SIMULES + 1):
            grille[(i, j)] = _poisson(i, lambda_dom) * _poisson(j, lambda_ext)

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


def _facteurs(nom: str, stats: Optional[Dict[str, Any]]) -> List[str]:
    if not stats:
        return [f"Aucune donnée récente trouvée pour {nom} (sources indisponibles)"]

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


def _facteur_elo(elo_equipe1: Optional[float], elo_equipe2: Optional[float]) -> Tuple[float, float]:
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

    Sans ELO disponible pour les deux équipes : (1.0, 1.0), neutre —
    n'affecte pas le calcul basé sur les buts récents.
    """
    if elo_equipe1 is None or elo_equipe2 is None:
        return 1.0, 1.0

    DIVISEUR_ECHELLE_BESOCCER = 30  # ~73/27 pour un écart de 13 points, ~81/19 pour 19 points

    force1 = 10 ** (elo_equipe1 / DIVISEUR_ECHELLE_BESOCCER)
    force2 = 10 ** (elo_equipe2 / DIVISEUR_ECHELLE_BESOCCER)
    part1 = force1 / (force1 + force2)  # entre 0 et 1

    # part1 = 0.5 (équipes égales) -> multiplicateur neutre (1.0, 1.0)
    # part1 > 0.5 -> boost équipe1, réduction équipe2, et inversement
    return 2 * part1, 2 * (1 - part1)


def _choisir_stats_contexte(stats_globales: Optional[Dict[str, Any]], sous_bloc: str) -> Optional[Dict[str, Any]]:
    """
    Utilise les statistiques spécifiquement "à domicile" ou "à
    l'extérieur" pour CE match précis quand elles existent (au moins 1
    match dans ce sous-bloc), sinon retombe sur les statistiques
    globales (mélangées) — mieux vaut une donnée générale que rien.

    Une équipe peut être solide à domicile et fébrile en déplacement (ou
    l'inverse) — mélanger les deux masque cet écart, pourtant fréquent
    en football. Les indisponibilités, elles, ne sont PAS scindées
    (une blessure compte pareil, à domicile ou à l'extérieur).
    """
    if not stats_globales:
        return None
    bloc = stats_globales.get(sous_bloc)
    if bloc and bloc.get("matchs_analyses", 0) > 0:
        return {**bloc, "indisponibles": stats_globales.get("indisponibles")}
    return stats_globales


def generer_pronostic(
    equipe1: str,
    equipe2: str,
    type_match: str,
    stats1_sources: List[Dict[str, Any]],
    stats2_sources: List[Dict[str, Any]],
    elo_confrontation: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    stats1 = _fusionner_stats(stats1_sources)
    stats2 = _fusionner_stats(stats2_sources)

    # equipe1 reçoit dans CE match -> ses stats "à domicile" si connues ;
    # equipe2 se déplace -> ses stats "à l'extérieur" si connues.
    stats1_contexte = _choisir_stats_contexte(stats1, "domicile")
    stats2_contexte = _choisir_stats_contexte(stats2, "exterieur")

    force_att1, faib_def1 = _force_attaque_defense(stats1_contexte)
    force_att2, faib_def2 = _force_attaque_defense(stats2_contexte)

    lambda1 = force_att1 * faib_def2 * MOYENNE_BUTS_LIGUE * AVANTAGE_DOMICILE
    lambda2 = force_att2 * faib_def1 * MOYENNE_BUTS_LIGUE

    elo1 = elo_confrontation.get("elo_equipe1") if elo_confrontation else None
    elo2 = elo_confrontation.get("elo_equipe2") if elo_confrontation else None
    facteur_elo1, facteur_elo2 = _facteur_elo(elo1, elo2)
    lambda1 *= facteur_elo1
    lambda2 *= facteur_elo2

    grille = _grille_scores(lambda1, lambda2)
    p1, p_nul, p2 = _probabilites_1x2(grille)
    marches = _marches_supplementaires(grille)

    facteurs = _facteurs(equipe1, stats1) + _facteurs(equipe2, stats2)

    facteur_dom = _facteur_contexte_domicile_exterieur(equipe1, stats1, stats1_contexte, "à domicile")
    if facteur_dom:
        facteurs.append(facteur_dom)
    facteur_ext = _facteur_contexte_domicile_exterieur(equipe2, stats2, stats2_contexte, "à l'extérieur")
    if facteur_ext:
        facteurs.append(facteur_ext)

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

    if not stats1 and not stats2:
        resume = (
            f"Aucune statistique récente n'a pu être récupérée pour {equipe1} ou {equipe2} "
            f"(vérifie l'orthographe, ou les sources sont temporairement indisponibles). "
            f"Le pronostic ci-dessus repose uniquement sur l'avantage du terrain et doit être "
            f"pris avec beaucoup de prudence."
        )
    elif favori:
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
        "facteursCles": facteurs,
        "resumeAnalyse": resume,
        "butsAttendusEquipe1": round(lambda1, 2),
        "butsAttendusEquipe2": round(lambda2, 2),
        **marches,
    }
