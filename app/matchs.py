"""
Liste des matchs du jour / J+1 — FUSION de CINQ fournisseurs, appelés
EN PARALLÈLE (asyncio.gather) :
  - football-data.org  : ~12 grandes compétitions, données officielles propres.
  - TheSportsDB        : ~617 championnats, large mais base contributive.
  - API-Football/RapidAPI : ~1 236 championnats (Afrique/Asie/Amérique du
    Sud comprises), compte RapidAPI séparé de celui suspendu sur
    api-sports.io direct.
  - OpenLigaDB         : football allemand uniquement, mais sans clé ni
    limite de débit, avec un bon niveau de détail sur les divisions
    inférieures allemandes.
  - SportAPI7          : calendrier mondial via scheduled-events/{date};
    le détail /event/{id} est disponible à la demande.

Chaque module sources/ normalise DÉJÀ ses matchs au même format interne
{fixture_id (préfixé par source : "fd-", "tsdb-", "rapid-", "openliga-"),
date, league_id, league, equipe1, equipe2, donneesDisponibles=None} —
matchs.py n'a donc qu'à fusionner, pas à re-parser chaque format brut.

Un même match remonté par plusieurs sources est dédupliqué (voir
_cle_dedup, par date + équipes normalisées). Ordre de priorité en cas de
doublon (le dernier assigné dans la fusion l'emporte) : OpenLigaDB <
TheSportsDB < RapidAPI < football-data.org — données officielles
d'abord, niche en dernier recours.

L'identifiant de chaque match (fixture_id) est recalculé par un hash
STABLE de (jour, équipe1, équipe2) — jamais l'id brut d'un fournisseur.
Ça garantit que le même match garde le même fixture_id d'un
rafraîchissement à l'autre même s'il change de fournisseur "gagnant"
entre deux appels — sans ça, la disponibilité BeSoccer déjà vérifiée
serait perdue à chaque changement de source.

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
from typing import Any, Dict, List

from .config import TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
from .sources import thesportsdb, football_data, livefootball_rapidapi, openliga, besoccer, sportapi7
from .v3 import source_probe
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


def _recalculer_id(m: Dict[str, Any]) -> Dict[str, Any]:
    """Chaque module sources/ normalise déjà au format interne commun,
    mais avec son PROPRE id préfixé — on le remplace ici par le hash
    stable commun, pour que la déduplication et la conservation de la
    disponibilité marchent pareil quel que soit le fournisseur d'origine."""
    m = dict(m)
    m["fixture_id"] = _fixture_id_stable(m["date"][:10], m["equipe1"], m["equipe2"])
    return m


async def _rafraichir_jour(jour_iso: str) -> List[Dict[str, Any]]:
    resultats = await asyncio.gather(
        openliga.get_fixtures_du_jour(jour_iso),
        thesportsdb.get_fixtures_du_jour(jour_iso),
        livefootball_rapidapi.get_fixtures_du_jour(jour_iso),
        football_data.get_fixtures_du_jour(jour_iso),
        sportapi7.get_fixtures_du_jour(jour_iso),
    )
    matchs_openliga, matchs_tsdb, matchs_live, matchs_fd, matchs_sportapi7 = resultats

    # Sources RapidAPI supplémentaires activées depuis Swagger.
    # Elles sont appelées séparément et leurs données sont filtrées par
    # date + équipes avant d'entrer dans la fusion. Une source active qui
    # ne retourne rien pour la date demandée n'empêche jamais les autres.
    active_ids = source_probe.sources_actives_fixture()
    if active_ids:
        extra = await source_probe.fetch_active_fixture_matches(jour_iso)
        resultats = (*resultats, *extra)

    # Chaque fonction source renvoie None uniquement en cas d'échec
    # total (clé absente, quota, erreur réseau) — jamais en cas de
    # liste simplement vide. Si LES QUATRE échouent, on garde le cache.
    if all(r is None for r in resultats):
        return repo.lister_matchs_jour(jour_iso)

    fusionnes: Dict[str, Dict[str, Any]] = {}

    # Ordre de priorité croissante (le dernier écrase un doublon) :
    # niche d'abord, données officielles en dernier.
    for groupe in resultats:
        for m in groupe or []:
            m = _recalculer_id(m)
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
    (quatre fournisseurs en parallèle). La vérification BeSoccer se fait
    à part, en tâche de fond.
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
