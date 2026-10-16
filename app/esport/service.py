"""
Orchestration du module esport. Aucune dépendance à FastAPI (testable seul) :
les erreurs métier sont des `ErreurEsport(statut_http, message)` que routes.py traduit.

Endpoints vérifiés sur captures réelles : `category/{id}/events/{j}/{m}/{a}` (tous les matchs d'un jeu
pour une date, 1 appel), `matches/live`, `search/{texte}`, `event/{id}` (+ games, duel, votes).

ARCHITECTURE « PRONOSTICS GRATUITS » (le quota de l'API est de 50 requêtes/jour) :
  1. Chaque liste du jour récupérée contient aussi les matchs TERMINÉS : on les mémorise (esport_resultats).
  2. Avec le quota restant en fin de journée, on rattrape progressivement les jours passés (45 jours max).
  3. Un classement Elo est calculé sur ces résultats, SANS appel API (elo.py).
  4. Chaque match à venir reçoit son pronostic dans la liste, figé au premier calcul (donc le bilan est honnête).
  5. Le bilan se vérifie sur les résultats mémorisés, sans appel API non plus.
Résultat : afficher le pronostic de TOUS les matchs ne coûte aucune requête supplémentaire.

Fuseau : la date de l'API suit un fuseau d'environ UTC+9 (déduit d'une capture) ; demander J et J+1 couvre la
journée UTC de J.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Set
from urllib.parse import quote

from . import client, depot, elo, modele, parsing
from .equipes_connues import EQUIPES_CONNUES

TTL_NEAR = 3 * 3600
TTL_EQUIPE = 7 * 86400
TTL_EVENT = 120           # match prévu/en cours : fraîcheur courte...
TTL_EVENT_FINI = 86400    # ...mais un match terminé ne change plus
TTL_DETAIL = 6 * 3600     # cartes / duel / votes

# Les 3 jeux couverts par l'API : identifiants lus dans /api/esport/tournament/categories.
JEUX = {
    "csgo": {"id": 1572, "nom": "Counter Strike"},
    "dota2": {"id": 1570, "nom": "Dota 2"},
    "lol": {"id": 1571, "nom": "LoL"},
}
CHEMIN_LIVE = "/api/esport/matches/live"
TTL_LIVE = 600            # 10 min : le direct est le plus gros consommateur de quota
LIVE_MAX_PAR_JOUR = 10    # appels RÉELS au direct par jour UTC ; au-delà on n'appelle plus
TTL_LISTE_AUJOURDHUI = 3 * 3600
TTL_LISTE_DEMAIN = 8 * 3600
TTL_RECHERCHE = 24 * 3600
RESULTATS_RECENTS_H = 24  # un match terminé reste dans la liste 24 h
RETARD_TOLERE_S = 600     # un match "prévu" dont l'heure est passée de plus de 10 min est suspect
LOT_REQUETES = 2          # requêtes simultanées au maximum : évite le 429 "trop de requêtes d'un coup"

JOURS_HISTORIQUE = 45     # profondeur visée pour le classement
SEUIL_RATTRAPAGE = 30     # on ne rattrape le passé que tant qu'il RESTE au moins ce nombre de requêtes
RATTRAPAGE_MAX_PAR_PASSAGE = 2
# AMORÇAGE : tant que les JOURS_AMORCAGE derniers jours ne sont pas tous mémorisés, il n'y a pas assez de matchs pour
# pronostiquer. On rattrape alors plus vite (4 jours par passage) et avec un seuil plus bas : tant qu'il reste plus
# que la réserve de fond (15). C'est un coût UNIQUE d'environ 15 requêtes (5 jours × 3 jeux).
JOURS_AMORCAGE = 5
AMORCAGE_MAX_PAR_PASSAGE = 4
TTL_MEMOIRE_RESULTATS = 900
TTL_MEMOIRE_PRONOSTICS = 300
DELAI_VERIFICATION_H = 2  # on ne vérifie un pronostic qu'au moins 2 h après le coup d'envoi
EXPIRATION_PRONOSTIC_J = 7
MIN_BILAN_FIABLE = 30

_ORDRE_STATUT = {"en_direct": 0, "prevu": 1, "inconnu": 2, "reporte": 3, "termine": 4, "annule": 5}
_live_depenses: Dict[str, Any] = {"jour": None, "n": 0}
_recoltes: Set[int] = set()          # identifiants de matchs terminés déjà traités (évite de réécrire en base)
_verrou_etat: Dict[str, Any] = {"boucle": None, "verrou": None}
_memoire: Dict[str, Any] = {"resultats": (0.0, []), "classement": None, "pronostics": (0.0, {}), "jours": (0.0, set()),
                            "pause_rattrapage": 0.0}
PAUSE_RATTRAPAGE_S = 600  # après un échec du rattrapage, on ne réessaie pas avant 10 min (pas d'acharnement sur le quota)


class ErreurEsport(Exception):
    def __init__(self, statut: int, message: str):
        super().__init__(message)
        self.statut = statut
        self.message = message


def reinitialiser_memoire() -> None:
    """Vide les caches du module et les compteurs (tests)."""
    _live_depenses.update(jour=None, n=0)
    _recoltes.clear()
    _verrou_etat.update(boucle=None, verrou=None)
    _memoire.update(resultats=(0.0, []), classement=None, pronostics=(0.0, {}), jours=(0.0, set()), pause_rattrapage=0.0)


def _chemin_event(event_id: int) -> str:
    return f"/api/esport/event/{event_id}"


def _chemin_jour(categorie_id: int, jour: datetime) -> str:
    return f"/api/esport/category/{categorie_id}/events/{jour.day}/{jour.month}/{jour.year}"


def _id_jour(slug: str, jour: datetime) -> str:
    return f"{slug}_{jour.year}-{jour.month:02d}-{jour.day:02d}"


# --------------------------------------------------------------------------
# Équipes suivies (favoris), jeux, recherche
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
# Résultats mémorisés, classement, pronostics figés
# --------------------------------------------------------------------------

def _compact(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Un match terminé exploitable par le classement (sinon None : forfait, sans score, sans vainqueur)."""
    if ev["statut"] != "termine" or ev["forfait"] or ev["score1"] is None or ev["score2"] is None:
        return None
    if ev["vainqueur"] is None or ev["equipe1"]["id"] is None or ev["equipe2"]["id"] is None:
        return None
    return {"id": ev["id"], "jeu": ev["jeu"], "ts": ev["ts"], "e1": ev["equipe1"]["id"], "n1": ev["equipe1"]["nom"],
            "e2": ev["equipe2"]["id"], "n2": ev["equipe2"]["nom"], "s1": ev["score1"], "s2": ev["score2"],
            "bo": ev["best_of"], "v": ev["vainqueur"]}


