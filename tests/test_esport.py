"""Tests du module esport. Fixtures = extraits RÉELS du playground RapidAPI (tests/fixtures/esport_*.json)."""
import asyncio
import copy
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests._stubs import fixture

from app.esport import client, depot, modele, parsing, service
from app.esport.service import ErreurEsport

FAZE = 364396


def lancer(c):
    return asyncio.run(c)


class Isole(unittest.TestCase):
    def remplacer(self, cible, nom, valeur):
        p = mock.patch.object(cible, nom, valeur)
        p.start()
        self.addCleanup(p.stop)

    def setUp(self):
        client.vider_cache()
        client._quota.update(limite=None, restant=None, fenetre_fin=None)
        depot._mem_equipes.clear()
        depot._mem_pronostics.clear()


class Parsing(unittest.TestCase):
    def test_evenement_reel(self):
        ev = parsing.normaliser_evenement(fixture("esport_event.json")["event"])
        self.assertEqual((ev["jeu"], ev["jeu_nom"]), ("csgo", "Counter Strike"))
        self.assertEqual(ev["best_of"], 3)
        self.assertEqual((ev["statut"], ev["score1"], ev["score2"], ev["vainqueur"]), ("termine", 1, 2, 2))
        self.assertEqual(ev["equipe1"]["nom"], "Iron Branch")

    def test_historique_exclut_forfaits_et_matchs_non_termines(self):
        brut = fixture("esport_previous.json")
        self.assertEqual(len(brut["events"]), 30)
        hist = parsing.historique_equipe(brut, FAZE)
        self.assertEqual(len(hist), 28)  # 2 walkovers retirés
        self.assertTrue(all(h["cartes_pour"] is not None for h in hist))
        self.assertEqual([h["ts"] for h in hist], sorted((h["ts"] for h in hist), reverse=True))

    def test_perspective_equipe(self):
        ev = parsing.normaliser_evenement(fixture("esport_previous.json")["events"][0])
        pour_faze = parsing.vue_equipe(ev, FAZE)
        adverse = parsing.vue_equipe(ev, ev["equipe1"]["id"] if ev["equipe2"]["id"] == FAZE else ev["equipe2"]["id"])
        self.assertEqual(pour_faze["cartes_pour"], adverse["cartes_contre"])
        self.assertNotEqual(pour_faze["victoire"], adverse["victoire"])
        self.assertIsNone(parsing.vue_equipe(ev, 1))

    def test_donnees_incompletes_et_statuts(self):
        self.assertIsNone(parsing.normaliser_evenement({"id": 1}))
        self.assertIsNone(parsing.normaliser_evenement(None))
        base = fixture("esport_event.json")["event"]
        for brut, attendu in (("notstarted", "prevu"), ("inprogress", "en_direct"), ("postponed", "reporte"), ("bizarre", "inconnu")):
            ev = dict(base, status={"type": brut, "description": ""})
            self.assertEqual(parsing.normaliser_evenement(ev)["statut"], attendu)
        self.assertIsNone(parsing.normaliser_evenement(dict(base, status={"type": "notstarted"}))["score1"])


def _hist(resultats, jours_depuis=1.0, maintenant=1_800_000_000):
    """resultats : liste de (cartes_pour, cartes_contre)."""
    return [{"ts": maintenant - (jours_depuis + i) * 86400, "cartes_pour": p, "cartes_contre": c,
             "victoire": p > c, "nul": p == c, "adversaire_id": i, "adversaire": "x", "forfait": False,
             "termine": True, "best_of": 3} for i, (p, c) in enumerate(resultats)]


NOW = 1_800_000_000


