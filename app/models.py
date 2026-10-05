from pydantic import BaseModel
from typing import List, Optional, Dict, Any


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
    avisFiabiliteV1: str
    avisFiabiliteNul: str
    avisFiabiliteV2: str
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
    pronosticsRestantsAujourdhui: Optional[int] = None
    # Enrichissements provenant des endpoints RapidAPI — facultatifs.
    predictionExterne: Optional[str] = None
    sourcePredictionExterne: Optional[str] = None
    probabilitePredictionExterne: Optional[float] = None
    confianceGlobale: Optional[float] = None

class MatchDetailResponse(BaseModel):
    fixture_id: str
    event_id: Optional[int] = None
    date: Optional[str] = None
    league: Optional[str] = None
    equipe1: str
    equipe2: str
    statut: Optional[Dict[str, Any]] = None
    score: List[Dict[str, Any]] = []
    statistiques: List[Dict[str, Any]] = []
    statistiquesTop: List[Dict[str, Any]] = []
    confrontations: Optional[Dict[str, Any]] = None
    h2hRecents: List[Dict[str, Any]] = []
    compositions: Dict[str, Any] = {}
    formations: Dict[str, Any] = {}
    detail: Optional[Dict[str, Any]] = None

