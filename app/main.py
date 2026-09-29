import asyncio
import random
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel

from .models import MatchAnalysisRequest, MatchAnalysisResponse
from .sources import sofascore, besoccer, flashscore, api_football
from .analysis import generer_pronostic
from . import db
from . import repo
from . import paygate
from . import auth
from . import matchs as matchs_service
from .config import PRIX_VIP_FCFA, DUREE_VIP_JOURS, LIMITE_GRATUITE_QUOTIDIENNE

app = FastAPI(
    title="PronosticApp API",
    openapi_tags=[
        {"name": "Système", "description": "Santé du service et callback paiement (aucune authentification)."},
        {"name": "📖 Compte — Infos", "description": "Consulter son propre profil (lecture seule)."},
        {"name": "📖 VIP — Infos", "description": "Consulter son propre statut VIP (lecture seule)."},
        {"name": "📖 Admin — Diagnostic", "description": "Outils de lecture/debug BeSoccer, Firestore, codes VIP (réservé admin)."},
        {"name": "✏️ Pronostic — Actions", "description": "Analyser un match — le cœur de l'appli."},
        {"name": "✏️ Compte — Actions", "description": "Modifier ou supprimer son propre compte."},
        {"name": "✏️ VIP — Actions", "description": "Payer, confirmer ou activer le VIP."},
        {"name": "✏️ Admin — Actions", "description": "Générer des codes VIP, tester le moteur de pronostic (réservé admin)."},
    ],
)

# CORS ouvert : simple pour un backend consommé uniquement par l'appli Android.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Système"])
async def health():
    """Endpoint de santé, utile pour vérifier que le déploiement fonctionne."""
    return {"status": "ok", "service": "PronosticApp API"}


@app.get("/debug/db", tags=["📖 Admin — Diagnostic"])
def debug_db(uid: str = Depends(auth.utilisateur_courant)):
    """🔧 Diagnostic (admin) : état réel de la connexion Firestore."""
    auth.exiger_admin(uid)
    client = db.get_client()
    resultat: Dict[str, Any] = {"firestore_configure": client is not None}
    if client:
        try:
            list(client.collection(repo.COLLECTION_UTILISATEURS).limit(1).stream())
            resultat["connexion_ok"] = True
        except Exception as e:
            resultat["connexion_ok"] = False
            resultat["erreur"] = f"{type(e).__name__}: {e}"
    return resultat


@app.get("/debug/elo", tags=["📖 Admin — Diagnostic"])
async def debug_elo(equipe1: str, equipe2: str, uid: str = Depends(auth.utilisateur_courant)):
    """🔧 Diagnostic (admin) pour la recherche d'ELO/confrontation directe."""
    auth.exiger_admin(uid)
    try:
        resultat = await besoccer.get_elo_confrontation(equipe1, equipe2)
        return {"equipe1": equipe1, "equipe2": equipe2, "resultat": resultat}
    except Exception as e:
        return {"equipe1": equipe1, "equipe2": equipe2, "erreur": f"{type(e).__name__}: {e}"}