def _verrou() -> asyncio.Lock:
    """Un verrou par boucle d'événements (un verrou lié à une ancienne boucle lèverait une erreur)."""
    boucle = asyncio.get_running_loop()
    if _verrou_etat["boucle"] is not boucle:
        _verrou_etat.update(boucle=boucle, verrou=asyncio.Lock())
    return _verrou_etat["verrou"]


def _trouver_evenements(o: Any) -> Iterator[Dict[str, Any]]:
    """Tous les objets « match » d'une réponse quelconque de l'API (liste du jour, direct, détail, historique...)."""
    pile = [o]
    while pile:
        x = pile.pop()
        if isinstance(x, dict):
            if "homeTeam" in x and "awayTeam" in x and "startTimestamp" in x:
                yield x
            pile.extend(x.values())
        elif isinstance(x, list):
            pile.extend(x)


def _id_vrac(c: Dict[str, Any]) -> str:
    """Les matchs récoltés sont groupés par jeu et semaine ISO : peu de documents, jamais trop gros."""
    jour = datetime.fromtimestamp(c["ts"], tz=timezone.utc).isocalendar()
    return f"vrac_{c['jeu']}_{jour[0]}-W{jour[1]:02d}"


async def recolter(donnees: Any) -> int:
    """
    Mémorise TOUS les matchs terminés contenus dans une réponse de l'API, quelle que soit la requête : c'est le
    cache de résultats qui nourrit le classement et donc les pronostics. Branché sur chaque appel par client.RECOLTEUR.
    Renvoie le nombre de matchs nouvellement mémorisés ; ne lève jamais d'exception.
    """
    try:
        par_doc: Dict[str, List[Dict[str, Any]]] = {}
        for brut in _trouver_evenements(donnees):
            ev = parsing.normaliser_evenement(brut)
            c = _compact(ev) if ev is not None else None
            if c is None or c["id"] in _recoltes or not c["jeu"]:
                continue
            par_doc.setdefault(_id_vrac(c), []).append(c)
        if not par_doc:
            return 0
        ajoutes = 0
        async with _verrou():  # écritures sérialisées : pas de mise à jour perdue entre deux réponses simultanées
            for doc_id, liste in par_doc.items():
                ajoutes += await asyncio.to_thread(depot.fusionner_resultats, doc_id, liste)
            if len(_recoltes) > 50_000:
                _recoltes.clear()
            _recoltes.update(c["id"] for liste in par_doc.values() for c in liste)
        if ajoutes:
            _memoire.update(resultats=(0.0, []), classement=None)
        return ajoutes
    except Exception:
        return 0


