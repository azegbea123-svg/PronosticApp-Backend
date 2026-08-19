import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from .models import MatchAnalysisRequest, MatchAnalysisResponse
from .sources import sofascore, besoccer, flashscore
from .analysis import generer_pronostic
from .db_models import Pronostic
from . import db

app = FastAPI(title="PronosticApp API")


@app.on_event("startup")
def au_demarrage():
    db.creer_tables()  # ne fait rien si DATABASE_URL n'est pas configurée

# CORS ouvert : simple pour un backend consommé uniquement par l'appli Android.
# À restreindre si un jour ce backend est aussi appelé depuis un site web public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/equipes/recherche")
async def rechercher_equipes(q: str):
    """
    Suggestions d'équipes pour l'autocomplétion côté appli, au fur et à
    mesure que l'utilisateur tape. Ne remplace pas la saisie libre —
    l'utilisateur peut toujours taper un nom qui n'apparaît pas dans les
    suggestions et lancer l'analyse quand même.
    """
    try:
        suggestions = await sofascore.rechercher_equipes(q)
    except Exception:
        suggestions = []
    return {"suggestions": suggestions}


@app.get("/debug/elo")
async def debug_elo(equipe1: str, equipe2: str):
    """🔧 Diagnostic temporaire pour la recherche d'ELO/confrontation directe."""
    try:
        resultat = await besoccer.get_elo_confrontation(equipe1, equipe2)
        return {"equipe1": equipe1, "equipe2": equipe2, "resultat": resultat}
    except Exception as e:
        return {"equipe1": equipe1, "equipe2": equipe2, "erreur": f"{type(e).__name__}: {e}"}


@app.get("/debug/db")
def debug_db(session: Optional[Session] = Depends(db.get_session)):
    """🔧 Diagnostic temporaire : état réel de la connexion base de données."""
    resultat: Dict[str, Any] = {
        "DATABASE_URL_definie": bool(db.DATABASE_URL),
        "engine_cree": db.engine is not None,
        "session_disponible": session is not None,
    }

    if session is not None:
        try:
            nb_lignes = len(session.exec(select(Pronostic)).all())
            resultat["connexion_ok"] = True
            resultat["nb_pronostics_en_base"] = nb_lignes
        except Exception as e:
            resultat["connexion_ok"] = False
            resultat["erreur"] = f"{type(e).__name__}: {e}"

    return resultat


@app.get("/debug/besoccer")
async def debug_besoccer(equipe: str):
    """🔧 Diagnostic temporaire, adapté à la version basée sur les slugs d'équipe."""
    import httpx
    from .sources.besoccer import _candidats_slug, TEAM_URL, HEADERS, _extraire_forme_recente

    resultat: Dict[str, Any] = {
        "equipe": equipe,
        "candidats_slug": _candidats_slug(equipe),
        "essais": [],
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for slug in _candidats_slug(equipe):
            url = TEAM_URL.format(slug=slug)
            try:
                r = await client.get(url, headers=HEADERS, timeout=10.0)
                essai: Dict[str, Any] = {"url": url, "status_code": r.status_code}

                if r.status_code == 200:
                    stats = _extraire_forme_recente(r.text)
                    essai["stats_extraites"] = stats
                    resultat["essais"].append(essai)
                    break

                resultat["essais"].append(essai)
            except Exception as e:
                resultat["essais"].append({"url": url, "erreur": f"{type(e).__name__}: {e}"})

    return resultat


@app.get("/debug/sofascore")
async def debug_sofascore(equipe: str):
    """
    🔧 Endpoint de diagnostic TEMPORAIRE — à retirer une fois le problème
    de scraping résolu. Permet de voir ce que Sofascore répond réellement
    depuis le serveur déployé (code HTTP, début du corps de la réponse),
    sans passer par toute la logique de parsing qui masquerait l'erreur.
    """
    import httpx
    from .sources.sofascore import BASE, HEADERS

    resultat: Dict[str, Any] = {"equipe": equipe}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{BASE}/search/all?q={equipe}", headers=HEADERS, timeout=10.0
            )
            resultat["search_status_code"] = r.status_code
            resultat["search_body_extrait"] = r.text[:500]
        except Exception as e:
            resultat["search_erreur"] = f"{type(e).__name__}: {e}"

    return resultat


@app.get("/")
async def health():
    """Endpoint de santé, utile pour vérifier que le déploiement fonctionne."""
    return {"status": "ok", "service": "PronosticApp API"}


