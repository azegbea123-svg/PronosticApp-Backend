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
COLLECTION_PREDICTIONS_BACKTEST = "predictions_backtest"
COLLECTION_MATCHS_JOUR = "matchs_jour"
COLLECTION_MATCHS_JOUR_META = "matchs_jour_meta"


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
    """
    Enregistre un pronostic — sert UNIQUEMENT au suivi du quota gratuit
    quotidien désormais (voir pronostics_utilises_aujourdhui). Le moteur
    peut aller chercher un résultat réel directement sur BeSoccer à la
    demande si besoin, donc plus la peine de stocker/vérifier nous-mêmes
    des résultats en base — ce système a été retiré pour rester simple.
    """
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
            "cree_le": _maintenant(),
        }
    )
    return doc_ref.id


def pronostics_utilises_aujourdhui(uid: str) -> int:
    client = db.get_client()
    if not client:
        return 0

    debut_jour = _debut_journee()

    try:
        query = (
            client.collection(COLLECTION_PRONOSTICS)
            .where("uid", "==", uid)
            .where("cree_le", ">=", debut_jour)
        )
        return len(list(query.stream()))
    except Exception:
        docs = client.collection(COLLECTION_PRONOSTICS).where("uid", "==", uid).stream()
        return sum(1 for d in docs if (d.get("cree_le") or debut_jour) >= debut_jour)


# ==== Utilisateurs (compte + statut VIP) ====

def obtenir_utilisateur(uid: str) -> Optional[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_UTILISATEURS).document(uid).get()
    return doc.to_dict() if doc.exists else None


def creer_ou_maj_profil(uid: str, email: Optional[str] = None, telephone: Optional[str] = None) -> None:
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


# ==== Suppression de compte (droit à l'effacement) ====

def supprimer_toutes_donnees_utilisateur(uid: str) -> None:
    client = db.get_client()
    if not client:
        return

    docs = client.collection(COLLECTION_PRONOSTICS).where("uid", "==", uid).stream()
    for doc in docs:
        doc.reference.delete()

    client.collection(COLLECTION_UTILISATEURS).document(uid).delete()


# ==== Collecte progressive de matchs pour le backtest ====

def enregistrer_prediction_backtest(
    equipe1: str,
    equipe2: str,
    type_match: str,
    slug1: Optional[str],
    slug2: Optional[str],
    probabilite_v1: float,
    probabilite_nul: float,
    probabilite_v2: float,
) -> str:
    client = db.get_client()
    if not client:
        raise RuntimeError("Firestore non configuré")

    doc_ref = client.collection(COLLECTION_PREDICTIONS_BACKTEST).document()
    doc_ref.set({
        "equipe1": equipe1,
        "equipe2": equipe2,
        "type_match": type_match,
        "slug1": slug1,
        "slug2": slug2,
        "probabilite_v1": probabilite_v1,
        "probabilite_nul": probabilite_nul,
        "probabilite_v2": probabilite_v2,
        "date_prediction": _maintenant(),
        "verifie": False,
        "resultat_reel": None,
    })
    return doc_ref.id


def lister_predictions_en_attente(limite: int = 50) -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []

    query = (
        client.collection(COLLECTION_PREDICTIONS_BACKTEST)
        .where("verifie", "==", False)
        .order_by("date_prediction")
        .limit(limite)
    )
    resultats = []
    for doc in query.stream():
        d = doc.to_dict()
        d["id"] = doc.id
        resultats.append(d)
    return resultats


def marquer_prediction_verifiee(prediction_id: str, resultat_reel: str) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_PREDICTIONS_BACKTEST).document(prediction_id).update({
        "verifie": True,
        "resultat_reel": resultat_reel,
        "date_verification": _maintenant(),
    })


def lister_predictions_verifiees(limite: int = 500) -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []

    query = (
        client.collection(COLLECTION_PREDICTIONS_BACKTEST)
        .where("verifie", "==", True)
        .limit(limite)
    )
    resultats = []
    for doc in query.stream():
        d = doc.to_dict()
        d["id"] = doc.id
        resultats.append(d)
    return resultats


# ==== Liste de matchs J / J+1 (nouvelle approche) ====
#
# COLLECTION_MATCHS_JOUR : un document par match (id = fixture_id
# API-Football), avec le champ "jour" (YYYY-MM-DD) pour filtrer.
# COLLECTION_MATCHS_JOUR_META : un document par jour, juste pour savoir
# QUAND la liste/disponibilité a été reconstruite pour la dernière fois
# (voir TTL_RAFRAICHISSEMENT_MATCHS_SECONDES dans config.py).

def enregistrer_matchs_jour(jour_iso: str, matchs: List[Dict[str, Any]]) -> None:
    """Remplace en une seule opération tous les matchs connus pour ce jour."""
    client = db.get_client()
    if not client:
        return
    batch = client.batch()
    for m in matchs:
        ref = client.collection(COLLECTION_MATCHS_JOUR).document(str(m["fixture_id"]))
        batch.set(ref, {**m, "jour": jour_iso, "maj_le": _maintenant()})
    batch.commit()


def lister_matchs_jour(jour_iso: str) -> List[Dict[str, Any]]:
    client = db.get_client()
    if not client:
        return []
    query = client.collection(COLLECTION_MATCHS_JOUR).where("jour", "==", jour_iso)
    resultats = [d.to_dict() for d in query.stream()]
    resultats.sort(key=lambda m: m.get("date", ""))
    return resultats


def dernier_rafraichissement_matchs(jour_iso: str) -> Optional[datetime]:
    client = db.get_client()
    if not client:
        return None
    doc = client.collection(COLLECTION_MATCHS_JOUR_META).document(jour_iso).get()
    if not doc.exists:
        return None
    return doc.to_dict().get("rafraichi_le")


def marquer_matchs_rafraichis(jour_iso: str) -> None:
    client = db.get_client()
    if not client:
        return
    client.collection(COLLECTION_MATCHS_JOUR_META).document(jour_iso).set(
        {"rafraichi_le": _maintenant()}
    )
