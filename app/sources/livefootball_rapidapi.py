"""
free-api-live-football-data (via RapidAPI) — connecteur principal pour :
  - la liste des matchs d'un jour (logos, score, statut, minute en direct) ;
  - les scores EN DIRECT (/football-current-live) ;
  - le classement d'un championnat (/football-get-standing-all) ;
  - les confrontations directes d'un match (/football-get-head-to-head) ;
  - l'annuaire des championnats (noms, pays, logos, popularité), utilisé
    par ligues.py pour remplacer l'ancien affichage "Ligue 920744".

Une SEULE clé RapidAPI (variable RAPIDAPI_KEY) pour toutes les API
RapidAPI du compte. ⚠️ Ne jamais écrire la clé dans le code ni dans un
fichier versionné — uniquement en variable d'environnement sur Render.

Architecture : tous les appels passent par `_appeler` (cache mémoire +
TTL), qui délègue à `_get` (limiteur de débit + HTTP). Les tests
remplacent `_get` par des réponses tirées de tests/fixtures/.

Convention de retour des fonctions get_* : `None` = ÉCHEC (clé absente,
quota, réseau) ; liste/dict vide = réponse valide mais sans contenu. Les
appelants ne doivent jamais confondre les deux.
"""

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import CLE_RAPIDAPI

HOTE = "free-api-live-football-data.p.rapidapi.com"
BASE_URL = f"https://{HOTE}"

URL_LOGO_EQUIPE = "https://images.fotmob.com/image_resources/logo/teamlogo/{id}_small.png"
URL_LOGO_LIGUE = "https://images.fotmob.com/image_resources/logo/leaguelogo/dark/{id}.png"

# Durées de cache (secondes) — compromis fraîcheur / quota.
TTL_MATCHS_JOUR = 10 * 60
TTL_DIRECT = 30
TTL_CLASSEMENT = 6 * 3600
TTL_CONFRONTATIONS = 24 * 3600
TTL_ANNUAIRE = 24 * 3600

_MAX_APPELS_PAR_MINUTE = 8  # marge prudente, aucune limite officielle documentée
_horodatages_appels: List[float] = []

# clé (chemin, params triés) -> (expire_le, données)
_cache: Dict[Tuple[str, Tuple], Tuple[float, Any]] = {}
_CACHE_MAX_ENTREES = 300


# --------------------------------------------------------------------------
# Accès HTTP (limiteur + cache)
# --------------------------------------------------------------------------

def _peut_appeler() -> bool:
    maintenant = time.time()
    _horodatages_appels[:] = [t for t in _horodatages_appels if maintenant - t < 60]
    return len(_horodatages_appels) < _MAX_APPELS_PAR_MINUTE


def quota_restant() -> Dict[str, int]:
    maintenant = time.time()
    recents = len([t for t in _horodatages_appels if maintenant - t < 60])
    return {
        "appels_derniere_minute": recents,
        "restant_cette_minute": max(0, _MAX_APPELS_PAR_MINUTE - recents),
    }


