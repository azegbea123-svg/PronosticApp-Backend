"""
Authentification — UN SEUL mécanisme pour tout le backend, via l'en-tête
standard "Authorization: Bearer <jeton>".

Deux types de jetons acceptés, résolus de façon transparente :
  1. Un vrai jeton Firebase (ce qu'envoie l'appli Android normalement)
     -> renvoie l'uid du compte réel.
  2. Le mot de passe admin lui-même, utilisé comme "jeton universel" de
     debug -> renvoie un uid spécial "admin_debug", qui a accès à tout
     (y compris les actions réservées à l'admin, voir _exiger_admin).

Avantage concret : Swagger (/docs) affiche un vrai bouton "Authorize" en
haut de page grâce à HTTPBearer — on colle le jeton UNE FOIS, et il
s'applique automatiquement à tous les endpoints protégés ensuite, sans
avoir à le retaper à chaque requête ni à jongler entre plusieurs
mécanismes différents selon l'endpoint.
"""

from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth as firebase_auth

from . import db
from .config import MOT_DE_PASSE_ADMIN

_schema_securite = HTTPBearer(
    description="Colle ici soit ton mot de passe admin (mode debug), soit un vrai jeton Firebase (appli)."
)

UID_ADMIN_DEBUG = "admin_debug"


def _decoder_token_firebase(id_token: str):
    if not db.get_client():  # l'app Firebase Admin n'est initialisée que si la clé est configurée
        return None
    try:
        return firebase_auth.verify_id_token(id_token)
    except Exception:
        return None


async def utilisateur_courant(
    identifiants: HTTPAuthorizationCredentials = Security(_schema_securite),
) -> str:
    """
    Dépendance FastAPI utilisée par TOUS les endpoints protégés.
    Renvoie soit l'uid réel (jeton Firebase valide), soit "admin_debug"
    (mot de passe admin utilisé comme jeton). Lève une 401 sinon.
    """
    jeton = identifiants.credentials.strip()

    if jeton == MOT_DE_PASSE_ADMIN:
        return UID_ADMIN_DEBUG

    decoded = _decoder_token_firebase(jeton)
    if not decoded:
        raise HTTPException(401, "Jeton invalide — colle soit ton mot de passe admin, soit un vrai jeton Firebase")

    uid = decoded["uid"]
    email = decoded.get("email")
    if email:
        # Import différé pour éviter un import circulaire (repo importe db, pas auth)
        from . import repo
        try:
            repo.creer_ou_maj_profil(uid, email=email)
        except Exception:
            pass  # ne doit jamais faire échouer l'authentification elle-même

    return uid


def exiger_admin(uid: str) -> None:
    """À appeler dans les endpoints réservés à l'admin (génération de codes VIP, etc.)."""
    if uid != UID_ADMIN_DEBUG:
        raise HTTPException(403, "Réservé à l'administrateur (connecte-toi avec le mot de passe admin)")


def supprimer_compte_firebase(uid: str) -> None:
    """Supprime définitivement le compte Firebase Authentication (irréversible)."""
    firebase_auth.delete_user(uid)
