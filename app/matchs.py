"""
Liste des matchs du jour / J+1 — FUSION de CINQ fournisseurs, appelés
EN PARALLÈLE (asyncio.gather), chacun derrière un disjoncteur
(sources/sante.py : 3 échecs de suite = pause de 30 min) :
  - SportAPI7 (format Sofascore) : couverture mondiale, chemin de liste à
    vérifier (voir sources/sportapi7.py). Priorité la plus basse.
  - football-data.org  : ~12 grandes compétitions, données officielles propres.
  - TheSportsDB        : ~617 championnats, large mais base contributive.
  - API-Football/RapidAPI : ~1 236 championnats (Afrique/Asie/Amérique du
    Sud comprises), compte RapidAPI séparé de celui suspendu sur
    api-sports.io direct.
  - OpenLigaDB         : football allemand uniquement, mais sans clé ni
    limite de débit, avec un bon niveau de détail sur les divisions
    inférieures allemandes.

Chaque module sources/ normalise DÉJÀ ses matchs au même format interne
{fixture_id (préfixé par source : "fd-", "tsdb-", "rapid-", "openliga-"),
date, league_id, league, equipe1, equipe2, donneesDisponibles=None} —
matchs.py n'a donc qu'à fusionner, pas à re-parser chaque format brut.

Un même match remonté par plusieurs sources est dédupliqué (voir
_cle_dedup, par date + équipes normalisées). Ordre de priorité en cas de
doublon (le dernier assigné dans la fusion l'emporte) : OpenLigaDB <
TheSportsDB < RapidAPI < football-data.org — données officielles
d'abord, niche en dernier recours.

L'identifiant de chaque match (fixture_id) est recalculé par un hash
STABLE de (jour, équipe1, équipe2) — jamais l'id brut d'un fournisseur.
Ça garantit que le même match garde le même fixture_id d'un
rafraîchissement à l'autre même s'il change de fournisseur "gagnant"
entre deux appels — sans ça, la disponibilité BeSoccer déjà vérifiée
serait perdue à chaque changement de source.

Enrichissement (free-api-live-football-data) : logos d'équipes, score,
statut (prevu / en_direct / termine / reporte / annule), minute en
direct, nom + logo + pays + rang du championnat (ligues.py). Les scores
en direct sont appliqués À LA LECTURE par appliquer_direct, avec un
budget d'appels quasi nul : un seul appel /football-current-live (cache
30 s) et seulement s'il existe un match proche de son coup d'envoi ou
en cours — voir les constantes FENETRE_* ci-dessous.

Disponibilité des données BeSoccer : vérifiée EN ARRIÈRE-PLAN, en
priorité pour les matchs dont le coup d'envoi approche, et plafonnée
par cycle (MAX_VERIFICATIONS_PAR_CYCLE) — vérifier tous les matchs
d'une journée mondiale d'un coup a déjà provoqué un blocage BeSoccer
par le passé avec seulement 150 clubs d'un coup (voir sources/besoccer.py).

Un match reste cliquable dans l'appli UNIQUEMENT une fois
donneesDisponibles=true (voir MatchsScreen.kt côté Android).
"""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
from .sources import thesportsdb, football_data, livefootball_rapidapi, openliga, besoccer, sportapi7, sante
from . import repo, ligues, normalisation

# Nombre max de vérifications BeSoccer (équipes) par cycle en arrière-plan.
MAX_VERIFICATIONS_PAR_CYCLE = 60

# Champs apportés par le fournisseur RapidAPI. Quand le même match remonte
# de plusieurs sources, la source "officielle" gagne pour le nom du
# championnat, mais on garde ces champs de l'autre doublon s'ils manquent.
_CHAMPS_ENRICHIS = (
    "rapid_id", "rapid_league_id", "equipe1_id", "equipe2_id", "sofa_id",
    "logo1", "logo2", "statut", "score1", "score2", "minute",
    "league", "league_pays",
)

# Fenêtre pendant laquelle un match mérite une vérification en direct :
# de 15 min avant le coup d'envoi à 170 min après (prolongations incluses).
FENETRE_AVANT_COUP_ENVOI_MIN = 15
FENETRE_APRES_COUP_ENVOI_MIN = 170

# Fenêtre de coups d'envoi dans laquelle deux entrées de sources différentes
# peuvent désigner le même match (les sources diffèrent parfois de quelques minutes).
FENETRE_FUSION_MIN = 90
SEUIL_SIMILARITE_EQUIPE = 0.82

# Ordre de priorité CROISSANTE : en cas de doublon, la dernière source l'emporte
# (données officielles en dernier), et hérite des champs enrichis des autres.
SOURCES = (
    ("sportapi7", sportapi7),
    ("openliga", openliga),
    ("thesportsdb", thesportsdb),
    ("rapidapi", livefootball_rapidapi),
    ("football-data", football_data),
)


