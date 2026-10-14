"""
Annuaire des championnats : leagueId (fournisseur RapidAPI) -> nom, pays,
rang d'importance, championnat parent, logo.

Pourquoi ce module existe : /football-get-matches-by-date ne renvoie
qu'un leagueId numérique, d'où l'ancien affichage "Ligue 920744" dans
l'appli. Et l'annuaire officiel (/football-get-all-leagues-with-countries)
ne suffit pas : sur un échantillon réel du 1er octobre 2026, il ne
reconnaissait que 12 matchs sur 68, car beaucoup de leagueId sont des
identifiants "de saison / de phase" (qualifs de la Coupe du monde, MLS,
Ligue des champions féminine, ligues colombiennes en phase Clausura...).

Stratégie, par ordre de coût croissant, chaque résultat étant mémorisé
(mémoire + un seul document Firestore) pour ne JAMAIS être redemandé :
  1. annuaire complet     (1 appel, au plus 1 fois / 7 jours)
  2. endpoint groupé par championnat pour la date (1 appel, seulement
     s'il reste des inconnus) — donne aussi le pays, le rang d'importance
     et le championnat parent
  3. détail d'un championnat (1 appel chacun, plafonné par passage :
     MAX_DETAILS_PAR_PASSAGE) pour les derniers inconnus ; un échec est
     mémorisé et ne sera retenté qu'après DELAI_RETENTATIVE.

`appliquer()` ne fait AUCUN appel réseau : il lit juste la mémoire. Les
inconnus restants se résolvent donc progressivement, passage après
passage (voir main.lister_matchs, qui lance `completer` en tâche de fond).
"""

import asyncio
import time
from typing import Any, Dict, Iterable, List, Optional

from .sources import livefootball_rapidapi as rapid
from . import repo

MAX_DETAILS_PAR_PASSAGE = 3
DELAI_RETENTATIVE = 24 * 3600
VALIDITE_ANNUAIRE = 7 * 24 * 3600
VALIDITE_POPULAIRES = 24 * 3600

# Libellés que les anciennes versions stockaient faute de mieux — on les
# considère comme "pas de vrai nom" pour les remplacer par le vrai.
_NOMS_GENERIQUES_PREFIXE = "Ligue "

# Libellé provisoire posé tant qu'un championnat n'est pas résolu ; remplacé dès qu'il l'est.
_NOM_PROVISOIRE = "Autre compétition"

_ligues: Optional[Dict[str, Dict[str, Any]]] = None  # str(id) -> entrée
_meta: Dict[str, Any] = {}
_verrou = asyncio.Lock()


def _charger() -> None:
    global _ligues, _meta
    if _ligues is not None:
        return
    doc = repo.charger_annuaire_ligues() or {}
    _ligues = dict(doc.get("ligues") or {})
    _meta = {
        "annuaire_maj": doc.get("annuaire_maj", 0),
        "populaires": list(doc.get("populaires") or []),
        "populaires_maj": doc.get("populaires_maj", 0),
    }


def _sauver() -> None:
    try:
        repo.enregistrer_annuaire_ligues({"ligues": _ligues, **_meta})
    except Exception:
        pass  # l'annuaire reste en mémoire ; pas de raison de faire échouer une requête


def _connue(ligue_id: int) -> bool:
    e = (_ligues or {}).get(str(ligue_id))
    return bool(e) and not e.get("inconnu")


def _a_retenter(ligue_id: int, maintenant: float) -> bool:
    e = (_ligues or {}).get(str(ligue_id))
    if not e:
        return True
    return bool(e.get("inconnu")) and maintenant - e.get("tente_le", 0) > DELAI_RETENTATIVE


def _fusionner_entree(ligue_id: int, nouvelle: Dict[str, Any], ecraser: bool) -> None:
    """Fusionne sans jamais perdre une information plus riche déjà connue
    (ex: le rang vient de l'endpoint groupé ; l'annuaire complet, qui
    met rang=0, ne doit pas l'écraser)."""
    assert _ligues is not None
    cle = str(ligue_id)
    ancienne = _ligues.get(cle)
    if not ancienne or ancienne.get("inconnu"):
        _ligues[cle] = nouvelle
        return
    if ecraser:
        fusion = dict(ancienne)
        fusion.update({k: v for k, v in nouvelle.items() if v not in (None, "")})
        if nouvelle.get("rang", 0) == 0:
            fusion["rang"] = ancienne.get("rang", 0)
        _ligues[cle] = fusion