async def _marquer_jour(slug: str, jour: datetime) -> None:
    """Marque un jour passé comme « traité » (même vide) pour ne pas le redemander. Les matchs, eux, arrivent par recolter()."""
    await asyncio.to_thread(depot.sauver_resultats_jour, _id_jour(slug, jour),
                            {"jeu": slug, "jour": _id_jour(slug, jour), "maj": int(time.time()), "matchs": []})
    _memoire.update(jours=(0.0, set()))


async def _resultats() -> List[Dict[str, Any]]:
    expire, cache = _memoire["resultats"]
    if expire > time.time():
        return cache
    liste = await asyncio.to_thread(depot.lister_resultats)
    _memoire["resultats"] = (time.time() + TTL_MEMOIRE_RESULTATS, liste)
    return liste


async def _classement() -> Dict[str, Any]:
    if _memoire["classement"] is None:
        _memoire["classement"] = elo.calculer(await _resultats())
    return _memoire["classement"]


async def _pronostics_geles() -> Dict[int, Dict[str, Any]]:
    expire, cache = _memoire["pronostics"]
    if expire > time.time():
        return cache
    docs = await asyncio.to_thread(depot.lister_pronostics)
    table = {d["eventId"]: d for d in docs if "eventId" in d}
    _memoire["pronostics"] = (time.time() + TTL_MEMOIRE_PRONOSTICS, table)
    return table


def _vue_pronostic(doc: Dict[str, Any], ev: Dict[str, Any], gele: bool) -> Dict[str, Any]:
    p1, pn, p2 = doc["probabiliteVictoireEquipe1"], doc["probabiliteMatchNul"], doc["probabiliteVictoireEquipe2"]
    favori = elo.favori(p1, pn, p2)
    return {
        "p1": p1, "pnul": pn, "p2": p2, "favori": favori, "confiance": doc["confiance"],
        "donnees_suffisantes": doc.get("donnees_suffisantes", True),
        "details": doc.get("details"), "gele": gele, "limites": modele.LIMITES,
        # Match terminé : le pronostic figé avant le match est confronté au résultat réel (match équilibré : non évalué).
        "correct": ((favori == ev["vainqueur"]) if favori != 0 else None)
        if ev["statut"] == "termine" and ev["vainqueur"] in (1, 2, 3) else None,
    }


async def _ajouter_pronostics(liste: List[Dict[str, Any]], maintenant: float) -> None:
    """Ajoute `pronostic` à chaque match à venir ou en cours de la liste. Zéro appel API ; toujours un pronostic."""
    classement = await _classement()
    geles = await _pronostics_geles()
    nouveaux: List[Dict[str, Any]] = []
    for ev in liste:
        ev["pronostic"] = None
        doc = geles.get(ev["id"])
        if doc:
            ev["pronostic"] = _vue_pronostic(doc, ev, gele=True)
            continue
        if ev["statut"] not in ("prevu", "inconnu", "en_direct"):
            continue
        p = elo.pronostiquer(classement, ev["equipe1"]["id"], ev["equipe2"]["id"], ev["best_of"], maintenant)["pronostic"]
        doc = {"eventId": ev["id"], "jeu": ev["jeu"], "jeu_nom": ev["jeu_nom"], "equipe1": ev["equipe1"]["nom"],
               "equipe2": ev["equipe2"]["nom"], "best_of": ev["best_of"], "debut": ev["debut"], "debut_ts": ev["ts"],
               "calcule_le": int(maintenant), "statut": "en_attente", "probabiliteVictoireEquipe1": p["p1"],
               "probabiliteMatchNul": p["pnul"], "probabiliteVictoireEquipe2": p["p2"], "confiance": p["confiance"],
               "donnees_suffisantes": p["donnees_suffisantes"], "details": p["details"]}
        # On ne FIGE que les matchs qui n'ont pas commencé : un pronostic calculé en cours de match n'est pas honnête.
        if ev["statut"] == "prevu" and ev["ts"] > maintenant:
            nouveaux.append(doc)
            geles[ev["id"]] = doc
            ev["pronostic"] = _vue_pronostic(doc, ev, gele=True)
        else:
            ev["pronostic"] = _vue_pronostic(doc, ev, gele=False)
    if nouveaux:
        await asyncio.to_thread(depot.sauver_pronostics, nouveaux)


