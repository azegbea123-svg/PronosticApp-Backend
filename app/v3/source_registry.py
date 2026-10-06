"""Catalogue des endpoints fournis par l'utilisateur.

Ce registre est volontairement descriptif : il ne lance aucun appel API.
Les endpoints dont la réponse fournie ne correspond pas à la requête sont
placés en quarantaine jusqu'à validation, afin d'éviter de polluer le moteur.
"""
SOURCES = [
    {"name":"SofaScore RapidAPI", "host":"sofascore.p.rapidapi.com", "role":"match/form", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"teams/detail fourni retourne un objet event + pregameForm; le contrat annoncé est ambigu."},
    {"name":"SportAPI7", "host":"sportapi7.p.rapidapi.com", "role":"match", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"/event/7881945 fourni retourne rankings Tennis au lieu d'un event football."},
    {"name":"AllSportsAPI2", "host":"allsportsapi2.p.rapidapi.com", "role":"historique_matchs", "status":"QUARANTINE_ENDPOINT_MISMATCH", "reason":"URL /tennis/rankings/atp mais payload fourni = matchs Football; utilisable seulement après validation du contrat réel."},
    {"name":"Odds Feed", "host":"odds-feed.p.rapidapi.com", "role":"cotes", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"requête events fournie mais réponse observée = catalogue des sports, pas des cotes."},
    {"name":"All Sport Live Stream", "host":"all-sport-live-stream.p.rapidapi.com", "role":"transferts", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"/allSportid fourni retourne des transferts, pas un flux de matchs."},
    {"name":"FlashLive Sports", "host":"flashlive-sports.p.rapidapi.com", "role":"matchs", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"endpoint teams/transfers fourni retourne un match NFL."},
    {"name":"FootballData1", "host":"football-data1.p.rapidapi.com", "role":"joueurs/disciplines", "status":"QUARANTINE_RESPONSE_MISMATCH", "reason":"endpoint match/list/live fourni retourne des statistiques de joueurs."},
    {"name":"SoccerData", "host":"soccer-data.p.rapidapi.com", "role":"discipline", "status":"PARTIAL", "reason":"réponse football cohérente, mais sans noms d'équipes dans le bloc fourni; utile uniquement comme enrichissement mappable par event id."},
    {"name":"Football Live Score 2", "host":"football-live-score2.p.rapidapi.com", "role":"fixtures/live", "status":"UNVERIFIED", "reason":"aucun corps JSON fourni dans le fichier."},
    {"name":"free-api-live-football-data", "host":"free-api-live-football-data.p.rapidapi.com", "role":"source principale large", "status":"RATE_LIMITED", "reason":"quota actuellement épuisé; ne pas solliciter automatiquement."},
    {"name":"football-prediction-api", "host":"football-prediction-api.p.rapidapi.com", "role":"probabilités externes", "status":"VALIDATED", "reason":"source externe déjà intégrée au moteur."},
]

def catalogue():
    return {"sources": SOURCES, "safe_to_auto_call": [s["name"] for s in SOURCES if s["status"] in ("VALIDATED", "PARTIAL")], "no_live_test_required": True}
