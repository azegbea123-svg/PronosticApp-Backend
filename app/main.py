import asyncio
import os
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .models import MatchAnalysisRequest, MatchAnalysisResponse
from .sources import sofascore, besoccer, flashscore
from .analysis import generer_pronostic
from . import db
from . import repo
from . import paygate
from . import auth

# 💎 Config VIP — modifiable directement ici.
PRIX_VIP_FCFA = 500
DUREE_VIP_JOURS = 3
LIMITE_GRATUITE_QUOTIDIENNE = 3

# ⚠️ Mot de passe admin simple (pas un vrai système d'auth). À définir en
# variable d'environnement sur Render plutôt que de garder la valeur par
# défaut ci-dessous en production.
MOT_DE_PASSE_ADMIN = os.environ.get("ADMIN_PASSWORD", "change-moi")

app = FastAPI(title="PronosticApp API")

# CORS ouvert : simple pour un backend consommé uniquement par l'appli Android.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health():
    """Endpoint de santé, utile pour vérifier que le déploiement fonctionne."""
    return {"status": "ok", "service": "PronosticApp API"}


@app.get("/debug/db")
def debug_db():
    """🔧 Diagnostic : état réel de la connexion Firestore."""
    client = db.get_client()
    resultat: Dict[str, Any] = {"firestore_configure": client is not None}
    if client:
        try:
            historique = repo.lister_historique(limite=1)
            resultat["connexion_ok"] = True
            resultat["exemple_lecture_ok"] = True
        except Exception as e:
            resultat["connexion_ok"] = False
            resultat["erreur"] = f"{type(e).__name__}: {e}"
    return resultat


@app.get("/debug/elo")
async def debug_elo(equipe1: str, equipe2: str):
    """🔧 Diagnostic temporaire pour la recherche d'ELO/confrontation directe."""
    try:
        resultat = await besoccer.get_elo_confrontation(equipe1, equipe2)
        return {"equipe1": equipe1, "equipe2": equipe2, "resultat": resultat}
    except Exception as e:
        return {"equipe1": equipe1, "equipe2": equipe2, "erreur": f"{type(e).__name__}: {e}"}


