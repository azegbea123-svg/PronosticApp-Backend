"""
Routes /esport/* — AJOUT au backend existant : aucune route football n'est modifiée.
Branché dans main.py par `app.include_router(esport_router)`.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from . import client, service
from .service import ErreurEsport

router = APIRouter(prefix="/esport", tags=["🎮 Esport"])


class EsportEquipeRequest(BaseModel):
    teamId: int


def _traduire(e: ErreurEsport) -> HTTPException:
    return HTTPException(e.statut, e.message)


@router.get("/jeux")
async def jeux(uid: str = Depends(auth.utilisateur_courant)):
    """Les 3 jeux couverts par l'API (Counter Strike, Dota 2, LoL) + compteur de quota de l'API."""
    return {"jeux": service.lister_jeux(), "quota": client.quota()}


@router.get("/matchs")
async def matchs(jeu: Optional[str] = None, uid: str = Depends(auth.utilisateur_courant)):
    """
    Tous les matchs e-sport : en direct, à venir (aujourd'hui, demain) et terminés (24 dernières
    heures), pour un jeu (`jeu` = csgo | dota2 | lol) ou tous. Chaque match porte son `pronostic`
    (probabilités, favori, confiance), calculé sans appel API et figé avant le coup d'envoi ; il est
    toujours fourni (confiance « faible » et `donnees_suffisantes` faux si peu de matchs connus).
    `favori` (champ du match) = une des équipes est suivie.
    Coût API : au plus 7 appels, presque toujours 0 (cache persistant : 10 min pour le direct, 3 h
    aujourd'hui, 8 h demain ; direct plafonné à 10 appels réels par jour). `diagnostic` détaille chaque
    requête (ok, cache, vide, échec, ignoré) et `historique` la progression du classement.
    """
    return await service.lister_matchs(jeu)


@router.get("/recherche")
async def recherche(q: str, uid: str = Depends(auth.utilisateur_courant)):
    """Cherche des équipes e-sport par nom (1 appel API, mis en cache 24 h). Sert à retrouver un identifiant pour /esport/equipes."""
    try:
        return {"equipes": await service.rechercher_equipes(q)}
    except ErreurEsport as e:
        raise _traduire(e)


@router.get("/match/{event_id}")
async def match(event_id: int, avec: str = "", uid: str = Depends(auth.utilisateur_courant)):
    """
    Détail et suivi d'un match. `avec` = liste séparée par des virgules parmi
    cartes, duel, votes (chacun coûte 1 appel API, mis en cache 6 h).
    """
    try:
        return await service.detail(event_id, {a.strip() for a in avec.split(",") if a.strip()})
    except ErreurEsport as e:
        raise _traduire(e)


@router.get("/bilan")
async def bilan(uid: str = Depends(auth.utilisateur_courant)):
    """Précision réelle des pronostics e-sport déjà vérifiés. Ne consomme aucune requête de l'API."""
    return await service.bilan()


@router.get("/equipes")
async def equipes(uid: str = Depends(auth.utilisateur_courant)):
    from . import depot
    return {"equipes": depot.lister_equipes()}


@router.post("/equipes")
async def suivre_equipe(requete: EsportEquipeRequest, uid: str = Depends(auth.utilisateur_courant)):
    """🔧 Admin — ajouter une équipe aux FAVORIS (1 appel API). Trouve son identifiant avec GET /esport/recherche?q=nom."""
    auth.exiger_admin(uid)
    try:
        return await service.ajouter_equipe(requete.teamId)
    except ErreurEsport as e:
        raise _traduire(e)


@router.post("/equipes/importer-connues")
async def importer_connues(principales_seulement: bool = False, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Admin — ajoute d'un coup aux FAVORIS les 33 équipes CS extraites des captures (aucun appel API,
    aucun quota dépensé). `principales_seulement=true` : seulement les grandes organisations (17).
    """
    auth.exiger_admin(uid)
    return service.importer_equipes_connues(principales_seulement)


@router.delete("/equipes/{team_id}")
async def ne_plus_suivre(team_id: int, uid: str = Depends(auth.utilisateur_courant)):
    auth.exiger_admin(uid)
    from . import depot
    if not depot.supprimer_equipe(team_id):
        raise HTTPException(404, "Équipe non suivie.")
    return {"supprimee": team_id}
