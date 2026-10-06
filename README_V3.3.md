# PronosticApp Backend V3.3.2

## Nouveautés

- SportAPI7 devient une source de calendrier football intégrable dans `/matchs`.
- Flux SportAPI7 : catégories du jour puis `/category/{id}/scheduled-events/{date}` ; le calendrier reconnaît `dailyUniqueTournaments[].uniqueTournamentIds`.
- Fusion/déduplication conservée : le même match provenant de plusieurs sources ne compte qu’une seule fois.
- Routes admin :
  - `GET /debug/v3/sportapi7/fixtures/{jour_iso}`
  - `GET /debug/v3/sportapi7/event/{event_id}`
  - `GET /debug/v3/source/{source_id}/test`
  - `GET /debug/v3/sources/test-all`
  - `GET /debug/v3/sources/state`
  - `POST /debug/v3/source/{source_id}/activate`
  - `POST /debug/v3/source/{source_id}/deactivate`

## Activation

L’état d’activation reste en mémoire du processus Render. SportAPI7 n’est pas forcé en production simplement par la présence du connecteur : il doit être testé puis activé depuis les routes admin.

## Quota

Le test de toutes les sources consomme des requêtes RapidAPI. Préférer le test individuel. Le calendrier SportAPI7 est utilisé pour la liste des matchs et le détail d’un match est récupéré uniquement à la demande.

## Vérification locale

`python -m compileall -q app` doit terminer sans erreur.


## Correctif V3.3.2

Le calendrier réel observé renvoie `dailyUniqueTournaments[].uniqueTournamentIds`. Le diagnostic expose désormais les deux formats (`stageIds` et `uniqueTournamentIds`).

Nouvelle route admin :
- `GET /debug/v3/sportapi7/diagnostic/{jour_iso}`

Cette route compare les statuts HTTP et la structure de `categories`, `scheduled-events` et `calendar`, sans transformer un `uniqueTournamentId` en faux `eventId`.
