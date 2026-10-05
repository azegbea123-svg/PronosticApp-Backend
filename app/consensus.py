"""Fusion statistique V2 PRO.

Ce module ne prétend pas "prédire" avec certitude. Il fusionne plusieurs
signaux indépendants, mesure la qualité des données et évite d'appeler une
probabilité externe une "confiance" sans tenir compte de l'accord entre
modèles et de la quantité de données disponible.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, Tuple


def _norm3(p: Dict[str, float]) -> Dict[str, float]:
    vals = {k: max(0.0, float(p.get(k, 0.0))) for k in ("V1", "NUL", "V2")}
    s = sum(vals.values())
    if s <= 0:
        return {"V1": 1/3, "NUL": 1/3, "V2": 1/3}
    return {k: vals[k] / s for k in vals}


def odds_to_probabilities(odds: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Convertit les cotes 1/X/2 en probabilités implicites sans marge."""
    if not isinstance(odds, dict):
        return None
    inv = []
    for key in ("1", "X", "2"):
        try:
            cote = float(odds[key])
            if cote <= 1.0:
                return None
            inv.append(1.0 / cote)
        except (KeyError, TypeError, ValueError):
            return None
    total = sum(inv)
    if total <= 0:
        return None
    return {k: inv[i] / total for i, k in enumerate(("V1", "NUL", "V2"))}


def _as_stats_list(stats: Any) -> list[Dict[str, Any]]:
    if isinstance(stats, list):
        return [x for x in stats if isinstance(x, dict)]
    if isinstance(stats, dict):
        return [stats]
    return []


def _source_count(stats: Any) -> int:
    items = _as_stats_list(stats)
    sources = []
    for item in items:
        sources.extend(item.get("sources") or [item.get("source")])
    return len(set(str(x) for x in sources if x))


def _effective_sample(stats: Any) -> float:
    items = _as_stats_list(stats)
    if not items:
        return 0.0
    detail = []
    for item in items:
        detail.extend(item.get("matchs_detail") or [])
    # Le moteur de forme utilise une décroissance temporelle ; la somme
    # géométrique donne donc une meilleure mesure que le simple nombre brut.
    if detail:
        total = 0.0
        for i, _ in enumerate(detail[:20]):
            total += 0.85 ** i
        return total
    try:
        return min(10.0, sum(float(item.get("matchs_analyses") or 0) for item in items))
    except (TypeError, ValueError):
        return 0.0


def data_quality(stats1: Optional[Dict[str, Any]], stats2: Optional[Dict[str, Any]], elo: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Score 0-100 de qualité des entrées réellement disponibles."""
    s1 = _effective_sample(stats1)
    s2 = _effective_sample(stats2)
    src = min(2.0, (_source_count(stats1) + _source_count(stats2)) / 2.0)
    sample_score = min(45.0, ((s1 + s2) / 2.0) / 7.0 * 45.0)
    source_score = src / 2.0 * 25.0
    elo_score = 15.0 if elo and elo.get("elo_equipe1") is not None and elo.get("elo_equipe2") is not None else 0.0
    s1_items = _as_stats_list(stats1)
    s2_items = _as_stats_list(stats2)
    context_score = 15.0 if any(x.get("domicile") for x in s1_items) or any(x.get("exterieur") for x in s2_items) else 0.0
    score = round(min(100.0, sample_score + source_score + elo_score + context_score))
    if score >= 80:
        niveau = "EXCELLENTE"
    elif score >= 65:
        niveau = "BONNE"
    elif score >= 45:
        niveau = "MOYENNE"
    else:
        niveau = "FAIBLE"
    return {"score": score, "niveau": niveau, "echantillon_effectif": round((s1 + s2) / 2, 2), "sources": round(src)}


def fuse(model_probs: Dict[str, float], external_probs: Optional[Dict[str, float]], quality_score: float, external_available: bool) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Fusion prudente.

    Le modèle interne reste majoritaire. Le signal externe augmente avec la
    qualité de ses données mais reste plafonné afin qu'une seule API ne puisse
    jamais écraser le modèle interne.
    """
    m = _norm3(model_probs)
    if not external_available or not external_probs:
        return m, {"poids_modele": 1.0, "poids_externe": 0.0, "accord": None}
    e = _norm3(external_probs)
    # 12% à 28% pour le signal externe : il s'agit d'un contrôle indépendant,
    # pas d'une vérité terrain.
    w_ext = 0.12 + min(0.16, max(0.0, quality_score) / 100.0 * 0.16)
    w_model = 1.0 - w_ext
    final = {k: w_model * m[k] + w_ext * e[k] for k in m}
    final = _norm3(final)
    ecart = max(abs(m[k] - e[k]) for k in m)
    accord = "FORT" if ecart < 0.08 else ("PARTIEL" if ecart < 0.18 else "FAIBLE")
    return final, {"poids_modele": round(w_model, 3), "poids_externe": round(w_ext, 3), "accord": accord, "ecart_max": round(ecart, 3)}


def confidence_score(probs: Dict[str, float], quality_score: float, consensus_meta: Dict[str, Any]) -> Dict[str, Any]:
    p = _norm3(probs)
    ordre = sorted(p.values(), reverse=True)
    top = ordre[0]
    second = ordre[1]
    marge = max(0.0, top - second)
    # Score de confiance distinct de la probabilité : qualité des données,
    # séparation du favori et accord des modèles.
    accord_bonus = {"FORT": 12.0, "PARTIEL": 6.0, "FAIBLE": 0.0}.get(consensus_meta.get("accord"), 0.0)
    score = 45.0 * top + 35.0 * min(1.0, marge / 0.35) + 20.0 * (quality_score / 100.0) + accord_bonus
    score = int(round(min(99.0, max(1.0, score))))
    if score >= 80:
        niveau = "FORTE"
    elif score >= 65:
        niveau = "BONNE"
    elif score >= 50:
        niveau = "MODÉRÉE"
    else:
        niveau = "PRUDENTE"
    return {"score": score, "niveau": niveau, "probabilite_favorite": round(top, 3), "marge_favori": round(marge, 3)}
