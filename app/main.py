import asyncio
import os
import random
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import Session, select

from .models import MatchAnalysisRequest, MatchAnalysisResponse
from .sources import sofascore, besoccer, flashscore
from .analysis import generer_pronostic
from .db_models import Pronostic, Utilisateur, CodeVip
from . import db
from . import paygate

# 💎 Config VIP — modifiable directement ici.
PRIX_VIP_FCFA = 500
DUREE_VIP_JOURS = 3
LIMITE_GRATUITE_QUOTIDIENNE = 3

# ⚠️ Mot de passe admin simple (pas un vrai système d'auth). À définir en
# variable d'environnement sur Render plutôt que de garder la valeur par
# défaut ci-dessous en production.
MOT_DE_PASSE_ADMIN = os.environ.get("ADMIN_PASSWORD", "change-moi")

app = FastAPI(title="PronosticApp API")


@app.on_event("startup")
def au_demarrage():
    try:
        db.creer_tables()  # ne fait rien si DATABASE_URL n'est pas configurée
    except Exception as e:
        print(f"⚠️ Erreur lors de la création des tables (non bloquante) : {e}")
    try:
        db.migrer_schema()  # ajoute les colonnes manquantes sur les tables déjà existantes
    except Exception as e:
        # Un souci de migration ne doit JAMAIS empêcher l'appli de démarrer
        # (mieux vaut tourner avec un schéma partiellement à jour que ne
        # pas tourner du tout — et ça évite un 502 permanent).
        print(f"⚠️ Erreur lors de la migration de schéma (non bloquante) : {e}")

# CORS ouvert : simple pour un backend consommé uniquement par l'appli Android.
# À restreindre si un jour ce backend est aussi appelé depuis un site web public.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/debug/elo")
async def debug_elo(equipe1: str, equipe2: str):
    """🔧 Diagnostic temporaire pour la recherche d'ELO/confrontation directe."""
    try:
        resultat = await besoccer.get_elo_confrontation(equipe1, equipe2)
        return {"equipe1": equipe1, "equipe2": equipe2, "resultat": resultat}
    except Exception as e:
        return {"equipe1": equipe1, "equipe2": equipe2, "erreur": f"{type(e).__name__}: {e}"}


@app.get("/debug/db")
def debug_db(session: Optional[Session] = Depends(db.get_session)):
    """🔧 Diagnostic temporaire : état réel de la connexion base de données."""
    resultat: Dict[str, Any] = {
        "DATABASE_URL_definie": bool(db.DATABASE_URL),
        "engine_cree": db.engine is not None,
        "session_disponible": session is not None,
    }

    if session is not None:
        try:
            nb_lignes = len(session.exec(select(Pronostic)).all())
            resultat["connexion_ok"] = True
            resultat["nb_pronostics_en_base"] = nb_lignes
        except Exception as e:
            resultat["connexion_ok"] = False
            resultat["erreur"] = f"{type(e).__name__}: {e}"

    return resultat


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
    """
    🔧 Endpoint de diagnostic TEMPORAIRE — à retirer une fois le problème
    de scraping résolu. Permet de voir ce que Sofascore répond réellement
    depuis le serveur déployé (code HTTP, début du corps de la réponse),
    sans passer par toute la logique de parsing qui masquerait l'erreur.
    """
    import httpx
    from .sources.sofascore import BASE, HEADERS

    resultat: Dict[str, Any] = {"equipe": equipe}

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                f"{BASE}/search/all?q={equipe}", headers=HEADERS, timeout=10.0
            )
            resultat["search_status_code"] = r.status_code
            resultat["search_body_extrait"] = r.text[:500]
        except Exception as e:
            resultat["search_erreur"] = f"{type(e).__name__}: {e}"

    return resultat


@app.get("/")
async def health():
    """Endpoint de santé, utile pour vérifier que le déploiement fonctionne."""
    return {"status": "ok", "service": "PronosticApp API"}


