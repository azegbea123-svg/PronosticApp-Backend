"""
Moteur de pronostic.

Combine les stats récupérées (forme récente, buts) en probabilités de
victoire/nul/défaite, et génère un texte d'analyse basé sur les données
réellement trouvées — pas de texte générique si les stats manquent, on
le dit explicitement à l'utilisateur.

Modèle utilisé : heuristique simple et transparente (pas un modèle de ML),
volontairement explicable, plus adaptée qu'une boîte noire pour un
prototype où l'utilisateur doit comprendre "pourquoi" ce pronostic.
"""

from typing import Optional, Dict, Any, List

AVANTAGE_DOMICILE = 0.06  # léger bonus, cohérent avec les stats générales du foot


def _score_forme(forme: List[str]) -> float:
    """Convertit une série de résultats (V/N/D) en score entre 0 et 1."""
    if not forme:
        return 0.5  # neutre si aucune donnée
    points = {"V": 3, "N": 1, "D": 0}
    total = sum(points[r] for r in forme)
    maximum = 3 * len(forme)
    return total / maximum if maximum else 0.5


def _facteurs(nom: str, stats: Optional[Dict[str, Any]]) -> List[str]:
    if not stats:
        return [f"Aucune donnée récente trouvée pour {nom} (sources indisponibles)"]

    forme = stats.get("forme", [])
    source = stats.get("source", "source inconnue")
    resume_forme = "".join(forme) if forme else "N/A"

    facteurs = [f"{nom} — forme sur les {len(forme)} derniers matchs : {resume_forme} ({source})"]

    if "buts_marques" in stats and "buts_encaisses" in stats:
        facteurs.append(
            f"{nom} — {stats['buts_marques']} buts marqués / "
            f"{stats['buts_encaisses']} encaissés sur cette période"
        )

    return facteurs


def generer_pronostic(
    equipe1: str,
    equipe2: str,
    type_match: str,
    stats1: Optional[Dict[str, Any]],
    stats2: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    score1 = _score_forme(stats1.get("forme", []) if stats1 else [])
    score2 = _score_forme(stats2.get("forme", []) if stats2 else [])

    force1 = score1 + AVANTAGE_DOMICILE
    force2 = score2

    total_force = force1 + force2
    if total_force == 0:
        p1, p2 = 0.4, 0.4
    else:
        p1 = 0.72 * (force1 / total_force)
        p2 = 0.72 * (force2 / total_force)

    p_nul = max(0.16, 1 - p1 - p2)
    total = p1 + p_nul + p2
    p1, p_nul, p2 = p1 / total, p_nul / total, p2 / total

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
            f"(vérifie l'orthographe des noms, ou les sources sont temporairement indisponibles). "
            f"Le pronostic ci-dessus repose uniquement sur l'avantage du terrain et doit être "
            f"pris avec beaucoup de prudence."
        )
    elif favori:
        resume = (
            f"{favori} part favori pour ce {type_match}, sur la base de sa forme récente. "
            f"L'écart reste modéré : les facteurs clés ci-dessous méritent d'être suivis "
            f"avant le coup d'envoi."
        )
    else:
        resume = f"Match équilibré entre {equipe1} et {equipe2} pour ce {type_match}, sans favori net."

    return {
        "equipe1": equipe1,
        "equipe2": equipe2,
        "probabiliteVictoireEquipe1": round(p1, 3),
        "probabiliteMatchNul": round(p_nul, 3),
        "probabiliteVictoireEquipe2": round(p2, 3),
        "facteursCles": facteurs,
        "resumeAnalyse": resume,
    }
