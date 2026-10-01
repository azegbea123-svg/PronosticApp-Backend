"""Constantes de configuration partagées, centralisées ici pour éviter les doublons."""

import os

# 💎 Config VIP
PRIX_VIP_FCFA = 500
DUREE_VIP_JOURS = 3
LIMITE_GRATUITE_QUOTIDIENNE = 3

# ⚠️ Mot de passe admin — sert aussi de "jeton universel" pour le debug
# (voir auth.py). À définir en variable d'environnement sur Render.
MOT_DE_PASSE_ADMIN = os.environ.get("ADMIN_PASSWORD", "change-moi")

# ⚠️ Clé API-Football (api-sports.io) — compte suspendu (sept. 2026),
# plus utilisée par matchs.py. Gardée ici au cas où le compte serait
# réactivé un jour ; voir CLE_FOOTBALL_DATA pour le fournisseur actuel.
CLE_API_FOOTBALL = os.environ.get("API_FOOTBALL_KEY", "")

# ⚠️ Clé football-data.org (v4) — utilisée EN PARALLÈLE de TheSportsDB
# (voir CLE_THESPORTSDB), les deux listes étant fusionnées dans
# matchs.py. Plan gratuit : ~12 grandes compétitions, 10 requêtes/minute.
CLE_FOOTBALL_DATA = os.environ.get("FOOTBALL_DATA_API_KEY", "")

# ⚠️ Clé TheSportsDB — utilisée EN PARALLÈLE de football-data.org (voir
# sources/thesportsdb.py et matchs.py), couverture plus large (~617
# championnats) mais fiabilité de données un peu plus variable (base
# contributive). "3" = clé de test gratuite partagée par la communauté ;
# définis THESPORTSDB_API_KEY sur Render pour une clé personnelle
# (patreon.com/thesportsdb) si besoin de plus de fiabilité.
CLE_THESPORTSDB = os.environ.get("THESPORTSDB_API_KEY", "3")

# ==== Liste de matchs J / J+1 ====
#
# /matchs fusionne les matchs de football-data.org ET de TheSportsDB
# (voir matchs.py — appel des deux en parallèle, dédoublonnage par date
# + équipes). La vérification BeSoccer (disponibilité des données) se
# fait EN AVANCE, en arrière-plan, progressivement — voir
# matchs.verifier_disponibilite_prochains et
# MAX_VERIFICATIONS_PAR_CYCLE dans matchs.py.

# Durée avant de reconsidérer la liste des matchs comme périmée et de
# la reconstruire (nouvelle requête API-Football). 3h : largement
# assez réactif pour des matchs qui ne changent pas d'heure en heure,
# sans solliciter le quota à chaque ouverture de l'écran.
TTL_RAFRAICHISSEMENT_MATCHS_SECONDES = 3 * 3600