@app.post("/debug/verifier-clubs", tags=["✏️ Admin — Actions"])
async def debug_verifier_clubs(noms: List[str], uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Vérifie en masse une liste de noms de clubs contre BeSoccer (admin).

    ⚠️ Volontairement LENT (petits lots + pause entre chaque) — un test
    en rafale de 150 clubs a déjà déclenché un blocage temporaire côté
    BeSoccer, faussant les résultats (même "Real Madrid", pourtant
    fiable, avait échoué). Mieux vaut quelques minutes de plus qu'une
    liste d'échecs polluée de faux positifs.
    """
    auth.exiger_admin(uid)
    resultats_echecs = []

    async def _verifier_un(nom: str):
        try:
            stats = await besoccer.get_team_stats(nom)
            if not stats:
                resultats_echecs.append({"nom": nom, "raison": "aucune donnée trouvée"})
        except Exception as e:
            resultats_echecs.append({"nom": nom, "raison": f"{type(e).__name__}: {e}"})

    taille_lot = 3
    for i in range(0, len(noms), taille_lot):
        lot = noms[i : i + taille_lot]
        await asyncio.gather(*[_verifier_un(nom) for nom in lot])
        if i + taille_lot < len(noms):
            await asyncio.sleep(2.0)  # pause entre les lots pour rester discret

    return {
        "total_verifies": len(noms),
        "total_echecs": len(resultats_echecs),
        "echecs": resultats_echecs,
    }


@app.get("/debug/api-football", tags=["📖 Admin — Diagnostic"])
async def debug_api_football(
    equipe1: Optional[str] = None,
    equipe2: Optional[str] = None,
    uid: str = Depends(auth.utilisateur_courant),
):
    """
    🔧 Diagnostic (admin) — montre le quota restant (protection contre
    une nouvelle suspension) et, si equipe1/equipe2 sont fournis, teste
    la résolution d'ID et l'historique de confrontations directes.
    """
    auth.exiger_admin(uid)
    from .config import CLE_API_FOOTBALL
    from .sources import api_football

    resultat: Dict[str, Any] = {
        "cle_configuree": bool(CLE_API_FOOTBALL),
        "quota": api_football.quota_restant(),
    }

    if equipe1:
        resultat["id_equipe1"] = await api_football.get_team_id(equipe1)
    if equipe2:
        resultat["id_equipe2"] = await api_football.get_team_id(equipe2)
    if equipe1 and equipe2:
        resultat["historique_confrontations"] = await api_football.get_historique_confrontations(equipe1, equipe2)

    return resultat


@app.get("/debug/besoccer", tags=["📖 Admin — Diagnostic"])
async def debug_besoccer(equipe: str, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Diagnostic (admin) — montre TOUTES les variantes de slug essayées,
    y compris celles qui répondent HTTP 200 mais dont l'extraction
    échoue (page trouvée mais rien d'exploitable dessus). Reflète
    exactement le comportement réel de get_team_stats depuis la
    correction du bug "premier 200 = arrêt", qui faisait parfois rater
    la bonne page pour certaines équipes (cas réel : Celta Vigo).
    """
    auth.exiger_admin(uid)
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
                    stats = _extraire_forme_recente(r.text, slug)
                    essai["extraction_reussie"] = stats is not None
                    essai["stats_extraites"] = stats
                    resultat["essais"].append(essai)
                    if stats is not None:
                        break  # c'est cette page qui sera réellement utilisée par l'appli
                    continue  # 200 mais rien d'exploitable -> on essaie le candidat suivant
                resultat["essais"].append(essai)
            except Exception as e:
                resultat["essais"].append({"url": url, "erreur": f"{type(e).__name__}: {e}"})

    return resultat


@app.get("/debug/sofascore", tags=["📖 Admin — Diagnostic"])
async def debug_sofascore(equipe: str, uid: str = Depends(auth.utilisateur_courant)):
    """🔧 Diagnostic (admin) : Sofascore reste bloqué (403) depuis l'IP de Render, gardé pour vérifier si ça change un jour."""
    auth.exiger_admin(uid)
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
    """Le résultat que le modèle donne comme le plus probable des trois — utilisé pour le backtest."""
    if p_v1 >= p_nul and p_v1 >= p_v2:
        return "V1"
    if p_v2 >= p_nul and p_v2 >= p_v1:
        return "V2"
    return "NUL"


@app.post("/match/analyse", response_model=MatchAnalysisResponse, tags=["✏️ Pronostic — Actions"])
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

    # Enrichissement optionnel via API-Football : historique COMPLET des
    # confrontations directes (contrairement à la détection BeSoccer, qui
    # ne regarde que par coïncidence dans les derniers matchs scrappés).
    # Se dégrade silencieusement si la clé n'est pas configurée, si le
    # quota est atteint, ou si l'équipe est introuvable — n'affecte
    # jamais le calcul des probabilités, purement informatif.
    try:
        historique = await api_football.get_historique_confrontations(requete.equipe1, requete.equipe2)
        if historique:
            v1 = sum(1 for m in historique if (m["equipe_domicile"] == requete.equipe1 and m["buts_domicile"] > m["buts_exterieur"]) or (m["equipe_exterieur"] == requete.equipe1 and m["buts_exterieur"] > m["buts_domicile"]))
            nuls = sum(1 for m in historique if m["buts_domicile"] == m["buts_exterieur"])
            v2 = len(historique) - v1 - nuls
            resultat["facteursCles"].append(
                f"Historique complet des confrontations directes (API-Football, {len(historique)} derniers matchs) : "
                f"{v1} victoire(s) {requete.equipe1}, {nuls} nul(s), {v2} victoire(s) {requete.equipe2}"
            )
    except Exception:
        pass  # enrichissement optionnel — ne doit jamais faire échouer l'analyse principale

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


@app.get("/matchs", tags=["📖 Pronostic — Infos"])
async def lister_matchs(jour: str = "today", uid: str = Depends(auth.utilisateur_courant)):
    """
    Liste des matchs du jour ou du lendemain (jour="today"|"tomorrow"),
    limitée aux championnats suivis (voir CHAMPIONNATS_SUIVIS dans
    config.py), avec `donneesDisponibles` déjà calculé pour chaque match
    (scraping BeSoccer fait en arrière-plan lors du rafraîchissement,
    jamais au moment où le client clique). Utilise ensuite
    /match/analyse pour l'analyse détaillée d'un match précis — même
    moteur, même quota de 3 analyses gratuites par jour que d'habitude.
    """
    if jour not in ("today", "tomorrow"):
        raise HTTPException(400, "jour doit être 'today' ou 'tomorrow'")
    return await matchs_service.obtenir_matchs(jour)


@app.get("/debug/ligues", tags=["📖 Admin — Diagnostic"])
async def debug_ligues(pays: str, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Cherche l'ID API-Football d'un championnat par pays (ex:
    pays=Togo) — utile pour compléter CHAMPIONNATS_SUIVIS dans
    config.py, notamment pour trouver le championnat togolais.
    """
    auth.exiger_admin(uid)
    resultat = await api_football.rechercher_ligues(pays)
    return {"pays": pays, "ligues": resultat}


class ProfilRequest(BaseModel):
    telephone: Optional[str] = None


class PaiementRequest(BaseModel):
    telephone: str  # requis ici : PayGate a besoin d'un vrai numéro pour débiter
    reseau: str  # "TMONEY" ou "FLOOZ"


class ConfirmationRequest(BaseModel):
    txReference: str


@app.get("/profil", tags=["📖 Compte — Infos"])
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


@app.post("/profil", tags=["✏️ Compte — Actions"])
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


@app.get("/vip/statut", tags=["📖 VIP — Infos"])
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


@app.post("/vip/payer", tags=["✏️ VIP — Actions"])
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

    # Nécessaire pour que le webhook (callback PayGate) sache à quel
    # compte attribuer le VIP une fois la confirmation reçue.
    try:
        repo.enregistrer_paiement_initie(reference, uid, PRIX_VIP_FCFA)
    except Exception:
        pass  # le paiement fonctionne quand même via le sondage classique en repli

    return {"txReference": reference, "montant": PRIX_VIP_FCFA, "dureeJours": DUREE_VIP_JOURS}


@app.post("/vip/confirmer", tags=["✏️ VIP — Actions"])
async def vip_confirmer(requete: ConfirmationRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    À appeler après /vip/payer pour vérifier et activer le VIP si payé.
    Vérifie d'abord si le webhook PayGate a déjà traité ce paiement
    (rapide, aucun appel réseau supplémentaire) avant de retomber sur une
    vérification active auprès de PayGate en repli.
    """
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")

    paiement = repo.obtenir_paiement(requete.txReference)
    if paiement and paiement.get("traite"):
        utilisateur = repo.obtenir_utilisateur(uid)
        vip_expire = utilisateur.get("vip_expire_le") if utilisateur else None
        return {
            "confirme": True,
            "vipExpireLe": vip_expire.isoformat() if vip_expire else None,
        }

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
    if paiement:
        repo.marquer_paiement_traite(requete.txReference)

    return {"confirme": True, "vipExpireLe": nouvelle_expiration.isoformat()}


# ⚠️ Callback PARTAGÉ avec LotoPredict — un seul compte PayGate Global,
# une seule URL de callback possible. On distingue les deux applis par le
# montant (300 FCFA = LotoPredict, 500 FCFA = PronosticApp) : c'est
# fragile si les deux applis utilisent un jour le même montant, mais
# c'est le compromis choisi en attendant un compte PayGate séparé.
LOTOPREDICT_CALLBACK_URL = "https://paygate-api.onrender.com/callback"


@app.post("/webhooks/paygate", tags=["Système"])
async def webhook_paygate(requete: Request):
    try:
        payload = await requete.json()
    except Exception:
        return {"erreur": "corps de requête invalide"}

    try:
        montant_recu = float(payload.get("amount"))
    except (TypeError, ValueError):
        montant_recu = None

    if montant_recu != PRIX_VIP_FCFA:
        # Pas pour PronosticApp — on relaie tel quel vers LotoPredict,
        # qui ne voit aucune différence par rapport à avant.
        try:
            async with httpx.AsyncClient() as client:
                await client.post(LOTOPREDICT_CALLBACK_URL, json=payload, timeout=10.0)
        except Exception:
            pass
        return {"relaye": True}

    tx_reference = payload.get("tx_reference")
    if not tx_reference or not db.get_client():
        return {"traite": False}

    paiement = repo.obtenir_paiement(tx_reference)
    if not paiement or paiement.get("traite"):
        return {"traite": False}

    uid = paiement["uid"]
    maintenant = datetime.now(timezone.utc)
    utilisateur = repo.obtenir_utilisateur(uid)
    base = (
        utilisateur["vip_expire_le"]
        if (utilisateur and utilisateur.get("vip_expire_le") and utilisateur["vip_expire_le"] > maintenant)
        else maintenant
    )
    nouvelle_expiration = base + timedelta(days=DUREE_VIP_JOURS)
    repo.definir_expiration_vip(uid, nouvelle_expiration)
    repo.marquer_paiement_traite(tx_reference)

    return {"traite": True}




# ==== Codes VIP (porte dérobée admin) ====

class GenererCodeRequest(BaseModel):
    duree_jours: int


class ActiverCodeRequest(BaseModel):
    code: str


def _generer_code_aleatoire(longueur: int = 8) -> str:
    caracteres = string.ascii_uppercase + string.digits
    return "".join(random.choice(caracteres) for _ in range(longueur))


@app.post("/admin/codes-vip", tags=["✏️ Admin — Actions"])
def admin_generer_code(requete: GenererCodeRequest, uid: str = Depends(auth.utilisateur_courant)):
    """Génère un code VIP activable manuellement — porte dérobée admin."""
    auth.exiger_admin(uid)
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


@app.get("/admin/codes-vip", tags=["📖 Admin — Diagnostic"])
def admin_lister_codes(uid: str = Depends(auth.utilisateur_courant)):
    auth.exiger_admin(uid)
    if not db.get_client():
        raise HTTPException(503, "Base de données non configurée sur ce déploiement")
    return repo.lister_codes_vip()


@app.post("/vip/activer-code", tags=["✏️ VIP — Actions"])
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


@app.delete("/compte", tags=["✏️ Compte — Actions"])
def supprimer_compte(uid: str = Depends(auth.utilisateur_courant)):
    """
    Suppression DÉFINITIVE du compte et de toutes les données associées
    (pronostics, profil, statut VIP) — conforme à l'exigence Google Play
    de pouvoir supprimer son compte depuis l'application. Irréversible.
    """
    try:
        repo.supprimer_toutes_donnees_utilisateur(uid)
    except Exception as e:
        # On tente quand même la suppression du compte Firebase ensuite,
        # mais on garde une trace de l'erreur au lieu de la faire
        # disparaître silencieusement (un vrai souci ici doit être visible
        # dans les logs, pas juste ignoré).
        print(f"⚠️ Erreur lors de la suppression des données Firestore pour {uid} : {e}")

    try:
        auth.supprimer_compte_firebase(uid)
    except Exception as e:
        raise HTTPException(500, f"Erreur lors de la suppression du compte : {e}")

    return {"supprime": True}


class DebugAnalyseRequest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str


@app.post("/debug/analyser-match", tags=["✏️ Admin — Actions"])
async def debug_analyser_match(requete: DebugAnalyseRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Fait tourner EXACTEMENT le même moteur de pronostic que
    /match/analyse (mêmes sources, même modèle Poisson, même recherche
    ELO), réservé à l'admin. Aucun quota, aucun enregistrement en base.
    """
    auth.exiger_admin(uid)

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

    # Infos brutes en plus, utiles pour comprendre le calcul en détail
    resultat["_debug_stats1_sources"] = stats1_sources
    resultat["_debug_stats2_sources"] = stats2_sources
    resultat["_debug_elo_confrontation"] = elo_confrontation

    return resultat


class MatchBacktest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str
    resultatReel: str  # "V1", "NUL" ou "V2"
    butsEquipe1: Optional[int] = None  # optionnel — permet de vérifier aussi BTTS et over/under si fourni
    butsEquipe2: Optional[int] = None


class BacktestRequest(BaseModel):
    matchs: List[MatchBacktest]
    rho_dixon_coles: Optional[float] = None
    poids_lissage: Optional[float] = None
    diviseur_echelle_elo: Optional[float] = None
    coefficient_attenuation_elo: Optional[float] = None


@app.post("/debug/backtest", tags=["✏️ Admin — Actions"])
async def debug_backtest(requete: BacktestRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Fait tourner le moteur de pronostic actuel sur une liste de matchs
    DÉJÀ JOUÉS (avec leur vrai résultat connu), et calcule le taux de
    réussite réel — pour mesurer objectivement la qualité du modèle
    plutôt que de juger sur un seul cas. Réservé admin, aucun quota,
    aucun enregistrement en base.

    ⚠️ Traité par lots de 4 matchs en parallèle (avec courte pause entre
    chaque lot) pour rester raisonnablement rapide sans bombarder
    BeSoccer de requêtes — au-delà d'environ 100-150 matchs par appel,
    le temps total risque quand même de dépasser le délai maximal d'une
    requête HTTP (502) : mieux vaut découper un très gros fichier en
    plusieurs appels séparés plutôt que tout envoyer d'un coup.

    rho_dixon_coles et poids_lissage sont optionnels : si fournis, ils
    remplacent les valeurs par défaut UNIQUEMENT pour ce backtest — sans
    jamais affecter /match/analyse pour les vrais utilisateurs. Pratique
    pour comparer objectivement plusieurs réglages sur le même jeu de
    matchs déjà validés.
    """
    auth.exiger_admin(uid)

    from .analysis import RHO_DIXON_COLES, POIDS_LISSAGE, DIVISEUR_ECHELLE_ELO, COEFFICIENT_ATTENUATION_ELO
    rho = requete.rho_dixon_coles if requete.rho_dixon_coles is not None else RHO_DIXON_COLES
    lissage = requete.poids_lissage if requete.poids_lissage is not None else POIDS_LISSAGE
    diviseur_elo = requete.diviseur_echelle_elo if requete.diviseur_echelle_elo is not None else DIVISEUR_ECHELLE_ELO
    coeff_elo = (
        requete.coefficient_attenuation_elo
        if requete.coefficient_attenuation_elo is not None
        else COEFFICIENT_ATTENUATION_ELO
    )

    details = []
    corrects_1x2 = 0

    async def _traiter_un_match(m: MatchBacktest):
        if m.resultatReel not in ("V1", "NUL", "V2"):
            return {
                "equipe1": m.equipe1, "equipe2": m.equipe2,
                "erreur": "resultatReel doit être 'V1', 'NUL' ou 'V2'",
            }
        try:
            stats1_sources, stats2_sources, elo_confrontation = await asyncio.gather(
                _stats_toutes_sources(m.equipe1),
                _stats_toutes_sources(m.equipe2),
                _elo_confrontation_sure(m.equipe1, m.equipe2),
            )
            resultat = generer_pronostic(
                m.equipe1, m.equipe2, m.typeMatch,
                stats1_sources, stats2_sources,
                elo_confrontation=elo_confrontation,
                rho_dixon_coles=rho,
                poids_lissage=lissage,
                diviseur_echelle_elo=diviseur_elo,
                coefficient_attenuation_elo=coeff_elo,
            )
        except Exception as e:
            return {
                "equipe1": m.equipe1, "equipe2": m.equipe2,
                "erreur": f"{type(e).__name__}: {e}",
            }

        predit = _resultat_predit(
            resultat["probabiliteVictoireEquipe1"],
            resultat["probabiliteMatchNul"],
            resultat["probabiliteVictoireEquipe2"],
        )

        detail = {
            "equipe1": m.equipe1,
            "equipe2": m.equipe2,
            "probabilites": {
                "V1": resultat["probabiliteVictoireEquipe1"],
                "NUL": resultat["probabiliteMatchNul"],
                "V2": resultat["probabiliteVictoireEquipe2"],
            },
            "predit": predit,
            "resultatReel": m.resultatReel,
            "correct": predit == m.resultatReel,
        }

        # Vérification BTTS/over-under, uniquement si le score exact a
        # été fourni (sinon on ne peut tout simplement pas savoir).
        if m.butsEquipe1 is not None and m.butsEquipe2 is not None:
            total_buts_reel = m.butsEquipe1 + m.butsEquipe2
            btts_reel = m.butsEquipe1 > 0 and m.butsEquipe2 > 0

            detail["verification_marches"] = {
                "btts": {
                    "probabilite_predite": resultat["probabiliteBTTS"],
                    "reel": btts_reel,
                    "predit_oui": resultat["probabiliteBTTS"] > 0.5,
                    "correct": (resultat["probabiliteBTTS"] > 0.5) == btts_reel,
                },
                "over_1_5": {
                    "probabilite_predite": resultat["probabiliteOver15"],
                    "reel": total_buts_reel > 1.5,
                    "predit_oui": resultat["probabiliteOver15"] > 0.5,
                    "correct": (resultat["probabiliteOver15"] > 0.5) == (total_buts_reel > 1.5),
                },
                "over_2_5": {
                    "probabilite_predite": resultat["probabiliteOver25"],
                    "reel": total_buts_reel > 2.5,
                    "predit_oui": resultat["probabiliteOver25"] > 0.5,
                    "correct": (resultat["probabiliteOver25"] > 0.5) == (total_buts_reel > 2.5),
                },
            }

        return detail

    taille_lot = 4
    for i in range(0, len(requete.matchs), taille_lot):
        lot = requete.matchs[i : i + taille_lot]
        resultats_lot = await asyncio.gather(*[_traiter_un_match(m) for m in lot])
        details.extend(resultats_lot)
        if i + taille_lot < len(requete.matchs):
            await asyncio.sleep(1.0)

    corrects_1x2 = sum(1 for d in details if d.get("correct"))
    total_valides = len([d for d in details if "erreur" not in d])

    # Statistiques BTTS/over-under, uniquement sur les matchs où le score était fourni
    avec_score = [d for d in details if "verification_marches" in d]
    stats_marches = None
    if avec_score:
        stats_marches = {}
        for marche in ("btts", "over_1_5", "over_2_5"):
            corrects_marche = sum(1 for d in avec_score if d["verification_marches"][marche]["correct"])
            stats_marches[marche] = {
                "total_verifies": len(avec_score),
                "corrects": corrects_marche,
                "taux_reussite": round(corrects_marche / len(avec_score), 3),
            }

    return {
        "parametres_utilises": {
            "rho_dixon_coles": rho,
            "poids_lissage": lissage,
            "diviseur_echelle_elo": diviseur_elo,
            "coefficient_attenuation_elo": coeff_elo,
        },
        "total_matchs": len(requete.matchs),
        "total_valides": total_valides,
        "corrects_1x2": corrects_1x2,
        "taux_reussite_1x2": round(corrects_1x2 / total_valides, 3) if total_valides > 0 else None,
        "stats_marches_avances": stats_marches,
        "details": details,
    }


# ==== Collecte progressive de matchs pour le backtest ====

class CollecteRequest(BaseModel):
    equipe1: str
    equipe2: str
    typeMatch: str


@app.post("/debug/collecte-prediction", tags=["✏️ Admin — Actions"])
async def debug_collecte_prediction(requete: CollecteRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Enregistre une prédiction pour un match PAS ENCORE JOUÉ, en vue
    d'une vérification ultérieure (voir /debug/verifier-predictions).
    C'est la bonne façon de construire un jeu de backtest dans la durée
    sans le biais temporel découvert avec des matchs déjà anciens : la
    prédiction est capturée au bon moment, juste avant le coup d'envoi.
    """
    auth.exiger_admin(uid)

    stats1_sources, stats2_sources, elo_confrontation = await asyncio.gather(
        _stats_toutes_sources(requete.equipe1),
        _stats_toutes_sources(requete.equipe2),
        _elo_confrontation_sure(requete.equipe1, requete.equipe2),
    )

    resultat = generer_pronostic(
        requete.equipe1, requete.equipe2, requete.typeMatch,
        stats1_sources, stats2_sources,
        elo_confrontation=elo_confrontation,
    )

    slug1 = next((s.get("slug") for s in stats1_sources if s.get("slug")), None)
    slug2 = next((s.get("slug") for s in stats2_sources if s.get("slug")), None)

    prediction_id = repo.enregistrer_prediction_backtest(
        requete.equipe1, requete.equipe2, requete.typeMatch,
        slug1, slug2,
        resultat["probabiliteVictoireEquipe1"],
        resultat["probabiliteMatchNul"],
        resultat["probabiliteVictoireEquipe2"],
    )

    return {
        "id": prediction_id,
        "equipe1": requete.equipe1,
        "equipe2": requete.equipe2,
        "probabilites": {
            "V1": resultat["probabiliteVictoireEquipe1"],
            "NUL": resultat["probabiliteMatchNul"],
            "V2": resultat["probabiliteVictoireEquipe2"],
        },
        "message": "Prédiction enregistrée — repasse dans quelques jours avec /debug/verifier-predictions",
    }


@app.post("/debug/verifier-predictions", tags=["✏️ Admin — Actions"])
async def debug_verifier_predictions(uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Reprend toutes les prédictions en attente et vérifie si le match a
    été joué depuis (en cherchant, dans la forme récente ACTUELLE des
    deux équipes sur BeSoccer, un match les opposant l'une à l'autre).
    Réservé admin — à appeler périodiquement (manuellement ou via un
    cron externe), quelques jours après avoir collecté des prédictions.
    """
    auth.exiger_admin(uid)

    en_attente = repo.lister_predictions_en_attente(limite=50)
    verifiees = 0
    toujours_en_attente = 0

    for p in en_attente:
        try:
            stats1 = await besoccer.get_team_stats(p["equipe1"])
        except Exception:
            stats1 = None

        resultat_trouve = None
        if stats1 and p.get("slug2"):
            for m in stats1.get("matchs_detail", []):
                if m.get("adversaire_slug") == p["slug2"]:
                    # Match retrouvé : reconstruire V1/NUL/V2 du point de
                    # vue de equipe1 (peu importe si elle jouait à
                    # domicile ou à l'extérieur cette fois-là).
                    if m["buts_pour"] > m["buts_contre"]:
                        resultat_trouve = "V1"
                    elif m["buts_pour"] < m["buts_contre"]:
                        resultat_trouve = "V2"
                    else:
                        resultat_trouve = "NUL"
                    break

        if resultat_trouve:
            repo.marquer_prediction_verifiee(p["id"], resultat_trouve)
            verifiees += 1
        else:
            toujours_en_attente += 1

    return {
        "predictions_examinees": len(en_attente),
        "nouvellement_verifiees": verifiees,
        "toujours_en_attente": toujours_en_attente,
    }


@app.get("/debug/predictions-verifiees", tags=["📖 Admin — Diagnostic"])
def debug_predictions_verifiees(uid: str = Depends(auth.utilisateur_courant)):
    """
    🔧 Renvoie toutes les prédictions déjà vérifiées, prêtes à être
    copiées dans /debug/backtest — le vrai jeu de test qui grossit dans
    le temps, sans biais temporel.
    """
    auth.exiger_admin(uid)
    verifiees = repo.lister_predictions_verifiees()

    matchs_backtest = [
        {
            "equipe1": p["equipe1"],
            "equipe2": p["equipe2"],
            "typeMatch": p["type_match"],
            "resultatReel": p["resultat_reel"],
        }
        for p in verifiees
    ]

    return {"total": len(matchs_backtest), "matchs": matchs_backtest}


# ==== Calculateur de mises (dutching) ====
#
# Répartit un budget entre plusieurs paris pour obtenir le MÊME gain
# quel que soit celui qui se réalise (technique dite "dutching"). Pur
# calcul mathématique, aucune donnée externe requise.
#
# ⚠️ Ne peut jamais garantir un gain cible arbitraire : la somme des
# probabilités implicites (1/cote pour chaque pari) reflète la marge du
# bookmaker. Si elle dépasse 100% (quasi toujours en pratique, sauf
# rare erreur de cotation exploitable), le retour égal atteignable avec
# un budget donné est mathématiquement plafonné — on le calcule et on le
# dit clairement plutôt que d'inventer une répartition qui donnerait une
# fausse impression de gain garanti.

class ParimutuelRequest(BaseModel):
    cotes: List[float]  # ex: [1.64, 1.22, 1.40]
    budget: float  # budget total disponible, en FCFA (ou toute devise)
    gain_cible: Optional[float] = None  # optionnel : gain souhaité si l'un des paris passe


@app.post("/outils/calculateur-mises", tags=["✏️ Outils — Actions"])
def calculateur_mises(requete: ParimutuelRequest, uid: str = Depends(auth.utilisateur_courant)):
    """
    Répartit `budget` entre les paris de `cotes` pour un gain identique
    quel que soit celui qui gagne (dutching). Si `gain_cible` est fourni,
    indique en plus le budget qu'il faudrait réellement pour l'atteindre,
    et précise si le budget donné suffit ou non — sans jamais prétendre
    qu'une répartition peut dépasser ce que les cotes permettent
    mathématiquement.
    """
    if not requete.cotes or any(c <= 1.0 for c in requete.cotes):
        raise HTTPException(400, "Toutes les cotes doivent être des nombres supérieurs à 1.0")
    if requete.budget <= 0:
        raise HTTPException(400, "Le budget doit être positif")

    inverses = [1.0 / c for c in requete.cotes]
    somme_inverses = sum(inverses)

    mises = [requete.budget * inv / somme_inverses for inv in inverses]
    retour_egal = requete.budget / somme_inverses  # identique pour chaque mise, par construction
    gain_net_egal = retour_egal - requete.budget

    reponse: Dict[str, Any] = {
        "cotes": requete.cotes,
        "budget": requete.budget,
        "somme_probabilites_implicites_pourcent": round(somme_inverses * 100, 1),
        "marge_bookmaker_pourcent": round((somme_inverses - 1) * 100, 1) if somme_inverses > 1 else 0.0,
        "mises_par_pari": [round(m, 0) for m in mises],
        "retour_si_lun_gagne": round(retour_egal, 0),
        "gain_net_si_lun_gagne": round(gain_net_egal, 0),
        "rentable": gain_net_egal > 0,
    }

    if requete.gain_cible is not None:
        budget_necessaire = requete.gain_cible * somme_inverses
        reponse["gain_cible"] = requete.gain_cible
        reponse["budget_necessaire_pour_gain_cible"] = round(budget_necessaire, 0)
        reponse["budget_actuel_suffisant"] = requete.budget >= budget_necessaire
        if requete.budget < budget_necessaire:
            reponse["message"] = (
                f"Avec ces cotes, un gain garanti de {requete.gain_cible:.0f} (quel que soit le pari qui passe) "
                f"nécessite un budget d'au moins {budget_necessaire:.0f}, pas {requete.budget:.0f}. "
                f"Avec {requete.budget:.0f}, le retour garanti atteignable est de {retour_egal:.0f}."
            )
        else:
            reponse["message"] = (
                f"Le budget de {requete.budget:.0f} suffit pour garantir au moins {requete.gain_cible:.0f}."
            )

    return reponse
