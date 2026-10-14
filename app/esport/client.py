"""
Client de l'API "Esport API" (RapidAPI, hôte esportapi1.p.rapidapi.com) —
données au format Sofascore pour l'e-sport (CS, Dota 2...).

⚠️ CONTRAINTE MAJEURE : le plan gratuit observé dans le playground est de
50 REQUÊTES PAR JOUR (en-têtes x-ratelimit-requests-limit/-remaining/-reset).
Tout ce module est donc conçu autour de ce budget :
  - cache agressif (TTL par type de donnée, y compris pour les réponses
    vides 204 — sinon une équipe sans match prévu coûterait 1 appel à chaque
    ouverture de l'écran) ;
  - le compteur réel est LU dans chaque réponse (jamais deviné) ;
  - deux réserves : les tâches "de fond" (liste des matchs) s'arrêtent à
    RESERVE_ARRIERE_PLAN appels restants pour laisser de quoi faire les
    actions demandées par l'utilisateur (pronostic, détail), qui elles ne
    s'arrêtent qu'à RESERVE_UTILISATEUR.

Convention (comme sources/livefootball_rapidapi.py) : `None` = ÉCHEC (clé
absente, quota, réseau, HTTP != 200/204) ; `{}` = réponse valide mais vide
(HTTP 204) ; dict = contenu. Les appelants ne confondent jamais les deux.

Clé : variable d'environnement RAPIDAPI_KEY (la même que pour football).
"""

import time
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

from ..config import CLE_RAPIDAPI

HOTE = "esportapi1.p.rapidapi.com"
BASE_URL = f"https://{HOTE}"

RESERVE_ARRIERE_PLAN = 15
RESERVE_UTILISATEUR = 2
TTL_VIDE_MAX = 3 * 3600  # une réponse 204 est retenue au plus 3 h

_quota: Dict[str, Any] = {"limite": None, "restant": None, "fenetre_fin": None}
_cache: Dict[str, Tuple[float, Any]] = {}
_CACHE_MAX_ENTREES = 400


def _entier(valeur: Any) -> Optional[int]:
    try:
        return int(valeur)
    except (TypeError, ValueError):
        return None


def _maj_quota(en_tetes: Any) -> None:
    restant = _entier(en_tetes.get("x-ratelimit-requests-remaining"))
    if restant is None:
        return
    _quota["restant"] = restant
    _quota["limite"] = _entier(en_tetes.get("x-ratelimit-requests-limit"))
    reset = _entier(en_tetes.get("x-ratelimit-requests-reset"))
    _quota["fenetre_fin"] = time.time() + reset if reset is not None else None


def quota() -> Dict[str, Any]:
    """Dernier compteur observé. `restant` vaut None si inconnu ou si la fenêtre a expiré."""
    fin = _quota["fenetre_fin"]
    if fin is not None and time.time() > fin:
        _quota["restant"] = None
        _quota["fenetre_fin"] = None
    return {
        "limite": _quota["limite"],
        "restant": _quota["restant"],
        "reinitialisation_dans_s": None if fin is None else max(0, int(fin - time.time())),
    }


def peut_appeler(utilisateur: bool = True, besoin: int = 1) -> bool:
    """Vrai si `besoin` appels tiennent encore au-dessus de la réserve applicable."""
    restant = quota()["restant"]
    if restant is None:
        return True  # compteur inconnu : on tente, la réponse nous le dira
    reserve = RESERVE_UTILISATEUR if utilisateur else RESERVE_ARRIERE_PLAN
    return restant - besoin >= reserve


async def _get(chemin: str, params: Optional[Dict[str, Any]] = None, *, utilisateur: bool = True) -> Optional[Dict[str, Any]]:
    if not CLE_RAPIDAPI:
        return None
    if not peut_appeler(utilisateur):
        return None
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}{chemin}",
                headers={"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": HOTE},
                params=params or {},
                timeout=15.0,
            )
    except Exception:
        return None
    _maj_quota(r.headers)
    if r.status_code == 204:
        return {}
    if r.status_code == 429:
        _quota["restant"] = 0
        return None
    if r.status_code != 200:
        return None
    try:
        corps = r.json()
    except Exception:
        return None
    return corps if isinstance(corps, dict) else None


def en_cache(chemin: str) -> bool:
    hit = _cache.get(chemin)
    return bool(hit and hit[0] > time.time())


def manquants(chemins: Iterable[str]) -> int:
    """Nombre d'appels RÉELS qu'il faudrait pour ces chemins (ceux déjà en cache sont gratuits)."""
    return sum(1 for c in set(chemins) if not en_cache(c))


async def appeler(chemin: str, ttl: int, *, utilisateur: bool = True) -> Optional[Dict[str, Any]]:
    """Cache devant `_get`. Un échec n'est JAMAIS mis en cache ; une réponse vide l'est brièvement."""
    maintenant = time.time()
    hit = _cache.get(chemin)
    if hit and hit[0] > maintenant:
        return hit[1]
    donnees = await _get(chemin, utilisateur=utilisateur)
    if donnees is None:
        return None
    if len(_cache) >= _CACHE_MAX_ENTREES:
        for k in [k for k, (exp, _) in _cache.items() if exp <= maintenant] or list(_cache)[:100]:
            _cache.pop(k, None)
    _cache[chemin] = (maintenant + (min(ttl, TTL_VIDE_MAX) if donnees == {} else ttl), donnees)
    return donnees


def prolonger(chemin: str, ttl: int) -> None:
    """Allonge la durée de vie d'une entrée déjà en cache (ex. match terminé : ne changera plus)."""
    hit = _cache.get(chemin)
    if hit:
        _cache[chemin] = (time.time() + ttl, hit[1])


def vider_cache() -> None:
    _cache.clear()