class Modele(unittest.TestCase):
    def test_retrecissement_peu_de_donnees_proche_de_50(self):
        t = modele.taux_cartes(_hist([(2, 0)]), NOW)
        self.assertGreater(t["taux"], 0.5)
        self.assertLess(t["taux"], 0.75)  # 1 seule série ne donne jamais de certitude

    def test_log5_symetrique_et_neutre(self):
        self.assertAlmostEqual(modele.log5(0.5, 0.5), 0.5)
        self.assertAlmostEqual(modele.log5(0.7, 0.4) + modele.log5(0.4, 0.7), 1.0)

    def test_serie_amplifie_selon_le_format(self):
        p = 0.6
        bo1, bo3, bo5 = (modele.proba_serie(p, n)["p1"] for n in (1, 3, 5))
        self.assertAlmostEqual(bo1, p)
        self.assertLess(bo1, bo3)
        self.assertLess(bo3, bo5)
        for n in (1, 2, 3, 5, None):
            s = modele.proba_serie(p, n)
            self.assertAlmostEqual(s["p1"] + s["nul"] + s["p2"], 1.0)
        self.assertGreater(modele.proba_serie(p, 2)["nul"], 0)
        self.assertEqual(modele.proba_serie(p, 3)["nul"], 0)

    def test_h2h_ignore_sous_le_seuil_et_plafonne(self):
        assert modele.ajustement_h2h(_hist([(2, 0), (2, 0)]))["delta"] == 0.0
        gros = modele.ajustement_h2h(_hist([(2, 0)] * 12))
        self.assertGreater(gros["delta"], 0)
        self.assertLessEqual(gros["delta"], modele.H2H_PLAFOND)
        self.assertLess(modele.ajustement_h2h(_hist([(0, 2)] * 12))["delta"], 0)

    def test_pronostic_equipe_forte_contre_faible_et_symetrie(self):
        forte, faible = _hist([(2, 0)] * 25), _hist([(0, 2)] * 25)
        a = modele.pronostiquer(forte, faible, None, 3, "A", "B", NOW)
        b = modele.pronostiquer(faible, forte, None, 3, "B", "A", NOW)
        self.assertGreater(a["probabiliteVictoireEquipe1"], 0.85)
        self.assertAlmostEqual(a["probabiliteVictoireEquipe1"], b["probabiliteVictoireEquipe2"], places=3)
        self.assertAlmostEqual(a["probabiliteVictoireEquipe1"] + a["probabiliteMatchNul"] + a["probabiliteVictoireEquipe2"], 1.0, places=3)
        self.assertEqual(a["confiance"], "bonne")
        self.assertTrue(a["limites"])

    def test_confiance_selon_volume_et_fraicheur(self):
        self.assertEqual(modele.confiance(30, 30, NOW - 5 * 86400, NOW), "bonne")
        self.assertEqual(modele.confiance(30, 10, NOW - 5 * 86400, NOW), "moyenne")
        self.assertEqual(modele.confiance(30, 3, NOW - 5 * 86400, NOW), "faible")
        self.assertEqual(modele.confiance(30, 30, NOW - 120 * 86400, NOW), "faible")  # données périmées
        self.assertEqual(modele.confiance(30, 30, None, NOW), "faible")

    def test_donnees_reelles_faze(self):
        hist = parsing.historique_equipe(fixture("esport_previous.json"), FAZE)
        t = modele.taux_cartes(hist, hist[0]["ts"] + 86400)
        self.assertTrue(0.2 < t["taux"] < 0.9)
        self.assertEqual(t["nb_series"], 28)


class FauxClientHttp:
    reponses = []
    appels = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None, timeout=None):
        FauxClientHttp.appels.append((url, headers))
        return FauxClientHttp.reponses.pop(0)


class Rep:
    def __init__(self, code, corps=None, restant=None):
        self.status_code = code
        self._corps = corps
        self.headers = {} if restant is None else {
            "x-ratelimit-requests-limit": "50", "x-ratelimit-requests-remaining": str(restant),
            "x-ratelimit-requests-reset": "3600"}

    def json(self):
        if self._corps is None:
            raise ValueError("pas de corps")
        return self._corps


