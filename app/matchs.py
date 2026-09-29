"""
Liste des matchs du jour / J+1, filtrée aux championnats suivis
(CHAMPIONNATS_SUIVIS dans config.py), avec disponibilité des données
d'analyse PRÉCALCULÉE — le scraping BeSoccer se fait en arrière-plan
(dès qu'on reconstruit la liste), jamais au moment où le client clique
sur un match. L'analyse détaillée elle-même reste /match/analyse,
inchangé — même moteur, même quota de 3 gratuites/jour.

Le rafraîchissement se déclenche à la demande (au premier appel de
/matchs qui trouve la liste périmée), pas via un cron séparé : plus
simple à héberger sur Render (le plan gratuit s'endort de toute façon
en cas d'inactivité, un cron externe serait nécessaire pour un vrai
scheduler) et suffisant pour l'usage réel (peu d'appels concurrents).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from .config import CHAMPIONNATS_SUIVIS, TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
from .sources import api_football, besoccer
from . import repo


def _jour_iso(offset: int = 0) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def _normaliser(fixture: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fixture_id": fixture["fixture"]["id"],
        "date": fixture["fixture"]["date"],
        "league_id": fixture["league"]["id"],
        "league": fixture["league"]["name"],
        "equipe1": fixture["teams"]["home"]["name"],
        "equipe2": fixture["teams"]["away"]["name"],
        "donneesDisponibles": False,
    }


async def _verifier_disponibilite(match: Dict[str, Any]) -> bool:
    """
    Réutilise le cache BeSoccer existant (get_team_stats, TTL 6h succès /
    1h échec) : un match déjà vérifié récemment, ou une équipe déjà
    consultée pour un autre match du jour, ne redéclenche AUCUNE requête
    HTTP supplémentaire — juste une lecture de cache mémoire.
    """
    stats1 = await besoccer.get_team_stats(match["equipe1"])
    stats2 = await besoccer.get_team_stats(match["equipe2"])
    return stats1 is not None and stats2 is not None


async def _rafraichir_jour(jour_iso: str) -> List[Dict[str, Any]]:
    fixtures = await api_football.get_fixtures_du_jour(jour_iso)
    if fixtures is None:
        # Quota API-Football épuisé, clé non configurée, ou erreur réseau
        # : on garde ce qui est déjà en base plutôt que de renvoyer une
        # liste vide qui ferait croire à "aucun match aujourd'hui".
        return repo.lister_matchs_jour(jour_iso)

    matchs = [
        _normaliser(f) for f in fixtures if f["league"]["id"] in CHAMPIONNATS_SUIVIS
    ]

    # ⚠️ Même prudence que /debug/verifier-clubs : petits lots + pause
    # entre chaque, pour ne pas redéclencher un blocage BeSoccer avec un
    # jour à beaucoup de matchs (rappel : un test en rafale de 150 clubs
    # avait déjà causé un blocage temporaire).
    taille_lot = 3
    for i in range(0, len(matchs), taille_lot):
        lot = matchs[i : i + taille_lot]
        disponibilites = await asyncio.gather(*[_verifier_disponibilite(m) for m in lot])
        for m, dispo in zip(lot, disponibilites):
            m["donneesDisponibles"] = dispo
        if i + taille_lot < len(matchs):
            await asyncio.sleep(2.0)

    repo.enregistrer_matchs_jour(jour_iso, matchs)
    repo.marquer_matchs_rafraichis(jour_iso)
    return matchs


async def obtenir_matchs(jour: str) -> Dict[str, Any]:
    """
    jour: "today" ou "tomorrow". Renvoie la liste déjà en cache si elle a
    moins de TTL_RAFRAICHISSEMENT_MATCHS_SECONDES, sinon la reconstruit
    (nouvelle requête API-Football + nouvelle vérification BeSoccer).
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
