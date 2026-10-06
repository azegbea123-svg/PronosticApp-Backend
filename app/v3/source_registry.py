"""Catalogue des sources V3.1 + état dynamique de test/activation."""
from .source_probe import ENDPOINTS, etat_sources

SOURCES = [
    {"id":"sofascore", "name":"SofaScore RapidAPI", "host":"sofascore.p.rapidapi.com", "role":"team/form/enrichment", "status":"TESTABLE", "reason":"Endpoint teams/detail fourni; validation réelle disponible depuis Swagger."},
    {"id":"sportapi7", "name":"SportAPI7", "host":"sportapi7.p.rapidapi.com", "role":"fixtures + event detail", "status":"VALIDATED_EVENT_ENDPOINT", "reason":"Le connecteur V3.3 utilise scheduled-events/{date} pour la liste puis /event/{id} pour le détail. L'activation doit être confirmée par un test de la liste du jour."},
    {"id":"allsportsapi2", "name":"AllSportsAPI2", "host":"allsportsapi2.p.rapidapi.com", "role":"historical/fixtures", "status":"TESTABLE", "reason":"URL rankings ATP mais payload fourni contenant du football; validation manuelle nécessaire."},
    {"id":"odds-feed", "name":"Odds Feed", "host":"odds-feed.p.rapidapi.com", "role":"odds", "status":"TESTABLE", "reason":"La réponse fournie ne ressemblait pas à des cotes; test réel disponible."},
    {"id":"all-sport-live-stream", "name":"All Sport Live Stream", "host":"all-sport-live-stream.p.rapidapi.com", "role":"live/events", "status":"TESTABLE", "reason":"Payload fourni incohérent avec l'URL; test réel nécessaire."},
    {"id":"flashlive", "name":"FlashLive Sports", "host":"flashlive-sports.p.rapidapi.com", "role":"events/transfers", "status":"TESTABLE", "reason":"Payload fourni contenait NFL; validation football obligatoire."},
    {"id":"football-data1", "name":"FootballData1", "host":"football-data1.p.rapidapi.com", "role":"fixtures/live", "status":"TESTABLE", "reason":"Payload fourni ressemblait à des statistiques joueurs; test réel nécessaire."},
    {"id":"soccer-data", "name":"SoccerData", "host":"soccer-data.p.rapidapi.com", "role":"discipline", "status":"TESTABLE", "reason":"Réponse football avec identifiants d'événements; utile après test de mappage."},
    {"id":"football-live-score2", "name":"Football Live Score 2", "host":"football-live-score2.p.rapidapi.com", "role":"fixtures/live", "status":"TESTABLE", "reason":"Réponse non vérifiée dans le fichier fourni."},
    {"id":"free-api-live-football-data", "name":"free-api-live-football-data", "host":"free-api-live-football-data.p.rapidapi.com", "role":"source principale large", "status":"RATE_LIMITED", "reason":"Source existante; le quota observé était épuisé."},
    {"id":"football-prediction-api", "name":"football-prediction-api", "host":"football-prediction-api.p.rapidapi.com", "role":"probabilités externes", "status":"VALIDATED", "reason":"Source externe déjà intégrée au moteur."},
]

def catalogue():
    dynamic = {x["source_id"]: x for x in etat_sources()["sources"]}
    out = []
    for src in SOURCES:
        d = dynamic.get(src.get("id"), {})
        item = dict(src)
        item["active"] = bool(d.get("active", False))
        item["last_test"] = d.get("last_test")
        item["fixture_capable"] = d.get("fixture_capable", False)
        out.append(item)
    return {
        "sources": out,
        "safe_to_auto_call": [s["id"] for s in out if s.get("active")],
        "live_test_routes": [
            "/debug/v3/source/{source_id}/test",
            "/debug/v3/sources/test-all",
            "/debug/v3/sources/state",
            "/debug/v3/source/{source_id}/activate",
            "/debug/v3/source/{source_id}/deactivate",
        ],
        "activation_note": "Activation est en mémoire du processus et peut être réinitialisée après redémarrage/redéploiement Render.",
    }