async def _stats_toutes_sources(nom_equipe: str) -> List[Dict[str, Any]]:
    """
    Interroge TOUTES les sources en parallèle (plus seulement la première
    qui répond) et renvoie la liste de celles qui ont réussi. La fusion
    (moyennes, cumuls) est faite ensuite dans analysis.py — additionner
    les compteurs bruts de plusieurs sources est mathématiquement sûr même
    si elles se recoupent sur les mêmes matchs réels.
    """
    resultats: List[Dict[str, Any]] = []

    async def _essayer(fonction_source):
        try:
            stats = await fonction_source(nom_equipe)
            if stats:
                resultats.append(stats)
        except Exception:
            # Une source qui échoue ne doit jamais faire planter toute la requête
            pass

    # ⚠️ Sofascore est retiré ici : bloqué de façon permanente (403) depuis
    # l'IP de Render. Le garder ne ferait qu'ajouter un délai d'attente
    # (timeout) à chaque requête sans jamais réussir. Le module reste
    # disponible (app/sources/sofascore.py) si un jour le déploiement
    # change d'hébergeur et que le blocage ne s'applique plus.
    await asyncio.gather(
        _essayer(besoccer.get_team_stats),
        _essayer(flashscore.get_team_stats),
    )

    return resultats


async def _elo_confrontation_sure(equipe1: str, equipe2: str) -> Optional[Dict[str, Any]]:
    """
    Ne fait jamais planter la requête si la recherche d'ELO échoue, et ne
    la laisse jamais traîner trop longtemps : cette recherche peut
    déclencher jusqu'à 3 requêtes HTTP en cascade (page équipe 1, page
    équipe 2 en repli, page d'analyse) — sans borne globale, ça pouvait
    faire dépasser les 30-40 secondes dans le pire cas et provoquer des
    timeouts côté appli. Plafonné ici à 12 secondes au total : au-delà,
    on abandonne ce signal plutôt que de faire attendre l'utilisateur.
    """
    try:
        return await asyncio.wait_for(
            besoccer.get_elo_confrontation(equipe1, equipe2), timeout=12.0
        )
    except (asyncio.TimeoutError, Exception):
        return None


def _normaliser_telephone(telephone: str) -> str:
    """
    Normalise un numéro de téléphone en identifiant stable : retire les
    espaces et le signe '+'. Important car '+' est traditionnellement
    décodé comme un espace dans une chaîne de requête HTTP — sans cette
    normalisation, "+22890000000" envoyé en query param pouvait finir
    par ne plus correspondre au même numéro stocké ailleurs.
    Ex: "+228 90 00 00 00" -> "22890000000"
    """
    return telephone.replace(" ", "").replace("+", "").strip()


def _est_vip(utilisateur: Optional[Utilisateur]) -> bool:
    if not utilisateur or not utilisateur.vip_expire_le:
        return False
    return utilisateur.vip_expire_le > datetime.utcnow()


def _pronostics_utilises_aujourdhui(session: Session, telephone: str) -> int:
    debut_jour = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    lignes = session.exec(
        select(Pronostic).where(Pronostic.telephone == telephone, Pronostic.cree_le >= debut_jour)
    ).all()
    return len(lignes)


