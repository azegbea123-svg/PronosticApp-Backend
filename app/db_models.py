"""
Table de base de données pour l'historique des pronostics.

Séparé de models.py (qui définit le contrat API JSON échangé avec l'appli
Android) : ceci est le schéma de stockage interne, pas la forme des
réponses HTTP.
"""

from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class Pronostic(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    cree_le: datetime = Field(default_factory=datetime.utcnow)

    equipe1: str
    equipe2: str
    type_match: str

    probabilite_v1: float
    probabilite_nul: float
    probabilite_v2: float

    # Rempli plus tard, une fois le résultat réel du match connu.
    # "V1" (victoire équipe1) | "NUL" | "V2" (victoire équipe2)
    resultat_reel: Optional[str] = None
    verifie: bool = False
    correct: Optional[bool] = None
