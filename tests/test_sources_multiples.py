import asyncio
import time
import unittest
from unittest import mock
from datetime import datetime, timezone

from tests._stubs import fixture  # noqa: F401  (installe les coquilles AVANT les imports app)

from app import matchs, normalisation
from app.sources import sante, sportapi7


def lancer(coro):
    return asyncio.run(coro)


async def _coro(valeur):
    return valeur


JOUR = datetime.now(timezone.utc).date().isoformat()


class Isole(unittest.TestCase):
    """Remplace un attribut de module pour la durée d'UN test seulement (restauré ensuite),
    afin de ne jamais polluer les autres fichiers de tests."""

    def remplacer(self, cible, nom, valeur):
        p = mock.patch.object(cible, nom, valeur)
        p.start()
        self.addCleanup(p.stop)


def m(src_id, e1, e2, heure="18:45", **extra):
    base = {"fixture_id": src_id, "date": f"{JOUR}T{heure}:00Z", "league_id": 1, "league": "",
            "equipe1": e1, "equipe2": e2, "donneesDisponibles": None}
    base.update(extra)
    return base


class NormalisationEquipes(unittest.TestCase):
    def test_variantes_d_un_meme_club(self):
        n = normalisation.normaliser
        self.assertEqual(n("Manchester City FC"), n("Manchester City"))
        self.assertEqual(n("Man City"), n("Manchester City"))
        self.assertEqual(n("Atlético Madrid"), n("Atletico Madrid"))
        self.assertEqual(n("Paris SG"), n("Paris Saint-Germain"))

    def test_clubs_voisins_ne_fusionnent_pas(self):
        n = normalisation.normaliser
        self.assertLess(normalisation.similarite(n("Manchester United"), n("Manchester City")), 0.82)
        self.assertLess(normalisation.similarite(n("Real Madrid"), n("Real Sociedad")), 0.82)

    def test_marqueurs_feminines_jeunes_reserves(self):
        n = normalisation.normaliser
        self.assertEqual(normalisation.similarite(n("Arna-Bjørnar (W)"), n("Arna-Bjørnar")), 0.0)
        self.assertEqual(normalisation.similarite(n("Germany U21"), n("Germany")), 0.0)
        self.assertEqual(normalisation.similarite(n("Bayern II"), n("Bayern")), 0.0)
        self.assertEqual(normalisation.similarite(n("Arna-Bjørnar (W)"), n("Arna-Bjornar W")), 1.0)

    def test_inclusion_de_jetons(self):
        n = normalisation.normaliser
        self.assertGreaterEqual(normalisation.similarite(n("Inter"), n("Inter Milan")), 0.82)
        self.assertGreaterEqual(normalisation.similarite(n("Stuttgart"), n("VfB Stuttgart")), 0.82)

    def test_nom_entierement_decoratif_conserve(self):
        self.assertNotEqual(normalisation.normaliser("FC"), "")


