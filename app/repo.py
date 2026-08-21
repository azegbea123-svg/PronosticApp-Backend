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
COLLECTION_PAIEMENTS = "paiements_en_attente"


def _maintenant():
    return datetime.now(timezone.utc)


def _debut_journee():
    return _maintenant().replace(hour=0, minute=0, second=0, microsecond=0)


# ==== Pronostics ====

def enregistrer_pronostic(
    uid: str,
    equipe1: str,
    equipe2: str,
    type_match: str,
    probabilite_v1: float,
    probabilite_nul: float,
    probabilite_v2: float,
    telephone: Optional[str] = None,
) -> Optional[str]:
    client = db.get_client()
    if not client:
        return None

    doc_ref = client.collection(COLLECTION_PRONOSTICS).document()
    doc_ref.set(
        {
            "uid": uid,
            "telephone": telephone,  # bonus, juste pour contacter au besoin
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


def pronostics_utilises_aujourdhui(uid: str) -> int:
    client = db.get_client()
    if not client:
        return 0

    debut_jour = _debut_journee()

    # Filtrage DIRECTEMENT côté Firestore (uid + date) — beaucoup plus
    # rapide que tout récupérer et filtrer en Python, et surtout ne
    # ralentit pas au fil du temps à mesure que l'historique grossit.
    # Nécessite un index composite (uid + cree_le) — Firestore le
    # signale avec un lien direct pour le créer en un clic si absent.
    try:
        query = (
            client.collection(COLLECTION_PRONOSTICS)
            .where("uid", "==", uid)
            .where("cree_le", ">=", debut_jour)
        )
        return len(list(query.stream()))
    except Exception:
        # Repli : l'ancienne méthode (plus lente mais fonctionne toujours
        # sans configuration Firestore supplémentaire)
        docs = client.collection(COLLECTION_PRONOSTICS).where("uid", "==", uid).stream()
        return sum(1 for d in docs if (d.get("cree_le") or debut_jour) >= debut_jour)


def lister_historique_utilisateur(uid: str, limite: int = 20) -> List[Dict[str, Any]]:
    """
    Historique PERSONNEL d'un utilisateur. Essaie d'abord un tri
    directement côté Firestore (rapide, ne récupère que ce qui est
    nécessaire) ; si l'index composite correspondant n'existe pas encore,
    retombe sur l'ancienne méthode (tout récupérer puis trier en Python).
    """
    client = db.get_client()
    if not client:
        return []

    try:
        query = (
            client.collection(COLLECTION_PRONOSTICS)
            .where("uid", "==", uid)
            .order_by("cree_le", direction="DESCENDING")
            .limit(limite)
        )
        resultats = []
        for doc in query.stream():
            d = doc.to_dict()
            d["id"] = doc.id
            resultats.append(d)
        return resultats
    except Exception:
        pass

    docs = client.collection(COLLECTION_PRONOSTICS).where("uid", "==", uid).stream()
    resultats = []
    for doc in docs:
        d = doc.to_dict()
        d["id"] = doc.id
        resultats.append(d)

    resultats.sort(key=lambda d: d.get("cree_le") or _maintenant(), reverse=True)
    return resultats[:limite]


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


# ==== Utilisateurs (compte + statut VIP) ====

def obtenir_utilisateur(uid: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_UTILISATEURS).document(uid).get()
    return doc.to_dict() if doc.exists else None


def creer_ou_maj_profil(uid: str, email: Optional[str] = None, telephone: Optional[str] = None) -> None:
    """
    Crée le profil au premier contact, ou met à jour seulement les champs
    fournis sinon (merge=True — ne touche pas aux autres champs déjà en
    base, comme le statut VIP).
    """
    client = db.get_client()
    if not client:
        return

    donnees: Dict[str, Any] = {}
    if email is not None:
        donnees["email"] = email
    if telephone is not None:
        donnees["telephone"] = telephone

    if not client.collection(COLLECTION_UTILISATEURS).document(uid).get().exists:
        donnees["cree_le"] = _maintenant()

    if donnees:
        client.collection(COLLECTION_UTILISATEURS).document(uid).set(donnees, merge=True)


def definir_expiration_vip(uid: str, expiration: datetime) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_UTILISATEURS).document(uid).set(
        {"vip_expire_le": expiration}, merge=True
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
            "uid_utilisateur": None,
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


def marquer_code_utilise(code: str, uid: str) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_CODES_VIP).document(code).update(
        {"utilise": True, "uid_utilisateur": uid, "utilise_le": _maintenant()}
    )


# ==== Paiements en attente (pour le callback PayGate partagé avec LotoPredict) ====

def enregistrer_paiement_initie(tx_reference: str, uid: str, montant: int) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_PAIEMENTS).document(tx_reference).set(
        {
            "uid": uid,
            "montant": montant,
            "traite": False,
            "cree_le": _maintenant(),
        }
    )


def obtenir_paiement(tx_reference: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_PAIEMENTS).document(tx_reference).get()
    return doc.to_dict() if doc.exists else None


def marquer_paiement_traite(tx_reference: str) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_PAIEMENTS).document(tx_reference).update({"traite": True})
