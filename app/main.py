import asyncio
from typing import Optional, Dict, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .models import MatchAnalysisRequest, MatchAnalysisResponse
from .sources import sofascore, besoccer, flashscore
from .analysis import generer_pronostic

app = FastAPI(title="PronosticApp API")

# CORS ouvert : simple pour un backend consommé uniquement par l'appli Android.
# À restreindre si un jour ce backend est aussi appelé depuis un site web public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/debug/besoccer")
async def debug_besoccer(equipe: str):
    """🔧 Diagnostic temporaire, même principe que /debug/sofascore."""
    import httpx
    from .sources.besoccer import SEARCH_URL, HEADERS

    resultat: Dict[str, Any] = {"equipe": equipe}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            r = await client.get(
                SEARCH_URL.format(query=equipe), headers=HEADERS, timeout=10.0
            )
            resultat["status_code"] = r.status_code
            resultat["body_extrait"] = r.text[:500]
        except Exception as e:
            resultat["erreur"] = f"{type(e).__name__}: {e}"

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


async def _stats_multi_sources(nom_equipe: str) -> Optional[Dict[str, Any]]:
    """
    Essaie chaque source dans l'ordre de fiabilité (Sofascore > BeSoccer > Flashscore)
    et renvoie la première qui répond avec des données exploitables.
    """
    for source in (sofascore.get_team_stats, besoccer.get_team_stats, flashscore.get_team_stats):
        try:
            stats = await source(nom_equipe)
            if stats:
                return stats
        except Exception:
            # Une source qui échoue ne doit jamais faire planter toute la requête
            continue
    return None


@app.post("/match/analyse", response_model=MatchAnalysisResponse)
async def analyser_match(requete: MatchAnalysisRequest):
    stats1, stats2 = await asyncio.gather(
        _stats_multi_sources(requete.equipe1),
        _stats_multi_sources(requete.equipe2),
    )

    resultat = generer_pronostic(
        requete.equipe1, requete.equipe2, requete.typeMatch, stats1, stats2
    )
    return resultat
