import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone

from tests._stubs import fixture  # noqa: F401  (installe les coquilles AVANT les imports app)

from app.sources import livefootball_rapidapi as rapid
from app import ligues, matchs, contexte


def lancer(coro):
    return asyncio.run(coro)


def simuler_api(reponses):
    """Remplace rapid._get par un dictionnaire {chemin: réponse JSON}, et
    compte les appels par chemin."""
    appels = []

    async def faux_get(chemin, params=None):
        appels.append((chemin, dict(params or {})))
        rep = reponses.get(chemin)
        return copy.deepcopy(rep) if rep is not None else None

    rapid._get = faux_get
    rapid._cache.clear()
    return appels


class NormalisationMatchs(unittest.TestCase):
    def setUp(self):
        self.brut = fixture("matches_by_date.json")["response"]["matches"]
        self.norm = {m["rapid_id"]: m for m in map(rapid._normaliser, self.brut)}

    def test_match_termine_avec_score_et_logos(self):
        m = self.norm[5181829]  # Allemagne 2-0 Serbie
        self.assertEqual(m["statut"], "termine")
        self.assertEqual((m["score1"], m["score2"]), (2, 0))
        self.assertEqual(m["logo1"], "https://images.fotmob.com/image_resources/logo/teamlogo/8570_small.png")
        self.assertEqual(m["rapid_league_id"], 920744)
        self.assertEqual(m["league"], "")  # résolu plus tard par ligues.py
        self.assertEqual(m["date"], "2026-10-01T18:45:00Z")

    def test_match_reporte_sans_score(self):
        m = self.norm[6366622]  # Betis - Ceuta, "PP"
        self.assertEqual(m["statut"], "reporte")
        self.assertIsNone(m["score1"])
        self.assertIsNone(m["score2"])

    def test_match_non_demarre_na_pas_de_score(self):
        brut = copy.deepcopy(self.brut[0])
        brut["status"].update({"started": False, "finished": False})
        brut["home"]["score"], brut["away"]["score"] = 0, 0
        m = rapid._normaliser(brut)
        self.assertEqual(m["statut"], "prevu")
        self.assertIsNone(m["score1"])  # pas de "0 - 0" affiché avant le coup d'envoi

    def test_minute_nettoyee_des_marques_invisibles(self):
        live = fixture("current_live.json")["response"]["live"][0]
        self.assertEqual(rapid._nettoyer_minute(live["status"]["liveTime"]), "61'")
        self.assertEqual(rapid._nettoyer_minute({"short": "HT"}), "HT")
        self.assertEqual(rapid._nettoyer_minute({"short": "90+3\u200e\u2019"}), "90+3'")
        self.assertIsNone(rapid._nettoyer_minute(None))

    def test_donnee_incomplete_ignoree(self):
        self.assertIsNone(rapid._normaliser({"id": 1}))


class DonneesLive(unittest.TestCase):
    def test_en_direct(self):
        appels = simuler_api({"football-current-live": fixture("current_live.json")})
        direct = lancer(rapid.get_en_direct())
        premier = direct[5190041]
        self.assertEqual((premier["score1"], premier["score2"], premier["minute"], premier["statut"]),
                         (6, 0, "61'", "en_direct"))
        lancer(rapid.get_en_direct())  # 2e appel : servi par le cache
        self.assertEqual(len(appels), 1)

    def test_en_direct_echec_different_de_aucun_match(self):
        simuler_api({})
        self.assertIsNone(lancer(rapid.get_en_direct()))
        simuler_api({"football-current-live": {"status": "success", "response": {"live": []}}})
        self.assertEqual(lancer(rapid.get_en_direct()), {})

    def test_classement(self):
        simuler_api({"football-get-standing-all": fixture("standing_all.json")})
        c = lancer(rapid.get_classement(47))
        self.assertEqual(c[0]["nom"], "Man City")
        self.assertEqual((c[0]["rang"], c[0]["points"], c[0]["diff"]), (1, 15, 8))

    def test_confrontations_vues_depuis_equipe1(self):
        # eventid 4621624 : Feyenoord (id 10235, domicile) - Salzburg.
        # Salzburg 1-2 Feyenoord (2022) et Salzburg 3-1 Feyenoord (2019).
        simuler_api({"football-get-head-to-head": fixture("head_to_head.json")})
        h = lancer(rapid.get_confrontations(4621624, 10235))
        self.assertEqual(h["bilan"], {"equipe1": 1, "nuls": 0, "equipe2": 1})
        self.assertEqual(h["total"], 2)
        self.assertEqual(h["derniers"][0]["date"][:4], "2022")  # plus récent d'abord
        # vu depuis Salzburg, le bilan s'inverse
        rapid._cache.clear()
        h2 = lancer(rapid.get_confrontations(4621624, 10013))
        self.assertEqual(h2["bilan"], {"equipe1": 1, "nuls": 0, "equipe2": 1})