class ClientQuota(Isole):
    def setUp(self):
        super().setUp()
        FauxClientHttp.reponses, FauxClientHttp.appels = [], []
        self.remplacer(client.httpx, "AsyncClient", FauxClientHttp)
        self.remplacer(client, "CLE_RAPIDAPI", "cle-de-test")

    def test_quota_lu_dans_les_en_tetes_et_cache(self):
        FauxClientHttp.reponses = [Rep(200, {"a": 1}, restant=40)]
        self.assertEqual(lancer(client.appeler("/x", 60)), {"a": 1})
        self.assertEqual(client.quota()["restant"], 40)
        self.assertEqual(client.quota()["limite"], 50)
        self.assertEqual(lancer(client.appeler("/x", 60)), {"a": 1})  # cache : aucun 2e appel
        self.assertEqual(len(FauxClientHttp.appels), 1)
        self.assertEqual(FauxClientHttp.appels[0][1]["x-rapidapi-host"], client.HOTE)

    def test_204_est_vide_pas_echec_et_est_mis_en_cache(self):
        FauxClientHttp.reponses = [Rep(204, restant=30)]
        self.assertEqual(lancer(client.appeler("/vide", 3600)), {})
        self.assertEqual(lancer(client.appeler("/vide", 3600)), {})
        self.assertEqual(len(FauxClientHttp.appels), 1)

    def test_echec_jamais_mis_en_cache(self):
        FauxClientHttp.reponses = [Rep(500, restant=30), Rep(200, {"ok": 1}, restant=29)]
        self.assertIsNone(lancer(client.appeler("/y", 3600)))
        self.assertEqual(lancer(client.appeler("/y", 3600)), {"ok": 1})

    def test_reserves_arriere_plan_et_utilisateur(self):
        client._quota.update(restant=14, fenetre_fin=time.time() + 100)
        self.assertFalse(client.peut_appeler(utilisateur=False))  # sous la réserve de fond (15)
        self.assertTrue(client.peut_appeler(utilisateur=True))
        client._quota["restant"] = 3
        self.assertFalse(client.peut_appeler(utilisateur=True, besoin=2))  # 3-2 < 2
        self.assertTrue(client.peut_appeler(utilisateur=True, besoin=1))   # 3-1 = 2 : exactement la réserve
        client._quota["restant"] = 2
        self.assertIsNone(lancer(client.appeler("/z", 60)))  # 2-1 < 2 : ne fait AUCUN appel réseau
        self.assertEqual(FauxClientHttp.appels, [])

    def test_429_epuise_le_quota_et_fenetre_expiree_le_reinitialise(self):
        FauxClientHttp.reponses = [Rep(429)]
        self.assertIsNone(lancer(client.appeler("/q", 60)))
        self.assertEqual(client.quota()["restant"], 0)
        client._quota.update(restant=0, fenetre_fin=time.time() - 1)
        self.assertIsNone(client.quota()["restant"])  # fenêtre écoulée : compteur inconnu

    def test_sans_cle_aucun_appel(self):
        self.remplacer(client, "CLE_RAPIDAPI", "")
        self.assertIsNone(lancer(client.appeler("/k", 60)))
        self.assertEqual(FauxClientHttp.appels, [])


def faux_appeler(table, compteur=None):
    """Remplace client.appeler : `table` = {fragment_de_chemin: reponse | None}."""
    async def f(chemin, ttl, *, utilisateur=True):
        if compteur is not None:
            compteur.append(chemin)
        for fragment, rep in table.items():
            if fragment in chemin:
                return copy.deepcopy(rep)
        return None
    return f


def evenement_a_venir(id_=555, e1=FAZE, e2=99, ts=None, statut="notstarted"):
    ev = copy.deepcopy(fixture("esport_event.json")["event"])
    ev.update(id=id_, customId="CUSTOM", startTimestamp=int(ts or time.time() + 7200),
              status={"type": statut, "description": ""}, winnerCode=None, bestOf=3)
    ev["homeTeam"].update(id=e1, name="FaZe")
    ev["awayTeam"].update(id=e2, name="Rivaux")
    return ev