@app.post("/match/analyse", response_model=MatchAnalysisResponse)
async def analyser_match(
    requete: MatchAnalysisRequest, session: Optional[Session] = Depends(db.get_session)
):
    telephone = _normaliser_telephone(requete.telephone)

    est_vip = False
    pronostics_restants: Optional[int] = None

    # Vérification du quota gratuit / statut VIP — seulement si une base
    # de données est configurée (sinon impossible de compter quoi que ce
    # soit, donc on laisse passer en illimité plutôt que de bloquer l'appli).
    if session:
        utilisateur = session.get(Utilisateur, telephone)
        est_vip = _est_vip(utilisateur)

        if not est_vip:
            deja_utilises = _pronostics_utilises_aujourdhui(session, telephone)
            if deja_utilises >= LIMITE_GRATUITE_QUOTIDIENNE:
                raise HTTPException(
                    429,
                    f"Limite gratuite de {LIMITE_GRATUITE_QUOTIDIENNE} pronostics par jour "
                    f"atteinte. Passe en VIP pour un accès illimité.",
                )
            # +1 car cette requête, si elle aboutit, va compter comme utilisée
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

    if session:
        try:
            enregistrement = Pronostic(
                telephone=telephone,
                equipe1=resultat["equipe1"],
                equipe2=resultat["equipe2"],
                type_match=requete.typeMatch,
                probabilite_v1=resultat["probabiliteVictoireEquipe1"],
                probabilite_nul=resultat["probabiliteMatchNul"],
                probabilite_v2=resultat["probabiliteVictoireEquipe2"],
            )
            session.add(enregistrement)
            session.commit()
        except Exception:
            # L'historique est un bonus : un souci de DB ne doit jamais
            # empêcher l'utilisateur de recevoir son pronostic.
            session.rollback()

    return resultat


class PaiementRequest(BaseModel):
    telephone: str
    reseau: str  # "TMONEY" ou "FLOOZ"


class ConfirmationRequest(BaseModel):
    telephone: str
    txReference: str


@app.get("/vip/statut")
def vip_statut(telephone: str, session: Optional[Session] = Depends(db.get_session)):
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    telephone = _normaliser_telephone(telephone)
    utilisateur = session.get(Utilisateur, telephone)
    vip = _est_vip(utilisateur)

    return {
        "vip": vip,
        "vipExpireLe": utilisateur.vip_expire_le.isoformat() if (utilisateur and utilisateur.vip_expire_le) else None,
        "pronosticsUtilisesAujourdhui": _pronostics_utilises_aujourdhui(session, telephone),
        "limiteQuotidienneGratuite": LIMITE_GRATUITE_QUOTIDIENNE,
    }


@app.post("/vip/payer")
async def vip_payer(requete: PaiementRequest):
    """
    Lance un paiement Mobile Money via paygate-api (même service que
    LotoPredict). `reseau` doit valoir "TMONEY" ou "FLOOZ".
    """
    if requete.reseau not in ("TMONEY", "FLOOZ"):
        raise HTTPException(400, "reseau doit être 'TMONEY' ou 'FLOOZ'")

    telephone = _normaliser_telephone(requete.telephone)
    reference = await paygate.initier_paiement(telephone, PRIX_VIP_FCFA, requete.reseau)
    if not reference:
        raise HTTPException(502, "Paiement non initialisé — service de paiement indisponible")

    return {"txReference": reference, "montant": PRIX_VIP_FCFA, "dureeJours": DUREE_VIP_JOURS}


@app.post("/vip/confirmer")
async def vip_confirmer(
    requete: ConfirmationRequest, session: Optional[Session] = Depends(db.get_session)
):
    """
    À appeler après /vip/payer, typiquement en interrogeant régulièrement
    (même logique que LotoPredict), pour vérifier si le paiement a été
    validé et activer le VIP le cas échéant.
    """
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    confirme = await paygate.verifier_paiement(requete.txReference)
    if not confirme:
        return {"confirme": False}

    telephone = _normaliser_telephone(requete.telephone)
    utilisateur = session.get(Utilisateur, telephone)
    maintenant = datetime.utcnow()

    # Si déjà VIP et pas encore expiré, on prolonge à partir de la date
    # d'expiration actuelle (pas de jours payés perdus en cas de
    # renouvellement anticipé). Sinon on repart de maintenant.
    base = (
        utilisateur.vip_expire_le
        if (utilisateur and utilisateur.vip_expire_le and utilisateur.vip_expire_le > maintenant)
        else maintenant
    )
    nouvelle_expiration = base + timedelta(days=DUREE_VIP_JOURS)

    if utilisateur:
        utilisateur.vip_expire_le = nouvelle_expiration
    else:
        utilisateur = Utilisateur(telephone=telephone, vip_expire_le=nouvelle_expiration)

    session.add(utilisateur)
    session.commit()

    return {"confirme": True, "vipExpireLe": nouvelle_expiration.isoformat()}


