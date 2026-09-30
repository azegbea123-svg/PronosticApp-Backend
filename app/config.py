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

# ==== Liste de matchs J / J+1 ====
#
# /matchs affiche TOUS les matchs remontés par API-Football, sans
# filtre par championnat. La vérification BeSoccer (disponibilité des
# données) se fait EN AVANCE, en arrière-plan, progressivement — voir
# matchs.verifier_disponibilite_prochains et
# MAX_VERIFICATIONS_PAR_CYCLE dans matchs.py.

# Durée avant de reconsidérer la liste des matchs comme périmée et de
# la reconstruire (nouvelle requête API-Football). 3h : largement
# assez réactif pour des matchs qui ne changent pas d'heure en heure,
# sans solliciter le quota à chaque ouverture de l'écran.
TTL_RAFRAICHISSEMENT_MATCHS_SECONDES = 3 * 3600