def _jour_iso(offset: int = 0) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def _normaliser_nom(nom: str) -> str:
    return normalisation.normaliser(nom)


def _cle_dedup(jour: str, equipe1: str, equipe2: str) -> str:
    return f"{jour}|{_normaliser_nom(equipe1)}|{_normaliser_nom(equipe2)}"


def _fixture_id_stable(jour: str, equipe1: str, equipe2: str) -> int:
    """Hash déterministe — voir docstring en tête de fichier."""
    empreinte = hashlib.sha1(_cle_dedup(jour, equipe1, equipe2).encode("utf-8")).hexdigest()
    return int(empreinte[:15], 16)  # tient largement dans un Long 64 bits côté Android


def _timestamp(m: Dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(m["date"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _meme_match(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    n1a, n1b = _normaliser_nom(a["equipe1"]), _normaliser_nom(b["equipe1"])
    n2a, n2b = _normaliser_nom(a["equipe2"]), _normaliser_nom(b["equipe2"])
    if n1a == n1b and n2a == n2b:
        return True
    if a.get("_src") == b.get("_src"):
        return False  # deux matchs distincts d'une même source ne fusionnent jamais en flou
    return (normalisation.similarite(n1a, n1b) >= SEUIL_SIMILARITE_EQUIPE
            and normalisation.similarite(n2a, n2b) >= SEUIL_SIMILARITE_EQUIPE)


def _regrouper(entrees: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Union-find sur les entrées triées par coup d'envoi ; seules les entrées
    à moins de FENETRE_FUSION_MIN minutes sont comparées (coût quasi linéaire)."""
    entrees = sorted(entrees, key=_timestamp)
    parent = list(range(len(entrees)))

    def racine(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ts = [_timestamp(e) for e in entrees]
    for i in range(len(entrees)):
        j = i + 1
        while j < len(entrees) and ts[j] - ts[i] <= FENETRE_FUSION_MIN * 60:
            if racine(i) != racine(j) and _meme_match(entrees[i], entrees[j]):
                parent[racine(j)] = racine(i)
            j += 1
    groupes: Dict[int, List[Dict[str, Any]]] = {}
    for i, e in enumerate(entrees):
        groupes.setdefault(racine(i), []).append(e)
    return list(groupes.values())


def _recalculer_id(m: Dict[str, Any]) -> Dict[str, Any]:
    m = dict(m)
    m["fixture_id"] = _fixture_id_stable(m["date"][:10], m["equipe1"], m["equipe2"])
    return m


def _fusionner_doublon(ancien: Optional[Dict[str, Any]], nouveau: Dict[str, Any]) -> Dict[str, Any]:
    """`nouveau` l'emporte, mais hérite des champs enrichis (logos, score,
    statut, identifiants RapidAPI) que `ancien` possède et lui non."""
    if ancien is None:
        return nouveau
    fusion = dict(nouveau)
    for champ in _CHAMPS_ENRICHIS:
        if fusion.get(champ) in (None, "") and ancien.get(champ) not in (None, ""):
            fusion[champ] = ancien[champ]
    return fusion


async def _collecter_sources(jour_iso: str) -> List[Optional[List[Dict[str, Any]]]]:
    """Interroge toutes les sources EN PARALLÈLE, chacune derrière son disjoncteur.
    Renvoie une liste alignée sur SOURCES ; None = échec/pause de cette source."""

    async def une(nom: str, module: Any):
        # `module.get_fixtures_du_jour` est résolu au moment de l'appel (substituable en test).
        return await sante.appeler(nom, lambda: module.get_fixtures_du_jour(jour_iso))

    return list(await asyncio.gather(*[une(nom, module) for nom, module in SOURCES]))


def _fusionner_groupe(groupe: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Un groupe de doublons (même match, sources différentes) -> un seul match.
    La source de plus haute priorité l'emporte et hérite des champs enrichis des autres."""
    groupe = sorted(groupe, key=lambda e: e["_prio"])
    fusion: Optional[Dict[str, Any]] = None
    for e in groupe:
        fusion = _fusionner_doublon(fusion, e)
    assert fusion is not None
    sources = sorted({e["_src"] for e in groupe})
    fusion = {k: v for k, v in fusion.items() if not k.startswith("_")}
    fusion["sources"] = sources
    fusion["nb_sources"] = len(sources)
    return fusion


async def _rafraichir_jour(jour_iso: str) -> List[Dict[str, Any]]:
    resultats = await _collecter_sources(jour_iso)

    # Chaque source renvoie None uniquement en cas d'échec total (clé
    # absente, quota, pause du disjoncteur, erreur réseau) — jamais pour
    # une liste simplement vide. Si TOUTES échouent, on garde le cache.
    if all(r is None for r in resultats):
        return repo.lister_matchs_jour(jour_iso)

    entrees: List[Dict[str, Any]] = []
    for prio, ((nom, _module), liste) in enumerate(zip(SOURCES, resultats)):
        for m in liste or []:
            e = dict(m)
            e["_src"], e["_prio"] = nom, prio
            entrees.append(e)

    if not entrees:
        return repo.lister_matchs_jour(jour_iso)

    # Statut de disponibilité déjà connu pour un match déjà vu : on réutilise
    # l'ancien fixture_id si l'un des membres du groupe y correspond (sinon la
    # vérification BeSoccer déjà faite serait perdue quand le fournisseur
    # "gagnant" ou l'orthographe change entre deux rafraîchissements).
    deja_connus = {m["fixture_id"]: m for m in repo.lister_matchs_jour(jour_iso)}

    matchs: List[Dict[str, Any]] = []
    ids_utilises = set()
    for groupe in _regrouper(entrees):
        fusion = _fusionner_groupe(groupe)
        candidats = [
            _fixture_id_stable(e["date"][:10], e["equipe1"], e["equipe2"])
            for e in sorted(groupe, key=lambda e: e["_prio"], reverse=True)  # le gagnant d'abord
        ]
        fid = next((c for c in candidats if c in deja_connus and c not in ids_utilises), candidats[0])
        if fid in ids_utilises:  # deux matchs distincts le même jour avec les mêmes équipes (très rare)
            fid = int(hashlib.sha1(f"{fid}|{fusion['date']}".encode("utf-8")).hexdigest()[:15], 16)
        ids_utilises.add(fid)
        fusion["fixture_id"] = fid
        ancien = deja_connus.get(fid)
        if ancien and ancien.get("donneesDisponibles") is not None:
            fusion["donneesDisponibles"] = ancien["donneesDisponibles"]
        matchs.append(fusion)
    matchs.sort(key=lambda m: m.get("date", ""))

    # Noms / logos / rang des championnats : résolus avant sauvegarde pour
    # que la liste persistée soit déjà lisible (les inconnus restants se
    # résoudront progressivement, voir completer_ligues).
    try:
        await ligues.completer([m.get("rapid_league_id") for m in matchs], jour_iso)
    except Exception:
        pass  # purement cosmétique — ne doit jamais bloquer la liste
    matchs = ligues.appliquer(matchs)

    repo.enregistrer_matchs_jour(jour_iso, matchs)
    repo.marquer_matchs_rafraichis(jour_iso)
    return matchs


def _minutes_avant_coup_envoi(match: Dict[str, Any]) -> float:
    """Plus petit = plus urgent à vérifier. Un match déjà commencé depuis
    plus de 3h passe en dernier (ni urgent, ni fiable à re-vérifier)."""
    try:
        coup_envoi = datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
    except Exception:
        return float("inf")
    delta_minutes = (coup_envoi - datetime.now(timezone.utc)).total_seconds() / 60
    return delta_minutes if delta_minutes >= -180 else float("inf")


async def _verifier_un_match(match: Dict[str, Any]) -> bool:
    """Réutilise le cache BeSoccer existant (get_team_stats, TTL 6h
    succès / 1h échec) — une équipe déjà vue pour un autre match du jour
    ne redéclenche aucune requête HTTP supplémentaire."""
    stats1 = await besoccer.get_team_stats(match["equipe1"])
    stats2 = await besoccer.get_team_stats(match["equipe2"])
    return stats1 is not None and stats2 is not None


async def verifier_disponibilite_prochains(jour_iso: str) -> None:
    """
    Tâche de fond (voir BackgroundTasks dans main.py) : vérifie la
    disponibilité BeSoccer des matchs pas encore vérifiés pour ce jour,
    en commençant par ceux dont le coup d'envoi est le plus proche.
    Volontairement plafonnée à MAX_VERIFICATIONS_PAR_CYCLE — les matchs
    restants seront traités lors d'un prochain appel à /matchs.
    """
    matchs = repo.lister_matchs_jour(jour_iso)
    a_verifier = [m for m in matchs if m.get("donneesDisponibles") is None]
    if not a_verifier:
        return

    a_verifier.sort(key=_minutes_avant_coup_envoi)
    a_verifier = a_verifier[:MAX_VERIFICATIONS_PAR_CYCLE]

    taille_lot = 3
    for i in range(0, len(a_verifier), taille_lot):
        lot = a_verifier[i : i + taille_lot]
        resultats = await asyncio.gather(*[_verifier_un_match(m) for m in lot])
        for m, dispo in zip(lot, resultats):
            repo.marquer_disponibilite_match(m["fixture_id"], dispo)
        if i + taille_lot < len(a_verifier):
            await asyncio.sleep(2.0)


def _minutes_depuis_coup_envoi(match: Dict[str, Any]) -> Optional[float]:
    try:
        coup_envoi = datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - coup_envoi).total_seconds() / 60


async def appliquer_direct(matchs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Met à jour statut / score / minute des matchs en cours ou sur le point
    de commencer, à partir de /football-current-live (1 appel, cache 30 s).

    - Aucun appel réseau s'il n'y a aucun match dans la fenêtre
      (voir FENETRE_*) : zéro quota dépensé la plupart du temps.
    - Appel en échec : on garde les scores connus mais on retire la minute
      (une minute périmée serait pire que pas de minute).
    - Un match "en_direct" qui n'est plus dans le direct passe "termine"
      avec son dernier score connu.
    - Un match encore "prevu" bien après la fin possible est clos sans
      score (aucun appel : on ne sait plus rien de lui).
    - Seuls les changements de statut/score sont persistés ; la minute ne
      l'est jamais (elle n'a aucun sens une minute plus tard).
    """
    suivis = []
    for m in matchs:
        if not m.get("rapid_id") or m.get("statut") not in ("prevu", "en_direct"):
            continue
        ecoule = _minutes_depuis_coup_envoi(m)
        if ecoule is not None and -FENETRE_AVANT_COUP_ENVOI_MIN <= ecoule <= FENETRE_APRES_COUP_ENVOI_MIN:
            suivis.append(m["fixture_id"])

    direct = await livefootball_rapidapi.get_en_direct() if suivis else None
    suivis_set = set(suivis)

    sortie: List[Dict[str, Any]] = []
    modifications: List[Dict[str, Any]] = []
    for m in matchs:
        n = dict(m)
        avant = (m.get("statut"), m.get("score1"), m.get("score2"))
        if n["fixture_id"] in suivis_set:
            if direct is None:
                n["minute"] = None
            elif n["rapid_id"] in direct:
                live = direct[n["rapid_id"]]
                n.update(statut="en_direct", score1=live["score1"], score2=live["score2"], minute=live["minute"])
            else:
                n["minute"] = None
                if n.get("statut") == "en_direct":
                    n["statut"] = "termine"
        elif n.get("rapid_id") and n.get("statut") in ("prevu", "en_direct"):
            ecoule = _minutes_depuis_coup_envoi(n)
            if ecoule is not None and ecoule > FENETRE_APRES_COUP_ENVOI_MIN:
                n["statut"] = "termine"
                n["minute"] = None
        apres = (n.get("statut"), n.get("score1"), n.get("score2"))
        if apres != avant:
            modifications.append({
                "fixture_id": n["fixture_id"],
                "champs": {"statut": n["statut"], "score1": n.get("score1"), "score2": n.get("score2"), "minute": None},
            })
        sortie.append(n)

    if modifications:
        try:
            await asyncio.to_thread(repo.maj_champs_matchs, modifications)
        except Exception:
            pass  # l'affichage reste correct même si la persistance échoue
    return sortie


async def completer_ligues(jour_iso: str) -> None:
    """Tâche de fond : résout progressivement les championnats encore inconnus."""
    try:
        matchs = repo.lister_matchs_jour(jour_iso)
        await ligues.completer([m.get("rapid_league_id") for m in matchs], jour_iso)
    except Exception:
        pass


async def obtenir_matchs(jour: str) -> Dict[str, Any]:
    """
    jour: "today" ou "tomorrow". Renvoie la liste déjà en cache si elle a
    moins de TTL_RAFRAICHISSEMENT_MATCHS_SECONDES, sinon la reconstruit
    (quatre fournisseurs en parallèle). La vérification BeSoccer se fait
    à part, en tâche de fond.
    """
    jour_cible = _jour_iso(0 if jour == "today" else 1)

    dernier = repo.dernier_rafraichissement_matchs(jour_cible)
    trop_ancien = (
        dernier is None
        or (datetime.now(timezone.utc) - dernier).total_seconds()
        > TTL_RAFRAICHISSEMENT_MATCHS_SECONDES
    )

    if trop_ancien:
        matchs = await _rafraichir_jour(jour_cible)
    else:
        matchs = repo.lister_matchs_jour(jour_cible)

    matchs = ligues.appliquer(matchs)  # mémoire seule, aucun appel réseau
    matchs = await appliquer_direct(matchs)
    return {"jour": jour_cible, "matchs": matchs}
