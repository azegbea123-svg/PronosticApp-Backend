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