class AnnuaireLigues(unittest.TestCase):
    def setUp(self):
        ligues._ligues = None
        ligues._meta = {}
        self.sauvegardes = []
        ligues.repo.charger_annuaire_ligues = lambda: None
        ligues.repo.enregistrer_annuaire_ligues = lambda c: self.sauvegardes.append(c)

    def reponses(self, **en_plus):
        base = {
            "football-get-all-leagues-with-countries": fixture("all_leagues_with_countries.json"),
            "football-popular-leagues": fixture("popular_leagues.json"),
            "football-get-matches-by-date-and-league": fixture("matches_by_date_and_league.json"),
            "football-get-league-detail": fixture("league_detail.json"),
        }
        base.update(en_plus)
        return base

    def test_ligue_groupee_resolue_sans_appel_de_detail(self):
        appels = simuler_api(self.reponses())
        lancer(ligues.completer([242], "2024-11-07"))  # 242 = DBU Pokalen, présent dans l'endpoint groupé
        chemins = [c for c, _ in appels]
        self.assertNotIn("football-get-league-detail", chemins)
        self.assertEqual(ligues._ligues["242"]["nom"], "DBU Pokalen")
        self.assertEqual(ligues._ligues["242"]["pays"], "DEN")
        self.assertTrue(self.sauvegardes)  # persisté

    def test_inconnus_plafonnes_et_echecs_memorises(self):
        appels = simuler_api(self.reponses())
        inconnus = [900001, 900002, 900003, 900004, 900005]
        lancer(ligues.completer(inconnus, "2026-10-01"))
        details = [p for c, p in appels if c == "football-get-league-detail"]
        self.assertEqual(len(details), ligues.MAX_DETAILS_PAR_PASSAGE)  # pas plus de 3 par passage
        # le détail du playground (Ligue des champions) répond pour n'importe quel id : ils sont résolus
        self.assertTrue(ligues._connue(900001))
        # 2e passage : seuls les 2 restants sont traités, les 3 résolus ne sont jamais redemandés
        appels.clear()
        lancer(ligues.completer(inconnus, "2026-10-01"))
        details = [p["leagueid"] for c, p in appels if c == "football-get-league-detail"]
        self.assertEqual(sorted(details), [900004, 900005])

    def test_echec_retente_seulement_apres_delai(self):
        appels = simuler_api(self.reponses(**{"football-get-league-detail": {"status": "success", "response": {"leagues": {}}}}))
        lancer(ligues.completer([900001], "2026-10-01"))
        self.assertTrue(ligues._ligues["900001"].get("inconnu"))
        appels.clear()
        lancer(ligues.completer([900001], "2026-10-01"))
        self.assertEqual([c for c, _ in appels if c == "football-get-league-detail"], [])  # pas de re-demande immédiate
        ligues._ligues["900001"]["tente_le"] -= ligues.DELAI_RETENTATIVE + 1
        lancer(ligues.completer([900001], "2026-10-01"))
        self.assertEqual(len([c for c, _ in appels if c == "football-get-league-detail"]), 1)

    def test_appliquer_ne_fait_aucun_appel_et_remplace_le_nom_generique(self):
        ligues._ligues = {"242": {"nom": "DBU Pokalen", "pays": "DEN", "rang": 0, "parent": None},
                          "7": {"inconnu": True, "tente_le": 0}}
        ligues._meta = {"populaires": [242]}
        out = ligues.appliquer([
            {"fixture_id": 1, "rapid_league_id": 242, "league": "Ligue 242"},
            {"fixture_id": 2, "rapid_league_id": 7, "league": ""},
            {"fixture_id": 3, "rapid_league_id": None, "league": "Premier League"},  # autre fournisseur : intact
        ])
        self.assertEqual(out[0]["league"], "DBU Pokalen")
        self.assertEqual(out[0]["league_rang"], 100)  # populaire -> en tête
        self.assertTrue(out[0]["league_logo"].endswith("/242.png"))
        self.assertEqual(out[1]["league"], "Autre compétition")
        self.assertEqual(out[2]["league"], "Premier League")