@app.get("/debug/besoccer")
async def debug_besoccer(equipe: str):
    """🔧 Diagnostic temporaire, adapté à la version basée sur les slugs d'équipe."""
    import httpx
    from .sources.besoccer import _candidats_slug, TEAM_URL, HEADERS, _extraire_forme_recente

    resultat: Dict[str, Any] = {
        "equipe": equipe,
        "candidats_slug": _candidats_slug(equipe),
        "essais": [],
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for slug in _candidats_slug(equipe):
            url = TEAM_URL.format(slug=slug)
            try:
                r = await client.get(url, headers=HEADERS, timeout=10.0)
                essai: Dict[str, Any] = {"url": url, "status_code": r.status_code}
                if r.status_code == 200:
                    stats = _extraire_forme_recente(r.text)
                    essai["stats_extraites"] = stats
                    resultat["essais"].append(essai)
                    break
                resultat["essais"].append(essai)
            except Exception as e:
                resultat["essais"].append({"url": url, "erreur": f"{type(e).__name__}: {e}"})

    return resultat


@app.get("/debug/sofascore")
async def debug_sofascore(equipe: str):
    """🔧 Diagnostic : Sofascore reste bloqué (403) depuis l'IP de Render, gardé pour vérifier si ça change un jour."""
    import httpx
    from .sources.sofascore import BASE, HEADERS

    resultat: Dict[str, Any] = {"equipe": equipe}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{BASE}/search/all?q={equipe}", headers=HEADERS, timeout=10.0)
            resultat["search_status_code"] = r.status_code
            resultat["search_body_extrait"] = r.text[:500]
        except Exception as e:
            resultat["search_erreur"] = f"{type(e).__name__}: {e}"
    return resultat


async def _stats_toutes_sources(nom_equipe: str) -> List[Dict[str, Any]]:
    """
    Interroge TOUTES les sources en parallèle et renvoie la liste de
    celles qui ont réussi. La fusion est faite ensuite dans analysis.py.
    """
    resultats: List[Dict[str, Any]] = []

    async def _essayer(fonction_source):
        try:
            stats = await fonction_source(nom_equipe)
            if stats:
                resultats.append(stats)
        except Exception:
            pass

    # ⚠️ Sofascore retiré : bloqué en permanence (403) depuis l'IP de Render.
    await asyncio.gather(
        _essayer(besoccer.get_team_stats),
        _essayer(flashscore.get_team_stats),
    )
    return resultats


async def _elo_confrontation_sure(equipe1: str, equipe2: str) -> Optional[Dict[str, Any]]:
    """Plafonné à 12s : évite qu'une recherche ELO en cascade fasse traîner la requête."""
    try:
        return await asyncio.wait_for(
            besoccer.get_elo_confrontation(equipe1, equipe2), timeout=12.0
        )
    except (asyncio.TimeoutError, Exception):
        return None


def _normaliser_telephone(telephone: str) -> str:
    """
    Normalise un numéro de téléphone en identifiant stable : retire les
    espaces et le signe '+' (qui peut être mal interprété dans une query
    string HTTP). Ex: "+228 90 00 00 00" -> "22890000000"
    """
    return telephone.replace(" ", "").replace("+", "").strip()


def _resultat_predit(p_v1: float, p_nul: float, p_v2: float) -> str:
    if p_v1 >= p_nul and p_v1 >= p_v2:
        return "V1"
    if p_v2 >= p_nul and p_v2 >= p_v1:
        return "V2"
    return "NUL"


@app.post("/match/analyse", response_model=MatchAnalysisResponse)
async def analyser_match(requete: MatchAnalysisRequest, uid: str = Depends(auth.utilisateur_courant)):
    est_vip = False
    pronostics_restants: Optional[int] = None

    if db.get_client():
        utilisateur = repo.obtenir_utilisateur(uid)
        est_vip = repo.est_vip(utilisateur)

        if not est_vip:
            deja_utilises = repo.pronostics_utilises_aujourdhui(uid)
            if deja_utilises >= LIMITE_GRATUITE_QUOTIDIENNE:
                raise HTTPException(
                    429,
                    f"Limite gratuite de {LIMITE_GRATUITE_QUOTIDIENNE} pronostics par jour "
                    f"atteinte. Passe en VIP pour un accès illimité.",
                )
            pronostics_restants = LIMITE_GRATUITE_QUOTIDIENNE - deja_utilises - 1

    stats1_sources, stats2_sources, elo_confrontation = await asyncio.gather(
        _stats_toutes_sources(requete.equipe1),
        _stats_toutes_sources(requete.equipe2),
        _elo_confrontation_sure(requete.equipe1, requete.equipe2),
    )

    resultat = generer_pronostic(
        requete.equipe1,
        requete.equipe2,
        requete.typeMatch,
        stats1_sources,
        stats2_sources,
        elo_confrontation=elo_confrontation,
    )
    resultat["vip"] = est_vip
    resultat["pronosticsRestantsAujourdhui"] = pronostics_restants

    try:
        repo.enregistrer_pronostic(
            uid=uid,
            equipe1=resultat["equipe1"],
            equipe2=resultat["equipe2"],
            type_match=requete.typeMatch,
            probabilite_v1=resultat["probabiliteVictoireEquipe1"],
            probabilite_nul=resultat["probabiliteMatchNul"],
            probabilite_v2=resultat["probabiliteVictoireEquipe2"],
        )
    except Exception:
        # L'historique est un bonus : un souci de DB ne doit jamais
        # empêcher l'utilisateur de recevoir son pronostic.
        pass

    return resultat


class ProfilRequest(BaseModel):
    telephone: Optional[str] = None


class PaiementRequest(BaseModel):
    telephone: str  # requis ici : PayGate a besoin d'un vrai numéro pour débiter
    reseau: str  # "TMONEY" ou "FLOOZ"


class ConfirmationRequest(BaseModel):
    txReference: str


@app.get("/profil")
def obtenir_profil(uid: str = Depends(auth.utilisateur_courant)):
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    utilisateur = repo.obtenir_utilisateur(uid) or {}
    vip = repo.est_vip(utilisateur)

    return {
        "uid": uid,
        "email": utilisateur.get("email"),
        "telephone": utilisateur.get("telephone"),
        "vip": vip,
        "vipExpireLe": utilisateur["vip_expire_le"].isoformat() if utilisateur.get("vip_expire_le") else None,
        "pronosticsUtilisesAujourdhui": repo.pronostics_utilises_aujourdhui(uid),
        "limiteQuotidienneGratuite": LIMITE_GRATUITE_QUOTIDIENNE,
    }


@app.post("/profil")
def maj_profil(requete: ProfilRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    Renseigne/actualise le numéro de téléphone du compte — une simple
    info de contact bonus, jamais l'identifiant du compte.
    """
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    telephone = _normaliser_telephone(requete.telephone) if requete.telephone else None
    repo.creer_ou_maj_profil(uid, telephone=telephone)
    return {"ok": True}


_MOIS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def _formater_date_fr(dt: datetime) -> str:
    """Formate une date en français sans dépendre de la locale du serveur (peu fiable)."""
    return f"{dt.day} {_MOIS_FR[dt.month - 1]} {dt.year} à {dt.hour:02d}:{dt.minute:02d}"


@app.get("/vip/statut")
def vip_statut(uid: str = Depends(auth.utilisateur_courant)):
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    utilisateur = repo.obtenir_utilisateur(uid)
    vip = repo.est_vip(utilisateur)
    expire_le = utilisateur.get("vip_expire_le") if utilisateur else None

    jours_restants = None
    expire_le_affichage = None
    if vip and expire_le:
        delta = expire_le - datetime.now(timezone.utc)
        # +1 pour arrondir "vers le haut" : il reste un peu de la journée en cours en plus
        jours_restants = max(0, delta.days + (1 if delta.seconds > 0 else 0))
        expire_le_affichage = _formater_date_fr(expire_le)

    return {
        "vip": vip,
        "vipExpireLe": expire_le.isoformat() if expire_le else None,
        "vipExpireLeAffichage": expire_le_affichage,
        "vipJoursRestants": jours_restants,
        "pronosticsUtilisesAujourdhui": repo.pronostics_utilises_aujourdhui(uid),
        "limiteQuotidienneGratuite": LIMITE_GRATUITE_QUOTIDIENNE,
    }


@app.post("/vip/payer")
async def vip_payer(requete: PaiementRequest, uid: str = Depends(auth.utilisateur_courant)):
    """Lance un paiement Mobile Money via l'API officielle PayGate Global."""
    if requete.reseau not in ("TMONEY", "FLOOZ"):
        raise HTTPException(400, "reseau doit être 'TMONEY' ou 'FLOOZ'")

    telephone = _normaliser_telephone(requete.telephone)
    reference = await paygate.initier_paiement(telephone, PRIX_VIP_FCFA, requete.reseau)
    if not reference:
        raise HTTPException(502, "Paiement non initialisé — service de paiement indisponible")

    # Bonus : on garde ce numéro sur le profil pour pouvoir contacter
    # l'utilisateur au besoin — ça ne devient jamais son identifiant.
    repo.creer_ou_maj_profil(uid, telephone=telephone)

    return {"txReference": reference, "montant": PRIX_VIP_FCFA, "dureeJours": DUREE_VIP_JOURS}


@app.post("/vip/confirmer")
async def vip_confirmer(requete: ConfirmationRequest, uid: str = Depends(auth.utilisateur_courant)):
    """À appeler après /vip/payer pour vérifier et activer le VIP si payé."""
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    confirme = await paygate.verifier_paiement(requete.txReference)
    if not confirme:
        return {"confirme": False}

    utilisateur = repo.obtenir_utilisateur(uid)
    maintenant = datetime.now(timezone.utc)

    base = (
        utilisateur["vip_expire_le"]
        if (utilisateur and utilisateur.get("vip_expire_le") and utilisateur["vip_expire_le"] > maintenant)
        else maintenant
    )
    nouvelle_expiration = base + timedelta(days=DUREE_VIP_JOURS)
    repo.definir_expiration_vip(uid, nouvelle_expiration)

    return {"confirme": True, "vipExpireLe": nouvelle_expiration.isoformat()}


@app.get("/historique")
def lister_historique(limite: int = 20, uid: str = Depends(auth.utilisateur_courant)):
    """Historique PERSONNEL de l'utilisateur connecté (plus une vue globale)."""
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    return repo.lister_historique_utilisateur(uid, limite)


@app.patch("/historique/{pronostic_id}")
def enregistrer_resultat_reel(pronostic_id: str, resultat_reel: str):
    """
    Renseigne le résultat réel d'un match une fois connu (activation
    manuelle en complément de la vérification automatique).
    resultat_reel doit valoir "V1", "NUL" ou "V2".
    """
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    if resultat_reel not in ("V1", "NUL", "V2"):
        raise HTTPException(400, "resultat_reel doit être 'V1', 'NUL' ou 'V2'")

    ligne = repo.obtenir_pronostic(pronostic_id)
    if not ligne:
        raise HTTPException(404, "Pronostic introuvable")

    predit = _resultat_predit(ligne["probabilite_v1"], ligne["probabilite_nul"], ligne["probabilite_v2"])
    correct = predit == resultat_reel
    repo.marquer_pronostic_verifie(pronostic_id, resultat_reel, correct)

    return {"id": pronostic_id, "resultatReel": resultat_reel, "correct": correct}


@app.get("/historique/stats")
def stats_fiabilite():
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    stats = repo.stats_fiabilite()
    total = stats["total"]
    corrects = stats["corrects"]

    return {
        "total_pronostics_verifies": total,
        "pronostics_corrects": corrects,
        "taux_reussite": round(corrects / total, 3) if total > 0 else None,
    }


async def _verifier_un_pronostic(p: Dict[str, Any]) -> bool:
    """
    Tente de vérifier UN pronostic en re-consultant BeSoccer. Renvoie True
    si un résultat a été trouvé et enregistré, False sinon (match pas
    encore joué, ou dernier match contre quelqu'un d'autre pour l'instant).
    """
    resultat_equipe1 = await besoccer.verifier_dernier_match(p["equipe1"], p["equipe2"])
    if resultat_equipe1 is None:
        return False

    correspondance = {"V": "V1", "N": "NUL", "D": "V2"}
    resultat_reel = correspondance[resultat_equipe1]
    predit = _resultat_predit(p["probabilite_v1"], p["probabilite_nul"], p["probabilite_v2"])

    repo.marquer_pronostic_verifie(p["id"], resultat_reel, predit == resultat_reel)
    return True


@app.post("/taches/verifier-resultats")
async def tache_verifier_resultats(limite: int = 20):
    """
    Vérification AUTOMATIQUE des pronostics en attente — à appeler
    périodiquement par un déclencheur externe (ex: cron-job.org).
    """
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    en_attente = repo.lister_pronostics_non_verifies(limite)

    nouvellement_verifies = 0
    for p in en_attente:
        try:
            if await _verifier_un_pronostic(p):
                nouvellement_verifies += 1
        except Exception:
            continue

    return {
        "pronostics_examines": len(en_attente),
        "nouvellement_verifies": nouvellement_verifies,
    }


# ==== Codes VIP (porte dérobée admin) ====

class GenererCodeRequest(BaseModel):
    duree_jours: int
    mot_de_passe_admin: str


class ActiverCodeRequest(BaseModel):
    code: str


def _generer_code_aleatoire(longueur: int = 8) -> str:
    caracteres = string.ascii_uppercase + string.digits
    return "".join(random.choice(caracteres) for _ in range(longueur))


@app.post("/admin/codes-vip")
def admin_generer_code(requete: GenererCodeRequest):
    """Génère un code VIP activable manuellement — porte dérobée admin."""
    if requete.mot_de_passe_admin != MOT_DE_PASSE_ADMIN:
        raise HTTPException(403, "Mot de passe admin incorrect")
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    if requete.duree_jours <= 0:
        raise HTTPException(400, "duree_jours doit être positif")

    for _ in range(5):
        code = _generer_code_aleatoire()
        if not repo.code_existe(code):
            break
    else:
        raise HTTPException(500, "Impossible de générer un code unique, réessaie")

    repo.creer_code_vip(code, requete.duree_jours)
    return {"code": code, "dureeJours": requete.duree_jours}


@app.get("/admin/codes-vip")
def admin_lister_codes(mot_de_passe_admin: str):
    if mot_de_passe_admin != MOT_DE_PASSE_ADMIN:
        raise HTTPException(403, "Mot de passe admin incorrect")
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    return repo.lister_codes_vip()


@app.post("/vip/activer-code")
def vip_activer_code(requete: ActiverCodeRequest, uid: str = Depends(auth.utilisateur_courant)):
    """Active le VIP sur le compte connecté, à partir d'un code généré par l'admin."""
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    code = requete.code.strip().upper()
    entree_code = repo.obtenir_code_vip(code)
    if not entree_code:
        raise HTTPException(404, "Code invalide")
    if entree_code.get("utilise"):
        raise HTTPException(409, "Ce code a déjà été utilisé")

    maintenant = datetime.now(timezone.utc)

    utilisateur = repo.obtenir_utilisateur(uid)
    base = (
        utilisateur["vip_expire_le"]
        if (utilisateur and utilisateur.get("vip_expire_le") and utilisateur["vip_expire_le"] > maintenant)
        else maintenant
    )
    nouvelle_expiration = base + timedelta(days=entree_code["duree_jours"])

    repo.definir_expiration_vip(uid, nouvelle_expiration)
    repo.marquer_code_utilise(code, uid)

    return {"active": True, "vipExpireLe": nouvelle_expiration.isoformat()}