def _resultat_predit(p_v1: float, p_nul: float, p_v2: float) -> str:
    if p_v1 >= p_nul and p_v1 >= p_v2:
        return "V1"
    if p_v2 >= p_nul and p_v2 >= p_v1:
        return "V2"
    return "NUL"


@app.get("/historique")
def lister_historique(limite: int = 20, session: Optional[Session] = Depends(db.get_session)):
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    lignes = session.exec(
        select(Pronostic).order_by(Pronostic.cree_le.desc()).limit(limite)
    ).all()
    return lignes


@app.patch("/historique/{pronostic_id}")
def enregistrer_resultat_reel(
    pronostic_id: int, resultat_reel: str, session: Optional[Session] = Depends(db.get_session)
):
    """
    Renseigne le résultat réel d'un match une fois connu (saisie manuelle
    pour l'instant — une vérification automatique via re-scraping des
    scores finaux est une amélioration possible pour plus tard).

    resultat_reel doit valoir "V1", "NUL" ou "V2".
    """
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    if resultat_reel not in ("V1", "NUL", "V2"):
        raise HTTPException(400, "resultat_reel doit être 'V1', 'NUL' ou 'V2'")

    ligne = session.get(Pronostic, pronostic_id)
    if not ligne:
        raise HTTPException(404, "Pronostic introuvable")

    predit = _resultat_predit(ligne.probabilite_v1, ligne.probabilite_nul, ligne.probabilite_v2)

    ligne.resultat_reel = resultat_reel
    ligne.verifie = True
    ligne.correct = predit == resultat_reel

    session.add(ligne)
    session.commit()
    session.refresh(ligne)
    return ligne


@app.get("/historique/stats")
def stats_fiabilite(session: Optional[Session] = Depends(db.get_session)):
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    verifies = session.exec(select(Pronostic).where(Pronostic.verifie == True)).all()  # noqa: E712
    total = len(verifies)
    corrects = sum(1 for p in verifies if p.correct)

    return {
        "total_pronostics_verifies": total,
        "pronostics_corrects": corrects,
        "taux_reussite": round(corrects / total, 3) if total > 0 else None,
    }


async def _verifier_un_pronostic(session: Session, p: Pronostic) -> bool:
    """
    Tente de vérifier UN pronostic en re-consultant BeSoccer. Renvoie True
    s'il a effectivement pu être vérifié à cette occasion (résultat trouvé
    et enregistré), False sinon (match pas encore joué, ou dernier match
    de l'équipe contre quelqu'un d'autre pour l'instant).
    """
    resultat_equipe1 = await besoccer.verifier_dernier_match(p.equipe1, p.equipe2)
    if resultat_equipe1 is None:
        return False

    correspondance = {"V": "V1", "N": "NUL", "D": "V2"}
    resultat_reel = correspondance[resultat_equipe1]
    predit = _resultat_predit(p.probabilite_v1, p.probabilite_nul, p.probabilite_v2)

    p.resultat_reel = resultat_reel
    p.verifie = True
    p.correct = predit == resultat_reel

    session.add(p)
    session.commit()
    return True