async def _jours_memorises() -> Set[str]:
    """Identifiants (« csgo_2026-10-13 ») des jours déjà mémorisés ; relu si le cache a été invalidé."""
    expire, deja = _memoire["jours"]
    if expire <= time.time():
        deja = await asyncio.to_thread(depot.ids_resultats_jours)
        _memoire["jours"] = (time.time() + TTL_MEMOIRE_RESULTATS, deja)
    return deja


async def _rattraper_passe(maintenant_dt: datetime) -> int:
    """
    Récupère des jours passés pour nourrir le classement. Deux régimes :
      - AMORÇAGE (les 5 derniers jours ne sont pas tous mémorisés) : 4 jours par passage, tant qu'il reste plus
        que la réserve de fond. Sans cela, aucun pronostic n'apparaît.
      - ENTRETIEN (jusqu'à 45 jours) : 2 jours par passage, avec le quota EXCÉDENTAIRE seulement (≥ 30 restantes).
    """
    if time.time() < _memoire["pause_rattrapage"]:
        return 0
    deja = await _jours_memorises()
    recents = sum(1 for k in range(1, JOURS_AMORCAGE + 1) for slug in JEUX
                  if _id_jour(slug, maintenant_dt - timedelta(days=k)) in deja)
    amorcage = recents < JOURS_AMORCAGE * len(JEUX)
    seuil = client.RESERVE_ARRIERE_PLAN if amorcage else SEUIL_RATTRAPAGE
    par_passage = AMORCAGE_MAX_PAR_PASSAGE if amorcage else RATTRAPAGE_MAX_PAR_PASSAGE

    restant = client.quota()["restant"]
    if restant is None:
        # Compteur inconnu (tout vient du cache) : on tente seulement en amorçage ; un 429 déclencherait la pause.
        budget = par_passage if amorcage else 0
    else:
        budget = min(par_passage, restant - seuil)
    if budget <= 0 or not client.peut_appeler(False):
        return 0

    a_faire = []
    for k in range(1, JOURS_HISTORIQUE + 1):  # du plus récent au plus ancien
        jour = maintenant_dt - timedelta(days=k)
        a_faire += [(slug, jour) for slug in JEUX if _id_jour(slug, jour) not in deja]
    fait = 0
    for slug, jour in a_faire[:budget]:
        # persister=False : le contenu utile est déjà stocké dans esport_resultats, inutile de le doubler.
        rep = await client.appeler(_chemin_jour(JEUX[slug]["id"], jour), 3600, utilisateur=False, persister=False)
        if rep is None:
            _memoire["pause_rattrapage"] = time.time() + PAUSE_RATTRAPAGE_S
            break
        await _marquer_jour(slug, jour)  # les matchs terminés de la réponse sont déjà récoltés par le client
        fait += 1
    return fait


async def _progression_historique(maintenant_dt: datetime) -> Dict[str, Any]:
    deja = await _jours_memorises()  # relu si un enregistrement vient d'invalider le cache (sinon : « 0 jour » à tort)
    cibles = JOURS_HISTORIQUE * len(JEUX)
    couverts = sum(1 for k in range(1, JOURS_HISTORIQUE + 1) for slug in JEUX
                   if _id_jour(slug, maintenant_dt - timedelta(days=k)) in deja)
    recents = sum(1 for k in range(1, JOURS_AMORCAGE + 1) for slug in JEUX
                  if _id_jour(slug, maintenant_dt - timedelta(days=k)) in deja)
    return {"jours_couverts": couverts // len(JEUX), "jours_cibles": JOURS_HISTORIQUE,
            "matchs_memorises": len(await _resultats()),
            "amorcage_termine": recents >= JOURS_AMORCAGE * len(JEUX)}


# --------------------------------------------------------------------------
# Liste des matchs
# --------------------------------------------------------------------------