class DirectEtFusion(unittest.TestCase):
    def match(self, rid, minutes_depuis_ko, statut, **extra):
        ko = datetime.now(timezone.utc) - timedelta(minutes=minutes_depuis_ko)
        m = {"fixture_id": rid, "rapid_id": rid, "date": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "statut": statut, "score1": None, "score2": None, "minute": None}
        m.update(extra)
        return m

    def setUp(self):
        self.ecritures = []
        matchs.repo.maj_champs_matchs = lambda mods: self.ecritures.extend(mods)

    def test_aucun_appel_hors_fenetre(self):
        appels = simuler_api({"football-current-live": fixture("current_live.json")})
        lancer(matchs.appliquer_direct([self.match(1, -300, "prevu"), self.match(2, 20, "termine")]))
        self.assertEqual(appels, [])  # ni match proche, ni match en cours : zéro quota dépensé

    def test_score_en_direct_applique_et_ecrit_seulement_au_changement(self):
        simuler_api({"football-current-live": fixture("current_live.json")})
        m = self.match(5190041, 61, "prevu")
        out = lancer(matchs.appliquer_direct([m]))[0]
        self.assertEqual((out["statut"], out["score1"], out["score2"], out["minute"]), ("en_direct", 6, 0, "61'"))
        self.assertEqual(len(self.ecritures), 1)
        self.assertIsNone(self.ecritures[0]["champs"]["minute"])  # la minute n'est jamais persistée
        # même score, seule la minute change -> aucune écriture
        self.ecritures.clear()
        m2 = self.match(5190041, 62, "en_direct", score1=6, score2=0)
        lancer(matchs.appliquer_direct([m2]))
        self.assertEqual(self.ecritures, [])

    def test_match_sorti_du_direct_passe_termine_avec_dernier_score(self):
        simuler_api({"football-current-live": {"status": "success", "response": {"live": []}}})
        m = self.match(99, 100, "en_direct", score1=2, score2=1, minute="88'")
        out = lancer(matchs.appliquer_direct([m]))[0]
        self.assertEqual((out["statut"], out["score1"], out["score2"], out["minute"]), ("termine", 2, 1, None))

    def test_prevu_beaucoup_apres_le_coup_denvoi_est_clos_sans_appel(self):
        appels = simuler_api({})
        out = lancer(matchs.appliquer_direct([self.match(7, 200, "prevu")]))[0]
        self.assertEqual(out["statut"], "termine")
        self.assertIsNone(out["score1"])
        self.assertEqual(appels, [])

    def test_appel_direct_en_echec_garde_les_scores_sans_minute_perimee(self):
        simuler_api({})  # football-current-live -> None
        m = self.match(5, 50, "en_direct", score1=1, score2=0, minute="49'")
        out = lancer(matchs.appliquer_direct([m]))[0]
        self.assertEqual((out["statut"], out["score1"], out["minute"]), ("en_direct", 1, None))

    def test_fusion_conserve_logos_et_scores_du_doublon_perdant(self):
        jour = datetime.now(timezone.utc).date().isoformat()
        date = f"{jour}T18:45:00Z"
        rapide = {"fixture_id": "live-1", "date": date, "league_id": 5, "league": "", "equipe1": "Germany",
                  "equipe2": "Serbia", "donneesDisponibles": None, "rapid_id": 1, "rapid_league_id": 920744,
                  "logo1": "L1", "logo2": "L2", "statut": "termine", "score1": 2, "score2": 0}
        officiel = {"fixture_id": "fd-9", "date": date, "league_id": 2000, "league": "WC Qualification",
                    "equipe1": "Germany", "equipe2": "Serbia", "donneesDisponibles": None}

        async def vide(_): return []
        async def liste(l): return lambda _j: l

        matchs.openliga.get_fixtures_du_jour = lambda j: _coro([])
        matchs.thesportsdb.get_fixtures_du_jour = lambda j: _coro([])
        matchs.livefootball_rapidapi.get_fixtures_du_jour = lambda j: _coro([dict(rapide)])
        matchs.football_data.get_fixtures_du_jour = lambda j: _coro([dict(officiel)])
        matchs.ligues.completer = lambda ids, j: _coro(None)
        matchs.repo.lister_matchs_jour = lambda j: []
        sauves = []
        matchs.repo.enregistrer_matchs_jour = lambda j, ms: sauves.append(ms)
        matchs.repo.marquer_matchs_rafraichis = lambda j: None

        res = lancer(matchs._rafraichir_jour(jour))
        self.assertEqual(len(res), 1)  # dédoublonné
        m = res[0]
        self.assertEqual(m["league"], "WC Qualification")  # le nom officiel gagne
        self.assertEqual((m["logo1"], m["score1"], m["statut"], m["rapid_league_id"]), ("L1", 2, "termine", 920744))


