"""
Connexion Firestore — remplace l'ancienne couche Postgres/SQLModel.
Fini l'expiration à 90 jours du plan Postgres gratuit de Render.

Recherche le fichier de clé de service à deux emplacements possibles :
  - En production sur Render : /etc/secrets/firebase-service-account.json
    (Secret File Render, jamais commité sur Git)
  - En local : firebase-service-account.json à la racine du projet
    (listé dans .gitignore)

Si aucun des deux n'est trouvé, l'app démarre quand même (comme avant
avec DATABASE_URL absente) : les fonctionnalités liées à la base
renvoient une erreur claire plutôt que de faire planter le serveur.
"""

import os
from typing import Optional
import firebase_admin
from firebase_admin import credentials, firestore

_CHEMINS_POSSIBLES = [
    "/etc/secrets/firebase-service-account.json",  # Render Secret File
    "firebase-service-account.json",  # local, à la racine du projet
]

_client = None
_tentative_initialisation_faite = False


def get_client():
    """Renvoie le client Firestore, ou None si non configuré/erreur."""
    global _client, _tentative_initialisation_faite

    if _client is not None:
        return _client
    if _tentative_initialisation_faite:
        return None  # déjà tenté et échoué, pas la peine de réessayer à chaque appel

    _tentative_initialisation_faite = True

    chemin_cle = next((c for c in _CHEMINS_POSSIBLES if os.path.exists(c)), None)
    if not chemin_cle:
        print("⚠️ Aucun fichier de clé Firebase trouvé — fonctionnalités DB désactivées")
        return None

    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(chemin_cle)
            firebase_admin.initialize_app(cred)
        _client = firestore.client()
        return _client
    except Exception as e:
        print(f"⚠️ Erreur d'initialisation Firestore (non bloquante) : {e}")
        return None


def base_de_donnees_configuree() -> bool:
    return get_client() is not None
