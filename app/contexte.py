"""
Contexte d'un match de la liste (/match/contexte) : classement des deux
équipes et confrontations directes, via free-api-live-football-data.

Purement INFORMATIF : ne touche jamais au calcul des probabilités (voir
analysis.py). Se dégrade silencieusement — pas de classement pour une
coupe, pas d'identifiant fournisseur pour un match venu d'une autre
source, quota atteint... — en renvoyant simplement moins d'éléments.

Coût en quota RapidAPI : 1 appel de classement par championnat (cache
6 h) + 1 appel de confrontations par match (cache 24 h), seulement
quand un utilisateur ouvre un match — voir sources/livefootball_rapidapi.py.
"""

import asyncio
from typing import Any, Dict, List, Optional

from .sources import livefootball_rapidapi as rapid
from . import ligues
from . import repo


async def construire(fixture_id: int) -> Dict[str, Any]:
    match = repo.obtenir_match(fixture_id)
    if not match:
        return {"disponible": False, "raison": "match introuvable dans la liste du jour"}

    rapid_id = match.get("rapid_id")
    if not rapid_id:
        return {"disponible": False, "raison": "contexte non fourni pour ce match"}

    ligue = ligues.ligue_parente(match.get("rapid_league_id"))
    classement_brut, confrontations = await asyncio.gather(
        rapid.get_classement(ligue) if ligue else asyncio.sleep(0, result=None),
        rapid.get_confrontations(rapid_id, match.get("equipe1_id")),
        return_exceptions=True,
    )
    if isinstance(classement_brut, BaseException):
        classement_brut = None
    if isinstance(confrontations, BaseException):
        confrontations = None

    classement = None
    if classement_brut:
        par_id = {l["equipe_id"]: l for l in classement_brut}
        l1 = par_id.get(match.get("equipe1_id"))
        l2 = par_id.get(match.get("equipe2_id"))
        if l1 or l2:
            classement = {"equipe1": l1, "equipe2": l2, "total_equipes": len(classement_brut)}

    return {
        "disponible": bool(classement or confrontations),
        "classement": classement,
        "confrontations": confrontations,
    }


def _ordinal(n: int) -> str:
    return "1er" if n == 1 else f"{n}e"


def resumer(contexte: Dict[str, Any], equipe1: str, equipe2: str, ignorer_h2h: bool = False) -> List[str]:
    """Lignes prêtes à être ajoutées à `facteursCles` de /match/analyse."""
    lignes: List[str] = []

    c = contexte.get("classement")
    if c and c.get("equipe1") and c.get("equipe2"):
        a, b = c["equipe1"], c["equipe2"]
        lignes.append(
            f"Classement : {equipe1} {_ordinal(a['rang'])} ({a['points']} pts, diff. {a['diff']:+d}) · "
            f"{equipe2} {_ordinal(b['rang'])} ({b['points']} pts, diff. {b['diff']:+d}) "
            f"sur {c['total_equipes']} équipes"
        )

    h = contexte.get("confrontations")
    if h and h.get("bilan") and h.get("total") and not ignorer_h2h:
        b = h["bilan"]
        lignes.append(
            f"Confrontations directes ({h['total']} dernier(s) match(s)) : "
            f"{b['equipe1']} victoire(s) {equipe1}, {b['nuls']} nul(s), {b['equipe2']} victoire(s) {equipe2}"
        )
    return lignes
