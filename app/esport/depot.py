"""
Persistance du module esport (Firestore, avec repli mémoire si la base est
désactivée — développement local). Collections dédiées : le football n'est
pas touché.

  esport_equipes/{team_id}      équipes suivies (liste blanche de découverte)
  esport_pronostics/{event_id}  pronostics figés AVANT le match + résultat réel une fois connu
"""

from typing import Any, Dict, List, Optional

from .. import db

COLLECTION_EQUIPES = "esport_equipes"
COLLECTION_PRONOSTICS = "esport_pronostics"

_mem_equipes: Dict[str, Dict[str, Any]] = {}
_mem_pronostics: Dict[str, Dict[str, Any]] = {}


def _col(nom: str):
    client = db.get_client()
    return client.collection(nom) if client else None


# ---- équipes suivies ----

def lister_equipes() -> List[Dict[str, Any]]:
    col = _col(COLLECTION_EQUIPES)
    if col is None:
        return list(_mem_equipes.values())
    try:
        return [d.to_dict() for d in col.stream()]
    except Exception:
        return []


def sauver_equipe(equipe: Dict[str, Any]) -> None:
    col = _col(COLLECTION_EQUIPES)
    if col is None:
        _mem_equipes[str(equipe["id"])] = equipe
        return
    col.document(str(equipe["id"])).set(equipe)


def supprimer_equipe(team_id: int) -> bool:
    col = _col(COLLECTION_EQUIPES)
    if col is None:
        return _mem_equipes.pop(str(team_id), None) is not None
    ref = col.document(str(team_id))
    existe = ref.get().exists
    if existe:
        ref.delete()
    return existe


# ---- pronostics ----

def obtenir_pronostic(event_id: int) -> Optional[Dict[str, Any]]:
    col = _col(COLLECTION_PRONOSTICS)
    if col is None:
        return _mem_pronostics.get(str(event_id))
    try:
        doc = col.document(str(event_id)).get()
        return doc.to_dict() if doc.exists else None
    except Exception:
        return None


def sauver_pronostic(p: Dict[str, Any]) -> None:
    col = _col(COLLECTION_PRONOSTICS)
    if col is None:
        _mem_pronostics[str(p["eventId"])] = p
        return
    try:
        col.document(str(p["eventId"])).set(p)
    except Exception:
        pass  # le suivi est un bonus : ne jamais empêcher de répondre à l'utilisateur


def maj_pronostic(event_id: int, champs: Dict[str, Any]) -> None:
    col = _col(COLLECTION_PRONOSTICS)
    if col is None:
        if str(event_id) in _mem_pronostics:
            _mem_pronostics[str(event_id)].update(champs)
        return
    try:
        col.document(str(event_id)).set(champs, merge=True)
    except Exception:
        pass


def lister_pronostics(limite: int = 300) -> List[Dict[str, Any]]:
    col = _col(COLLECTION_PRONOSTICS)
    if col is None:
        return list(_mem_pronostics.values())[:limite]
    try:
        return [d.to_dict() for d in col.limit(limite).stream()]
    except Exception:
        return []
