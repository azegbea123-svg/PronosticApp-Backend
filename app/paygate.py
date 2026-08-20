"""
Intégration DIRECTE avec PayGate Global (paygateglobal.com), suivant
exactement leur fiche d'intégration officielle. Remplace l'ancien
wrapper qui passait par un micro-service intermédiaire
(paygate-api.onrender.com) responsable des erreurs 502 répétées — un
point de défaillance en moins.

Endpoints officiels utilisés :
  POST https://paygateglobal.com/api/v1/pay
    {auth_token, phone_number, amount, description, identifier, network}
    -> {tx_reference, status}   (status 0 = enregistré avec succès)

  POST https://paygateglobal.com/api/v1/status
    {auth_token, tx_reference}
    -> {tx_reference, status, ...}   (status 0 = paiement réussi)

Codes de statut PayGate Global (identiques pour /pay et /status) :
  0 = succès | 2 = jeton invalide / paiement en cours (contexte-dépendant)
  4 = paramètres invalides / expiré | 6 = doublon / annulé
"""

import os
import uuid
from typing import Optional, Dict, Any
import httpx

# ⚠️ SÉCURITÉ : à terme, configure PAYGATE_AUTH_TOKEN comme variable
# d'environnement sur Render plutôt que de laisser la clé en clair dans
# le code source (surtout si ce dépôt est ou devient public un jour).
# La valeur ci-dessous ne sert que de repli si la variable n'est pas définie.
AUTH_TOKEN = os.environ.get("PAYGATE_AUTH_TOKEN", "a407ceed-d492-4102-b8ba-fa54630e427e")

BASE_URL = "https://paygateglobal.com/api/v1"


async def initier_paiement(telephone: str, montant: int, reseau: str) -> Optional[str]:
    """
    Lance un paiement Mobile Money. Renvoie la référence de transaction
    (tx_reference) si la transaction a bien été enregistrée, sinon None.
    """
    # 'identifier' doit être unique par transaction côté e-commerce —
    # un UUID garantit ça sans avoir à gérer de compteur.
    identifiant_unique = str(uuid.uuid4())

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{BASE_URL}/pay",
                json={
                    "auth_token": AUTH_TOKEN,
                    "phone_number": telephone,
                    "amount": montant,
                    "description": "Acces VIP PronosticApp",
                    "identifier": identifiant_unique,
                    "network": reseau,
                },
                timeout=15.0,
            )
            data = r.json()
            if data.get("status") == 0 and data.get("tx_reference"):
                return data["tx_reference"]
            return None
        except Exception:
            return None


async def verifier_paiement(tx_reference: str) -> bool:
    """Vérifie si un paiement a été confirmé (status 0 = payé avec succès)."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{BASE_URL}/status",
                json={"auth_token": AUTH_TOKEN, "tx_reference": tx_reference},
                timeout=15.0,
            )
            data = r.json()
            return data.get("status") == 0
        except Exception:
            return False
