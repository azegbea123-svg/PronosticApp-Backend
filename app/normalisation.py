"""
Normalisation et comparaison de noms d'équipes pour dédoublonner un même
match remonté par plusieurs fournisseurs sous des orthographes différentes
("Manchester City FC" / "Man City" / "Manchester City").

Principe de sécurité : on préfère rater une fusion (un doublon affiché)
que fusionner deux matchs différents. D'où :
  - les marqueurs qui distinguent deux équipes d'un même club (féminines
    "(W)", jeunes "U21", réserves "II") DOIVENT être identiques ;
  - "Manchester United" et "Manchester City" ne fusionnent jamais (le
    seul jeton commun est la ville) ;
  - la fusion exige en plus des coups d'envoi proches (voir matchs.py).
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import FrozenSet

# Jetons purement "décoratifs" : leur présence ou absence ne change pas l'identité du club.
_JETONS_IGNORES = frozenset({
    "fc", "cf", "sc", "afc", "ac", "cfc", "ssc", "fk", "sk", "bk", "sv", "tsv",
    "fsv", "vfb", "vfl", "tsg", "club", "de", "del", "la", "le", "the", "as", "us",
})

# Jetons qui distinguent une équipe d'une autre du même club : à conserver ET à comparer.
_MARQUEURS = re.compile(r"^(w|ii|iii|b|u\d{2}|reserves?|women|feminin|femenino)$")

# Alias fréquents après normalisation (clé : forme normalisée du nom abrégé).
_ALIAS = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "paris saint germain": "paris saint germain",
    "inter milan": "inter",
    "internazionale": "inter",
    "atletico madrid": "atletico",
    "atl madrid": "atletico",
    "bayern munich": "bayern",
    "bayern munchen": "bayern",
    "borussia monchengladbach": "monchengladbach",
    "gladbach": "monchengladbach",
    "wolves": "wolverhampton",
    "wolverhampton wanderers": "wolverhampton",
    "newcastle united": "newcastle",
}


# Lettres que la décomposition Unicode ne ramène PAS à l'ASCII (Bjørnar, Łódź, Straße...).
_TRANSLIT = str.maketrans({
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
    "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "þ": "th", "Þ": "Th", "ð": "d", "Ð": "D",
    "ı": "i", "İ": "I",
})


def _sans_accents(texte: str) -> str:
    texte = texte.translate(_TRANSLIT)
    return "".join(c for c in unicodedata.normalize("NFKD", texte) if not unicodedata.combining(c))


def normaliser(nom: str) -> str:
    """Forme canonique : sans accents, minuscules, ponctuation retirée, jetons décoratifs retirés."""
    t = _sans_accents(nom or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    jetons = t.split()
    utiles = [j for j in jetons if j not in _JETONS_IGNORES]
    if not utiles:  # nom entièrement "décoratif" : on garde l'original plutôt que de produire ""
        utiles = jetons
    forme = " ".join(utiles)
    return _ALIAS.get(forme, forme)


def marqueurs(nom_normalise: str) -> FrozenSet[str]:
    return frozenset(j for j in nom_normalise.split() if _MARQUEURS.match(j))


def similarite(a: str, b: str) -> float:
    """Score 0..1 entre deux noms DÉJÀ normalisés."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if marqueurs(a) != marqueurs(b):
        return 0.0  # équipe A vs équipe B / féminines / jeunes : jamais la même
    ja, jb = set(a.split()), set(b.split())
    petit, grand = (ja, jb) if len(ja) <= len(jb) else (jb, ja)
    # "stuttgart" ⊂ "stuttgart ii" est déjà écarté par les marqueurs ; ici "inter" ⊂ "inter milan".
    if petit <= grand and any(len(j) >= 5 for j in petit):
        return 0.92
    sm = SequenceMatcher(None, a, b)
    if sm.quick_ratio() < 0.8:
        return 0.0
    return sm.ratio()
