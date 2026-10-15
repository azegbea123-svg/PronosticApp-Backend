"""Tests du module esport. Fixtures = extraits RÉELS du playground RapidAPI (tests/fixtures/esport_*.json)."""
import asyncio
import copy
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests._stubs import fixture

from app.esport import client, depot, elo, modele, nettoyage, parsing, service
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
        # SÉCURITÉ : ces tests ne doivent JAMAIS toucher une vraie base Firestore (clé présente sur la machine
        # du développeur). On force le repli en mémoire du dépôt, quoi qu'il y ait dans l'environnement.
        self.remplacer(depot.db, "get_client", lambda: None)
        client.vider_cache()
        client.reinitialiser_etat()
        service.reinitialiser_memoire()
        for d in (depot._mem_equipes, depot._mem_pronostics, depot._mem_cache, depot._mem_resultats):
            d.clear()


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

    def test_timestamp_hors_plage_ne_leve_jamais(self):
        base = fixture("esport_event.json")["event"]
        for ts in (10 ** 20, -10 ** 20, 2 ** 70):
            self.assertIsNone(parsing.normaliser_evenement(dict(base, startTimestamp=ts)))
        # Windows lève OSError (et non ValueError) pour un timestamp hors plage : simulé ici.
        with mock.patch.object(parsing, "datetime") as faux:
            faux.fromtimestamp.side_effect = OSError(22, "Invalid argument")
            self.assertIsNone(parsing.normaliser_evenement(base))

    def test_donnees_incompletes_et_statuts(self):
        self.assertIsNone(parsing.normaliser_evenement({"id": 1}))
        self.assertIsNone(parsing.normaliser_evenement(None))
        base = fixture("esport_event.json")["event"]
        for brut, attendu in (("notstarted", "prevu"), ("inprogress", "en_direct"), ("postponed", "reporte"), ("bizarre", "inconnu")):
            ev = dict(base, status={"type": brut, "description": ""})
            self.assertEqual(parsing.normaliser_evenement(ev)["statut"], attendu)
        self.assertIsNone(parsing.normaliser_evenement(dict(base, status={"type": "notstarted"}))["score1"])




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

    def test_429_trop_de_requetes_ne_bloque_qu_une_minute(self):
        # 429 SANS indication de quota épuisé (ou avec du quota restant) = rafale : pause courte, pas une journée.
        FauxClientHttp.reponses = [Rep(429, restant=40)]
        self.assertIsNone(lancer(client.appeler("/a", 60)))
        self.assertEqual(client.quota()["restant"], 40)           # le quota réel n'est PAS écrasé
        self.assertGreater(client.quota()["pause_restante_s"], 0)
        self.assertLessEqual(client.quota()["pause_restante_s"], client.PAUSE_429_S)
        self.assertFalse(client.peut_appeler())
        self.assertIsNone(lancer(client.appeler("/b", 60)))       # en pause : aucun appel réseau
        self.assertEqual(len(FauxClientHttp.appels), 1)
        client._pause_jusqua = time.time() - 1                     # la pause est écoulée
        FauxClientHttp.reponses = [Rep(200, {"ok": 1}, restant=39)]
        self.assertEqual(lancer(client.appeler("/b", 60)), {"ok": 1})

    def test_429_sans_aucun_en_tete_aussi_pause_courte(self):
        FauxClientHttp.reponses = [Rep(429)]
        self.assertIsNone(lancer(client.appeler("/a", 60)))
        self.assertIsNone(client.quota()["restant"])
        self.assertGreater(client.quota()["pause_restante_s"], 0)

    def test_429_quota_du_jour_epuise_est_distingue(self):
        FauxClientHttp.reponses = [Rep(429, restant=0)]
        self.assertIsNone(lancer(client.appeler("/a", 60)))
        self.assertEqual(client.quota()["restant"], 0)
        self.assertEqual(client.quota()["pause_restante_s"], 0)    # le quota gère déjà le blocage
        self.assertIn("quota du jour", client.quota()["derniere_erreur"]["detail"])
        client._quota["fenetre_fin"] = time.time() - 1
        self.assertIsNone(client.quota()["restant"])                # fenêtre écoulée : compteur inconnu

    def test_derniere_erreur_memorisee(self):
        FauxClientHttp.reponses = [Rep(500, restant=30)]
        lancer(client.appeler("/x", 60))
        self.assertEqual(client.quota()["derniere_erreur"]["detail"], "HTTP 500")
        self.assertEqual(client.quota()["derniere_erreur"]["chemin"], "/x")

    def test_cache_persistant_survit_a_un_redemarrage(self):
        FauxClientHttp.reponses = [Rep(200, {"events": [{"id": 1, "fieldTranslations": {"x": "lourd"}}]}, restant=40)]
        lancer(client.appeler("/liste", 3600))
        self.assertEqual(len(FauxClientHttp.appels), 1)
        stocke = depot._mem_cache["/liste"][1]
        self.assertNotIn("fieldTranslations", stocke)               # allégé avant stockage
        client.vider_cache()                                        # = redémarrage du serveur
        self.assertFalse(client.en_cache("/liste"))
        self.assertEqual(lancer(client.appeler("/liste", 3600))["events"][0]["id"], 1)
        self.assertEqual(len(FauxClientHttp.appels), 1)             # AUCUN nouvel appel API

    def test_cache_persistant_expire_et_option_memoire_seule(self):
        depot._mem_cache["/vieux"] = (time.time() - 10, json.dumps({"a": 1}))
        FauxClientHttp.reponses = [Rep(200, {"a": 2}, restant=40), Rep(200, {"b": 1}, restant=39)]
        self.assertEqual(lancer(client.appeler("/vieux", 60)), {"a": 2})  # entrée expirée ignorée
        lancer(client.appeler("/ephemere", 60, persister=False))
        self.assertNotIn("/ephemere", depot._mem_cache)

    def test_precharger_remonte_le_cache_persistant_sans_appel(self):
        depot._mem_cache["/p1"] = (time.time() + 600, json.dumps({"z": 1}))
        self.assertFalse(client.en_cache("/p1"))
        lancer(client.precharger(["/p1", "/inconnu"]))
        self.assertTrue(client.en_cache("/p1"))
        self.assertFalse(client.en_cache("/inconnu"))
        self.assertEqual(FauxClientHttp.appels, [])

    def test_sans_cle_aucun_appel(self):
        self.remplacer(client, "CLE_RAPIDAPI", "")
        self.assertIsNone(lancer(client.appeler("/k", 60)))
        self.assertEqual(FauxClientHttp.appels, [])


