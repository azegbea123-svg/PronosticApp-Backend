"""
Connexion à la base de données (Postgres en prod via Render, mais le code
fonctionne avec n'importe quelle URL SQLAlchemy — utile pour tester en
local avec SQLite sans rien installer de plus).

Si DATABASE_URL n'est pas définie (ex: développement local sans DB liée),
l'app démarre quand même : les fonctionnalités d'historique renvoient une
erreur claire au lieu de planter, mais /match/analyse continue de
fonctionner normalement.
"""

import os
from typing import Generator, Optional
from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Render (et beaucoup d'hébergeurs) fournissent une URL commençant par
# "postgres://", mais SQLAlchemy 2.x exige "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, echo=False) if DATABASE_URL else None


def base_de_donnees_configuree() -> bool:
    return engine is not None


def creer_tables() -> None:
    if engine:
        SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Optional[Session], None, None]:
    if not engine:
        yield None
        return
    with Session(engine) as session:
        yield session