def _live_autorise(maintenant_dt: datetime) -> bool:
    jour = maintenant_dt.date().isoformat()
    if _live_depenses["jour"] != jour:
        _live_depenses.update(jour=jour, n=0)
    return _live_depenses["n"] < LIVE_MAX_PAR_JOUR


async def lister_matchs(jeu: Optional[str] = None) -> Dict[str, Any]:
    """
    Matchs en direct, à venir (aujourd'hui, demain) et terminés (24 h) pour un jeu ou tous, CHACUN avec son
    pronostic. Coût API : au plus 7 appels (1 direct + 2 jours × 3 jeux), presque toujours 0 grâce au cache
    persistant ; le direct est plafonné à LIVE_MAX_PAR_JOUR appels réels par jour.
    """
    slugs = [jeu] if jeu in JEUX else list(JEUX)
    maintenant_dt = datetime.now(timezone.utc)
    demain_dt = maintenant_dt + timedelta(days=1)

    # Ordre de PRIORITÉ si le quota ne permet pas tout : direct, puis aujourd'hui, puis demain.
    demandes: List[Dict[str, Any]] = [{"chemin": CHEMIN_LIVE, "ttl": TTL_LIVE, "nature": "live", "jeu": None, "quand": "direct"}]
    demandes += [{"chemin": _chemin_jour(JEUX[j]["id"], maintenant_dt), "ttl": TTL_LISTE_AUJOURDHUI, "nature": "jour",
                  "jeu": j, "quand": "aujourd'hui"} for j in slugs]
    demandes += [{"chemin": _chemin_jour(JEUX[j]["id"], demain_dt), "ttl": TTL_LISTE_DEMAIN, "nature": "jour",
                  "jeu": j, "quand": "demain"} for j in slugs]

    await client.precharger([d["chemin"] for d in demandes])  # remonte le cache persistant : 0 appel API
    en_cache_avant = {d["chemin"] for d in demandes if client.en_cache(d["chemin"])}

    live_plafonne = False
    if CHEMIN_LIVE not in en_cache_avant and not _live_autorise(maintenant_dt):
        demandes = [d for d in demandes if d["nature"] != "live"]
        live_plafonne = True

    restant = client.quota()["restant"]
    autorises = None if restant is None else max(0, restant - client.RESERVE_ARRIERE_PLAN)
    interrogees = []
    for d in demandes:
        if d["chemin"] in en_cache_avant:
            interrogees.append(d)  # gratuit
        elif autorises is None or autorises > 0:
            interrogees.append(d)
            if autorises is not None:
                autorises -= 1
            if d["nature"] == "live":
                _live_depenses["n"] += 1

    reponses: Dict[str, Any] = {}
    for debut in range(0, len(interrogees), LOT_REQUETES):  # par petits lots : jamais 7 requêtes d'un coup
        lot = interrogees[debut:debut + LOT_REQUETES]
        reps = await asyncio.gather(*[client.appeler(d["chemin"], d["ttl"], utilisateur=False) for d in lot])
        reponses.update({d["chemin"]: r for d, r in zip(lot, reps)})

    def _etat(d: Dict[str, Any]) -> str:
        if d not in interrogees:
            return "ignoré (quota insuffisant)"
        r = reponses.get(d["chemin"])
        if r is None:
            return "échec"
        if not r:
            return "vide (204)"
        origine = "cache" if d["chemin"] in en_cache_avant else "ok"
        return f"{origine} ({len(r.get('events') or [])} match(s))"

    diagnostic = [{"requete": d["quand"] if d["nature"] == "live" else f"{d['jeu']} {d['quand']}", "etat": _etat(d)}
                  for d in demandes]
    if live_plafonne:
        diagnostic.insert(0, {"requete": "direct", "etat": f"plafond de {LIVE_MAX_PAR_JOUR} appels/jour atteint"})

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
        evs = [e for e in (parsing.normaliser_evenement(b) for b in rep.get("events") or []) if e is not None]
        for ev in evs:
            if ev["jeu"] == d["jeu"]:
                matchs[ev["id"]] = ev

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

    # Quota excédentaire -> historique ; puis pronostics (aucun appel API dans _ajouter_pronostics).
    await _rattraper_passe(maintenant_dt)
    await _resultats()
    await _ajouter_pronostics(liste, maintenant)

    conseil = None
    if not liste and len(jeux_en_echec) == len(slugs) and live_en_echec:
        conseil = "Données esport indisponibles (quota de l'API épuisé ou service injoignable). Réessaie plus tard."
    elif jeux_en_echec:
        conseil = "Données incomplètes pour : " + ", ".join(sorted(JEUX[j]["nom"] for j in jeux_en_echec)) + "."
    return {"matchs": liste, "jeux_en_echec": sorted(jeux_en_echec), "direct_disponible": not live_en_echec,
            "quota": client.quota(), "conseil": conseil, "diagnostic": diagnostic,
            "historique": await _progression_historique(maintenant_dt)}


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
# Bilan (précision réelle des pronostics figés) — aucun appel API
# --------------------------------------------------------------------------

