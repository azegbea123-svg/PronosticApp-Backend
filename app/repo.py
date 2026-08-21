"""
Couche d'accès aux données Firestore. Remplace toutes les anciennes
requêtes SQLModel/SQL par des opérations Firestore équivalentes.

⚠️ Choix de conception important : plusieurs requêtes ci-dessous
évitent volontairement de combiner un filtre d'égalité ET un tri/filtre
d'intervalle sur des champs différents dans une même requête Firestore
— ça évite d'avoir besoin de créer manuellement un "index composite"
dans la console Firebase (une étape de configuration en plus, source
d'erreurs faciles à éviter). À la place, on filtre le résultat côté
Python après une requête plus simple. Le volume de données de cette
appli reste largement assez petit pour que ce soit sans impact réel
sur les performances.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from . import db

COLLECTION_PRONOSTICS = "pronostics"
COLLECTION_UTILISATEURS = "utilisateurs"
COLLECTION_CODES_VIP = "codes_vip"


def _maintenant():
    return datetime.now(timezone.utc)


def _debut_journee():
    return _maintenant().replace(hour=0, minute=0, second=0, microsecond=0)


# ==== Pronostics ====

def enregistrer_pronostic(
    telephone: str,
    equipe1: str,
    equipe2: str,
    type_match: str,
    probabilite_v1: float,
    probabilite_nul: float,
    probabilite_v2: float,
) -> Optional[str]:
    client = db.get_client()
    if not client:
        return None

    doc_ref = client.collection(COLLECTION_PRONOSTICS).document()
    doc_ref.set(
        {
            "telephone": telephone,
            "equipe1": equipe1,
            "equipe2": equipe2,
            "type_match": type_match,
            "probabilite_v1": probabilite_v1,
            "probabilite_nul": probabilite_nul,
            "probabilite_v2": probabilite_v2,
            "resultat_reel": None,
            "verifie": False,
            "correct": None,
            "cree_le": _maintenant(),
        }
    )
    return doc_ref.id


def pronostics_utilises_aujourdhui(telephone: str) -> int:
    client = db.get_client()
    if not client:
        return 0

    debut_jour = _debut_journee()
    # Filtre uniquement sur telephone (égalité simple, pas de composite
    # nécessaire) puis on compte côté Python ceux du jour.
    docs = client.collection(COLLECTION_PRONOSTICS).where("telephone", "==", telephone).stream()
    return sum(1 for d in docs if (d.get("cree_le") or debut_jour) >= debut_jour)


def lister_historique(limite: int = 20) -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []

    query = (
        client.collection(COLLECTION_PRONOSTICS)
        .order_by("cree_le", direction="DESCENDING")
        .limit(limite)
    )
    resultats = []
    for doc in query.stream():
        d = doc.to_dict()
        d["id"] = doc.id
        resultats.append(d)
    return resultats


def obtenir_pronostic(pronostic_id: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_PRONOSTICS).document(pronostic_id).get()
    if not doc.exists:
        return None
    d = doc.to_dict()
    d["id"] = doc.id
    return d


def marquer_pronostic_verifie(pronostic_id: str, resultat_reel: str, correct: bool) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_PRONOSTICS).document(pronostic_id).update(
        {"resultat_reel": resultat_reel, "verifie": True, "correct": correct}
    )


def lister_pronostics_non_verifies(limite: int = 20) -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []
    query = (
        client.collection(COLLECTION_PRONOSTICS)
        .where("verifie", "==", False)
        .limit(limite)
    )
    resultats = []
    for doc in query.stream():
        d = doc.to_dict()
        d["id"] = doc.id
        resultats.append(d)
    return resultats


def stats_fiabilite() -> Dict[str, Any]:
    client = db.get_client()
    if not client:
        return {"total": 0, "corrects": 0}

    docs = client.collection(COLLECTION_PRONOSTICS).where("verifie", "==", True).stream()
    total = 0
    corrects = 0
    for doc in docs:
        total += 1
        if doc.to_dict().get("correct"):
            corrects += 1
    return {"total": total, "corrects": corrects}


# ==== Utilisateurs (statut VIP) ====

def obtenir_utilisateur(telephone: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_UTILISATEURS).document(telephone).get()
    return doc.to_dict() if doc.exists else None


def definir_expiration_vip(telephone: str, expiration: datetime) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_UTILISATEURS).document(telephone).set(
        {"vip_expire_le": expiration, "cree_le": _maintenant()}, merge=True
    )


def est_vip(utilisateur: Optional[Dict[str, Any]]) -> bool:
    if not utilisateur or not utilisateur.get("vip_expire_le"):
        return False
    return utilisateur["vip_expire_le"] > _maintenant()


# ==== Codes VIP (activation manuelle / admin) ====

def creer_code_vip(code: str, duree_jours: int) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_CODES_VIP).document(code).set(
        {
            "duree_jours": duree_jours,
            "utilise": False,
            "telephone_utilisateur": None,
            "cree_le": _maintenant(),
            "utilise_le": None,
        }
    )


def code_existe(code: str) -> bool:
    client = db.get_client()
    if not client:
        return False
    return client.collection(COLLECTION_CODES_VIP).document(code).get().exists


def obtenir_code_vip(code: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_CODES_VIP).document(code).get()
    return doc.to_dict() if doc.exists else None


def lister_codes_vip() -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []
    query = client.collection(COLLECTION_CODES_VIP).order_by("cree_le", direction="DESCENDING")
    resultats = []
    for doc in query.stream():
        d = doc.to_dict()
        d["code"] = doc.id
        resultats.append(d)
    return resultats


def marquer_code_utilise(code: str, telephone: str) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_CODES_VIP).document(code).update(
        {"utilise": True, "telephone_utilisateur": telephone, "utilise_le": _maintenant()}
    )
