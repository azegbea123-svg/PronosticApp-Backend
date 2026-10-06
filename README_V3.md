# PronosticApp Backend — V3

## Moteur
V3 ajoute un ensemble probabiliste adaptatif :
- Dixon-Coles avec matrice de scores ;
- Bivariate Poisson ;
- Elo ;
- régression logistique multinomiale légère, sans dépendance ML lourde ;
- marché 1X2 si les cotes sont disponibles ;
- signal Football Prediction API comme source auxiliaire ;
- pondération adaptative selon le log-loss récent ;
- calibration par temperature scaling lorsque l'historique est suffisant ;
- moteur NO_BET basé sur probabilité dominante, marge, désaccord et qualité des données ;
- walk-forward chronologique sans entraînement sur le futur.

## Compatibilité
`POST /match/analyse` reste compatible avec le modèle Android existant. Le V3 remplace progressivement les probabilités 1X2 lorsque son exécution réussit ; en cas d'erreur, le moteur historique reste utilisé.

## Diagnostic
- `GET /debug/v3/status`
- `GET /debug/v3/weights`
- `POST /debug/v3/walk-forward`

Le walk-forward attend des snapshots disponibles AVANT chaque coup d'envoi. Utiliser les statistiques actuelles pour prédire un match historique introduirait une fuite temporelle et invaliderait le backtest.

## RapidAPI
La V3 ne crée ni ne remplace aucune clé RapidAPI. Elle réutilise la configuration existante du backend.

## V3.1 — Fusion multi-sources et anti-redondance

Cette version ajoute une couche de provenance/dédoublonnage dans `app/v3/source_fusion.py` et un catalogue de validation dans `app/v3/source_registry.py`.

### Principe
- identité canonique : date + équipe domicile normalisée + équipe extérieure normalisée ;
- une rencontre remontée par 2, 3 ou 5 fournisseurs reste **1 seul match** ;
- les champs manquants d'une source complètent ceux d'une autre ;
- en cas de conflit, une priorité de source est appliquée ;
- les sources utilisées sont conservées dans `sources` ;
- les réponses qui ne correspondent pas au sport/contrat attendu sont rejetées.

### Important — endpoints du fichier `endpoints 2.txt`
Les réponses fournies ont été inspectées avant activation. Plusieurs couples endpoint/réponse sont incohérents (ex. endpoint Tennis renvoyant du Football, endpoint transfers renvoyant du NFL, endpoint events renvoyant un catalogue de sports). Ils sont donc **en quarantaine** et ne sont pas appelés automatiquement. Cela protège le quota RapidAPI et empêche l'introduction de données erronées.

Le catalogue est visible via `GET /debug/v3/sources` (admin) et **ne fait aucun appel API**.

## V3.2 — Diagnostic et activation des sources RapidAPI

Les nouvelles sources fournies peuvent maintenant être testées depuis Swagger sans
modifier la clé `RAPIDAPI_KEY`.

Routes admin :
- `GET /debug/v3/source/{source_id}/test` : appel réel d'une source et extraction des matchs détectés.
- `GET /debug/v3/sources/test-all` : test séquentiel de toutes les nouvelles sources (consomme du quota RapidAPI).
- `GET /debug/v3/sources/state` : état test/activation sans appel externe.
- `POST /debug/v3/source/{source_id}/activate?force=false` : activation après validation.
- `POST /debug/v3/source/{source_id}/deactivate` : désactivation.

Identifiants : `sofascore`, `sportapi7`, `allsportsapi2`, `odds-feed`,
`all-sport-live-stream`, `flashlive`, `football-data1`, `soccer-data`,
`football-live-score2`.

Une source activée et déclarée `fixture_capable` peut alimenter `/matchs`. Les
matchs sont ensuite soumis à la déduplication existante. Les sources d'enrichissement
(team/form/discipline/odds) peuvent être activées pour diagnostic sans être
injectées comme fixtures.

L'activation est volontairement conservée en mémoire du processus Render : un
redémarrage ou redéploiement remet les activations à zéro. Cela évite d'utiliser
le disque éphémère Render comme pseudo-base de configuration.