def faux_appeler(table, compteur=None):
    """Remplace client.appeler : `table` = {fragment_de_chemin: reponse | None}."""
    async def f(chemin, ttl, *, utilisateur=True, persister=True):
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


def ev_liste(id_, jeu, ts, statut, e1=1, e2=2, cat_id=None):
    """Un événement tiré de la capture réelle (matchs par catégorie), recalé sur `jeu`, `ts` et `statut`."""
    ev = copy.deepcopy(fixture("esport_category_events.json")["events"][0])
    ev.update(id=id_, startTimestamp=int(ts), status={"type": statut, "description": ""})
    ev["tournament"]["category"].update(slug=jeu, id=cat_id or service.JEUX[jeu]["id"])
    ev["homeTeam"]["id"], ev["awayTeam"]["id"] = e1, e2
    return ev


def faux_reseau(table, appeles=None):
    """Remplace client.appeler par chemin EXACT (les chemins non listés échouent -> None)."""
    async def f(chemin, ttl, *, utilisateur=True, persister=True):
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


def m_compact(id_, e1, e2, s1, s2, ts, jeu="csgo", bo=3):
    return {"id": id_, "jeu": jeu, "ts": ts, "e1": e1, "n1": f"E{e1}", "e2": e2, "n2": f"E{e2}",
            "s1": s1, "s2": s2, "bo": bo, "v": 1 if s1 > s2 else 2}


