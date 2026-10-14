"""
Sélection (PURE, sans accès base) des documents créés par erreur par d'anciennes versions des tests
dans la vraie base Firestore. Un document n'est retenu que s'il correspond à la fois à un identifiant
ET à des noms factices : un vrai match ou une vraie équipe n'est jamais sélectionné.
"""

from typing import Any, Dict, Iterable, List

IDS_PRONOSTICS_TEST = set(range(10)) | {555, 556}
EQUIPES_PRONOSTICS_TEST = {("A", "B"), ("FaZe", "Rivaux")}


def pronostics_de_test(docs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in docs
            if d.get("eventId") in IDS_PRONOSTICS_TEST
            and (d.get("equipe1"), d.get("equipe2")) in EQUIPES_PRONOSTICS_TEST]


def equipes_de_test(docs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [d for d in docs if d.get("id") == 1 and d.get("nom") == "A"]