async def _stats_toutes_sources(nom_equipe: str) -> List[Dict[str, Any]]:
    """
    Interroge TOUTES les sources en parallèle (plus seulement la première
    qui répond) et renvoie la liste de celles qui ont réussi. La fusion
    (moyennes, cumuls) est faite ensuite dans analysis.py — additionner
    les compteurs bruts de plusieurs sources est mathématiquement sûr même
    si elles se recoupent sur les mêmes matchs réels.
    """
    resultats: List[Dict[str, Any]] = []

    async def _essayer(fonction_source):
        try:
            stats = await fonction_source(nom_equipe)
            if stats:
                resultats.append(stats)
        except Exception:
            # Une source qui échoue ne doit jamais faire planter toute la requête
            pass

    await asyncio.gather(
        _essayer(sofascore.get_team_stats),
        _essayer(besoccer.get_team_stats),
        _essayer(flashscore.get_team_stats),
    )

    return resultats


async def _elo_confrontation_sure(equipe1: str, equipe2: str) -> Optional[Dict[str, Any]]:
    """Ne fait jamais planter la requête si la recherche d'ELO échoue."""
    try:
        return await besoccer.get_elo_confrontation(equipe1, equipe2)
    except Exception:
        return None


@app.post("/match/analyse", response_model=MatchAnalysisResponse)
async def analyser_match(
    requete: MatchAnalysisRequest, session: Optional[Session] = Depends(db.get_session)
):
    stats1_sources, stats2_sources, elo_confrontation = await asyncio.gather(
        _stats_toutes_sources(requete.equipe1),
        _stats_toutes_sources(requete.equipe2),
        _elo_confrontation_sure(requete.equipe1, requete.equipe2),
    )

    resultat = generer_pronostic(
        requete.equipe1,
        requete.equipe2,
        requete.typeMatch,
        stats1_sources,
        stats2_sources,
        elo_confrontation=elo_confrontation,
    )

    if session:
        try:
            enregistrement = Pronostic(
                equipe1=resultat["equipe1"],
                equipe2=resultat["equipe2"],
                type_match=requete.typeMatch,
                probabilite_v1=resultat["probabiliteVictoireEquipe1"],
                probabilite_nul=resultat["probabiliteMatchNul"],
                probabilite_v2=resultat["probabiliteVictoireEquipe2"],
            )
            session.add(enregistrement)
            session.commit()
        except Exception:
            # L'historique est un bonus : un souci de DB ne doit jamais
            # empêcher l'utilisateur de recevoir son pronostic.
            session.rollback()

    return resultat


def _resultat_predit(p_v1: float, p_nul: float, p_v2: float) -> str:
    if p_v1 >= p_nul and p_v1 >= p_v2:
        return "V1"
    if p_v2 >= p_nul and p_v2 >= p_v1:
        return "V2"
    return "NUL"


@app.get("/historique")
def lister_historique(limite: int = 20, session: Optional[Session] = Depends(db.get_session)):
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    lignes = session.exec(
        select(Pronostic).order_by(Pronostic.cree_le.desc()).limit(limite)
    ).all()
    return lignes


@app.patch("/historique/{pronostic_id}")
def enregistrer_resultat_reel(
    pronostic_id: int, resultat_reel: str, session: Optional[Session] = Depends(db.get_session)
):
    """
    Renseigne le résultat réel d'un match une fois connu (saisie manuelle
    pour l'instant — une vérification automatique via re-scraping des
    scores finaux est une amélioration possible pour plus tard).

    resultat_reel doit valoir "V1", "NUL" ou "V2".
    """
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    if resultat_reel not in ("V1", "NUL", "V2"):
        raise HTTPException(400, "resultat_reel doit être 'V1', 'NUL' ou 'V2'")

    ligne = session.get(Pronostic, pronostic_id)
    if not ligne:
        raise HTTPException(404, "Pronostic introuvable")

    predit = _resultat_predit(ligne.probabilite_v1, ligne.probabilite_nul, ligne.probabilite_v2)

    ligne.resultat_reel = resultat_reel
    ligne.verifie = True
    ligne.correct = predit == resultat_reel

    session.add(ligne)
    session.commit()
    session.refresh(ligne)
    return ligne


@app.get("/historique/stats")
def stats_fiabilite(session: Optional[Session] = Depends(db.get_session)):
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    verifies = session.exec(select(Pronostic).where(Pronostic.verifie == True)).all()  # noqa: E712
    total = len(verifies)
    corrects = sum(1 for p in verifies if p.correct)

    return {
        "total_pronostics_verifies": total,
        "pronostics_corrects": corrects,
        "taux_reussite": round(corrects / total, 3) if total > 0 else None,
    }
