from pydantic import BaseModel
from typing import List


class MatchAnalysisRequest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str


class MatchAnalysisResponse(BaseModel):
    equipe1: str
    equipe2: str
    probabiliteVictoireEquipe1: float
    probabiliteMatchNul: float
    probabiliteVictoireEquipe2: float
    facteursCles: List[str]
    resumeAnalyse: str
