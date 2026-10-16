"""
Classement Elo des équipes e-sport, calculé à partir des matchs terminés mémorisés (aucun appel API).

Pourquoi Elo plutôt qu'un simple taux de victoires : il tient compte de la FORCE DES ADVERSAIRES (battre
une grande équipe rapporte plus que battre une petite) — ce que l'ancien modèle ignorait.

Principe : chaque série met à jour les deux équipes avec le score fractionnaire S = cartes gagnées /
cartes jouées (2-0 compte plus qu'un 2-1), K = 24, départ 1500. La probabilité de gagner une carte vient
de l'écart de classement ; modele.proba_serie la convertit en probabilité de série (BO1, BO2, BO3, BO5).

Un pronostic est TOUJOURS fourni ; avec peu de matchs connus il porte la mention « confiance faible » (modele.confiance
exige 8 matchs pour mieux) et `donnees_suffisantes` vaut faux, ce que le bilan prend en compte.
"""

from typing import Any, Dict, List, Optional

from . import modele

BASE = 1500.0
K = 24.0
MIN_MATCHS = 2


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


def favori(p1: float, pnul: float, p2: float) -> int:
    """1 ou 2 : équipe favorite ; 3 : nul le plus probable ; 0 : match ÉQUILIBRÉ (écart < 3 points)."""
    probas = {1: p1, 3: pnul, 2: p2}
    meilleur = max(probas, key=probas.get)
    if meilleur in (1, 2) and abs(p1 - p2) < 0.03:
        return 0
    return meilleur


def pronostiquer(classement: Dict[str, Any], id1: int, id2: int, best_of: Optional[int],
                 maintenant_ts: float) -> Dict[str, Any]:
    """
    Pronostic TOUJOURS fourni. Une équipe sans historique part du classement neutre (1500) : si l'autre est connue,
    l'écart reste informatif ; si aucune ne l'est, le match sort « équilibré ». `donnees_suffisantes` dit
    honnêtement si chaque équipe a au moins MIN_MATCHS matchs connus (le bilan les distingue des autres).
    """
    nombre = classement["nombre"]
    n1, n2 = nombre.get(id1, 0), nombre.get(id2, 0)
    r1, r2 = classement["notes"].get(id1, BASE), classement["notes"].get(id2, BASE)
    p_carte = 1.0 / (1.0 + 10 ** ((r2 - r1) / 400.0))
    serie = modele.proba_serie(p_carte, best_of)
    connus = [classement["dernier"][i] for i in (id1, id2) if i in classement["dernier"]]
    plus_ancien = min(connus) if connus else None
    p1, pnul, p2 = round(serie["p1"], 4), round(serie["nul"], 4), round(serie["p2"], 4)
    return {"pronostic": {
        "p1": p1, "pnul": pnul, "p2": p2,
        "favori": favori(p1, pnul, p2),
        # Une équipe inconnue force « faible » : modele.confiance exige 8 matchs connus de chacune.
        "confiance": modele.confiance(n1, n2, plus_ancien, maintenant_ts),
        "donnees_suffisantes": n1 >= MIN_MATCHS and n2 >= MIN_MATCHS,
        "details": {"elo1": round(r1), "elo2": round(r2), "matchs1": n1, "matchs2": n2,
                    "jours_depuis_dernier_match": None if plus_ancien is None else int((maintenant_ts - plus_ancien) // 86400)},
    }}