class DedoublonnageMultiSources(Isole):
    def setUp(self):
        sante.reinitialiser()
        self.sauves = []
        self.remplacer(matchs.repo, "lister_matchs_jour", lambda j: [])
        self.remplacer(matchs.repo, "enregistrer_matchs_jour", lambda j, ms: self.sauves.append(ms))
        self.remplacer(matchs.repo, "marquer_matchs_rafraichis", lambda j: None)
        self.remplacer(matchs.ligues, "completer", lambda ids, j: _coro(None))

    def brancher(self, **par_source):
        defauts = {"openliga": [], "thesportsdb": [], "livefootball_rapidapi": [], "football_data": [], "sportapi7": []}
        defauts.update(par_source)
        for nom, valeur in defauts.items():
            self.remplacer(getattr(matchs, nom), "get_fixtures_du_jour", (lambda v: (lambda j: _coro(v)))(valeur))

    def test_meme_match_trois_orthographes_donne_une_ligne(self):
        self.brancher(
            sportapi7=[m("sofa-1", "Manchester City", "Arsenal FC", "19:00", sofa_id=1)],
            thesportsdb=[m("tsdb-1", "Man City", "Arsenal", "19:05")],
            football_data=[m("fd-1", "Manchester City FC", "Arsenal FC", "19:00", league="Premier League")],
        )
        res = lancer(matchs._rafraichir_jour(JOUR))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["nb_sources"], 3)
        self.assertEqual(res[0]["sources"], ["football-data", "sportapi7", "thesportsdb"])
        self.assertEqual(res[0]["league"], "Premier League")  # la source officielle gagne
        self.assertEqual(res[0]["sofa_id"], 1)  # mais hérite de l'identifiant des autres

    def test_matchs_differents_meme_heure_restent_separes(self):
        self.brancher(
            football_data=[m("fd-1", "Manchester United", "Chelsea", "19:00")],
            thesportsdb=[m("tsdb-2", "Manchester City", "Arsenal", "19:00")],
        )
        res = lancer(matchs._rafraichir_jour(JOUR))
        self.assertEqual(len(res), 2)
        self.assertTrue(all(r["nb_sources"] == 1 for r in res))

    def test_coups_d_envoi_trop_eloignes_ne_fusionnent_pas(self):
        self.brancher(
            football_data=[m("fd-1", "Lyon", "Nice", "12:00")],
            thesportsdb=[m("tsdb-1", "Lyon", "Nice", "20:00")],
        )
        # mêmes noms exacts => la règle "exacte" les réunit seulement dans la fenêtre de 90 min
        self.assertEqual(len(lancer(matchs._rafraichir_jour(JOUR))), 2)

    def test_marqueurs_feminines_pas_fusionnees_avec_les_hommes(self):
        self.brancher(
            football_data=[m("fd-1", "Lyon", "PSG", "19:00")],
            thesportsdb=[m("tsdb-1", "Lyon (W)", "PSG (W)", "19:00")],
        )
        self.assertEqual(len(lancer(matchs._rafraichir_jour(JOUR))), 2)

    def test_ancien_identifiant_reutilise_et_disponibilite_conservee(self):
        ancien_id = matchs._fixture_id_stable(JOUR, "Man City", "Arsenal")  # id issu d'une autre orthographe
        self.remplacer(matchs.repo, "lister_matchs_jour",
                       lambda j: [{"fixture_id": ancien_id, "donneesDisponibles": True, "date": f"{JOUR}T19:00:00Z"}])
        self.brancher(
            thesportsdb=[m("tsdb-1", "Man City", "Arsenal", "19:05")],
            football_data=[m("fd-1", "Manchester City FC", "Arsenal FC", "19:00")],
        )
        res = lancer(matchs._rafraichir_jour(JOUR))
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["fixture_id"], ancien_id)
        self.assertIs(res[0]["donneesDisponibles"], True)

    def test_une_source_en_echec_ne_bloque_pas_les_autres(self):
        self.brancher(football_data=[m("fd-1", "Lyon", "Nice")])
        self.remplacer(matchs.sportapi7, "get_fixtures_du_jour", lambda j: _coro(None))  # échec
        res = lancer(matchs._rafraichir_jour(JOUR))
        self.assertEqual(len(res), 1)

    def test_tout_en_echec_garde_le_cache(self):
        cache = [{"fixture_id": 9}]
        self.remplacer(matchs.repo, "lister_matchs_jour", lambda j: cache)
        for nom in ("openliga", "thesportsdb", "livefootball_rapidapi", "football_data", "sportapi7"):
            self.remplacer(getattr(matchs, nom), "get_fixtures_du_jour", lambda j: _coro(None))
        self.assertEqual(lancer(matchs._rafraichir_jour(JOUR)), cache)

    def test_deux_doublons_internes_a_une_source_fusionnent_si_noms_identiques(self):
        self.brancher(football_data=[m("fd-1", "Lyon", "Nice", "19:00"), m("fd-2", "Lyon", "Nice", "19:00")])
        self.assertEqual(len(lancer(matchs._rafraichir_jour(JOUR))), 1)


