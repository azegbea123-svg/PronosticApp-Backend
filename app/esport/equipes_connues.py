"""
Équipes e-sport DÉJÀ CONNUES, extraites des captures réelles du playground
(endpoint.txt : événements, historique de FaZe, confrontations directes).
Identifiants et noms : tels que renvoyés par l'API — rien n'est inventé.

`priorite` = grandes organisations CS : actualisées en premier quand le quota
de l'API (50 requêtes/jour) ne permet pas de tout rafraîchir d'un coup.
Cette liste ne contient QUE du Counter-Strike : aucune équipe Dota 2 (ou autre
jeu) n'apparaît dans les captures ; pour en ajouter, utiliser POST /esport/equipes
avec l'identifiant lu dans une réponse de l'API.
"""

EQUIPES_CONNUES = [
    {"id": 363904, "nom": "ATK", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
    {"id": 364232, "nom": "Natus Vincere", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "UKR", "priorite": True},
    {"id": 364252, "nom": "FURIA", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "BRA", "priorite": True},
    {"id": 364378, "nom": "Astralis", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "DNK", "priorite": True},
    {"id": 364394, "nom": "BIG", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "DEU", "priorite": True},
    {"id": 364395, "nom": "Heroic", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "DNK", "priorite": True},
    {"id": 364396, "nom": "FaZe", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "USA", "priorite": True},
    {"id": 364397, "nom": "G2", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "DEU", "priorite": True},
    {"id": 364400, "nom": "fnatic", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "GBR", "priorite": True},
    {"id": 364477, "nom": "paiN", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "BRA", "priorite": True},
    {"id": 365248, "nom": "MOUZ", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "DEU", "priorite": True},
    {"id": 366215, "nom": "TYLOO", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "CHN", "priorite": True},
    {"id": 366416, "nom": "3DMAX", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "FRA", "priorite": True},
    {"id": 366536, "nom": "NRG", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "USA", "priorite": True},
    {"id": 374336, "nom": "BEE", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "HUN", "priorite": False},
    {"id": 409766, "nom": "Team Falcons", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "SAU", "priorite": True},
    {"id": 414937, "nom": "Iron Branch", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "BLR", "priorite": False},
    {"id": 418691, "nom": "EYEBALLERS", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "SWE", "priorite": False},
    {"id": 423985, "nom": "Monte", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "UKR", "priorite": True},
    {"id": 424876, "nom": "Aurora Gaming", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "RUS", "priorite": True},
    {"id": 449587, "nom": "Nouns", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
    {"id": 451416, "nom": "paiN Academy", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "BRA", "priorite": False},
    {"id": 456350, "nom": "Permitta Esports", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "POL", "priorite": False},
    {"id": 488028, "nom": "CYBERSHOKE Esports", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "RUS", "priorite": False},
    {"id": 492359, "nom": "Passion UA", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "UKR", "priorite": False},
    {"id": 493034, "nom": "PARIVISION", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "RUS", "priorite": True},
    {"id": 504212, "nom": "aimclub", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
    {"id": 527769, "nom": "BC.Game Esports", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
    {"id": 552809, "nom": "Wildcard", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "USA", "priorite": False},
    {"id": 929876, "nom": "Game Hunters", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "BRA", "priorite": False},
    {"id": 1074797, "nom": "Inner Circle Esports", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
    {"id": 1137916, "nom": "illwill", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": "UKR", "priorite": False},
    {"id": 1175011, "nom": "Drama eSports", "jeu": "csgo", "jeu_nom": "Counter Strike", "pays": None, "priorite": False},
]
