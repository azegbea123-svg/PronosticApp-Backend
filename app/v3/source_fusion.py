"""Fusion et dédoublonnage multi-sources.

Principe : plusieurs fournisseurs peuvent décrire le même match. On ne
additionne jamais leurs mêmes buts/formes comme s'il s'agissait de matchs
différents. On construit d'abord une identité canonique du match puis on
fusionne les champs complémentaires avec une provenance explicite.
"""
from __future__ import annotations
import re, unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SUFFIXES = (" fc", " cf", " sc", " afc", " cfc", " ac", " u21", " u23")
SOURCE_PRIORITY = {
    "football-data.org": 100,
    "free-api-live-football-data": 90,
    "Sofascore": 85,
    "SportAPI": 80,
    "AllSportsAPI": 70,
    "TheSportsDB": 60,
    "OpenLigaDB": 55,
    "FlashLive": 50,
    "FootballData1": 40,
    "SoccerData": 30,
}

def normaliser_nom(nom: Any) -> str:
    s = str(nom or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    for suffix in _SUFFIXES:
        ss = suffix.strip()
        if s.endswith(ss):
            s = s[:-len(ss)].strip()
    return re.sub(r"\s+", " ", s)

def _date_key(value: Any) -> str:
    if not value:
        return ""
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return s[:10]

def match_key(match: Dict[str, Any]) -> str:
    date = _date_key(match.get("date") or match.get("startTimestamp"))
    home = normaliser_nom(match.get("equipe1") or match.get("home_team") or match.get("home"))
    away = normaliser_nom(match.get("equipe2") or match.get("away_team") or match.get("away"))
    return f"{date}|{home}|{away}"

def _richness(m: Dict[str, Any]) -> int:
    fields = ("date", "league", "league_id", "event_id", "statut", "score", "odds", "forme", "statistics", "venue")
    return sum(1 for f in fields if m.get(f) not in (None, "", [], {}))

def normaliser_match(source: str, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Accepte uniquement un enregistrement explicitement football."""
    if not isinstance(raw, dict):
        return None
    home = raw.get("equipe1") or raw.get("home_team")
    away = raw.get("equipe2") or raw.get("away_team")
    if isinstance(raw.get("home"), dict):
        home = home or raw["home"].get("name")
    if isinstance(raw.get("away"), dict):
        away = away or raw["away"].get("name")
    if not home or not away:
        return None
    sport = raw.get("sport")
    if isinstance(sport, dict):
        sport = sport.get("name") or sport.get("slug")
    if sport and str(sport).lower() not in ("football", "soccer", "1"):
        return None
    date = raw.get("date") or raw.get("start_at") or raw.get("startTimestamp")
    if not date:
        return None
    out = dict(raw)
    out["equipe1"] = str(home)
    out["equipe2"] = str(away)
    out["date"] = str(date)
    out["source"] = source
    out["source_priority"] = SOURCE_PRIORITY.get(source, 10)
    out["canonical_key"] = match_key(out)
    return out

def fusionner_matchs(groupes: Iterable[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Fusionne des listes de matchs et conserve les champs complémentaires."""
    fusion: Dict[str, Dict[str, Any]] = {}
    for groupe in groupes:
        for raw in groupe or []:
            source = raw.get("source", "unknown") if isinstance(raw, dict) else "unknown"
            m = normaliser_match(source, raw) if isinstance(raw, dict) else None
            if not m:
                continue
            key = m["canonical_key"]
            if key not in fusion:
                fusion[key] = dict(m)
                fusion[key]["sources"] = [source]
                continue
            cur = fusion[key]
            if source not in cur.setdefault("sources", []):
                cur["sources"].append(source)
            # Le fournisseur prioritaire gagne pour les champs conflictuels,
            # mais les champs absents sont toujours complétés.
            if m.get("source_priority", 0) > cur.get("source_priority", 0):
                for field in ("date", "league", "league_id", "event_id", "statut", "score"):
                    if m.get(field) not in (None, "", [], {}):
                        cur[field] = m[field]
                cur["source"] = source
                cur["source_priority"] = m["source_priority"]
            for k, v in m.items():
                if k in ("source", "source_priority", "canonical_key", "sources"):
                    continue
                if cur.get(k) in (None, "", [], {}):
                    cur[k] = v
    return list(fusion.values())

def football_payload_ok(payload: Any) -> bool:
    """Garde-fou contre les réponses manifestement hors football."""
    if isinstance(payload, dict):
        sport = payload.get("sport")
        if isinstance(sport, dict):
            sport = sport.get("name") or sport.get("slug")
        if sport and str(sport).lower() not in ("football", "soccer", "1"):
            return False
        for k in ("homeTeam", "awayTeam", "home", "away", "team_home", "team_away"):
            if k in payload:
                return True
    if isinstance(payload, list):
        return any(football_payload_ok(x) for x in payload[:10] if isinstance(x, dict))
    return False
