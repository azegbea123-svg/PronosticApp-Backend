"""
Orchestration du module esport. Aucune dépendance à FastAPI (testable seul) :
les erreurs métier sont des `ErreurEsport(statut_http, message)` que routes.py traduit.

Découverte des matchs — VÉRIFIÉE sur les captures réelles (endpoint2.txt) :
  - `GET /api/esport/category/{id}/events/{j}/{m}/{a}` : TOUS les matchs d'UN jeu pour UNE
    date en 1 appel (64 matchs dans la capture) ;
  - `GET /api/esport/matches/live` : les matchs en cours, tous jeux confondus, en 1 appel ;
  - `GET /api/esport/search/{texte}` : recherche d'équipes (et de joueurs) par nom ;
  - `GET /api/esport/tournament/categories` : les 3 jeux couverts (ids ci-dessous).
Les équipes SUIVIES ne servent plus à découvrir les matchs : elles sont des FAVORIS
(étoile et tri), sans aucun appel API.

Fuseau : la capture montre que la date "19/12" commence le 18/12 à 15:00 UTC, soit un fuseau
d'environ UTC+9 (déduit, à confirmer). Demander les jours J et J+1 couvre donc toute la
journée UTC de J.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Set

from . import client, depot, modele, parsing
from .equipes_connues import EQUIPES_CONNUES

TTL_NEAR = 3 * 3600
TTL_EQUIPE = 7 * 86400
TTL_HISTORIQUE = 12 * 3600
TTL_H2H = 24 * 3600
TTL_EVENT = 120           # match prévu/en cours : fraîcheur courte...
TTL_EVENT_FINI = 86400    # ...mais un match terminé ne change plus
TTL_DETAIL = 6 * 3600     # cartes / duel / votes
JOURS_RESULTATS_RECENTS = 7
DELAI_VERIFICATION_H = 2  # on ne vérifie un pronostic qu'au moins 2 h après le coup d'envoi
MIN_BILAN_FIABLE = 30

# Les 3 jeux couverts par l'API : identifiants lus dans /api/esport/tournament/categories.
JEUX = {
    "csgo": {"id": 1572, "nom": "Counter Strike"},
    "dota2": {"id": 1570, "nom": "Dota 2"},
    "lol": {"id": 1571, "nom": "LoL"},
}
CHEMIN_LIVE = "/api/esport/matches/live"
TTL_LIVE = 180            # un match en direct change vite ; au-delà de 3 min on ne dépense pas de quota
TTL_LISTE_AUJOURDHUI = 3 * 3600
TTL_LISTE_DEMAIN = 8 * 3600
TTL_RECHERCHE = 24 * 3600
RESULTATS_RECENTS_H = 24  # un match terminé reste dans la liste 24 h
RETARD_TOLERE_S = 600     # un match "prévu" dont l'heure est passée de plus de 10 min est suspect

PAGE_HISTORIQUE = 1  # page de `matches/previous/{page}` capturée (30 matchs) ; 0 non vérifiée


class ErreurEsport(Exception):
    def __init__(self, statut: int, message: str):
        super().__init__(message)
        self.statut = statut
        self.message = message


def _chemin_event(event_id: int) -> str:
    return f"/api/esport/event/{event_id}"


def _chemin_historique(team_id: int) -> str:
    return f"/api/esport/team/{team_id}/matches/previous/{PAGE_HISTORIQUE}"


# --------------------------------------------------------------------------
# Équipes suivies
# --------------------------------------------------------------------------

async def ajouter_equipe(team_id: int) -> Dict[str, Any]:
    rep = await client.appeler(f"/api/esport/team/{team_id}", TTL_EQUIPE)
    if rep is None:
        raise ErreurEsport(503, "API esport indisponible ou quota épuisé (réessaie plus tard).")
    t = rep.get("team")
    if not t:
        raise ErreurEsport(404, f"Équipe {team_id} introuvable.")
    # L'API contient aussi du football et d'autres sports : on refuse ce qui n'est pas e-sport.
    if ((t.get("category") or {}).get("sport") or {}).get("slug") != "esports":
        raise ErreurEsport(400, f"L'équipe {team_id} ({t.get('name')}) n'est pas une équipe e-sport.")
    equipe = {
        "id": t["id"],
        "nom": t.get("name") or t.get("shortName"),
        "jeu": (t.get("category") or {}).get("slug"),
        "jeu_nom": (t.get("category") or {}).get("name"),
        "pays": (t.get("country") or {}).get("alpha3"),
    }
    depot.sauver_equipe(equipe)
    return equipe


def importer_equipes_connues(principales_seulement: bool = False) -> Dict[str, int]:
    """Ajoute aux FAVORIS les équipes de equipes_connues.py (aucun appel API : noms/ids viennent des captures)."""
    deja = {e["id"] for e in depot.lister_equipes()}
    ajoutees = 0
    for e in EQUIPES_CONNUES:
        if (principales_seulement and not e["priorite"]) or e["id"] in deja:
            continue
        depot.sauver_equipe(dict(e))
        ajoutees += 1
    return {"ajoutees": ajoutees, "deja_suivies": len(deja), "total_suivies": len(depot.lister_equipes())}


def lister_jeux() -> List[Dict[str, Any]]:
    favoris: Dict[str, int] = {}
    for e in depot.lister_equipes():
        favoris[e.get("jeu") or "?"] = favoris.get(e.get("jeu") or "?", 0) + 1
    return [{"jeu": slug, "nom": j["nom"], "categorie_id": j["id"], "equipes": favoris.get(slug, 0)}
            for slug, j in JEUX.items()]


async def rechercher_equipes(texte: str) -> List[Dict[str, Any]]:
    """Équipes e-sport dont le nom contient `texte` (1 appel, mis en cache 24 h). Les joueurs sont ignorés."""
    texte = texte.strip()
    if len(texte) < 2:
        raise ErreurEsport(400, "Saisis au moins 2 caractères.")
    rep = await client.appeler(f"/api/esport/search/{quote(texte, safe='')}", TTL_RECHERCHE)
    if rep is None:
        raise ErreurEsport(503, "API esport indisponible ou quota épuisé.")
    sortie = []
    for r in rep.get("results") or []:
        e = r.get("entity") or {}
        if r.get("type") == "team" and (e.get("sport") or {}).get("slug") == "esports" and e.get("id") is not None:
            sortie.append({"id": e["id"], "nom": e.get("name"), "code": e.get("nameCode"), "national": e.get("national")})
    return sortie[:10]


# --------------------------------------------------------------------------
# Liste des matchs (équipes suivies)
# --------------------------------------------------------------------------

_ORDRE_STATUT = {"en_direct": 0, "prevu": 1, "inconnu": 2, "reporte": 3, "termine": 4, "annule": 5}


def _chemin_jour(categorie_id: int, jour: datetime) -> str:
    return f"/api/esport/category/{categorie_id}/events/{jour.day}/{jour.month}/{jour.year}"


async def lister_matchs(jeu: Optional[str] = None) -> Dict[str, Any]:
    """
    Matchs en direct, à venir (aujourd'hui, demain) et terminés (24 dernières heures) pour un jeu
    ou tous. Coût : 1 appel pour le direct + 2 par jeu (J et J+1), tous mis en cache — soit au plus
    7 appels pour les 3 jeux. Tâche de fond : s'arrête avant la réserve du quota.
    """
    slugs = [jeu] if jeu in JEUX else list(JEUX)
    maintenant_dt = datetime.now(timezone.utc)
    demain_dt = maintenant_dt + timedelta(days=1)

    # Ordre de PRIORITÉ si le quota ne permet pas tout : direct, puis aujourd'hui, puis demain.
    demandes: List[Dict[str, Any]] = [{"chemin": CHEMIN_LIVE, "ttl": TTL_LIVE, "nature": "live", "jeu": None}]
    demandes += [{"chemin": _chemin_jour(JEUX[j]["id"], maintenant_dt), "ttl": TTL_LISTE_AUJOURDHUI, "nature": "jour", "jeu": j} for j in slugs]
    demandes += [{"chemin": _chemin_jour(JEUX[j]["id"], demain_dt), "ttl": TTL_LISTE_DEMAIN, "nature": "jour", "jeu": j} for j in slugs]

    restant = client.quota()["restant"]
    autorises = None if restant is None else max(0, restant - client.RESERVE_ARRIERE_PLAN)
    interrogees = []
    for d in demandes:
        if client.en_cache(d["chemin"]):
            interrogees.append(d)  # gratuit
        elif autorises is None or autorises > 0:
            interrogees.append(d)
            if autorises is not None:
                autorises -= 1

    reps = await asyncio.gather(*[client.appeler(d["chemin"], d["ttl"], utilisateur=False) for d in interrogees])
    reponses = {d["chemin"]: r for d, r in zip(interrogees, reps)}

    maintenant = time.time()
    matchs: Dict[int, Dict[str, Any]] = {}
    jeux_en_echec = set()
    for d in demandes:
        if d["nature"] != "jour":
            continue
        rep = reponses.get(d["chemin"])
        if rep is None:  # échec, ou non interrogé faute de quota
            jeux_en_echec.add(d["jeu"])
            continue
        for brut in rep.get("events") or []:
            ev = parsing.normaliser_evenement(brut)
            if ev is not None and (ev["jeu"] == d["jeu"]):
                matchs[ev["id"]] = ev

    # Direct : plus frais que les listes du jour, il prime (statut, score).
    live = reponses.get(CHEMIN_LIVE)
    ids_en_direct = set()
    for brut in (live or {}).get("events") or []:
        ev = parsing.normaliser_evenement(brut)
        if ev is not None and ev["statut"] == "en_direct" and (not jeu or ev["jeu"] == jeu):
            matchs[ev["id"]] = ev
            ids_en_direct.add(ev["id"])
    live_en_echec = live is None

    favoris = {e["id"] for e in depot.lister_equipes()}
    liste = []
    for ev in matchs.values():
        if ev["statut"] == "annule":
            continue
        if ev["statut"] == "termine" and maintenant - ev["ts"] > RESULTATS_RECENTS_H * 3600:
            continue
        # Une liste du jour peut avoir 3 h de retard : un match "prévu" dont l'heure est passée et qui n'est
        # pas dans le direct n'a pas forcément commencé ni fini — on ne l'affirme pas.
        if ev["statut"] == "prevu" and ev["ts"] < maintenant - RETARD_TOLERE_S and ev["id"] not in ids_en_direct:
            ev["statut"] = "inconnu"
        ev["favori"] = ev["equipe1"]["id"] in favoris or ev["equipe2"]["id"] in favoris
        liste.append(ev)
    liste.sort(key=lambda m: (_ORDRE_STATUT.get(m["statut"], 9), m["ts"] if m["statut"] != "termine" else -m["ts"]))

    conseil = None
    if not liste and len(jeux_en_echec) == len(slugs) and live_en_echec:
        conseil = "Données esport indisponibles (quota de l'API épuisé ou service injoignable). Réessaie plus tard."
    elif jeux_en_echec:
        conseil = "Données incomplètes pour : " + ", ".join(sorted(JEUX[j]["nom"] for j in jeux_en_echec)) + "."
    return {"matchs": liste, "jeux_en_echec": sorted(jeux_en_echec), "direct_disponible": not live_en_echec,
            "quota": client.quota(), "conseil": conseil}


# --------------------------------------------------------------------------
# Détail / suivi d'un match
# --------------------------------------------------------------------------

def _carte(g: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": g.get("id"),
        "carte": (g.get("map") or {}).get("name"),
        "duree_s": g.get("length"),
        "statut": (g.get("status") or {}).get("type"),
        "vainqueur": g.get("winnerCode"),
        "score1": (g.get("homeScore") or {}).get("display"),
        "score2": (g.get("awayScore") or {}).get("display"),
    }


async def detail(event_id: int, avec: Set[str]) -> Dict[str, Any]:
    avec = avec & {"cartes", "duel", "votes"}
    chemins = {
        "cartes": f"/api/esport/event/{event_id}/games",
        "duel": f"/api/esport/event/{event_id}/duel",
        "votes": f"/api/esport/event/{event_id}/votes",
    }
    besoin = client.manquants([_chemin_event(event_id)] + [chemins[a] for a in avec])
    if besoin and not client.peut_appeler(True, besoin):
        raise ErreurEsport(503, f"Quota API esport insuffisant ({client.quota()['restant']} appel(s) restant(s), "
                                f"{besoin} nécessaire(s)). Réessaie après la réinitialisation.")
    rep = await client.appeler(_chemin_event(event_id), TTL_EVENT)
    if rep is None:
        raise ErreurEsport(503, "API esport indisponible.")
    ev = parsing.normaliser_evenement(rep.get("event"))
    if ev is None:
        raise ErreurEsport(404, f"Match {event_id} introuvable.")
    if ev["statut"] in ("termine", "annule"):
        client.prolonger(_chemin_event(event_id), TTL_EVENT_FINI)

    sortie: Dict[str, Any] = {"match": ev}
    if avec:
        noms = sorted(avec)
        reps = await asyncio.gather(*[client.appeler(chemins[n], TTL_DETAIL) for n in noms])
        brut = dict(zip(noms, reps))
        if "cartes" in brut:
            sortie["cartes"] = None if brut["cartes"] is None else [_carte(g) for g in brut["cartes"].get("games") or []]
        if "duel" in brut:
            d = (brut["duel"] or {}).get("teamDuel")
            sortie["duel"] = None if brut["duel"] is None else (
                {"victoires1": d.get("homeWins"), "victoires2": d.get("awayWins"), "nuls": d.get("draws")} if d else {})
        if "votes" in brut:
            v = (brut["votes"] or {}).get("vote")
            sortie["votes"] = None if brut["votes"] is None else (
                {"equipe1": v.get("vote1"), "equipe2": v.get("vote2")} if v else {})
    return sortie


# --------------------------------------------------------------------------
# Pronostic
# --------------------------------------------------------------------------

async def pronostic(event_id: int) -> Dict[str, Any]:
    """Pronostic FIGÉ au premier calcul (c'est ce qui rend le bilan honnête) : un second appel
    renvoie le même, sans dépenser de quota API."""
    existant = depot.obtenir_pronostic(event_id)
    if existant:
        return {**existant, "deja_calcule": True}

    rep_event = await client.appeler(_chemin_event(event_id), TTL_EVENT)
    if rep_event is None:
        raise ErreurEsport(503, "API esport indisponible ou quota épuisé.")
    ev = parsing.normaliser_evenement(rep_event.get("event"))
    if ev is None:
        raise ErreurEsport(404, f"Match {event_id} introuvable.")
    if ev["statut"] not in ("prevu", "inconnu"):
        raise ErreurEsport(409, "Pronostic impossible : le match est déjà commencé, terminé ou annulé.")

    id1, id2 = ev["equipe1"]["id"], ev["equipe2"]["id"]
    chemins = [_chemin_historique(id1), _chemin_historique(id2)]
    chemin_h2h = f"/api/esport/event/{ev['custom_id']}/h2h" if ev.get("custom_id") else None
    if chemin_h2h:
        chemins.append(chemin_h2h)
    besoin = client.manquants(chemins)
    if besoin and not client.peut_appeler(True, besoin):
        raise ErreurEsport(503, f"Quota API esport insuffisant ({client.quota()['restant']} appel(s) restant(s), "
                                f"~{besoin} nécessaire(s)). Réessaie après la réinitialisation.")

    r1, r2, rh = await asyncio.gather(
        client.appeler(chemins[0], TTL_HISTORIQUE),
        client.appeler(chemins[1], TTL_HISTORIQUE),
        client.appeler(chemin_h2h, TTL_H2H) if chemin_h2h else asyncio.sleep(0, result=None),
    )
    if r1 is None or r2 is None:
        raise ErreurEsport(503, "Historique d'une équipe indisponible : pronostic impossible pour l'instant.")

    hist1 = parsing.historique_equipe(r1, id1)
    hist2 = parsing.historique_equipe(r2, id2)
    if not hist1 or not hist2:
        raise ErreurEsport(422, "Pas assez de matchs récents pour une des deux équipes : pronostic impossible.")
    h2h = parsing.historique_equipe(rh, id1) if rh else None

    maintenant = time.time()
    res = modele.pronostiquer(hist1, hist2, h2h, ev["best_of"], ev["equipe1"]["nom"], ev["equipe2"]["nom"], maintenant)
    dernier = max(h["ts"] for h in hist1 + hist2)
    if (maintenant - dernier) / 86400 > 60:
        res["facteurs"].append("⚠️ Données anciennes : dernier match connu il y a plus de 60 jours.")
    if chemin_h2h and rh is None:
        res["facteurs"].append("Confrontations directes indisponibles (appel en échec).")

    complet = {
        "eventId": event_id,
        "jeu": ev["jeu"],
        "jeu_nom": ev["jeu_nom"],
        "equipe1": ev["equipe1"]["nom"],
        "equipe2": ev["equipe2"]["nom"],
        "best_of": ev["best_of"],
        "debut": ev["debut"],
        "debut_ts": ev["ts"],
        "calcule_le": int(maintenant),
        "statut": "en_attente",
        **res,
    }
    depot.sauver_pronostic(complet)
    return {**complet, "deja_calcule": False}


# --------------------------------------------------------------------------
# Bilan (précision réelle des pronostics figés)
# --------------------------------------------------------------------------

def _classe_predite(p: Dict[str, Any]) -> int:
    probas = {1: p["probabiliteVictoireEquipe1"], 3: p["probabiliteMatchNul"], 2: p["probabiliteVictoireEquipe2"]}
    return max(probas, key=probas.get)


def _brier(p: Dict[str, Any], reel: int) -> float:
    cible = {1: 0.0, 3: 0.0, 2: 0.0}
    cible[reel] = 1.0
    return ((p["probabiliteVictoireEquipe1"] - cible[1]) ** 2
            + (p["probabiliteMatchNul"] - cible[3]) ** 2
            + (p["probabiliteVictoireEquipe2"] - cible[2]) ** 2)


async def bilan(max_verifications: int = 3) -> Dict[str, Any]:
    pronos = depot.lister_pronostics()
    maintenant = time.time()
    a_verifier = sorted(
        [p for p in pronos if p.get("statut") == "en_attente"
         and p.get("debut_ts", 1e18) < maintenant - DELAI_VERIFICATION_H * 3600],
        key=lambda p: p["debut_ts"],
    )[:max_verifications]

    for p in a_verifier:
        rep = await client.appeler(_chemin_event(p["eventId"]), TTL_EVENT, utilisateur=False)
        ev = parsing.normaliser_evenement((rep or {}).get("event"))
        if ev is None:
            continue
        if ev["statut"] == "termine" and ev["vainqueur"] is not None:
            champs = {"statut": "verifie", "resultat": ev["vainqueur"], "score": f"{ev['score1']}-{ev['score2']}"}
        elif ev["forfait"] or ev["statut"] in ("annule", "reporte"):
            champs = {"statut": "ignore"}  # forfait/annulé/reporté : ne compte ni pour ni contre le modèle
        else:
            continue
        depot.maj_pronostic(p["eventId"], champs)
        p.update(champs)
        if ev["statut"] in ("termine", "annule"):
            client.prolonger(_chemin_event(p["eventId"]), TTL_EVENT_FINI)

    verifies = [p for p in pronos if p.get("statut") == "verifie"]
    n = len(verifies)
    bons = sum(1 for p in verifies if _classe_predite(p) == p["resultat"])
    brier = sum(_brier(p, p["resultat"]) for p in verifies) / n if n else None
    return {
        "verifies": n,
        "en_attente": sum(1 for p in pronos if p.get("statut") == "en_attente"),
        "precision": round(bons / n, 3) if n else None,
        "brier": round(brier, 4) if brier is not None else None,
        "fiable": n >= MIN_BILAN_FIABLE,
        "note": ("Échantillon trop petit pour conclure (moins de "
                 f"{MIN_BILAN_FIABLE} pronostics vérifiés)." if n < MIN_BILAN_FIABLE else
                 "Précision mesurée sur des pronostics figés avant les matchs (sans biais temporel)."),
        "derniers": [
            {"equipe1": p["equipe1"], "equipe2": p["equipe2"], "predit": _classe_predite(p),
             "reel": p["resultat"], "score": p.get("score")}
            for p in sorted(verifies, key=lambda p: p["debut_ts"], reverse=True)[:5]
        ],
    }
