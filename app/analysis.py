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
    }


def _force_attaque_defense(stats: Optional[Dict[str, Any]]) -> Tuple[float, float]:
    """
    Renvoie (force d'attaque, faiblesse défensive), normalisées à 1.0 =
    dans la moyenne. Sans donnée : (1.0, 1.0), hypothèse neutre plutôt
    qu'un biais arbitraire dans un sens ou l'autre.
    """
    if not stats or stats["matchs_analyses"] == 0:
        return 1.0, 1.0

    taux_marques = stats["buts_marques"] / stats["matchs_analyses"]
    taux_encaisses = stats["buts_encaisses"] / stats["matchs_analyses"]

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


def _probabilites_1x2(lambda_dom: float, lambda_ext: float) -> Tuple[float, float, float]:
    p_dom = p_nul = p_ext = 0.0
    for i in range(MAX_BUTS_SIMULES + 1):
        for j in range(MAX_BUTS_SIMULES + 1):
            p = _poisson(i, lambda_dom) * _poisson(j, lambda_ext)
            if i > j:
                p_dom += p
            elif i == j:
                p_nul += p
            else:
                p_ext += p

    total = p_dom + p_nul + p_ext
    if total == 0:
        return 1 / 3, 1 / 3, 1 / 3
    return p_dom / total, p_nul / total, p_ext / total


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


def generer_pronostic(
    equipe1: str,
    equipe2: str,
    type_match: str,
    stats1_sources: List[Dict[str, Any]],
    stats2_sources: List[Dict[str, Any]],
) -> Dict[str, Any]:
    stats1 = _fusionner_stats(stats1_sources)
    stats2 = _fusionner_stats(stats2_sources)

    force_att1, faib_def1 = _force_attaque_defense(stats1)
    force_att2, faib_def2 = _force_attaque_defense(stats2)

    lambda1 = force_att1 * faib_def2 * MOYENNE_BUTS_LIGUE * AVANTAGE_DOMICILE
    lambda2 = force_att2 * faib_def1 * MOYENNE_BUTS_LIGUE

    p1, p_nul, p2 = _probabilites_1x2(lambda1, lambda2)

    facteurs = _facteurs(equipe1, stats1) + _facteurs(equipe2, stats2)

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
    }
