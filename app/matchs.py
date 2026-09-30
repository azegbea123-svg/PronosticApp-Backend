"""
Liste des matchs du jour / J+1 — matchs remontés par TheSportsDB (voir
sources/thesportsdb.py ; remplace football-data.org pour une couverture
plus large, ~617 championnats de football contre ~12 sur le plan
gratuit de football-data.org, lui-même utilisé en remplacement
d'API-Football, dont le compte a été suspendu en sept. 2026).

Disponibilité des données BeSoccer : vérifiée EN ARRIÈRE-PLAN, en
priorité pour les matchs dont le coup d'envoi approche, et plafonnée
par cycle (MAX_VERIFICATIONS_PAR_CYCLE) — vérifier tous les matchs
d'une journée mondiale d'un coup (souvent 500-1000+ équipes) a déjà
provoqué un blocage BeSoccer par le passé avec seulement 150 clubs
d'un coup (voir sources/besoccer.py). La priorisation par horaire fait
naturellement s'étaler la charge sur la journée ; le plafond est un
filet de sécurité en plus, pour le cas où beaucoup de matchs
deviendraient "imminents" en même temps (ex: au redémarrage du service
après une pause, juste avant une grosse tranche horaire de matchs).

Un match reste cliquable dans l'appli UNIQUEMENT une fois
donneesDisponibles=true (voir MatchsScreen.kt côté Android) — pas de
scraping à la demande dans cette version : tout se décide à l'avance.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
from .sources import thesportsdb, besoccer
from . import repo

# Nombre max de vérifications BeSoccer (équipes) par cycle en arrière-plan.
MAX_VERIFICATIONS_PAR_CYCLE = 60


def _jour_iso(offset: int = 0) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


async def _rafraichir_jour(jour_iso: str) -> List[Dict[str, Any]]:
    matchs_bruts = await thesportsdb.get_fixtures_du_jour(jour_iso)
    if matchs_bruts is None:
        # Quota épuisé ou erreur réseau : on garde ce qui est déjà en
        # base plutôt que de renvoyer une liste vide qui ferait croire à
        # "aucun match".
        return repo.lister_matchs_jour(jour_iso)

    # thesportsdb.get_fixtures_du_jour renvoie déjà les matchs au bon
    # format interne (fixture_id, date, league_id, league, equipe1,
    # equipe2, donneesDisponibles=None) — pas de normalisation ici, le
    # format TheSportsDB est trop différent pour partager une fonction
    # commune avec un éventuel autre fournisseur.
    #
    # On garde le statut de disponibilité déjà connu pour un match déjà
    # vu lors d'un précédent rafraîchissement — pas la peine de repasser
    # "en attente" (et de re-vérifier BeSoccer) un match déjà confirmé.
    deja_connus = {m["fixture_id"]: m for m in repo.lister_matchs_jour(jour_iso)}

    matchs = []
    for m in matchs_bruts:
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
    restants seront traités lors d'un prochain appel à /matchs (chaque
    appel relance cette tâche, donc la liste se complète progressivement
    au fil des consultations de l'écran).
    """
    matchs = repo.lister_matchs_jour(jour_iso)
    a_verifier = [m for m in matchs if m.get("donneesDisponibles") is None]
    if not a_verifier:
        return

    a_verifier.sort(key=_minutes_avant_coup_envoi)
    a_verifier = a_verifier[:MAX_VERIFICATIONS_PAR_CYCLE]

    # ⚠️ Petits lots + pause : c'est cette prudence (déjà utilisée
    # ailleurs dans le backend) qui protège contre un nouveau blocage.
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
    (nouvelle requête API-Football). La vérification BeSoccer elle-même
    se fait à part, en tâche de fond (voir verifier_disponibilite_prochains,
    déclenchée depuis l'endpoint /matchs dans main.py).
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