def historique_pour(equipe_id):
    """Historique réel de FaZe, rejoué comme s'il appartenait à `equipe_id`."""
    texte = json.dumps(fixture("esport_previous.json")).replace(str(FAZE), str(equipe_id))
    return json.loads(texte)


def ev_liste(id_, jeu, ts, statut, e1=1, e2=2, cat_id=None):
    """Un événement tiré de la capture réelle (matchs par catégorie), recalé sur `jeu`, `ts` et `statut`."""
    ev = copy.deepcopy(fixture("esport_category_events.json")["events"][0])
    ev.update(id=id_, startTimestamp=int(ts), status={"type": statut, "description": ""})
    ev["tournament"]["category"].update(slug=jeu, id=cat_id or service.JEUX[jeu]["id"])
    ev["homeTeam"]["id"], ev["awayTeam"]["id"] = e1, e2
    return ev


def faux_reseau(table, appeles=None):
    """Remplace client.appeler par chemin EXACT (les chemins non listés échouent -> None)."""
    async def f(chemin, ttl, *, utilisateur=True):
        if appeles is not None:
            appeles.append(chemin)
        rep = table.get(chemin)
        return copy.deepcopy(rep) if rep is not None else None
    return f


class ServiceListes(Isole):
    def setUp(self):
        super().setUp()
        self.maintenant = datetime.now(timezone.utc)
        self.aujourdhui = service._chemin_jour(1572, self.maintenant)
        self.demain = service._chemin_jour(1572, self.maintenant + timedelta(days=1))

    def table_csgo(self, evs_aujourdhui, evs_demain=(), live=()):
        t = {service.CHEMIN_LIVE: {"events": list(live)}, self.aujourdhui: {"events": list(evs_aujourdhui)},
             self.demain: {"events": list(evs_demain)}}
        for slug in ("dota2", "lol"):
            t[service._chemin_jour(service.JEUX[slug]["id"], self.maintenant)] = {"events": []}
            t[service._chemin_jour(service.JEUX[slug]["id"], self.maintenant + timedelta(days=1))] = {"events": []}
        return t

    def test_jeux_couverts_correspondent_a_la_capture(self):
        cats = {c["slug"]: c["id"] for c in fixture("esport_categories.json")["categories"]}
        self.assertEqual({k: v["id"] for k, v in service.JEUX.items()}, cats)

    def test_liste_complete_sans_aucune_equipe_suivie(self):
        t0 = time.time()
        table = self.table_csgo(
            [ev_liste(1, "csgo", t0 + 3600, "notstarted"), ev_liste(2, "csgo", t0 - 7200, "finished"),
             ev_liste(3, "csgo", t0 - 40 * 3600, "finished"), ev_liste(4, "csgo", t0 + 7200, "canceled")],
            [ev_liste(5, "csgo", t0 + 20 * 3600, "notstarted")])
        self.remplacer(service.client, "appeler", faux_reseau(table))
        r = lancer(service.lister_matchs())
        # prévu (dans 1 h) et demain gardés ; terminé récent gardé ; terminé de 40 h et annulé exclus
        self.assertEqual([m["id"] for m in r["matchs"]], [1, 5, 2])
        self.assertEqual(r["jeux_en_echec"], [])
        self.assertTrue(r["direct_disponible"])
        self.assertIsNone(r["conseil"])

    def test_direct_prime_sur_la_liste_et_passe_en_tete(self):
        t0 = time.time()
        liste = ev_liste(7, "csgo", t0 - 1200, "notstarted")   # la liste en cache le croit encore "prévu"
        en_direct = ev_liste(7, "csgo", t0 - 1200, "inprogress")
        dota = ev_liste(8, "dota2", t0 - 600, "inprogress")
        table = self.table_csgo([liste, ev_liste(9, "csgo", t0 + 600, "notstarted")], live=[en_direct, dota])
        self.remplacer(service.client, "appeler", faux_reseau(table))
        r = lancer(service.lister_matchs())
        self.assertEqual([m["id"] for m in r["matchs"]], [7, 8, 9] if r["matchs"][0]["ts"] <= r["matchs"][1]["ts"] else [8, 7, 9])
        self.assertEqual({m["id"]: m["statut"] for m in r["matchs"]}, {7: "en_direct", 8: "en_direct", 9: "prevu"})

    def test_prevu_en_retard_hors_direct_devient_inconnu(self):
        t0 = time.time()
        table = self.table_csgo([ev_liste(1, "csgo", t0 - 3 * 3600, "notstarted")])
        self.remplacer(service.client, "appeler", faux_reseau(table))
        self.assertEqual(lancer(service.lister_matchs())["matchs"][0]["statut"], "inconnu")  # on n'affirme rien

    def test_filtre_par_jeu_et_ne_demande_que_ce_jeu(self):
        t0 = time.time()
        appeles = []
        table = self.table_csgo([ev_liste(1, "csgo", t0 + 600, "notstarted")])
        self.remplacer(service.client, "appeler", faux_reseau(table, appeles))
        r = lancer(service.lister_matchs("csgo"))
        self.assertEqual(len(r["matchs"]), 1)
        self.assertEqual(len(appeles), 3)  # direct + J + J+1 pour UN jeu
        self.assertEqual(lancer(service.lister_matchs("dota2"))["matchs"], [])

    def test_favoris_marques_sans_appel_api(self):
        t0 = time.time()
        depot.sauver_equipe({"id": 1, "nom": "A", "jeu": "csgo"})
        table = self.table_csgo([ev_liste(1, "csgo", t0 + 600, "notstarted", e1=1, e2=2),
                                 ev_liste(2, "csgo", t0 + 900, "notstarted", e1=3, e2=4)])
        self.remplacer(service.client, "appeler", faux_reseau(table))
        favoris = {m["id"]: m["favori"] for m in lancer(service.lister_matchs())["matchs"]}
        self.assertEqual(favoris, {1: True, 2: False})

    def test_un_jeu_en_echec_est_signale_sans_bloquer_les_autres(self):
        t0 = time.time()
        table = self.table_csgo([ev_liste(1, "csgo", t0 + 600, "notstarted")])
        del table[service._chemin_jour(1570, self.maintenant)]  # Dota 2 d'aujourd'hui en échec
        self.remplacer(service.client, "appeler", faux_reseau(table))
        r = lancer(service.lister_matchs())
        self.assertEqual(len(r["matchs"]), 1)
        self.assertEqual(r["jeux_en_echec"], ["dota2"])
        self.assertIn("Dota 2", r["conseil"])

    def test_tout_en_echec_donne_un_conseil_clair(self):
        self.remplacer(service.client, "appeler", faux_reseau({}))
        r = lancer(service.lister_matchs())
        self.assertEqual(r["matchs"], [])
        self.assertFalse(r["direct_disponible"])
        self.assertIn("indisponibles", r["conseil"])

    def test_quota_bas_priorite_direct_puis_aujourdhui_et_reserve_preservee(self):
        client._quota.update(restant=17, fenetre_fin=time.time() + 100)  # 17 - réserve 15 = 2 appels permis
        appeles = []
        self.remplacer(service.client, "appeler", faux_reseau(self.table_csgo([]), appeles))
        self.remplacer(service.client, "en_cache", lambda c: False)
        lancer(service.lister_matchs())
        self.assertEqual(len(appeles), 2)
        self.assertEqual(appeles[0], service.CHEMIN_LIVE)  # le direct passe en premier
        self.assertNotIn(self.demain, appeles)             # demain sacrifié en dernier

    def test_cache_gratuit_ne_consomme_pas_le_budget(self):
        client._quota.update(restant=15, fenetre_fin=time.time() + 100)  # zéro appel réel permis
        appeles = []
        self.remplacer(service.client, "appeler", faux_reseau(self.table_csgo([]), appeles))
        self.remplacer(service.client, "en_cache", lambda c: True)
        lancer(service.lister_matchs())
        self.assertEqual(len(appeles), 7)  # tout vient du cache : toutes les demandes passent

    def test_recherche_equipes_filtre_joueurs_et_autres_sports(self):
        self.remplacer(service.client, "appeler", faux_reseau({"/api/esport/search/ATK": fixture("esport_search.json")}))
        equipes = lancer(service.rechercher_equipes("ATK"))
        self.assertEqual(equipes[0], {"id": 363904, "nom": "ATK", "code": "ATK", "national": False})
        self.assertTrue(all(e["id"] for e in equipes))
        self.assertEqual(len(equipes), 5)  # les joueurs de la réponse sont ignorés
        with self.assertRaises(ErreurEsport):
            lancer(service.rechercher_equipes("a"))

    def test_recherche_encode_le_texte(self):
        appeles = []
        self.remplacer(service.client, "appeler", faux_reseau({}, appeles))
        with self.assertRaises(ErreurEsport):
            lancer(service.rechercher_equipes("Natus Vincere/x"))
        self.assertEqual(appeles, ["/api/esport/search/Natus%20Vincere%2Fx"])

    def test_evenement_en_direct_reel(self):
        ev = parsing.normaliser_evenement(fixture("esport_live.json")["events"][0])
        self.assertEqual((ev["jeu"], ev["statut"], ev["best_of"]), ("dota2", "en_direct", 3))
        self.assertIsNone(ev["vainqueur"])  # winnerCode présent en cours de match : ignoré tant que non terminé

    def test_ajouter_equipe_valide_le_sport(self):
        self.remplacer(service.client, "appeler", faux_appeler({"/api/esport/team/": fixture("esport_team.json")}))
        e = lancer(service.ajouter_equipe(FAZE))
        self.assertEqual((e["nom"], e["jeu"]), ("FaZe", "csgo"))
        self.assertEqual(len(depot.lister_equipes()), 1)
        foot = copy.deepcopy(fixture("esport_team.json"))
        foot["team"]["category"]["sport"]["slug"] = "football"
        self.remplacer(service.client, "appeler", faux_appeler({"/api/esport/team/": foot}))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.ajouter_equipe(2817))
        self.assertEqual(cm.exception.statut, 400)

    def test_import_des_equipes_connues_idempotent_et_coherent(self):
        self.assertEqual(service.importer_equipes_connues()["ajoutees"], 33)
        self.assertEqual(service.importer_equipes_connues()["ajoutees"], 0)
        ids = [e["id"] for e in service.EQUIPES_CONNUES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn(FAZE, ids)
        self.assertEqual(service.importer_equipes_connues(principales_seulement=True)["ajoutees"], 0)


class ServiceDetail(Isole):
    def test_detail_complet_et_normalise(self):
        table = {"/games": fixture("esport_games.json"), "/votes": fixture("esport_votes.json"),
                 "/duel": fixture("esport_duel.json"), "/api/esport/event/": fixture("esport_event.json")}
        self.remplacer(service.client, "appeler", faux_appeler(table))
        d = lancer(service.detail(1, {"cartes", "duel", "votes", "inconnu"}))
        self.assertEqual(d["match"]["jeu"], "csgo")
        self.assertEqual(len(d["cartes"]), 3)
        self.assertEqual(d["cartes"][0]["carte"], "Dust2")
        self.assertEqual((d["duel"]["victoires1"], d["duel"]["victoires2"]), (6, 2))
        self.assertEqual((d["votes"]["equipe1"], d["votes"]["equipe2"]), (38, 22))

    def test_sans_options_un_seul_appel(self):
        compteur = []
        self.remplacer(service.client, "appeler", faux_appeler({"/api/esport/event/": fixture("esport_event.json")}, compteur))
        lancer(service.detail(1, set()))
        self.assertEqual(len(compteur), 1)

    def test_quota_insuffisant_refuse_avant_tout_appel(self):
        client._quota.update(restant=3, fenetre_fin=time.time() + 100)
        compteur = []
        self.remplacer(service.client, "appeler", faux_appeler({}, compteur))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.detail(1, {"cartes", "duel", "votes"}))
        self.assertEqual(cm.exception.statut, 503)
        self.assertEqual(compteur, [])

    def test_match_introuvable(self):
        self.remplacer(service.client, "appeler", faux_appeler({"/api/esport/event/": {"event": None}}))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.detail(1, set()))
        self.assertEqual(cm.exception.statut, 404)