class Disjoncteur(unittest.TestCase):
    def setUp(self):
        sante.reinitialiser()

    def test_pause_apres_trois_echecs_puis_reprise(self):
        appels = []

        async def en_echec():
            appels.append(1)
            return None

        for _ in range(sante.SEUIL_ECHECS):
            self.assertIsNone(lancer(sante.appeler("x", en_echec)))
        self.assertEqual(len(appels), sante.SEUIL_ECHECS)
        self.assertFalse(sante.disponible("x"))
        lancer(sante.appeler("x", en_echec))  # en pause : n'appelle pas
        self.assertEqual(len(appels), sante.SEUIL_ECHECS)

        sante._etat["x"]["pause_jusqua"] = time.time() - 1  # la pause est écoulée
        self.assertEqual(lancer(sante.appeler("x", lambda: _coro([]))), [])  # liste vide = succès
        self.assertTrue(sante.disponible("x"))
        self.assertEqual(sante.rapport()["x"]["echecs_consecutifs"], 0)

    def test_exception_comptee_comme_echec_sans_remonter(self):
        async def boom():
            raise RuntimeError("panne")

        self.assertIsNone(lancer(sante.appeler("y", boom)))
        self.assertIn("RuntimeError", sante.rapport()["y"]["dernier_detail"])


class ParseurSportApi7(Isole):
    def setUp(self):
        self.event = fixture("sportapi7_event.json")["event"]  # capture RÉELLE de la requête /event/7881945

    def test_match_termine_de_la_capture(self):
        n = sportapi7._normaliser(self.event)
        self.assertEqual((n["equipe1"], n["equipe2"]), ("Sparta Rotterdam", "SC Cambuur"))
        self.assertEqual((n["statut"], n["score1"], n["score2"]), ("termine", 3, 0))
        self.assertEqual(n["date"], "2018-10-14T12:30:00Z")
        self.assertEqual(n["league"], "Eerste Divisie")
        self.assertEqual(n["league_pays"], "NLD")
        self.assertEqual(n["fixture_id"], "sofa-7881945")

    def test_statut_en_cours_ou_a_venir_non_expose(self):
        for type_statut in ("inprogress", "notstarted"):
            ev = dict(self.event, status={"code": 0, "description": "x", "type": type_statut})
            n = sportapi7._normaliser(ev)
            self.assertNotIn("statut", n)  # un statut périmé serait pire que pas de statut
            self.assertNotIn("score1", n)

    def test_reporte_sans_score(self):
        ev = dict(self.event, status={"code": 60, "description": "Postponed", "type": "postponed"})
        n = sportapi7._normaliser(ev)
        self.assertEqual(n["statut"], "reporte")
        self.assertIsNone(n["score1"])

    def test_donnee_incomplete_ignoree(self):
        self.assertIsNone(sportapi7._normaliser({"id": 1}))

    def test_liste_filtree_sur_le_jour_et_forme_inattendue_est_un_echec(self):
        async def faux_get(chemin):
            return {"events": [self.event]}

        self.remplacer(sportapi7, "_get", faux_get)
        self.remplacer(sportapi7, "_cache", {})
        self.assertEqual(lancer(sportapi7.get_fixtures_du_jour("2018-10-14"))[0]["equipe1"], "Sparta Rotterdam")
        sportapi7._cache.clear()
        self.assertEqual(lancer(sportapi7.get_fixtures_du_jour("2018-10-15")), [])  # autre jour : vide, pas échec

        async def forme_inattendue(chemin):
            return {"erreur": "x"}

        self.remplacer(sportapi7, "_get", forme_inattendue)
        sportapi7._cache.clear()
        self.assertIsNone(lancer(sportapi7.get_fixtures_du_jour("2018-10-14")))


if __name__ == "__main__":
    unittest.main()