async def _coro(valeur):
    return valeur


class Contexte(unittest.TestCase):
    def test_resume_pour_les_facteurs_cles(self):
        ctx = {
            "classement": {"equipe1": {"rang": 1, "points": 15, "diff": 8},
                           "equipe2": {"rang": 12, "points": 5, "diff": -3}, "total_equipes": 20},
            "confrontations": {"bilan": {"equipe1": 3, "nuls": 1, "equipe2": 1}, "total": 5},
        }
        lignes = contexte.resumer(ctx, "Man City", "Everton")
        self.assertIn("Man City 1er (15 pts, diff. +8)", lignes[0])
        self.assertIn("Everton 12e (5 pts, diff. -3)", lignes[0])
        self.assertIn("3 victoire(s) Man City, 1 nul(s), 1 victoire(s) Everton", lignes[1])
        self.assertEqual(len(contexte.resumer(ctx, "A", "B", ignorer_h2h=True)), 1)
        self.assertEqual(contexte.resumer({"classement": None, "confrontations": None}, "A", "B"), [])

    def test_match_sans_identifiant_fournisseur(self):
        contexte.repo.obtenir_match = lambda fid: {"fixture_id": fid}  # venu d'une autre source
        out = lancer(contexte.construire(1))
        self.assertFalse(out["disponible"])

    def test_construire_associe_les_equipes_au_classement(self):
        ligues._ligues = {}
        ligues._meta = {}
        contexte.repo.obtenir_match = lambda fid: {"fixture_id": fid, "rapid_id": 4621624, "rapid_league_id": 47,
                                                   "equipe1_id": 8456, "equipe2_id": 9825}
        simuler_api({"football-get-standing-all": fixture("standing_all.json"),
                     "football-get-head-to-head": fixture("head_to_head.json")})
        out = lancer(contexte.construire(1))
        self.assertTrue(out["disponible"])
        self.assertEqual(out["classement"]["equipe1"]["nom"], "Man City")
        self.assertEqual(out["classement"]["equipe2"]["rang"], 2)  # Arsenal


if __name__ == "__main__":
    unittest.main()