class ServicePronostic(Isole):
    def table(self, ev=None):
        return {
            "/team/%d/" % FAZE: historique_pour(FAZE),
            "/team/99/": historique_pour(99),
            "/h2h": fixture("esport_h2h.json"),
            "/api/esport/event/": {"event": ev or evenement_a_venir()},
        }

    def test_pronostic_fige_et_sans_quota_au_second_appel(self):
        compteur = []
        self.remplacer(service.client, "appeler", faux_appeler(self.table(), compteur))
        r1 = lancer(service.pronostic(555))
        self.assertFalse(r1["deja_calcule"])
        self.assertAlmostEqual(r1["probabiliteVictoireEquipe1"] + r1["probabiliteVictoireEquipe2"], 1.0, places=3)
        self.assertAlmostEqual(r1["probabiliteVictoireEquipe1"], 0.5, delta=0.1)  # historiques identiques
        self.assertIn(r1["confiance"], ("faible", "moyenne", "bonne"))
        self.assertTrue(r1["limites"])
        appels_premier = len(compteur)
        r2 = lancer(service.pronostic(555))
        self.assertTrue(r2["deja_calcule"])
        self.assertEqual(len(compteur), appels_premier)  # figé : zéro appel API en plus
        self.assertEqual(r1["probabiliteVictoireEquipe1"], r2["probabiliteVictoireEquipe1"])

    def test_match_deja_commence_refuse(self):
        for statut in ("inprogress", "finished"):
            self.remplacer(service.client, "appeler", faux_appeler(self.table(evenement_a_venir(statut=statut))))
            with self.assertRaises(ErreurEsport) as cm:
                lancer(service.pronostic(555))
            self.assertEqual(cm.exception.statut, 409)

    def test_historique_indisponible_ou_vide(self):
        t = self.table()
        t["/team/99/"] = None
        self.remplacer(service.client, "appeler", faux_appeler(t))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.pronostic(555))
        self.assertEqual(cm.exception.statut, 503)
        t["/team/99/"] = {"events": []}
        self.remplacer(service.client, "appeler", faux_appeler(t))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.pronostic(555))
        self.assertEqual(cm.exception.statut, 422)

    def test_h2h_en_echec_n_empeche_pas_le_pronostic(self):
        t = self.table()
        t["/h2h"] = None
        self.remplacer(service.client, "appeler", faux_appeler(t))
        r = lancer(service.pronostic(555))
        self.assertTrue(any("indisponibles" in f for f in r["facteurs"]))

    def test_quota_insuffisant(self):
        # L'événement est récupéré (cache/appel), puis le budget restant ne couvre pas les 3 appels suivants.
        client._quota.update(restant=3, fenetre_fin=time.time() + 100)
        self.remplacer(service.client, "appeler", faux_appeler(self.table()))
        with self.assertRaises(ErreurEsport) as cm:
            lancer(service.pronostic(555))
        self.assertEqual(cm.exception.statut, 503)
        self.assertIn("Quota", cm.exception.message)

    def test_donnees_anciennes_signalees(self):
        ev = evenement_a_venir(ts=time.time() + 3600)
        t = self.table(ev)
        vieux = copy.deepcopy(t["/team/99/"])
        for e in vieux["events"]:
            e["startTimestamp"] -= 200 * 86400
        t["/team/99/"] = vieux
        t["/team/%d/" % FAZE] = copy.deepcopy(vieux)
        self.remplacer(service.client, "appeler", faux_appeler(t))
        # historique_pour(99) contient des équipes ids différentes : on remet FAZE côté équipe 1
        t["/team/%d/" % FAZE] = json.loads(json.dumps(vieux).replace("99", str(FAZE)))
        r = lancer(service.pronostic(556))
        self.assertEqual(r["confiance"], "faible")