class Elo(unittest.TestCase):
    def test_somme_constante_et_force_relative(self):
        ms = [m_compact(i, 1, 2, 2, 0, 1000 + i) for i in range(6)]
        c = elo.calculer(ms)
        self.assertGreater(c["notes"][1], 1500)
        self.assertLess(c["notes"][2], 1500)
        self.assertAlmostEqual(c["notes"][1] + c["notes"][2], 3000.0)  # ce qu'un gagne, l'autre le perd

    def test_battre_un_fort_rapporte_plus_que_battre_un_faible(self):
        # 10 est fort (a battu tout le monde), 20 faible. 30 et 40 battent respectivement 10 et 20 une fois.
        base = [m_compact(i, 10, 20, 2, 0, 100 + i) for i in range(8)] + [m_compact(50 + i, 10, 21, 2, 0, 200 + i) for i in range(4)]
        a = elo.calculer(base + [m_compact(900, 30, 10, 2, 0, 5000)])["notes"][30]
        b = elo.calculer(base + [m_compact(901, 40, 20, 2, 0, 5000)])["notes"][40]
        self.assertGreater(a, b)

    def test_un_deux_zero_compte_plus_qu_un_deux_un(self):
        net = elo.calculer([m_compact(1, 1, 2, 2, 0, 1)])["notes"][1]
        serre = elo.calculer([m_compact(1, 1, 2, 2, 1, 1)])["notes"][1]
        self.assertGreater(net, serre)

    def test_pas_de_pronostic_sans_historique(self):
        c = elo.calculer([m_compact(1, 1, 2, 2, 0, 1)])
        r = elo.pronostiquer(c, 1, 2, 3, NOW_)
        self.assertIn("Historique insuffisant", r["raison"])
        self.assertIn("raison", elo.pronostiquer(c, 1, 999, 3, NOW_))

    def test_pronostic_symetrique_favori_et_format(self):
        ms = [m_compact(i, 1, 2, 2, 0, NOW_ - (40 - i) * 86400) for i in range(25)]
        ms += [m_compact(100 + i, 1, 3, 2, 0, NOW_ - (40 - i) * 86400) for i in range(25)]
        ms += [m_compact(200 + i, 3, 2, 2, 0, NOW_ - (40 - i) * 86400) for i in range(25)]
        c = elo.calculer(ms)
        a = elo.pronostiquer(c, 1, 2, 3, NOW_)["pronostic"]
        b = elo.pronostiquer(c, 2, 1, 3, NOW_)["pronostic"]
        self.assertGreater(a["p1"], 0.8)
        self.assertEqual(a["favori"], 1)
        self.assertAlmostEqual(a["p1"], b["p2"], places=3)
        self.assertAlmostEqual(a["p1"] + a["pnul"] + a["p2"], 1.0, places=3)
        self.assertEqual(a["confiance"], "bonne")
        bo1 = elo.pronostiquer(c, 1, 2, 1, NOW_)["pronostic"]["p1"]
        bo5 = elo.pronostiquer(c, 1, 2, 5, NOW_)["pronostic"]["p1"]
        self.assertLess(bo1, a["p1"])
        self.assertLess(a["p1"], bo5)
        nul = elo.pronostiquer(c, 1, 2, 2, NOW_)["pronostic"]
        self.assertGreater(nul["pnul"], 0)  # BO2 : le nul existe

    def test_confiance_selon_volume_et_fraicheur(self):
        self.assertEqual(modele.confiance(30, 30, NOW_ - 5 * 86400, NOW_), "bonne")
        self.assertEqual(modele.confiance(30, 10, NOW_ - 5 * 86400, NOW_), "moyenne")
        self.assertEqual(modele.confiance(30, 3, NOW_ - 5 * 86400, NOW_), "faible")
        self.assertEqual(modele.confiance(30, 30, NOW_ - 120 * 86400, NOW_), "faible")
        self.assertEqual(modele.confiance(30, 30, None, NOW_), "faible")

    def test_proba_serie(self):
        bo1, bo3, bo5 = (modele.proba_serie(0.6, n)["p1"] for n in (1, 3, 5))
        self.assertAlmostEqual(bo1, 0.6)
        self.assertLess(bo1, bo3)
        self.assertLess(bo3, bo5)
        for n in (1, 2, 3, 5, None):
            s = modele.proba_serie(0.6, n)
            self.assertAlmostEqual(s["p1"] + s["nul"] + s["p2"], 1.0)


NOW_ = 1_800_000_000