async def completer(ligue_ids: Iterable[Optional[int]], jour_iso: str) -> None:
    """Résout ce qui peut l'être parmi `ligue_ids` (voir stratégie en tête
    de fichier). Ne lève jamais : un échec réseau = on réessaiera au
    prochain passage."""
    ids = sorted({int(i) for i in ligue_ids if i})
    if not ids:
        return

    async with _verrou:
        _charger()
        maintenant = time.time()
        modifie = False

        # --- 1. annuaire complet (au plus 1 fois / 7 jours) -------------
        if any(not _connue(i) for i in ids) and maintenant - _meta["annuaire_maj"] > VALIDITE_ANNUAIRE:
            annuaire = await rapid.get_annuaire_ligues()
            if annuaire:
                for i, entree in annuaire.items():
                    _fusionner_entree(i, entree, ecraser=False)
                _meta["annuaire_maj"] = maintenant
                modifie = True

        # --- ligues populaires (au plus 1 fois / 24 h) -------------------
        if maintenant - _meta["populaires_maj"] > VALIDITE_POPULAIRES:
            pop = await rapid.get_ligues_populaires()
            if pop:
                _meta["populaires"] = pop
                _meta["populaires_maj"] = maintenant
                modifie = True

        # --- 2. endpoint groupé : seulement s'il reste des inconnus ------
        inconnus = [i for i in ids if not _connue(i) and _a_retenter(i, maintenant)]
        if inconnus:
            groupees = await rapid.get_ligues_du_jour_groupees(jour_iso)
            if groupees:
                for i, entree in groupees.items():
                    _fusionner_entree(i, entree, ecraser=True)
                modifie = True
            inconnus = [i for i in inconnus if not _connue(i)]

        # --- 3. détail, plafonné ----------------------------------------
        if inconnus:
            a_traiter = inconnus[:MAX_DETAILS_PAR_PASSAGE]
            details = await asyncio.gather(*[rapid.get_detail_ligue(i) for i in a_traiter])
            for i, d in zip(a_traiter, details):
                if d:
                    _ligues[str(i)] = d
                else:
                    _ligues[str(i)] = {"inconnu": True, "tente_le": maintenant}
            modifie = True

        if modifie:
            _sauver()


def _rang(ligue_id: int, entree: Dict[str, Any]) -> int:
    """Les 10 championnats "populaires" passent en tête (100, 99, ...),
    puis le rang d'importance du fournisseur (0 à 10)."""
    reel = entree.get("parent") or ligue_id
    populaires: List[int] = _meta.get("populaires") or []
    if reel in populaires:
        return 100 - populaires.index(reel)
    return int(entree.get("rang") or 0)


def appliquer(matchs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ajoute à chaque match issu du fournisseur RapidAPI (champ
    rapid_league_id) : league (si le nom manque), league_pays,
    league_logo, league_rang. Lecture mémoire uniquement — aucun appel
    réseau. Renvoie de nouveaux dictionnaires (les entrées d'origine ne
    sont pas modifiées)."""
    _charger()
    assert _ligues is not None
    sortie = []
    for m in matchs:
        m = dict(m)
        rid = m.get("rapid_league_id")
        entree = _ligues.get(str(rid)) if rid else None
        if entree and not entree.get("inconnu"):
            nom_actuel = m.get("league") or ""
            if not nom_actuel or nom_actuel.startswith(_NOMS_GENERIQUES_PREFIXE) or nom_actuel == _NOM_PROVISOIRE:
                m["league"] = entree["nom"]
            m["league_pays"] = entree.get("pays")
            m["league_logo"] = rapid.URL_LOGO_LIGUE.format(id=entree.get("parent") or rid)
            m["league_rang"] = _rang(int(rid), entree)
        elif rid and not m.get("league"):
            m["league"] = _NOM_PROVISOIRE  # mieux que "Ligue 920744" tant que non résolu
        sortie.append(m)
    return sortie


def ligue_parente(ligue_id: Optional[int]) -> Optional[int]:
    """Id du championnat "réel" (celui qui a un classement) pour un
    leagueId de saison/phase — sert à /football-get-standing-all."""
    if not ligue_id:
        return None
    _charger()
    assert _ligues is not None
    entree = _ligues.get(str(ligue_id))
    if entree and not entree.get("inconnu") and entree.get("parent"):
        return int(entree["parent"])
    return int(ligue_id)
