"""
Authentification — vérifie les jetons Firebase envoyés par l'appli
Android (email/mot de passe ou connexion Google, peu importe : Firebase
Auth produit le même type de jeton dans les deux cas).

L'identité de l'utilisateur est désormais son `uid` Firebase, plus
jamais son numéro de téléphone — le téléphone devient une simple info
de contact optionnelle, collectée au moment du paiement VIP.
"""

from typing import Optional
from fastapi import Header, HTTPException
from firebase_admin import auth as firebase_auth

from . import db


def _decoder_token(id_token: str) -> Optional[dict]:
    if not db.get_client():  # l'app Firebase Admin n'est initialisée que si la clé est configurée
        return None
    try:
        return firebase_auth.verify_id_token(id_token)
    except Exception:
        return None


async def utilisateur_courant(authorization: Optional[str] = Header(None)) -> str:
    """
    Dépendance FastAPI : vérifie le jeton envoyé dans l'en-tête
    'Authorization: Bearer <jeton>' et renvoie l'uid de l'utilisateur.
    Lève une 401 si l'en-tête est absent ou le jeton invalide/expiré.

    Enregistre aussi l'email sur le profil au passage (idempotent —
    ça garde le profil Firestore synchronisé avec Firebase Auth sans
    action supplémentaire nécessaire côté appli).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentification requise (connecte-toi d'abord)")

    token = authorization.removeprefix("Bearer ").strip()
    decoded = _decoder_token(token)
    if not decoded:
        raise HTTPException(401, "Session invalide ou expirée, reconnecte-toi")

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
