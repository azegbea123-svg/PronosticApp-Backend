"""Constantes de configuration partagées, centralisées ici pour éviter les doublons."""

import os

# 💎 Config VIP
PRIX_VIP_FCFA = 500
DUREE_VIP_JOURS = 3
LIMITE_GRATUITE_QUOTIDIENNE = 3

# ⚠️ Mot de passe admin — sert aussi de "jeton universel" pour le debug
# (voir auth.py). À définir en variable d'environnement sur Render.
MOT_DE_PASSE_ADMIN = os.environ.get("ADMIN_PASSWORD", "change-moi")

# ⚠️ Clé API-Football (api-sports.io) — À définir en variable
# d'environnement sur Render. Plan gratuit = 10 requêtes/minute, 100/jour
# — voir sources/api_football.py pour le limiteur de débit qui protège
# contre un nouveau blocage de compte.
CLE_API_FOOTBALL = os.environ.get("API_FOOTBALL_KEY", "")

# ==== Liste de matchs J / J+1 (nouvelle approche) ====
#
# /matchs affiche TOUS les matchs remontés par API-Football, sans
# filtre — ceci n'est PAS un filtre d'affichage. C'est la liste des
# championnats pour lesquels on vérifie la disponibilité des données
# BeSoccer À L'AVANCE (clé = ID API-Football du championnat, valeur =
# libellé). Un match hors de cette liste reste visible, simplement
# marqué "données indisponibles" par défaut — vérifier BeSoccer pour
# absolument tous les matchs du monde (souvent 500-1000+/jour)
# relancerait le volume de scraping qui avait déjà causé un blocage
# temporaire.
#
# ⚠️ IDs à vérifier — utilise /debug/ligues?pays=<pays> (nouvel endpoint
# admin) pour confirmer/ajuster, notamment pour trouver l'ID du
# championnat togolais.
CHAMPIONNATS_SUIVIS = {
    61: "Ligue 1",
    39: "Premier League",
    140: "La Liga",
    135: "Serie A",
    78: "Bundesliga",
    2: "Ligue des Champions",
    12: "Ligue des Champions CAF",
    20: "Coupe de la Confédération CAF",
    # TODO: ajouter ici l'ID du championnat togolais une fois trouvé
}

# Durée avant de reconsidérer la liste des matchs/disponibilités comme
# périmée et de la reconstruire (nouvelle requête API-Football +
# nouvelle vérification BeSoccer). 3h : largement assez réactif pour des
# matchs qui ne changent pas d'heure en heure, sans solliciter les
# quotas à chaque appel de /matchs.
TTL_RAFRAICHISSEMENT_MATCHS_SECONDES = 3 * 3600
