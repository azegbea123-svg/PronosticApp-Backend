"""
Routes /esport/* — AJOUT au backend existant : aucune route football n'est modifiée.
Branché dans main.py par `app.include_router(esport_router)`.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, db, repo
from ..config import LIMITE_GRATUITE_QUOTIDIENNE
from . import client, service
from .service import ErreurEsport

router = APIRouter(prefix="/esport", tags=["🎮 Esport"])


class EsportPronosticRequest(BaseModel):
    eventId: int


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
    heures), pour un jeu (`jeu` = csgo | dota2 | lol) ou tous. `favori` = une des équipes est suivie.
    Coût API : au plus 7 appels (1 direct + 2 jours × 3 jeux), mis en cache 3 min (direct), 3 h
    (aujourd'hui) et 8 h (demain) pour ménager le quota (50 requêtes/jour sur le plan gratuit).
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


@router.post("/pronostic")
async def pronostic(requete: EsportPronosticRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    Pronostic d'un match e-sport à venir. Partage le quota gratuit quotidien du
    football (même compteur). Le pronostic est FIGÉ au premier calcul pour que le
    bilan (/esport/bilan) mesure une précision réelle, sans biais temporel.
    """
    est_vip = False
    restants: Optional[int] = None
    if db.get_client():
        est_vip = repo.est_vip(repo.obtenir_utilisateur(uid))
        if not est_vip:
            deja = repo.pronostics_utilises_aujourdhui(uid)
            if deja >= LIMITE_GRATUITE_QUOTIDIENNE:
                raise HTTPException(
                    429,
                    f"Limite gratuite de {LIMITE_GRATUITE_QUOTIDIENNE} pronostics par jour atteinte. "
                    "Passe en VIP pour un accès illimité.",
                )
            restants = LIMITE_GRATUITE_QUOTIDIENNE - deja - 1

    try:
        resultat = await asyncio.wait_for(service.pronostic(requete.eventId), timeout=30.0)
    except ErreurEsport as e:
        raise _traduire(e)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Délai dépassé côté API esport.")

    try:
        repo.enregistrer_pronostic(
            uid=uid,
            equipe1=resultat["equipe1"],
            equipe2=resultat["equipe2"],
            type_match=f"esport:{resultat.get('jeu')}",
            probabilite_v1=resultat["probabiliteVictoireEquipe1"],
            probabilite_nul=resultat["probabiliteMatchNul"],
            probabilite_v2=resultat["probabiliteVictoireEquipe2"],
        )
    except Exception:
        pass  # comptage du quota : bonus, ne bloque jamais la réponse

    return {**resultat, "vip": est_vip, "pronosticsRestantsAujourdhui": restants}


@router.get("/bilan")
async def bilan(uid: str = Depends(auth.utilisateur_courant)):
    """Précision réelle des pronostics e-sport déjà vérifiés (Brier et taux de bonnes réponses)."""
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
