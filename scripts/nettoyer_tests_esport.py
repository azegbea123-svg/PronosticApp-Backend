"""
Supprime de la vraie base Firestore les documents factices créés par d'anciennes versions des
tests esport (pronostics "A vs B" / "FaZe vs Rivaux", équipe "A").

Usage (depuis le dossier du backend, avec ta clé Firebase habituelle) :
    python scripts/nettoyer_tests_esport.py                      # SIMULATION : liste sans rien supprimer
    python scripts/nettoyer_tests_esport.py --appliquer          # supprime réellement
    python scripts/nettoyer_tests_esport.py --appliquer --equipes-connues
        # supprime AUSSI les 33 équipes CS importées par les tests (à ne faire que si tu ne les veux pas)
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.esport import depot, nettoyage  # noqa: E402
from app.esport.equipes_connues import EQUIPES_CONNUES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--appliquer", action="store_true", help="supprimer réellement (sinon : simulation)")
    ap.add_argument("--equipes-connues", action="store_true", help="supprimer aussi les 33 équipes CS importées")
    args = ap.parse_args()

    client = db.get_client()
    if client is None:
        print("Aucune base Firestore accessible (clé Firebase absente) : rien à nettoyer.")
        return 0

    pronos = [d.to_dict() for d in client.collection(depot.COLLECTION_PRONOSTICS).stream()]
    equipes = [d.to_dict() for d in client.collection(depot.COLLECTION_EQUIPES).stream()]
    a_supprimer_pronos = nettoyage.pronostics_de_test(pronos)
    a_supprimer_equipes = nettoyage.equipes_de_test(equipes)
    if args.equipes_connues:
        connues = {e["id"] for e in EQUIPES_CONNUES}
        a_supprimer_equipes += [e for e in equipes if e.get("id") in connues and e not in a_supprimer_equipes]

    print(f"Pronostics : {len(pronos)} en base, {len(a_supprimer_pronos)} factice(s) à supprimer")
    for p in a_supprimer_pronos:
        print(f"   - {p['eventId']}  {p.get('equipe1')} vs {p.get('equipe2')}")
    print(f"Équipes    : {len(equipes)} en base, {len(a_supprimer_equipes)} à supprimer")
    for e in a_supprimer_equipes:
        print(f"   - {e['id']}  {e.get('nom')}")

    if not args.appliquer:
        print("\nSIMULATION : rien n'a été supprimé. Relance avec --appliquer pour confirmer.")
        return 0
    for p in a_supprimer_pronos:
        client.collection(depot.COLLECTION_PRONOSTICS).document(str(p["eventId"])).delete()
    for e in a_supprimer_equipes:
        client.collection(depot.COLLECTION_EQUIPES).document(str(e["id"])).delete()
    print("\nSuppression terminée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
