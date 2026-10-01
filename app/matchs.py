"""
Liste des matchs du jour / J+1 — FUSION de deux fournisseurs, appelés
EN PARALLÈLE (asyncio.gather) :
  - football-data.org (sources/football_data.py) : ~12 grandes
    compétitions seulement, mais données officielles et propres.
  - TheSportsDB (sources/thesportsdb.py) : ~617 championnats, beaucoup
    plus large, mais base contributive (horaires parfois imprécis).

Un même match remonté par les deux est dédupliqué (voir _cle_dedup) —
la version football-data.org est gardée en priorité pour ces cas-là
(données plus propres), TheSportsDB comble les championnats que
football-data.org ne couvre pas.

L'identifiant de chaque match (fixture_id) est calculé par un hash
STABLE de (jour, équipe1, équipe2) — PAS l'id brut du fournisseur. Ça
garantit que le même match garde le même fixture_id d'un rafraîchissement
à l'autre même s'il change de fournisseur "gagnant" entre deux appels
(ex: football-data.org qui a un souci un jour, TheSportsDB prend le
relais) — sans ça, la disponibilité BeSoccer déjà vérifiée serait perdue
à chaque changement de source.

Disponibilité des données BeSoccer : vérifiée EN ARRIÈRE-PLAN, en
priorité pour les matchs dont le coup d'envoi approche, et plafonnée
par cycle (MAX_VERIFICATIONS_PAR_CYCLE) — vérifier tous les matchs
d'une journée mondiale d'un coup a déjà provoqué un blocage BeSoccer
par le passé avec seulement 150 clubs d'un coup (voir sources/besoccer.py).

Un match reste cliquable dans l'appli UNIQUEMENT une fois
donneesDisponibles=true (voir MatchsScreen.kt côté Android).
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
from .sources import thesportsdb, football_data, besoccer
from . import repo

# Nombre max de vérifications BeSoccer (équipes) par cycle en arrière-plan.
MAX_VERIFICATIONS_PAR_CYCLE = 60

_SUFFIXES_A_IGNORER = (" fc", " cf", " sc", " afc", " cfc", " ac")


def _jour_iso(offset: int = 0) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def _normaliser_nom(nom: str) -> str:
    n = nom.strip().lower()
    for suffixe in _SUFFIXES_A_IGNORER:
        if n.endswith(suffixe):
            n = n[: -len(suffixe)].strip()
    return n


def _cle_dedup(jour: str, equipe1: str, equipe2: str) -> str:
    return f"{jour}|{_normaliser_nom(equipe1)}|{_normaliser_nom(equipe2)}"


def _fixture_id_stable(jour: str, equipe1: str, equipe2: str) -> int:
    """Hash déterministe (indépendant du fournisseur) — voir docstring en tête de fichier."""
    empreinte = hashlib.sha1(_cle_dedup(jour, equipe1, equipe2).encode("utf-8")).hexdigest()
    return int(empreinte[:15], 16)  # tient largement dans un Long 64 bits côté Android


def _normaliser_football_data(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        jour = m["utcDate"][:10]
        equipe1 = m["homeTeam"]["name"]
        equipe2 = m["awayTeam"]["name"]
        return {
            "fixture_id": _fixture_id_stable(jour, equipe1, equipe2),
            "date": m["utcDate"],
            "league_id": m["competition"]["id"],
            "league": m["competition"]["name"],
            "equipe1": equipe1,
            "equipe2": equipe2,
            "donneesDisponibles": None,
        }
    except (KeyError, TypeError):
        return None


def _recalculer_id_thesportsdb(m: Dict[str, Any]) -> Dict[str, Any]:
    """thesportsdb.py normalise déjà au format interne, mais avec son
    PROPRE id — on le remplace ici par le hash stable commun, pour que
    la déduplication et la conservation de la disponibilité marchent
    pareil quel que soit le fournisseur d'origine."""
    m["fixture_id"] = _fixture_id_stable(m["date"][:10], m["equipe1"], m["equipe2"])
    return m


