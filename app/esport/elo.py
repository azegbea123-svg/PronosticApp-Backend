"""
Classement Elo des équipes e-sport, calculé à partir des matchs terminés mémorisés (aucun appel API).

Pourquoi Elo plutôt qu'un simple taux de victoires : il tient compte de la FORCE DES ADVERSAIRES (battre
une grande équipe rapporte plus que battre une petite) — ce que l'ancien modèle ignorait.

Principe : chaque série met à jour les deux équipes avec le score fractionnaire S = cartes gagnées /
cartes jouées (2-0 compte plus qu'un 2-1), K = 24, départ 1500. La probabilité de gagner une carte vient
de l'écart de classement ; modele.proba_serie la convertit en probabilité de série (BO1, BO2, BO3, BO5).

Pas de pronostic sous MIN_MATCHS matchs connus pour l'une des équipes : afficher 50 % serait mentir.
"""

from typing import Any, Dict, List, Optional

from . import modele

BASE = 1500.0
K = 24.0
MIN_MATCHS = 3


def calculer(matchs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """matchs : dicts compacts {id, ts, e1, e2, s1, s2, ...} déjà terminés et hors forfait."""
    notes: Dict[int, float] = {}
    nombre: Dict[int, int] = {}
    dernier: Dict[int, int] = {}
    for m in sorted(matchs, key=lambda x: x["ts"]):
        total = m["s1"] + m["s2"]
        if total <= 0:
            continue
        a, b = m["e1"], m["e2"]
        ra, rb = notes.get(a, BASE), notes.get(b, BASE)
        attendu_a = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        delta = K * (m["s1"] / total - attendu_a)
        notes[a], notes[b] = ra + delta, rb - delta
        for e in (a, b):
            nombre[e] = nombre.get(e, 0) + 1
            dernier[e] = m["ts"]
    return {"notes": notes, "nombre": nombre, "dernier": dernier}


def pronostiquer(classement: Dict[str, Any], id1: int, id2: int, best_of: Optional[int],
                 maintenant_ts: float) -> Dict[str, Any]:
    """{"pronostic": {...}} ou {"raison": "..."} si les données ne suffisent pas."""
    nombre = classement["nombre"]
    n1, n2 = nombre.get(id1, 0), nombre.get(id2, 0)
    if n1 < MIN_MATCHS or n2 < MIN_MATCHS:
        return {"raison": f"Historique insuffisant ({n1} et {n2} matchs connus, {MIN_MATCHS} minimum)."}
    r1, r2 = classement["notes"][id1], classement["notes"][id2]
    p_carte = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
    serie = modele.proba_serie(p_carte, best_of)
    plus_ancien = min(classement["dernier"][id1], classement["dernier"][id2])
    probas = {1: serie["p1"], 3: serie["nul"], 2: serie["p2"]}
    return {"pronostic": {
        "p1": round(serie["p1"], 4),
        "pnul": round(serie["nul"], 4),
        "p2": round(serie["p2"], 4),
        "favori": max(probas, key=probas.get),
        "confiance": modele.confiance(n1, n2, plus_ancien, maintenant_ts),
        "details": {"elo1": round(r1), "elo2": round(r2), "matchs1": n1, "matchs2": n2,
                    "jours_depuis_dernier_match": int((maintenant_ts - plus_ancien) // 86400)},
    }}