class PronosticsDansLaListe(Isole):
    def setUp(self):
        super().setUp()
        self.maintenant = datetime.now(timezone.utc)
        self.aujourdhui = service._chemin_jour(1572, self.maintenant)
        self.demain = service._chemin_jour(1572, self.maintenant + timedelta(days=1))

    def semer_historique(self, force=10):
        """Équipes 10, 11, 12 : 10 domine 11 qui domine 12 (assez de matchs pour pronostiquer)."""
        t0 = int(time.time())
        ms = []
        for i in range(force):
            ms += [m_compact(1000 + i, 10, 11, 2, 0, t0 - (i + 3) * 86400), m_compact(2000 + i, 11, 12, 2, 0, t0 - (i + 3) * 86400),
                   m_compact(3000 + i, 10, 12, 2, 0, t0 - (i + 3) * 86400)]
        depot.sauver_resultats_jour("csgo_2026-01-01", {"jeu": "csgo", "jour": "csgo_2026-01-01", "matchs": ms})

    def table(self, evs_aujourdhui=(), live=()):
        t = {service.CHEMIN_LIVE: {"events": list(live)}, self.aujourdhui: {"events": list(evs_aujourdhui)}, self.demain: {"events": []}}
        for slug in ("dota2", "lol"):
            for d in (self.maintenant, self.maintenant + timedelta(days=1)):
                t[service._chemin_jour(service.JEUX[slug]["id"], d)] = {"events": []}
        return t

    def lister(self, table, appeles=None):
        self.remplacer(service.client, "appeler", faux_reseau(table, appeles))
        return lancer(service.lister_matchs())

    def test_pronostic_affiche_directement_sur_chaque_match_a_venir(self):
        self.semer_historique()
        t0 = time.time()
        r = self.lister(self.table([ev_liste(1, "csgo", t0 + 3600, "notstarted", e1=10, e2=12)]))
        m = r["matchs"][0]
        self.assertIsNone(m["pronostic_raison"])
        p = m["pronostic"]
        self.assertGreater(p["p1"], 0.6)
        self.assertEqual(p["favori"], 1)
        self.assertTrue(p["gele"])
        self.assertAlmostEqual(p["p1"] + p["pnul"] + p["p2"], 1.0, places=2)
        self.assertGreater(p["details"]["elo1"], p["details"]["elo2"])
        self.assertTrue(p["limites"])

    def test_zero_appel_api_supplementaire_pour_les_pronostics(self):
        t0 = time.time()
        sans, avec = [], []
        self.lister(self.table([ev_liste(1, "csgo", t0 + 3600, "notstarted", e1=10, e2=12)]), sans)
        self.semer_historique()
        service.reinitialiser_memoire()
        self.lister(self.table([ev_liste(1, "csgo", t0 + 3600, "notstarted", e1=10, e2=12)]), avec)
        self.assertEqual(len(avec), len(sans))  # avoir un historique ne change pas le nombre d'appels
        self.assertLessEqual(len(avec), 7)

    def test_historique_insuffisant_donne_une_raison_et_pas_un_faux_50_pour_cent(self):
        r = self.lister(self.table([ev_liste(1, "csgo", time.time() + 3600, "notstarted", e1=10, e2=12)]))
        self.assertIsNone(r["matchs"][0]["pronostic"])
        self.assertIn("insuffisant", r["matchs"][0]["pronostic_raison"])

    def test_pronostic_fige_au_premier_calcul(self):
        self.semer_historique()
        t0 = time.time()
        table = self.table([ev_liste(1, "csgo", t0 + 3600, "notstarted", e1=10, e2=12)])
        p1 = self.lister(table)["matchs"][0]["pronostic"]["p1"]
        self.assertEqual(depot._mem_pronostics["1"]["statut"], "en_attente")
        # L'historique change beaucoup (12 se met à dominer) : le pronostic déjà publié ne bouge pas.
        inverse = [m_compact(9000 + i, 12, 10, 2, 0, int(t0) - 3600 - i) for i in range(30)]
        depot.sauver_resultats_jour("csgo_2026-01-02", {"jeu": "csgo", "jour": "csgo_2026-01-02", "matchs": inverse})
        service.reinitialiser_memoire()
        p2 = self.lister(table)["matchs"][0]["pronostic"]["p1"]
        self.assertEqual(p1, p2)

    def test_en_direct_sans_pronostic_fige_est_calcule_mais_pas_enregistre(self):
        self.semer_historique()
        t0 = time.time()
        live = ev_liste(7, "csgo", t0 - 900, "inprogress", e1=10, e2=12)
        r = self.lister(self.table([], live=[live]))
        p = r["matchs"][0]["pronostic"]
        self.assertFalse(p["gele"])
        self.assertNotIn("7", depot._mem_pronostics)  # un pronostic calculé après le début n'entre jamais au bilan

    def test_match_termine_confronte_le_pronostic_fige_au_resultat(self):
        self.semer_historique()
        t0 = time.time()
        depot.sauver_pronostic({"eventId": 5, "equipe1": "A", "equipe2": "B", "debut_ts": t0 - 7200, "statut": "en_attente",
                                "probabiliteVictoireEquipe1": 0.7, "probabiliteMatchNul": 0.0,
                                "probabiliteVictoireEquipe2": 0.3, "confiance": "moyenne"})
        fini = ev_liste(5, "csgo", t0 - 7200, "finished", e1=10, e2=12)
        fini["winnerCode"] = 1
        m = self.lister(self.table([fini]))["matchs"][0]
        self.assertEqual(m["statut"], "termine")
        self.assertTrue(m["pronostic"]["correct"])  # prédit l'équipe 1, l'équipe 1 a gagné

    def test_resultats_du_jour_memorises_une_seule_fois(self):
        t0 = time.time()
        fini = ev_liste(3, "csgo", t0 - 7200, "finished", e1=10, e2=12)
        fini["winnerCode"] = 1
        self.lister(self.table([fini]))
        doc_id = service._id_jour("csgo", self.maintenant)
        self.assertEqual([m["id"] for m in depot._mem_resultats[doc_id]["matchs"]], [3])
        depot._mem_resultats[doc_id]["matchs"] = []     # sentinelle : une réécriture la remplacerait
        service.reinitialiser_memoire()
        client._cache[self.aujourdhui] = (time.time() + 600, {"events": [fini]})  # la liste est maintenant EN CACHE
        self.lister(self.table([fini]))
        self.assertEqual(depot._mem_resultats[doc_id]["matchs"], [])  # liste en cache : rien n'est réécrit

    def test_forfait_et_match_sans_vainqueur_ne_nourrissent_pas_le_classement(self):
        ev = ev_liste(4, "csgo", time.time() - 7200, "finished", e1=10, e2=12)
        ev["status"]["description"] = "Walkover"
        self.assertIsNone(service._compact(parsing.normaliser_evenement(ev)))
        ev2 = ev_liste(6, "csgo", time.time() - 7200, "finished", e1=10, e2=12)
        ev2["winnerCode"] = None
        self.assertIsNone(service._compact(parsing.normaliser_evenement(ev2)))


