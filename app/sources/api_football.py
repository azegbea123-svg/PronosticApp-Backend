"""
Intégration API-Football (api-sports.io) — utilisée en COMPLÉMENT de
BeSoccer, jamais en remplacement, pour deux raisons précises où BeSoccer
est structurellement limité :
  1. Historique COMPLET des confrontations directes (BeSoccer ne donne
     qu'un coup d'œil dans les derniers matchs déjà scrappés, pas un
     vrai historique dédié).
  2. Secours si BeSoccer échoue complètement pour une équipe.

⚠️ PLAN GRATUIT = 10 requêtes/minute, 100/jour. Dépasser la limite par
MINUTE peut bloquer le compte "sans préavis" (documentation officielle),
INDÉPENDAMMENT du quota journalier — c'est très probablement la cause
d'une suspension déjà vécue alors que le quota n'était pas atteint.

Le limiteur de débit ci-dessous est donc une protection non négociable :
toute requête qui dépasserait les seuils est simplement REFUSÉE (renvoie
None) plutôt que d'attendre ou de forcer — mieux vaut un signal manquant
qu'un compte re-suspendu.
"""

import time
from typing import Optional, Dict, Any, List

import httpx

from ..config import CLE_API_FOOTBALL

BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": CLE_API_FOOTBALL}

# ==== Limiteur de débit — marges de sécurité sous les vraies limites ====
# Volontairement plus strict que les seuils réels (10/min, 100/jour) pour
# garder de la marge : 7/min et 85/jour plutôt que de coller au maximum.
_MAX_APPELS_PAR_MINUTE = 7
_MAX_APPELS_PAR_JOUR = 85

_horodatages_appels: List[float] = []  # fenêtre glissante des appels de la dernière minute
_compteur_jour = 0
_jour_compteur_reinitialise: Optional[str] = None  # date UTC (YYYY-MM-DD) du dernier reset


def _jour_utc_actuel() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _peut_appeler() -> bool:
    """
    Vérifie les deux limites (par minute ET par jour) avant d'autoriser
    un appel. Ne fait jamais attendre — refuse simplement si on est trop
    près de la limite, par sécurité.
    """
    global _compteur_jour, _jour_compteur_reinitialise

    aujourd_hui = _jour_utc_actuel()
    if _jour_compteur_reinitialise != aujourd_hui:
        _compteur_jour = 0
        _jour_compteur_reinitialise = aujourd_hui

    if _compteur_jour >= _MAX_APPELS_PAR_JOUR:
        return False

    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    if len(_horodatages_appels) >= _MAX_APPELS_PAR_MINUTE:
        return False

    return True


def _enregistrer_appel() -> None:
    global _compteur_jour
    _horodatages_appels.append(time.time())
    _compteur_jour += 1


def quota_restant() -> Dict[str, int]:
    """Exposé pour diagnostic (voir /debug/api-football) — combien il reste avant la limite."""
    maintenant = time.time()
    appels_recents = len([t for t in _horodatages_appels if maintenant - t < 60])
    return {
        "appels_derniere_minute": appels_recents,
        "restant_cette_minute": max(0, _MAX_APPELS_PAR_MINUTE - appels_recents),
        "appels_aujourd_hui": _compteur_jour,
        "restant_aujourd_hui": max(0, _MAX_APPELS_PAR_JOUR - _compteur_jour),
    }


async def _appeler(endpoint: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not CLE_API_FOOTBALL:
        return None  # clé non configurée — désactivé silencieusement, pas d'erreur bruyante
    if not _peut_appeler():
        return None  # protection de débit — refuse plutôt que de risquer une suspension

    _enregistrer_appel()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params, timeout=10.0)
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


# ==== Cache des identifiants d'équipe — PERMANENT (les ID ne changent jamais) ====
_CACHE_ID_EQUIPE: Dict[str, Optional[int]] = {}


async def get_team_id(nom_equipe: str) -> Optional[int]:
    """
    Trouve l'identifiant API-Football d'une équipe par son nom. Mis en
    cache de façon PERMANENTE (contrairement au cache BeSoccer à TTL) —
    un ID d'équipe ne change jamais, autant ne consommer le quota qu'une
    seule fois par équipe pour toute la durée de vie du serveur.
    """
    cle = nom_equipe.strip().lower()
    if cle in _CACHE_ID_EQUIPE:
        return _CACHE_ID_EQUIPE[cle]

    data = await _appeler("/teams", {"search": nom_equipe})
    resultat_id = None
    if data and data.get("response"):
        resultat_id = data["response"][0]["team"]["id"]

    _CACHE_ID_EQUIPE[cle] = resultat_id  # même un échec est mis en cache — évite de re-tenter en boucle
    return resultat_id


# ==== Cache de l'historique des confrontations directes ====
_CACHE_H2H: Dict[str, tuple] = {}  # "id1-id2" (trié) -> (valeur, expire_a)
_TTL_H2H_SECONDES = 7 * 24 * 3600  # 7 jours — un vrai historique change rarement d'une semaine à l'autre


async def get_historique_confrontations(equipe1: str, equipe2: str, limite: int = 10) -> Optional[List[Dict[str, Any]]]:
    """
    Historique COMPLET des confrontations directes entre deux équipes —
    contrairement à besoccer._confrontation_recente qui ne fait que
    regarder par coïncidence dans les derniers matchs déjà scrappés,
    ceci interroge un vrai endpoint dédié à l'historique tête-à-tête.

    Renvoie une liste de matchs (le plus récent en premier), ou None si
    indisponible (clé non configurée, quota atteint, équipe introuvable,
    ou erreur réseau) — jamais d'exception, cohérent avec besoccer.py.
    """
    id1 = await get_team_id(equipe1)
    id2 = await get_team_id(equipe2)
    if id1 is None or id2 is None:
        return None

    cle_cache = f"{min(id1, id2)}-{max(id1, id2)}"
    entree = _CACHE_H2H.get(cle_cache)
    if entree is not None:
        valeur, expire_a = entree
        if time.time() < expire_a:
            return valeur

    data = await _appeler("/fixtures/headtohead", {"h2h": f"{id1}-{id2}", "last": limite})
    if data is None:
        return None

    matchs = []
    for fixture in data.get("response", []):
        try:
            buts = fixture["goals"]
            equipes = fixture["teams"]
            matchs.append({
                "date": fixture["fixture"]["date"][:10],
                "equipe_domicile": equipes["home"]["name"],
                "equipe_exterieur": equipes["away"]["name"],
                "buts_domicile": buts["home"],
                "buts_exterieur": buts["away"],
            })
        except (KeyError, TypeError):
            continue  # entrée malformée — on l'ignore plutôt que de tout faire échouer

    _CACHE_H2H[cle_cache] = (matchs, time.time() + _TTL_H2H_SECONDES)
    return matchs
