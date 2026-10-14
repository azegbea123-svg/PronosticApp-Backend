"""
Normalisation PURE (sans réseau) des réponses de l'Esport API. Les formes
traitées ici sont celles des captures réelles du playground
(tests/fixtures/esport_*.json) :

  événement : {tournament{category{name,slug}}, homeTeam, awayTeam, homeScore{current,...},
               awayScore, status{type,description}, winnerCode, bestOf, startTimestamp,
               customId, id}
  `homeScore.current` = nombre de CARTES (maps) gagnées dans la série ; les `periodN`
  sont les manches (rounds) de chaque carte.
  winnerCode : 1 = équipe 1 (domicile), 2 = équipe 2, 3 = nul.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

_STATUTS = {
    "notstarted": "prevu",
    "inprogress": "en_direct",
    "finished": "termine",
    "postponed": "reporte",
    "canceled": "annule",
    "cancelled": "annule",
}


def _equipe(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": t.get("id"),
        "nom": t.get("name") or t.get("shortName") or "?",
        "code": t.get("nameCode"),
        "pays": (t.get("country") or {}).get("alpha3"),
    }


def normaliser_evenement(ev: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Un événement brut -> format interne. None si incomplet (jamais d'exception)."""
    if not isinstance(ev, dict):
        return None
    try:
        statut_brut = ev.get("status") or {}
        statut = _STATUTS.get((statut_brut.get("type") or "").lower(), "inconnu")
        forfait = "walkover" in (statut_brut.get("description") or "").lower()
        ts = int(ev["startTimestamp"])
        categorie = (ev.get("tournament") or {}).get("category") or {}
        a_un_score = statut in ("en_direct", "termine") and not forfait
        gagnant = ev.get("winnerCode")
        return {
            "id": ev["id"],
            "custom_id": ev.get("customId"),
            "jeu": categorie.get("slug"),
            "jeu_nom": categorie.get("name"),
            "tournoi": (ev.get("tournament") or {}).get("name"),
            "equipe1": _equipe(ev["homeTeam"]),
            "equipe2": _equipe(ev["awayTeam"]),
            "statut": statut,
            "forfait": forfait,
            "ts": ts,
            "debut": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "best_of": ev.get("bestOf"),
            "score1": (ev.get("homeScore") or {}).get("current") if a_un_score else None,
            "score2": (ev.get("awayScore") or {}).get("current") if a_un_score else None,
            "vainqueur": gagnant if statut == "termine" and not forfait and gagnant in (1, 2, 3) else None,
        }
    except (KeyError, TypeError, ValueError):
        return None


def vue_equipe(ev: Dict[str, Any], equipe_id: int) -> Optional[Dict[str, Any]]:
    """Un événement normalisé vu depuis UNE équipe (None si elle n'y joue pas)."""
    if ev["equipe1"]["id"] == equipe_id:
        moi, lui, pour, contre, gagne_code = ev["equipe1"], ev["equipe2"], ev["score1"], ev["score2"], 1
    elif ev["equipe2"]["id"] == equipe_id:
        moi, lui, pour, contre, gagne_code = ev["equipe2"], ev["equipe1"], ev["score2"], ev["score1"], 2
    else:
        return None
    return {
        "ts": ev["ts"],
        "adversaire_id": lui["id"],
        "adversaire": lui["nom"],
        "cartes_pour": pour,
        "cartes_contre": contre,
        "victoire": None if ev["vainqueur"] is None else (ev["vainqueur"] == gagne_code),
        "nul": ev["vainqueur"] == 3,
        "forfait": ev["forfait"],
        "termine": ev["statut"] == "termine",
        "best_of": ev["best_of"],
    }


def historique_equipe(reponse: Optional[Dict[str, Any]], equipe_id: int) -> list:
    """Réponse `team/{id}/matches/previous/N` -> vues de l'équipe, matchs TERMINÉS et NON forfaits, du plus récent au plus ancien."""
    sortie = []
    for brut in (reponse or {}).get("events") or []:
        ev = normaliser_evenement(brut)
        if ev is None or ev["statut"] != "termine" or ev["forfait"]:
            continue
        v = vue_equipe(ev, equipe_id)
        if v is not None and v["cartes_pour"] is not None and v["cartes_contre"] is not None:
            sortie.append(v)
    sortie.sort(key=lambda x: x["ts"], reverse=True)
    return sortie