class Rattrapage(Isole):
    def setUp(self):
        super().setUp()
        self.maintenant = datetime.now(timezone.utc)
        self.appels = []

        async def f(chemin, ttl, *, utilisateur=True, persister=True):
            self.appels.append((chemin, ttl, persister))
            return {"events": []}

        self.remplacer(service.client, "appeler", f)

    def test_ne_rattrape_que_le_quota_excedentaire(self):
        for restant, attendu in ((None, 0), (29, 0), (30, 0), (31, 1), (45, 2)):
            self.appels.clear()
            service.reinitialiser_memoire()
            depot._mem_resultats.clear()
            client._quota.update(restant=restant, fenetre_fin=time.time() + 100)
            self.assertEqual(lancer(service._rattraper_passe(self.maintenant)), attendu, f"restant={restant}")
            self.assertEqual(len(self.appels), attendu)

    def test_progresse_jour_apres_jour_sans_refaire_ce_qui_est_fait(self):
        client._quota.update(restant=45, fenetre_fin=time.time() + 100)
        lancer(service._rattraper_passe(self.maintenant))
        lancer(service._rattraper_passe(self.maintenant))
        chemins = [a[0] for a in self.appels]
        self.assertEqual(len(chemins), 4)
        self.assertEqual(len(set(chemins)), 4)  # jamais deux fois le même jour
        self.assertEqual(len(depot._mem_resultats), 4)
        hier = service._chemin_jour(1572, self.maintenant - timedelta(days=1))
        self.assertIn(hier, chemins)  # on commence par les jours les plus récents
        self.assertTrue(all(a[2] is False for a in self.appels))  # pas de doublon de stockage en cache

    def test_s_arrete_au_premier_echec(self):
        client._quota.update(restant=45, fenetre_fin=time.time() + 100)
        resultats = [None]

        async def echec(chemin, ttl, *, utilisateur=True, persister=True):
            self.appels.append(chemin)
            return resultats.pop(0) if resultats else {"events": []}

        self.remplacer(service.client, "appeler", echec)
        self.assertEqual(lancer(service._rattraper_passe(self.maintenant)), 0)
        self.assertEqual(len(self.appels), 1)

    def test_apres_un_echec_pas_d_acharnement_pendant_dix_minutes(self):
        client._quota.update(restant=45, fenetre_fin=time.time() + 100)

        async def echec(chemin, ttl, *, utilisateur=True, persister=True):
            self.appels.append(chemin)
            return None

        self.remplacer(service.client, "appeler", echec)
        lancer(service._rattraper_passe(self.maintenant))
        lancer(service._rattraper_passe(self.maintenant))
        lancer(service._rattraper_passe(self.maintenant))
        self.assertEqual(len(self.appels), 1)                   # une seule tentative, puis pause
        service._memoire["pause_rattrapage"] = time.time() - 1   # la pause est écoulée
        lancer(service._rattraper_passe(self.maintenant))
        self.assertEqual(len(self.appels), 2)

    def test_historique_complet_ne_fait_plus_aucun_appel(self):
        for k in range(1, service.JOURS_HISTORIQUE + 1):
            for slug in service.JEUX:
                depot._mem_resultats[service._id_jour(slug, self.maintenant - timedelta(days=k))] = {"matchs": []}
        client._quota.update(restant=50, fenetre_fin=time.time() + 100)
        self.assertEqual(lancer(service._rattraper_passe(self.maintenant)), 0)
        self.assertEqual(self.appels, [])
        self.assertEqual(service._progression_historique(self.maintenant)["jours_couverts"], service.JOURS_HISTORIQUE)


