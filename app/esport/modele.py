"""
Modèle de pronostic e-sport — volontairement simple, transparent et
HONNÊTE sur ses limites.

Entrées : l'historique récent de chaque équipe (30 derniers matchs, 1 appel
chacun) et, si disponible, les confrontations directes (1 appel).

Méthode :
 1. Taux de CARTES gagnées par équipe, pondéré par la fraîcheur (demi-vie
    45 jours) et "rétréci" vers 50 % (k = 4 cartes fictives) : peu de données
    => probabilité proche de 50 %, jamais de certitude artificielle.
 2. Combinaison des deux équipes par la règle log5 (probabilité qu'A gagne
    une carte contre B).
 3. Ajustement léger par les confrontations directes (≥ 3 séries), plafonné.
 4. Passage de "carte" à "série" selon le format (BO1, BO2, BO3, BO5).

Limites assumées (renvoyées à l'appli) : pas d'ajustement à la force des
adversaires déjà affrontés, pas de joueurs/forme individuelle, pas de cotes ;
précision NON mesurée tant que le bilan (service.bilan) n'a pas assez de
pronostics vérifiés.
"""

import math
from typing import Any, Dict, List, Optional

DEMI_VIE_JOURS = 45.0
CARTES_FICTIVES = 4.0
H2H_MIN_SERIES = 3
H2H_POIDS = 0.35
H2H_PLAFOND = 0.40  # ajustement max en log-cotes (≈ ±10 points autour de 50 %)

LIMITES = [
    "Aucun ajustement à la force des adversaires déjà affrontés.",
    "Pas de données joueurs (forme individuelle, remplaçants) ni de cotes.",
    "Précision non mesurée : voir le bilan des pronostics vérifiés.",
]


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def taux_cartes(historique: List[Dict[str, Any]], maintenant_ts: float) -> Dict[str, Any]:
    """Taux de cartes gagnées pondéré/rétréci + volumes. `historique` : vues d'équipe (parsing.historique_equipe)."""
    pour = contre = 0.0
    for v in historique:
        age_j = max(0.0, (maintenant_ts - v["ts"]) / 86400.0)
        poids = 0.5 ** (age_j / DEMI_VIE_JOURS)
        pour += poids * v["cartes_pour"]
        contre += poids * v["cartes_contre"]
    taux = (pour + CARTES_FICTIVES * 0.5) / (pour + contre + CARTES_FICTIVES)
    return {
        "taux": taux,
        "nb_series": len(historique),
        "dernier_match_ts": max((v["ts"] for v in historique), default=None),
    }


def log5(pa: float, pb: float) -> float:
    """P(A bat B) à partir des taux de victoire de A et de B contre un adversaire moyen."""
    num = pa * (1 - pb)
    den = num + pb * (1 - pa)
    return 0.5 if den == 0 else num / den


def ajustement_h2h(h2h: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Confrontations directes vues depuis l'équipe 1 -> ajustement en log-cotes (0 si trop peu de séries)."""
    series = [v for v in h2h if v["victoire"] is not None]
    n = len(series)
    victoires = sum(1 for v in series if v["victoire"])
    if n < H2H_MIN_SERIES:
        return {"delta": 0.0, "n": n, "victoires1": victoires}
    taux = (victoires + 1.0) / (n + 2.0)  # lissage de Laplace
    delta = max(-H2H_PLAFOND, min(H2H_PLAFOND, H2H_POIDS * _logit(taux)))
    return {"delta": delta, "n": n, "victoires1": victoires}


def proba_serie(p_carte: float, best_of: Optional[int]) -> Dict[str, float]:
    """(équipe 1, nul, équipe 2) pour une série au meilleur de `best_of` cartes."""
    n = best_of if best_of in (1, 2, 3, 5) else 1
    if n == 2:
        return {"p1": p_carte ** 2, "nul": 2 * p_carte * (1 - p_carte), "p2": (1 - p_carte) ** 2}
    gagnantes = n // 2 + 1
    p1 = sum(math.comb(n, k) * p_carte ** k * (1 - p_carte) ** (n - k) for k in range(gagnantes, n + 1))
    return {"p1": p1, "nul": 0.0, "p2": 1 - p1}


def confiance(nb1: int, nb2: int, dernier_ts: Optional[float], maintenant_ts: float) -> str:
    """'faible' | 'moyenne' | 'bonne' — selon le volume ET la fraîcheur des données."""
    if dernier_ts is None:
        return "faible"
    anciennete_j = (maintenant_ts - dernier_ts) / 86400.0
    mini = min(nb1, nb2)
    if mini < 8 or anciennete_j > 90:
        return "faible"
    if mini >= 20 and anciennete_j <= 30:
        return "bonne"
    return "moyenne"


def pronostiquer(
    hist1: List[Dict[str, Any]],
    hist2: List[Dict[str, Any]],
    h2h: Optional[List[Dict[str, Any]]],
    best_of: Optional[int],
    nom1: str,
    nom2: str,
    maintenant_ts: float,
) -> Dict[str, Any]:
    t1 = taux_cartes(hist1, maintenant_ts)
    t2 = taux_cartes(hist2, maintenant_ts)
    p_carte = log5(t1["taux"], t2["taux"])

    adj = ajustement_h2h(h2h or [])
    if adj["delta"]:
        p_carte = _sigmoid(_logit(p_carte) + adj["delta"])

    serie = proba_serie(p_carte, best_of)
    derniers = [d for d in (t1["dernier_match_ts"], t2["dernier_match_ts"]) if d is not None]
    plus_ancien = min(derniers) if derniers else None

    facteurs = [
        f"{nom1} : {round(t1['taux'] * 100)} % de cartes gagnées sur {t1['nb_series']} matchs récents",
        f"{nom2} : {round(t2['taux'] * 100)} % de cartes gagnées sur {t2['nb_series']} matchs récents",
    ]
    if adj["n"] >= H2H_MIN_SERIES:
        facteurs.append(f"Confrontations directes : {adj['victoires1']} victoire(s) de {nom1} sur {adj['n']} séries")
    elif adj["n"]:
        facteurs.append(f"Confrontations directes : seulement {adj['n']} série(s), non prises en compte")
    else:
        facteurs.append("Aucune confrontation directe disponible")
    facteurs.append(f"Format : {'BO' + str(best_of) if best_of else 'non précisé (traité comme BO1)'}")

    return {
        "probabiliteVictoireEquipe1": round(serie["p1"], 4),
        "probabiliteMatchNul": round(serie["nul"], 4),
        "probabiliteVictoireEquipe2": round(serie["p2"], 4),
        "confiance": confiance(t1["nb_series"], t2["nb_series"], plus_ancien, maintenant_ts),
        "facteurs": facteurs,
        "limites": LIMITES,
        "donnees": {"series_equipe1": t1["nb_series"], "series_equipe2": t2["nb_series"], "h2h": adj["n"]},
    }