async def _rafraichir_jour(jour_iso: str) -> List[Dict[str, Any]]:
    matchs_fd_bruts, matchs_tsdb = await asyncio.gather(
        football_data.get_fixtures_du_jour(jour_iso),
        thesportsdb.get_fixtures_du_jour(jour_iso),
    )
    # Chaque fonction source renvoie None uniquement en cas d'échec
    # total (clé absente, quota, erreur réseau) — jamais en cas de
    # liste simplement vide. Si LES DEUX échouent, on garde le cache.
    if matchs_fd_bruts is None and matchs_tsdb is None:
        return repo.lister_matchs_jour(jour_iso)

    fusionnes: Dict[str, Dict[str, Any]] = {}

    # TheSportsDB en premier (comble les championnats hors football-data.org)...
    for m in matchs_tsdb or []:
        m = _recalculer_id_thesportsdb(dict(m))
        cle = _cle_dedup(m["date"][:10], m["equipe1"], m["equipe2"])
        fusionnes[cle] = m

    # ...puis football-data.org, qui ÉCRASE un doublon éventuel (données
    # officielles considérées plus fiables pour les compétitions qu'il couvre).
    for f in matchs_fd_bruts or []:
        m = _normaliser_football_data(f)
        if m is None:
            continue
        cle = _cle_dedup(m["date"][:10], m["equipe1"], m["equipe2"])
        fusionnes[cle] = m

    if not fusionnes:
        return repo.lister_matchs_jour(jour_iso)

    # On garde le statut de disponibilité déjà connu pour un match déjà
    # vu lors d'un précédent rafraîchissement (fixture_id stable, donc
    # comparable directement même si le fournisseur "gagnant" a changé).
    deja_connus = {m["fixture_id"]: m for m in repo.lister_matchs_jour(jour_iso)}

    matchs = []
    for m in fusionnes.values():
        ancien = deja_connus.get(m["fixture_id"])
        if ancien and ancien.get("donneesDisponibles") is not None:
            m["donneesDisponibles"] = ancien["donneesDisponibles"]
        matchs.append(m)

    repo.enregistrer_matchs_jour(jour_iso, matchs)
    repo.marquer_matchs_rafraichis(jour_iso)
    return matchs


def _minutes_avant_coup_envoi(match: Dict[str, Any]) -> float:
    """Plus petit = plus urgent à vérifier. Un match déjà commencé depuis
    plus de 3h passe en dernier (ni urgent, ni fiable à re-vérifier)."""
    try:
        coup_envoi = datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
    except Exception:
        return float("inf")
    delta_minutes = (coup_envoi - datetime.now(timezone.utc)).total_seconds() / 60
    return delta_minutes if delta_minutes >= -180 else float("inf")


async def _verifier_un_match(match: Dict[str, Any]) -> bool:
    """Réutilise le cache BeSoccer existant (get_team_stats, TTL 6h
    succès / 1h échec) — une équipe déjà vue pour un autre match du jour
    ne redéclenche aucune requête HTTP supplémentaire."""
    stats1 = await besoccer.get_team_stats(match["equipe1"])
    stats2 = await besoccer.get_team_stats(match["equipe2"])
    return stats1 is not None and stats2 is not None


async def verifier_disponibilite_prochains(jour_iso: str) -> None:
    """
    Tâche de fond (voir BackgroundTasks dans main.py) : vérifie la
    disponibilité BeSoccer des matchs pas encore vérifiés pour ce jour,
    en commençant par ceux dont le coup d'envoi est le plus proche.
    Volontairement plafonnée à MAX_VERIFICATIONS_PAR_CYCLE — les matchs
    restants seront traités lors d'un prochain appel à /matchs.
    """
    matchs = repo.lister_matchs_jour(jour_iso)
    a_verifier = [m for m in matchs if m.get("donneesDisponibles") is None]
    if not a_verifier:
        return

    a_verifier.sort(key=_minutes_avant_coup_envoi)
    a_verifier = a_verifier[:MAX_VERIFICATIONS_PAR_CYCLE]

    taille_lot = 3
    for i in range(0, len(a_verifier), taille_lot):
        lot = a_verifier[i : i + taille_lot]
        resultats = await asyncio.gather(*[_verifier_un_match(m) for m in lot])
        for m, dispo in zip(lot, resultats):
            repo.marquer_disponibilite_match(m["fixture_id"], dispo)
        if i + taille_lot < len(a_verifier):
            await asyncio.sleep(2.0)


async def obtenir_matchs(jour: str) -> Dict[str, Any]:
    """
    jour: "today" ou "tomorrow". Renvoie la liste déjà en cache si elle a
    moins de TTL_RAFRAICHISSEMENT_MATCHS_SECONDES, sinon la reconstruit
    (football-data.org + TheSportsDB en parallèle). La vérification
    BeSoccer se fait à part, en tâche de fond.
    """
    jour_cible = _jour_iso(0 if jour == "today" else 1)

    dernier = repo.dernier_rafraichissement_matchs(jour_cible)
    trop_ancien = (
        dernier is None
        or (datetime.now(timezone.utc) - dernier).total_seconds()
        > TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
    )

    if trop_ancien:
        matchs = await _rafraichir_jour(jour_cible)
    else:
        matchs = repo.lister_matchs_jour(jour_cible)

    return {"jour": jour_cible, "matchs": matchs}