class QuotaEtDiagnostic(Isole):
    def setUp(self):
        super().setUp()
        self.maintenant = datetime.now(timezone.utc)

    def tout_vide(self):
        t = {service.CHEMIN_LIVE: {"events": []}}
        for slug, j in service.JEUX.items():
            for d in (self.maintenant, self.maintenant + timedelta(days=1)):
                t[service._chemin_jour(j["id"], d)] = {"events": []}
        return t

    def test_le_direct_est_plafonne_par_jour(self):
        service._live_depenses.update(jour=self.maintenant.date().isoformat(), n=service.LIVE_MAX_PAR_JOUR)
        appeles = []
        self.remplacer(service.client, "appeler", faux_reseau(self.tout_vide(), appeles))
        r = lancer(service.lister_matchs())
        self.assertNotIn(service.CHEMIN_LIVE, appeles)
        self.assertIn("plafond", r["diagnostic"][0]["etat"])

    def test_chaque_appel_reel_au_direct_est_compte(self):
        self.remplacer(service.client, "appeler", faux_reseau(self.tout_vide()))
        lancer(service.lister_matchs())
        self.assertEqual(service._live_depenses["n"], 1)

    def test_un_direct_en_cache_ne_compte_pas(self):
        client._cache[service.CHEMIN_LIVE] = (time.time() + 600, {"events": []})
        self.remplacer(service.client, "appeler", faux_reseau(self.tout_vide()))
        lancer(service.lister_matchs())
        self.assertEqual(service._live_depenses["n"], 0)

    def test_diagnostic_decrit_chaque_requete(self):
        t = self.tout_vide()
        del t[service._chemin_jour(1570, self.maintenant)]            # Dota 2 d'aujourd'hui : échec
        t[service._chemin_jour(1571, self.maintenant)] = {}             # LoL d'aujourd'hui : 204
        self.remplacer(service.client, "appeler", faux_reseau(t))
        diag = {d["requete"]: d["etat"] for d in lancer(service.lister_matchs())["diagnostic"]}
        self.assertEqual(diag["dota2 aujourd'hui"], "échec")
        self.assertEqual(diag["lol aujourd'hui"], "vide (204)")
        self.assertTrue(diag["csgo aujourd'hui"].startswith("ok (0"))
        self.assertEqual(len(diag), 7)

    def test_jamais_plus_de_deux_requetes_simultanees(self):
        etat = {"en_cours": 0, "max": 0}

        async def lente(chemin, ttl, *, utilisateur=True, persister=True):
            etat["en_cours"] += 1
            etat["max"] = max(etat["max"], etat["en_cours"])
            await asyncio.sleep(0.01)
            etat["en_cours"] -= 1
            return {"events": []}

        self.remplacer(service.client, "appeler", lente)
        lancer(service.lister_matchs())
        self.assertEqual(etat["max"], service.LOT_REQUETES)

    def test_une_liste_servie_par_le_cache_persistant_ne_coute_aucun_appel(self):
        # Premier passage : « API » réelle simulée par le client HTTP factice ; second : après redémarrage.
        self.remplacer(client, "CLE_RAPIDAPI", "cle")
        self.remplacer(client.httpx, "AsyncClient", FauxClientHttp)
        FauxClientHttp.appels, FauxClientHttp.reponses = [], [Rep(200, {"events": []}, restant=20) for _ in range(7)]  # sous le seuil de rattrapage
        lancer(service.lister_matchs())
        premier = len(FauxClientHttp.appels)
        self.assertGreaterEqual(premier, 1)
        client.vider_cache()                  # redémarrage de Render
        service.reinitialiser_memoire()
        FauxClientHttp.reponses = [Rep(500)] * 7
        r = lancer(service.lister_matchs())
        self.assertEqual(len(FauxClientHttp.appels), premier)   # AUCUN appel de plus
        self.assertEqual(r["jeux_en_echec"], [])


