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

    # Optionnel pour ne pas casser les lignes déjà en base créées avant
    # l'introduction du système VIP/quota.
    telephone: Optional[str] = Field(default=None, index=True)

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


class Utilisateur(SQLModel, table=True):
    telephone: str = Field(primary_key=True)
    vip_expire_le: Optional[datetime] = None
    cree_le: datetime = Field(default_factory=datetime.utcnow)


class CodeVip(SQLModel, table=True):
    code: str = Field(primary_key=True)
    duree_jours: int
    utilise: bool = False
    telephone_utilisateur: Optional[str] = None
    cree_le: datetime = Field(default_factory=datetime.utcnow)
    utilise_le: Optional[datetime] = None