async def _get(chemin: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Un appel HTTP réel. None si clé absente, limite locale atteinte, HTTP != 200
    ou corps invalide / status != success."""
    if not CLE_RAPIDAPI:
        return None
    if not _peut_appeler():
        return None
    _horodatages_appels.append(time.time())
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{BASE_URL}/{chemin}",
                headers={"x-rapidapi-key": CLE_RAPIDAPI, "x-rapidapi-host": HOTE},
                params=params or {},
                timeout=15.0,
            )
        if r.status_code != 200:
            return None
        corps = r.json()
        if not isinstance(corps, dict) or corps.get("status") != "success":
            return None
        return corps
    except Exception:
        return None


async def _appeler(chemin: str, params: Optional[Dict[str, Any]] = None, ttl: int = 0) -> Optional[Dict[str, Any]]:
    """Cache en mémoire devant `_get`. Un échec n'est JAMAIS mis en cache."""
    cle = (chemin, tuple(sorted((params or {}).items())))
    maintenant = time.time()
    if ttl:
        hit = _cache.get(cle)
        if hit and hit[0] > maintenant:
            return hit[1]
    donnees = await _get(chemin, params)
    if donnees is not None and ttl:
        if len(_cache) >= _CACHE_MAX_ENTREES:
            for k in [k for k, (exp, _) in _cache.items() if exp <= maintenant] or list(_cache)[:50]:
                _cache.pop(k, None)
        _cache[cle] = (maintenant + ttl, donnees)
    return donnees


# --------------------------------------------------------------------------
# Normalisation d'un match
# --------------------------------------------------------------------------

_MARQUES_INVISIBLES = re.compile(r"[\u200e\u200f\u202a-\u202e]")


def _nettoyer_minute(live_time: Optional[Dict[str, Any]]) -> Optional[str]:
    """'61‎’‎' (avec marques de direction invisibles) -> "61'". 'HT' reste 'HT'."""
    if not live_time:
        return None
    court = live_time.get("short")
    if not court:
        return None
    court = _MARQUES_INVISIBLES.sub("", str(court)).replace("\u2019", "'").strip()
    if re.fullmatch(r"[\d+]+", court):
        court += "'"
    return court or None


def _statut(status: Dict[str, Any]) -> str:
    raison = ((status.get("reason") or {}).get("shortKey") or "").lower()
    if "postponed" in raison:
        return "reporte"
    if status.get("cancelled"):
        return "annule"
    if status.get("finished"):
        return "termine"
    if status.get("started"):
        return "en_direct"
    return "prevu"


def _normaliser(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Format interne commun + champs enrichis (rapid_*, logos, score, statut).
    `league` reste vide : le nom du championnat est résolu par ligues.py.
    fixture_id préfixé "live-" : remplacé plus tard par le hash stable (matchs.py)."""
    try:
        status = m["status"]
        date_iso = status["utcTime"].split(".")[0].rstrip("Z") + "Z"
        home, away = m["home"], m["away"]
        statut = _statut(status)
        a_un_score = statut in ("en_direct", "termine")
        return {
            "fixture_id": f"live-{m['id']}",
            "date": date_iso,
            "league_id": m.get("leagueId") or 0,
            "league": "",
            "equipe1": home["name"],
            "equipe2": away["name"],
            "donneesDisponibles": None,
            "rapid_id": m["id"],
            "rapid_league_id": m.get("leagueId"),
            "equipe1_id": home.get("id"),
            "equipe2_id": away.get("id"),
            "logo1": URL_LOGO_EQUIPE.format(id=home["id"]) if home.get("id") else None,
            "logo2": URL_LOGO_EQUIPE.format(id=away["id"]) if away.get("id") else None,
            "statut": statut,
            "score1": home.get("score") if a_un_score else None,
            "score2": away.get("score") if a_un_score else None,
            "minute": _nettoyer_minute(status.get("liveTime")) if statut == "en_direct" else None,
        }
    except (KeyError, TypeError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Liste des matchs, direct
# --------------------------------------------------------------------------

async def get_fixtures_du_jour(jour_iso: str) -> Optional[List[Dict[str, Any]]]:
    """Matchs du jour (YYYY-MM-DD), normalisés. None = échec."""
    data = await _appeler("football-get-matches-by-date", {"date": jour_iso.replace("-", "")}, TTL_MATCHS_JOUR)
    if data is None:
        return None
    bruts = (data.get("response") or {}).get("matches") or []
    matchs = [m for m in (_normaliser(b) for b in bruts) if m is not None]
    # Filtre défensif : l'heure locale "time" peut déborder sur un autre jour que utcTime.
    return [m for m in matchs if m["date"][:10] == jour_iso]


async def get_en_direct() -> Optional[Dict[int, Dict[str, Any]]]:
    """Matchs en cours, indexés par rapid_id. None = échec ; {} = rien en direct."""
    data = await _appeler("football-current-live", None, TTL_DIRECT)
    if data is None:
        return None
    bruts = (data.get("response") or {}).get("live") or []
    sortie: Dict[int, Dict[str, Any]] = {}
    for b in bruts:
        n = _normaliser(b)
        if n is not None:
            sortie[n["rapid_id"]] = n
    return sortie


# --------------------------------------------------------------------------
# Classement, confrontations
# --------------------------------------------------------------------------

async def get_classement(ligue_id: int) -> Optional[List[Dict[str, Any]]]:
    data = await _appeler("football-get-standing-all", {"leagueid": ligue_id}, TTL_CLASSEMENT)
    if data is None:
        return None
    lignes = (data.get("response") or {}).get("standing") or []
    sortie = []
    for l in lignes:
        try:
            sortie.append({
                "equipe_id": l["id"],
                "nom": l.get("shortName") or l["name"],
                "rang": int(l["idx"]),
                "joues": l.get("played"),
                "victoires": l.get("wins"),
                "nuls": l.get("draws"),
                "defaites": l.get("losses"),
                "buts": l.get("scoresStr"),
                "diff": int(l.get("goalConDiff") or 0),
                "points": int(l.get("pts") or 0),
                "logo": URL_LOGO_EQUIPE.format(id=l["id"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    sortie.sort(key=lambda x: x["rang"])
    return sortie


def _score(chaine: Optional[str]) -> Optional[Tuple[int, int]]:
    try:
        a, b = (chaine or "").split("-")
        return int(a.strip()), int(b.strip())
    except ValueError:
        return None


async def get_confrontations(event_id: int, equipe1_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """Bilan vu depuis equipe1 (équipe de gauche dans la liste de l'appli).
    None si échec ou si equipe1 n'apparaît pas dans l'historique (orientation impossible)."""
    data = await _appeler("football-get-head-to-head", {"eventid": event_id}, TTL_CONFRONTATIONS)
    if data is None or equipe1_id is None:
        return None
    bruts = ((data.get("response") or {}).get("lineup") or {}).get("matches") or []
    cible = str(equipe1_id)
    parties = []
    for b in bruts:
        try:
            s = _score((b["status"] or {}).get("scoreStr"))
            if s is None or not b["status"].get("finished"):
                continue
            parties.append({
                "date": b["time"]["utcTime"],
                "equipe_domicile": b["home"]["name"],
                "equipe_exterieur": b["away"]["name"],
                "id_domicile": str(b["home"]["id"]),
                "id_exterieur": str(b["away"]["id"]),
                "buts": s,
                "competition": (b.get("league") or {}).get("name"),
            })
        except (KeyError, TypeError):
            continue
    if not parties or not any(cible in (p["id_domicile"], p["id_exterieur"]) for p in parties):
        return None
    bilan = {"equipe1": 0, "nuls": 0, "equipe2": 0}
    for p in parties:
        bd, be = p["buts"]
        if bd == be:
            bilan["nuls"] += 1
        else:
            gagnant_domicile = bd > be
            if gagnant_domicile == (p["id_domicile"] == cible):
                bilan["equipe1"] += 1
            else:
                bilan["equipe2"] += 1
    parties.sort(key=lambda p: p["date"], reverse=True)
    return {
        "bilan": bilan,
        "total": len(parties),
        "derniers": [
            {"date": p["date"], "equipe_domicile": p["equipe_domicile"],
             "equipe_exterieur": p["equipe_exterieur"], "score": f"{p['buts'][0]} - {p['buts'][1]}",
             "competition": p["competition"]}
            for p in parties[:5]
        ],
    }


# --------------------------------------------------------------------------
# Annuaire des championnats (consommé par ligues.py)
# --------------------------------------------------------------------------

async def get_annuaire_ligues() -> Optional[Dict[int, Dict[str, Any]]]:
    """leagueId -> {nom, pays, rang:0, parent:None}. 1 appel."""
    data = await _appeler("football-get-all-leagues-with-countries", None, TTL_ANNUAIRE)
    if data is None:
        return None
    sortie: Dict[int, Dict[str, Any]] = {}
    for pays in (data.get("response") or {}).get("leagues") or []:
        for l in pays.get("leagues") or []:
            try:
                sortie[int(l["id"])] = {
                    "nom": l.get("localizedName") or l["name"],
                    "pays": l.get("ccode") or pays.get("ccode"),
                    "rang": 0,
                    "parent": None,
                }
            except (KeyError, TypeError, ValueError):
                continue
    return sortie


async def get_ligues_populaires() -> Optional[List[int]]:
    data = await _appeler("football-popular-leagues", None, TTL_ANNUAIRE)
    if data is None:
        return None
    return [int(l["id"]) for l in (data.get("response") or {}).get("popular") or [] if "id" in l]


async def get_ligues_du_jour_groupees(jour_iso: str) -> Optional[Dict[int, Dict[str, Any]]]:
    """Endpoint groupé par championnat pour une date : donne nom, pays, rang
    d'importance (internalRank) et championnat parent (primaryId), y compris
    pour les identifiants de saison/phase absents de l'annuaire."""
    data = await _appeler("football-get-matches-by-date-and-league", {"date": jour_iso.replace("-", "")}, TTL_MATCHS_JOUR)
    if data is None:
        return None
    sortie: Dict[int, Dict[str, Any]] = {}
    reponse = data.get("response")
    for l in reponse if isinstance(reponse, list) else []:
        try:
            lid = int(l["id"])
            primaire = int(l.get("primaryId") or lid)
            sortie[lid] = {
                "nom": l["name"],
                "pays": l.get("ccode"),
                "rang": int(l.get("internalRank") or 0),
                "parent": primaire if primaire != lid else None,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return sortie


async def get_detail_ligue(ligue_id: int) -> Optional[Dict[str, Any]]:
    # Pas de cache mémoire : ligues.py persiste déjà le résultat (et retient les échecs 24 h).
    data = await _appeler("football-get-league-detail", {"leagueid": ligue_id}, 0)
    if data is None:
        return None
    l = (data.get("response") or {}).get("leagues") or {}
    nom = l.get("name") or l.get("shortName")
    if not nom:
        return None
    return {"nom": nom, "pays": l.get("country"), "rang": 0, "parent": None}


# --------------------------------------------------------------------------
# Diagnostic (/debug/livefootball)
# --------------------------------------------------------------------------

async def appel_diagnostic(jour_iso: str) -> Optional[Dict[str, Any]]:
    """Réponse brute, non filtrée, hors cache."""
    if not CLE_RAPIDAPI:
        return {"erreur": "RAPIDAPI_KEY non configurée"}
    data = await _get("football-get-matches-by-date", {"date": jour_iso.replace("-", "")})
    if data is None:
        return {"erreur": "appel en échec (clé invalide, quota, limite locale ou réseau)"}
    return data