class BilanSansAppelApi(Isole):
    def frige(self, id_, p1, p2, debut_ts, statut="en_attente"):
        depot.sauver_pronostic({"eventId": id_, "equipe1": "A", "equipe2": "B", "debut_ts": debut_ts, "statut": statut,
                                "probabiliteVictoireEquipe1": p1, "probabiliteMatchNul": 0.0, "probabiliteVictoireEquipe2": p2,
                                "confiance": "moyenne"})

    def interdire_api(self):
        async def interdit(*a, **k):
            raise AssertionError("le bilan ne doit appeler AUCUN endpoint de l'API")

        self.remplacer(service.client, "appeler", interdit)

    def test_verifie_sur_les_resultats_memorises_precision_et_brier(self):
        self.interdire_api()
        passe = time.time() - 5 * 3600
        self.frige(1, 0.8, 0.2, passe)       # prédit 1, résultat 1 -> bon
        self.frige(2, 0.7, 0.3, passe + 1)   # prédit 1, résultat 2 -> mauvais
        depot.sauver_resultats_jour("csgo_2026-01-01", {"jeu": "csgo", "jour": "csgo_2026-01-01", "matchs": [
            m_compact(1, 10, 11, 2, 0, int(passe)), m_compact(2, 10, 11, 0, 2, int(passe))]})
        b = lancer(service.bilan())
        self.assertEqual((b["verifies"], b["precision"]), (2, 0.5))
        self.assertAlmostEqual(b["brier"], 0.53, places=3)  # (0.8-1)²+(0.2)² = 0.08 ; (0.7)²+(0.3-1)² = 0.98
        self.assertFalse(b["fiable"])
        self.assertIn("trop petit", b["note"])

    def test_pas_de_verification_avant_le_delai_ni_sans_resultat(self):
        self.interdire_api()
        self.frige(1, 0.6, 0.4, time.time() - 600)              # commencé il y a 10 min
        self.frige(2, 0.6, 0.4, time.time() - 5 * 3600)        # terminé mais résultat inconnu
        b = lancer(service.bilan())
        self.assertEqual((b["verifies"], b["en_attente"]), (0, 2))

    def test_pronostic_jamais_vu_termine_expire_sans_fausser_le_bilan(self):
        self.interdire_api()
        self.frige(1, 0.6, 0.4, time.time() - (service.EXPIRATION_PRONOSTIC_J + 1) * 86400)
        b = lancer(service.bilan())
        self.assertEqual((b["verifies"], b["en_attente"]), (0, 0))
        self.assertEqual(depot.obtenir_pronostic(1)["statut"], "ignore")


class Nettoyage(unittest.TestCase):
    def test_ne_selectionne_que_les_documents_factices(self):
        pronos = [
            {"eventId": 555, "equipe1": "FaZe", "equipe2": "Rivaux"},   # factice
            {"eventId": 3, "equipe1": "A", "equipe2": "B"},             # factice
            {"eventId": 555, "equipe1": "NAVI", "equipe2": "G2"},       # même id mais VRAIES équipes : conservé
            {"eventId": 15737068, "equipe1": "A", "equipe2": "B"},      # noms factices mais id réel : conservé
            {"eventId": 10944540, "equipe1": "Iron Branch", "equipe2": "Sparta"},
        ]
        self.assertEqual([p["eventId"] for p in nettoyage.pronostics_de_test(pronos)], [555, 3])
        equipes = [{"id": 1, "nom": "A"}, {"id": 1, "nom": "Vraie équipe"}, {"id": 364396, "nom": "FaZe"}]
        self.assertEqual(nettoyage.equipes_de_test(equipes), [{"id": 1, "nom": "A"}])


if __name__ == "__main__":
    unittest.main()