def _classe_predite(p: Dict[str, Any]) -> int:
    """1, 2, 3 (nul) ou 0 (match équilibré : aucun favori, donc ni juste ni faux)."""
    return elo.favori(p["probabiliteVictoireEquipe1"], p["probabiliteMatchNul"], p["probabiliteVictoireEquipe2"])


def _brier(p: Dict[str, Any], reel: int) -> float:
    cible = {1: 0.0, 3: 0.0, 2: 0.0}
    cible[reel] = 1.0
    return ((p["probabiliteVictoireEquipe1"] - cible[1]) ** 2
            + (p["probabiliteMatchNul"] - cible[3]) ** 2
            + (p["probabiliteVictoireEquipe2"] - cible[2]) ** 2)


async def bilan() -> Dict[str, Any]:
    """Confronte les pronostics figés aux résultats mémorisés. Ne consomme AUCUNE requête de l'API."""
    pronos = await asyncio.to_thread(depot.lister_pronostics)
    resultats = {m["id"]: m for m in await _resultats()}
    maintenant = time.time()
    for p in pronos:
        if p.get("statut") != "en_attente" or p.get("debut_ts", 1e18) > maintenant - DELAI_VERIFICATION_H * 3600:
            continue
        r = resultats.get(p["eventId"])
        if r is not None:
            champs = {"statut": "verifie", "resultat": r["v"], "score": f"{r['s1']}-{r['s2']}"}
        elif p["debut_ts"] < maintenant - EXPIRATION_PRONOSTIC_J * 86400:
            champs = {"statut": "ignore"}  # forfait, annulé ou jamais vu terminé : ne compte ni pour ni contre
        else:
            continue
        depot.maj_pronostic(p["eventId"], champs)
        p.update(champs)

    verifies = [p for p in pronos if p.get("statut") == "verifie"]
    n = len(verifies)
    avec_favori = [p for p in verifies if _classe_predite(p) != 0]
    bons = sum(1 for p in avec_favori if _classe_predite(p) == p["resultat"])
    solides = [p for p in avec_favori if p.get("donnees_suffisantes", True)]
    bons_solides = sum(1 for p in solides if _classe_predite(p) == p["resultat"])
    brier = sum(_brier(p, p["resultat"]) for p in verifies) / n if n else None
    return {
        "verifies": n,
        "en_attente": sum(1 for p in pronos if p.get("statut") == "en_attente"),
        "equilibres": n - len(avec_favori),   # matchs sans favori : exclus du taux de réussite
        "precision": round(bons / len(avec_favori), 3) if avec_favori else None,
        # Même mesure, uniquement quand chaque équipe avait assez de matchs connus : la plus représentative.
        "precision_donnees_suffisantes": round(bons_solides / len(solides), 3) if solides else None,
        "verifies_donnees_suffisantes": len(solides),
        "brier": round(brier, 4) if brier is not None else None,
        "fiable": len(solides) >= MIN_BILAN_FIABLE,
        "note": ("Échantillon trop petit pour conclure (moins de "
                 f"{MIN_BILAN_FIABLE} pronostics vérifiés avec des données suffisantes)." if len(solides) < MIN_BILAN_FIABLE else
                 "Précision mesurée sur des pronostics figés avant les matchs (sans biais temporel)."),
        "derniers": [
            {"equipe1": p["equipe1"], "equipe2": p["equipe2"], "predit": _classe_predite(p),
             "reel": p["resultat"], "score": p.get("score")}
            for p in sorted(verifies, key=lambda p: p["debut_ts"], reverse=True)[:5]
        ],
    }


# Chaque réponse de l'API (neuve ou relue du cache persistant) passe par recolter().
client.RECOLTEUR = recolter
