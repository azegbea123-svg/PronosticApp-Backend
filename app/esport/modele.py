"""
Briques de probabilité partagées par le moteur de pronostic (voir elo.py).

Limites assumées (renvoyées à l'appli) : le classement ne voit que les matchs mémorisés par le
serveur (l'historique se construit jour après jour), pas les joueurs individuels ni les cotes ;
la précision n'est affirmée qu'à partir du bilan (service.bilan) sur des pronostics figés.
"""

import math
from typing import Dict, List, Optional

LIMITES: List[str] = [
    "Basé sur les résultats mémorisés par le serveur : l'historique grandit chaque jour.",
    "Pas de données joueurs (forme individuelle, remplaçants) ni de cotes.",
    "Précision non garantie : voir le bilan des pronostics déjà vérifiés.",
]


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
