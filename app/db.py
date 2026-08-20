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
from sqlalchemy import inspect, text

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


def migrer_schema() -> None:
    """
    Migration légère et idempotente (pas d'Alembic ici, volontairement
    simple) : ajoute les colonnes manquantes sur des tables qui
    existaient DÉJÀ avant l'ajout d'un nouveau champ au modèle.

    `create_all()` ne crée que les tables totalement absentes — il ne
    modifie jamais une table déjà existante. Sans cette étape, une table
    créée avant l'introduction d'un champ garde l'ancien schéma en base,
    ce qui provoque une erreur SQL (colonne inexistante) à la moindre
    requête qui y fait référence.
    """
    if not engine:
        return

    inspecteur = inspect(engine)
    tables_existantes = inspecteur.get_table_names()

    colonnes_a_verifier = {
        "pronostic": [("telephone", "VARCHAR")],
    }

    with engine.connect() as connexion:
        for nom_table, colonnes in colonnes_a_verifier.items():
            if nom_table not in tables_existantes:
                continue  # table entièrement nouvelle : create_all() s'en charge déjà

            colonnes_presentes = {c["name"] for c in inspecteur.get_columns(nom_table)}
            for nom_colonne, type_sql in colonnes:
                if nom_colonne not in colonnes_presentes:
                    connexion.execute(
                        text(f"ALTER TABLE {nom_table} ADD COLUMN {nom_colonne} {type_sql}")
                    )
                    connexion.commit()


def get_session() -> Generator[Optional[Session], None, None]:
    if not engine:
        yield None
        return
    with Session(engine) as session:
        yield session
