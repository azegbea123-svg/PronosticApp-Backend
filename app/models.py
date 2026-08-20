from pydantic import BaseModel
from typing import List


class MatchAnalysisRequest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str
    telephone: str  # identifiant utilisateur simple, pour le suivi du quota gratuit / statut VIP


class ScoreProbable(BaseModel):
    score: str
    probabilite: float


class MatchAnalysisResponse(BaseModel):
    equipe1: str
    equipe2: str
    probabiliteVictoireEquipe1: float
    probabiliteMatchNul: float
    probabiliteVictoireEquipe2: float
    facteursCles: List[str]
    resumeAnalyse: str
    butsAttendusEquipe1: float
    butsAttendusEquipe2: float
    probabiliteBTTS: float
    probabiliteOver05: float
    probabiliteOver15: float
    probabiliteOver25: float
    scoresProbables: List[ScoreProbable]