class ServiceBilan(Isole):
    def pronostic_fige(self, event_id, p1, p2, debut_ts, statut="en_attente", resultat=None):
        depot.sauver_pronostic({
            "eventId": event_id, "equipe1": "A", "equipe2": "B", "debut_ts": debut_ts, "statut": statut,
            "probabiliteVictoireEquipe1": p1, "probabiliteMatchNul": 0.0, "probabiliteVictoireEquipe2": p2,
            **({"resultat": resultat} if resultat else {})})

    def test_verification_precision_et_brier(self):
        passe = time.time() - 5 * 3600
        self.pronostic_fige(1, 0.8, 0.2, passe)       # prédit 1, résultat 1 -> bon
        self.pronostic_fige(2, 0.7, 0.3, passe + 1)   # prédit 1, résultat 2 -> mauvais
        termine = lambda w: {"event": evenement_a_venir(statut="finished", ts=passe, **{}) | {"winnerCode": w}}
        reponses = {1: termine(1), 2: termine(2)}

        async def appeler(chemin, ttl, *, utilisateur=True):
            return copy.deepcopy(reponses[int(chemin.rsplit("/", 1)[1])])

        self.remplacer(service.client, "appeler", appeler)
        b = lancer(service.bilan())
        self.assertEqual(b["verifies"], 2)
        self.assertEqual(b["precision"], 0.5)
        # Brier : (0.8-1)²+(0.2-0)² = 0.08 ; (0.7-0)²+(0.3-1)² = 0.98 -> moyenne 0.53
        self.assertAlmostEqual(b["brier"], 0.53, places=3)
        self.assertFalse(b["fiable"])
        self.assertIn("trop petit", b["note"])

    def test_pas_de_verification_avant_le_delai_et_forfait_ignore(self):
        self.pronostic_fige(1, 0.6, 0.4, time.time() - 600)  # commencé il y a 10 min : trop tôt
        compteur = []
        self.remplacer(service.client, "appeler", faux_appeler({}, compteur))
        self.assertEqual(lancer(service.bilan())["en_attente"], 1)
        self.assertEqual(compteur, [])

        self.pronostic_fige(2, 0.6, 0.4, time.time() - 5 * 3600)
        wo = evenement_a_venir(statut="finished", ts=time.time() - 5 * 3600)
        wo["status"]["description"] = "Walkover"

        async def appeler(chemin, ttl, *, utilisateur=True):
            return {"event": copy.deepcopy(wo)}

        self.remplacer(service.client, "appeler", appeler)
        b = lancer(service.bilan())
        self.assertEqual(b["verifies"], 0)  # le forfait ne compte ni pour ni contre le modèle
        self.assertEqual(depot.obtenir_pronostic(2)["statut"], "ignore")

    def test_verifications_plafonnees_par_requete(self):
        for i in range(10):
            self.pronostic_fige(i, 0.6, 0.4, time.time() - 5 * 3600 - i)
        compteur = []
        self.remplacer(service.client, "appeler", faux_appeler({}, compteur))
        lancer(service.bilan(max_verifications=3))
        self.assertEqual(len(compteur), 3)  # préserve le quota API


if __name__ == "__main__":
    unittest.main()