@app.post("/taches/verifier-resultats")
async def tache_verifier_resultats(
    limite: int = 20, session: Optional[Session] = Depends(db.get_session)
):
    """
    Vérification AUTOMATIQUE des pronostics en attente — à appeler
    périodiquement par un déclencheur externe (ex: cron-job.org, gratuit,
    aucune inscription compliquée). Render (plan gratuit) n'a pas de tâche
    planifiée intégrée, d'où ce endpoint déclenché de l'extérieur.

    Pour chaque pronostic pas encore vérifié : regarde si le dernier match
    TERMINÉ de l'équipe 1 était bien contre l'équipe 2. Si oui, enregistre
    le résultat automatiquement. Sinon, laisse le pronostic en attente
    pour le prochain passage (le match n'a probablement pas encore eu lieu).

    Bonus involontaire : cet appel externe périodique maintient aussi le
    service éveillé sur le plan gratuit de Render (qui s'endort sinon
    après 15 minutes sans trafic).
    """
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    en_attente = session.exec(
        select(Pronostic).where(Pronostic.verifie == False).limit(limite)  # noqa: E712
    ).all()

    nouvellement_verifies = 0
    for p in en_attente:
        try:
            if await _verifier_un_pronostic(session, p):
                nouvellement_verifies += 1
        except Exception:
            # Un souci sur un pronostic ne doit pas bloquer les suivants
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
    telephone: str
    code: str


def _generer_code_aleatoire(longueur: int = 8) -> str:
    caracteres = string.ascii_uppercase + string.digits
    return "".join(random.choice(caracteres) for _ in range(longueur))


@app.post("/admin/codes-vip")
def admin_generer_code(
    requete: GenererCodeRequest, session: Optional[Session] = Depends(db.get_session)
):
    """
    Génère un code VIP activable manuellement — porte dérobée pour les cas
    où le paiement automatique ne fonctionne pas (même logique que
    l'admin panel de LotoPredict).
    """
    if requete.mot_de_passe_admin != MOT_DE_PASSE_ADMIN:
        raise HTTPException(403, "Mot de passe admin incorrect")
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    if requete.duree_jours <= 0:
        raise HTTPException(400, "duree_jours doit être positif")

    # Génère jusqu'à ce qu'un code non déjà utilisé soit trouvé (collision
    # quasi impossible avec 8 caractères alphanumériques, mais on se
    # protège quand même).
    for _ in range(5):
        code = _generer_code_aleatoire()
        if not session.get(CodeVip, code):
            break
    else:
        raise HTTPException(500, "Impossible de générer un code unique, réessaie")

    entree = CodeVip(code=code, duree_jours=requete.duree_jours)
    session.add(entree)
    session.commit()

    return {"code": code, "dureeJours": requete.duree_jours}


@app.get("/admin/codes-vip")
def admin_lister_codes(
    mot_de_passe_admin: str, session: Optional[Session] = Depends(db.get_session)
):
    if mot_de_passe_admin != MOT_DE_PASSE_ADMIN:
        raise HTTPException(403, "Mot de passe admin incorrect")
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    codes = session.exec(select(CodeVip).order_by(CodeVip.cree_le.desc())).all()
    return codes


@app.post("/vip/activer-code")
def vip_activer_code(
    requete: ActiverCodeRequest, session: Optional[Session] = Depends(db.get_session)
):
    """Active le VIP à partir d'un code généré par l'admin (activation manuelle)."""
    if not session:
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    entree_code = session.get(CodeVip, requete.code.strip().upper())
    if not entree_code:
        raise HTTPException(404, "Code invalide")
    if entree_code.utilise:
        raise HTTPException(409, "Ce code a déjà été utilisé")

    telephone = _normaliser_telephone(requete.telephone)
    maintenant = datetime.utcnow()

    utilisateur = session.get(Utilisateur, telephone)
    base = (
        utilisateur.vip_expire_le
        if (utilisateur and utilisateur.vip_expire_le and utilisateur.vip_expire_le > maintenant)
        else maintenant
    )
    nouvelle_expiration = base + timedelta(days=entree_code.duree_jours)

    if utilisateur:
        utilisateur.vip_expire_le = nouvelle_expiration
    else:
        utilisateur = Utilisateur(telephone=telephone, vip_expire_le=nouvelle_expiration)

    entree_code.utilise = True
    entree_code.telephone_utilisateur = telephone
    entree_code.utilise_le = maintenant

    session.add(utilisateur)
    session.add(entree_code)
    session.commit()

    return {"active": True, "vipExpireLe": nouvelle_expiration.isoformat()}
