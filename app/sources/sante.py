"""
Disjoncteur par source de données : après SEUIL_ECHECS échecs consécutifs
(clé invalide, quota, panne, délai), une source est mise en pause pendant
PAUSE_SECONDES — on n'insiste pas et on ne gaspille ni quota ni temps de
réponse. Une source qui répond de nouveau (même liste vide) est rétablie.

Convention des sources : `None` = échec ; liste (même vide) = succès.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Optional

SEUIL_ECHECS = 3
PAUSE_SECONDES = 30 * 60
DELAI_MAX_APPEL = 25.0

_etat: Dict[str, Dict[str, Any]] = {}


def _e(nom: str) -> Dict[str, Any]:
    return _etat.setdefault(nom, {"echecs_consecutifs": 0, "pause_jusqua": 0.0, "dernier_succes": None,
                                  "dernier_echec": None, "dernier_detail": None, "appels": 0, "succes": 0})


def disponible(nom: str) -> bool:
    return time.time() >= _e(nom)["pause_jusqua"]


def enregistrer(nom: str, ok: bool, detail: Optional[str] = None) -> None:
    e = _e(nom)
    e["appels"] += 1
    if ok:
        e["succes"] += 1
        e["echecs_consecutifs"] = 0
        e["pause_jusqua"] = 0.0
        e["dernier_succes"] = time.time()
    else:
        e["echecs_consecutifs"] += 1
        e["dernier_echec"] = time.time()
        e["dernier_detail"] = detail
        if e["echecs_consecutifs"] >= SEUIL_ECHECS:
            e["pause_jusqua"] = time.time() + PAUSE_SECONDES


async def appeler(nom: str, fabrique: Callable[[], Awaitable[Optional[Any]]]) -> Optional[Any]:
    """Appelle la source si elle n'est pas en pause. Ne lève jamais."""
    if not disponible(nom):
        return None
    try:
        resultat = await asyncio.wait_for(fabrique(), timeout=DELAI_MAX_APPEL)
    except asyncio.TimeoutError:
        enregistrer(nom, False, "délai dépassé")
        return None
    except Exception as exc:
        enregistrer(nom, False, f"{type(exc).__name__}: {exc}")
        return None
    enregistrer(nom, resultat is not None, None if resultat is not None else "réponse vide/en échec")
    return resultat


def rapport() -> Dict[str, Any]:
    maintenant = time.time()
    return {
        nom: {
            "disponible": maintenant >= e["pause_jusqua"],
            "pause_restante_s": max(0, int(e["pause_jusqua"] - maintenant)),
            "echecs_consecutifs": e["echecs_consecutifs"],
            "appels": e["appels"],
            "succes": e["succes"],
            "dernier_detail": e["dernier_detail"],
        }
        for nom, e in _etat.items()
    }


def reinitialiser() -> None:
    _etat.clear()
