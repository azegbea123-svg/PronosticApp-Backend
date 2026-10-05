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
