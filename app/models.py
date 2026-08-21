from pydantic import BaseModel
from typing import List, Optional


class MatchAnalysisRequest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str


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
    vip: bool  # true si l'utilisateur a accès aux marchés avancés (au-delà du 1X2)
    pronosticsRestantsAujourdhui: Optional[int] = None  # null si VIP (illimité) ou DB non configurée
