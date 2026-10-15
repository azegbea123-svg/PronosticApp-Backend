"""
Persistance du module esport (Firestore, avec repli mémoire si la base est
désactivée — développement local). Collections dédiées : le football n'est
pas touché.

  esport_equipes/{team_id}      équipes suivies (liste blanche de découverte)
  esport_pronostics/{event_id}  pronostics figés AVANT le match + résultat réel une fois connu
  esport_cache/{sha1(chemin)}   réponses de l'API (survit aux redémarrages du serveur)
  esport_resultats/{jeu}_{date} matchs TERMINÉS d'un jour pour un jeu (base du classement Elo)
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .. import db

COLLECTION_EQUIPES = "esport_equipes"
COLLECTION_PRONOSTICS = "esport_pronostics"

COLLECTION_CACHE = "esport_cache"
COLLECTION_RESULTATS = "esport_resultats"

_mem_equipes: Dict[str, Dict[str, Any]] = {}
_mem_pronostics: Dict[str, Dict[str, Any]] = {}
_mem_cache: Dict[str, Tuple[float, str]] = {}
_mem_resultats: Dict[str, Dict[str, Any]] = {}


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


# ---- cache des réponses de l'API ----

def _id_cache(chemin: str) -> str:
    return hashlib.sha1(chemin.encode("utf-8")).hexdigest()


def cache_lire(chemin: str) -> Optional[Tuple[float, Dict[str, Any]]]:
    """(expiration, données) ou None."""
    col = _col(COLLECTION_CACHE)
    if col is None:
        hit = _mem_cache.get(chemin)
        return (hit[0], json.loads(hit[1])) if hit else None
    try:
        doc = col.document(_id_cache(chemin)).get()
        if not doc.exists:
            return None
        champs = doc.to_dict()
        return float(champs["expire"]), json.loads(champs["json"])
    except Exception:
        return None


def cache_ecrire(chemin: str, expire: float, texte_json: str) -> None:
    col = _col(COLLECTION_CACHE)
    if col is None:
        _mem_cache[chemin] = (expire, texte_json)
        return
    try:
        col.document(_id_cache(chemin)).set({"chemin": chemin, "expire": expire, "json": texte_json})
    except Exception:
        pass


# ---- résultats terminés, par jour et par jeu ----

def sauver_resultats_jour(doc_id: str, contenu: Dict[str, Any]) -> None:
    col = _col(COLLECTION_RESULTATS)
    if col is None:
        _mem_resultats[doc_id] = contenu
        return
    try:
        col.document(doc_id).set({**contenu, "matchs": json.dumps(contenu["matchs"], ensure_ascii=False)})
    except Exception:
        pass


def lister_resultats() -> List[Dict[str, Any]]:
    """Tous les matchs terminés mémorisés (tous jours et jeux confondus), sans doublon d'identifiant."""
    col = _col(COLLECTION_RESULTATS)
    if col is None:
        jours = [j["matchs"] for j in _mem_resultats.values()]
    else:
        try:
            jours = [json.loads(doc.to_dict()["matchs"]) for doc in col.stream()]
        except Exception:
            jours = []
    uniques: Dict[Any, Dict[str, Any]] = {}
    for liste in jours:
        for m in liste:
            uniques[m["id"]] = m
    return list(uniques.values())


def ids_resultats_jours() -> Set[str]:
    """Identifiants des jours déjà mémorisés (« csgo_2026-10-13 ») : sert à savoir quoi rattraper."""
    col = _col(COLLECTION_RESULTATS)
    if col is None:
        return set(_mem_resultats)
    try:
        return {doc.id for doc in col.stream()}
    except Exception:
        return set()


def sauver_pronostics(pronostics: List[Dict[str, Any]]) -> None:
    """Enregistre plusieurs pronostics figés en une fois."""
    col = _col(COLLECTION_PRONOSTICS)
    if col is None:
        for p in pronostics:
            _mem_pronostics[str(p["eventId"])] = p
        return
    client = db.get_client()
    try:
        for debut in range(0, len(pronostics), 400):
            batch = client.batch()
            for p in pronostics[debut:debut + 400]:
                batch.set(col.document(str(p["eventId"])), p)
            batch.commit()
    except Exception:
        pass
