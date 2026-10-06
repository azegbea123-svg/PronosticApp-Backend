# PronosticApp Backend V3.3.3 — SportAPI7 Daily Sync + Cache

Cette version ajoute une synchronisation quotidienne SportAPI7 optimisee pour le quota.

## Strategie
- 1 appel calendrier pour identifier les tournois actifs du jour.
- 1 appel categories pour obtenir les categories football et leur nombre de matchs.
- Selection intelligente de categories avec un plafond de 35 appels.
- Chaque endpoint `/category/{id}/scheduled-events/{date}` peut retourner plusieurs matchs.
- Deduplication par `event_id`, puis date/equipes.
- Aucun appel `/event/{id}` pendant la synchronisation.
- Le detail d'un match reste disponible a la demande.

## Quota
Le plafond de 35 categories laisse une marge apres les 2 appels de pilotage et avant la limite observee de 50 requetes.

## Diagnostic
`GET /debug/v3/sportapi7/sync/{jour_iso}` synchronise et enregistre la journee dans Firestore `matchs_jour`.

`GET /matchs?jour=today` continue d'utiliser le cache existant de PronosticApp.
