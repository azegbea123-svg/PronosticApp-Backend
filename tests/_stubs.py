"""
Remplace httpx et firebase_admin (non nécessaires pour ces tests, et
absents d'un environnement sans réseau) par des coquilles vides, AVANT
d'importer l'application. Aucun appel réseau n'est jamais fait par ces
tests : les fonctions d'accès HTTP du connecteur sont remplacées par des
réponses tirées de tests/fixtures/ (extraits RÉELS du playground RapidAPI).
"""
import json
import os
import sys
import types

for nom in ("httpx", "firebase_admin"):
    try:
        __import__(nom)
    except ImportError:
        sys.modules[nom] = types.ModuleType(nom)

if not hasattr(sys.modules["httpx"], "AsyncClient"):
    sys.modules["httpx"].AsyncClient = object  # type: ignore[attr-defined]

fa = sys.modules["firebase_admin"]
if not hasattr(fa, "credentials"):
    fa.credentials = types.SimpleNamespace()  # type: ignore[attr-defined]
    fa.firestore = types.SimpleNamespace()  # type: ignore[attr-defined]
    fa._apps = {}  # type: ignore[attr-defined]
    sys.modules["firebase_admin.credentials"] = fa.credentials  # type: ignore[assignment]
    sys.modules["firebase_admin.firestore"] = fa.firestore  # type: ignore[assignment]

DOSSIER = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(nom: str):
    with open(os.path.join(DOSSIER, nom), encoding="utf-8") as f:
        return json.load(f)
