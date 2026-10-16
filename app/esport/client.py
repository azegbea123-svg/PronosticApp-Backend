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

CACHE PERSISTANT : l'hébergeur gratuit met le serveur en veille et le redémarre, ce qui vidait un cache
purement mémoire et re-dépensait le quota à chaque réveil. Les réponses sont donc aussi stockées
dans la base (depot.cache_*) avec leur date d'expiration : un redémarrage ne coûte plus aucune requête.

Convention (comme sources/livefootball_rapidapi.py) : `None` = ÉCHEC (clé
absente, quota, réseau, HTTP != 200/204) ; `{}` = réponse valide mais vide
(HTTP 204) ; dict = contenu. Les appelants ne confondent jamais les deux.

Clé : variable d'environnement RAPIDAPI_KEY (la même que pour football).
"""

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

import httpx

from ..config import CLE_RAPIDAPI

HOTE = "esportapi1.p.rapidapi.com"
BASE_URL = f"https://{HOTE}"

RESERVE_ARRIERE_PLAN = 15
RESERVE_UTILISATEUR = 2
TTL_VIDE_MAX = 3 * 3600  # une réponse 204 est retenue au plus 3 h

_quota: Dict[str, Any] = {"limite": None, "restant": None, "fenetre_fin": None}
# Crochet appelé avec CHAQUE réponse de l'API (neuve, ou relue du cache persistant) : le service y branche la
# mémorisation des matchs terminés qui nourrit les pronostics. Une erreur du crochet ne casse jamais un appel.
RECOLTEUR: Optional[Callable[[Any], Awaitable[Any]]] = None
_pause_jusqua = 0.0          # refroidissement après un 429 "trop de requêtes d'un coup" (pas un quota épuisé)
PAUSE_429_S = 60
_derniere_erreur: Dict[str, Any] = {}
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
        "pause_restante_s": max(0, int(_pause_jusqua - time.time())),
        "derniere_erreur": dict(_derniere_erreur) or None,
    }


def peut_appeler(utilisateur: bool = True, besoin: int = 1) -> bool:
    """Vrai si `besoin` appels tiennent encore au-dessus de la réserve applicable."""
    if time.time() < _pause_jusqua:
        return False
    restant = quota()["restant"]
    if restant is None:
        return True  # compteur inconnu : on tente, la réponse nous le dira
    reserve = RESERVE_UTILISATEUR if utilisateur else RESERVE_ARRIERE_PLAN
    return restant - besoin >= reserve


def _noter_erreur(chemin: str, detail: str) -> None:
    _derniere_erreur.clear()
    _derniere_erreur.update({"chemin": chemin, "detail": detail, "a": int(time.time())})


async def _get(chemin: str, params: Optional[Dict[str, Any]] = None, *, utilisateur: bool = True) -> Optional[Dict[str, Any]]:
    global _pause_jusqua
    if not CLE_RAPIDAPI:
        _noter_erreur(chemin, "RAPIDAPI_KEY non configurée")
        return None
    if not peut_appeler(utilisateur):
        return None  # ignoré faute de quota/pause : volontaire, pas une erreur de l'API
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}{chemin}",
                headers={"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": HOTE},
                params=params or {},
                timeout=15.0,
            )
    except Exception as exc:
        _noter_erreur(chemin, f"réseau : {type(exc).__name__}")
        return None
    _maj_quota(r.headers)
    if r.status_code == 204:
        return {}
    if r.status_code == 429:
        # Deux sens possibles pour un 429 : quota du jour épuisé (les en-têtes l'indiquent : restant = 0,
        # déjà enregistré par _maj_quota) OU trop de requêtes en même temps. Dans le second cas il reste
        # du quota : on ne bloque que PAUSE_429_S secondes, jamais jusqu'à la fin de la journée.
        if _quota["restant"] is None or _quota["restant"] > 0:
            _pause_jusqua = time.time() + PAUSE_429_S
            _noter_erreur(chemin, f"HTTP 429 (trop de requêtes d'un coup) : pause {PAUSE_429_S} s")
        else:
            _noter_erreur(chemin, "HTTP 429 : quota du jour épuisé")
        return None
    if r.status_code != 200:
        _noter_erreur(chemin, f"HTTP {r.status_code}")
        return None
    try:
        corps = r.json()
    except Exception:
        _noter_erreur(chemin, "réponse non JSON")
        return None
    if not isinstance(corps, dict):
        _noter_erreur(chemin, "forme de réponse inattendue")
        return None
    return corps


def en_cache(chemin: str) -> bool:
    hit = _cache.get(chemin)
    return bool(hit and hit[0] > time.time())


def manquants(chemins: Iterable[str]) -> int:
    """Nombre d'appels RÉELS qu'il faudrait pour ces chemins (ceux déjà en cache sont gratuits)."""
    return sum(1 for c in set(chemins) if not en_cache(c))


# Clés volumineuses et inutiles pour nous : retirées avant d'écrire en base (limite d'1 Mo par document).
_CLES_LOURDES = {"fieldTranslations", "changes", "teamColors", "nameTranslation", "playerColor",
                 "goalkeeperColor", "subTeams"}
_TAILLE_MAX_PERSISTEE = 900_000


def _alleger(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _alleger(v) for k, v in o.items() if k not in _CLES_LOURDES}
    if isinstance(o, list):
        return [_alleger(x) for x in o]
    return o


async def _lire_persistant(chemin: str) -> Optional[Tuple[float, Dict[str, Any]]]:
    try:
        from . import depot
        return await asyncio.to_thread(depot.cache_lire, chemin)
    except Exception:
        return None  # la base est un bonus : jamais bloquer


async def _ecrire_persistant(chemin: str, expire: float, donnees: Dict[str, Any]) -> None:
    try:
        from . import depot
        texte = json.dumps(_alleger(donnees), ensure_ascii=False)
        if len(texte) <= _TAILLE_MAX_PERSISTEE:
            await asyncio.to_thread(depot.cache_ecrire, chemin, expire, texte)
    except Exception:
        pass


def _memoriser(chemin: str, expire: float, donnees: Dict[str, Any]) -> None:
    maintenant = time.time()
    if len(_cache) >= _CACHE_MAX_ENTREES:
        for k in [k for k, (exp, _) in _cache.items() if exp <= maintenant] or list(_cache)[:100]:
            _cache.pop(k, None)
    _cache[chemin] = (expire, donnees)


async def _recolter(donnees: Any) -> None:
    if RECOLTEUR is None:
        return
    try:
        await RECOLTEUR(donnees)
    except Exception:
        pass


async def precharger(chemins: Iterable[str]) -> None:
    """Remonte en mémoire les entrées du cache persistant encore valides (aucun appel API).
    À appeler AVANT de planifier un budget d'appels : `en_cache` ne voit que la mémoire."""
    maintenant = time.time()
    manquants_ = [c for c in set(chemins) if not en_cache(c)]
    if not manquants_:
        return
    trouves = await asyncio.gather(*[_lire_persistant(c) for c in manquants_])
    for chemin, hit in zip(manquants_, trouves):
        if hit and hit[0] > maintenant:
            _memoriser(chemin, hit[0], hit[1])
            await _recolter(hit[1])  # relu du cache persistant : son contenu nourrit aussi les pronostics


async def appeler(chemin: str, ttl: int, *, utilisateur: bool = True, persister: bool = True) -> Optional[Dict[str, Any]]:
    """Cache (mémoire puis base) devant `_get`. Un échec n'est JAMAIS mis en cache ; une réponse vide
    l'est brièvement. `persister=False` : mémoire seulement (données déjà stockées ailleurs)."""
    maintenant = time.time()
    hit = _cache.get(chemin)
    if hit and hit[0] > maintenant:
        return hit[1]
    if persister:
        persiste = await _lire_persistant(chemin)
        if persiste and persiste[0] > maintenant:
            _memoriser(chemin, persiste[0], persiste[1])
            await _recolter(persiste[1])
            return persiste[1]
    donnees = await _get(chemin, utilisateur=utilisateur)
    if donnees is None:
        return None
    expire = maintenant + (min(ttl, TTL_VIDE_MAX) if donnees == {} else ttl)
    _memoriser(chemin, expire, donnees)
    if persister:
        await _ecrire_persistant(chemin, expire, donnees)
    await _recolter(donnees)  # CHAQUE appel réel alimente le cache de résultats
    return donnees


def prolonger(chemin: str, ttl: int) -> None:
    """Allonge la durée de vie d'une entrée déjà en cache (ex. match terminé : ne changera plus)."""
    hit = _cache.get(chemin)
    if hit:
        _cache[chemin] = (time.time() + ttl, hit[1])


def vider_cache() -> None:
    _cache.clear()


def reinitialiser_etat() -> None:
    """Remet à zéro quota, pause et dernière erreur (tests)."""
    global _pause_jusqua
    _quota.update(limite=None, restant=None, fenetre_fin=None)
    _pause_jusqua = 0.0
    _derniere_erreur.clear()
