# PronosticApp — Backend

API FastAPI qui reçoit deux noms d'équipe + un type de match, va chercher
des stats récentes (forme, buts) via **Sofascore** et **BeSoccer**, et
renvoie un pronostic avec probabilités + facteurs clés + résumé.

Le contrat JSON de `/match/analyse` correspond exactement à
`MatchAnalysisRequest` / `MatchAnalysisResponse` côté Kotlin — aucun
changement à faire dans l'appli Android pour la structure des données.

## 1. Lancer en local

**Sous Windows (PowerShell)** — c'est ton cas :
```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Si `Activate.ps1` refuse de s'exécuter (erreur de politique d'exécution),
lance d'abord, une seule fois par session PowerShell :
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

**Sous macOS/Linux (bash/zsh)** :
```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Dans les deux cas, l'API tourne sur `http://127.0.0.1:8000`. Documentation
interactive auto-générée : `http://127.0.0.1:8000/docs` (utilisable
directement dans le navigateur, plus simple que `curl` sous Windows).

Tester rapidement en PowerShell :
```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/match/analyse -Method Post `
  -ContentType "application/json" `
  -Body '{"equipe1":"Real Madrid","equipe2":"Barcelone","typeMatch":"championnat"}'
```

Ou plus simple : ouvre `http://127.0.0.1:8000/docs` dans ton navigateur,
déplie `POST /match/analyse`, clique "Try it out", remplis les champs et
"Execute".

## 2. ⚠️ À faire avant la mise en prod : vérifier le scraping en conditions réelles

Cet environnement de génération de code n'a **pas d'accès réseau** vers
Sofascore/BeSoccer/Flashscore. Le code a été écrit avec le plus grand
soin mais **n'a pas pu être testé contre les vraies pages** de ces sites.
Avant de considérer que c'est fini :

1. Lance le serveur en local et teste `/match/analyse` avec de vraies
   équipes (`curl` ci-dessus, ou Postman).
2. Regarde les `facteursCles` retournés :
   - Si tu vois `"Aucune donnée récente trouvée pour ..."` → la source
     n'a rien trouvé, il faut déboguer.
3. **Sofascore** (`app/sources/sofascore.py`) : le plus susceptible de
   marcher tel quel car c'est une API JSON. Si ça ne renvoie rien,
   ajoute un `print(data)` temporaire après l'appel `_get()` pour voir
   la vraie forme de la réponse et ajuster les noms de champs.
4. **BeSoccer** (`app/sources/besoccer.py`) : scraping HTML classique.
   Les sélecteurs CSS (`select_one(...)`) sont une hypothèse — va sur
   `besoccer.com`, fais une recherche d'équipe, clique droit > Inspecter,
   et ajuste les sélecteurs dans le fichier pour qu'ils correspondent
   au vrai HTML.
5. **Flashscore** : volontairement non implémenté (voir le commentaire
   dans `app/sources/flashscore.py`) — protection anti-bot trop lourde
   pour une simple requête HTTP. L'appli fonctionne avec 2 sources sur 3
   sans problème ; si tu veux vraiment Flashscore, il faudra une
   architecture à base de navigateur headless (Playwright), plus lourde
   à héberger.

## 3. Déployer sur Render (recommandé, gratuit pour démarrer)

Le dépôt contient déjà tout ce qu'il faut : `render.yaml` (config de
déploiement), `Procfile` (commande de démarrage), `.python-version`
(fixe Python 3.12 en prod, pour éviter les soucis de compatibilité
rencontrés en local avec Python 3.14 trop récent).

**Étape 1 — Mettre le backend sur GitHub** (Render déploie depuis un
dépôt Git, pas depuis ton PC directement) :

```powershell
cd backend
git init
git add .
git commit -m "Backend PronosticApp"
```

Puis crée un nouveau dépôt vide sur github.com (ex. `PronosticApp-Backend`,
**sans** cocher "Add a README"), et lie-le :

```powershell
git remote add origin https://github.com/TON_USER/PronosticApp-Backend.git
git branch -M main
git push -u origin main
```

**Étape 2 — Créer le service sur Render** :
1. Va sur [render.com](https://render.com), crée un compte (gratuit,
   connexion possible directement avec GitHub)
2. Dashboard → **New** → **Blueprint**
3. Sélectionne ton dépôt `PronosticApp-Backend`
4. Render détecte automatiquement `render.yaml` et propose la config
   déjà prête → clique **Apply**
5. Le premier déploiement prend 2-3 minutes (installation des
   dépendances puis démarrage)

**Étape 3 — Récupérer l'URL** : une fois déployé, Render affiche une
URL du type `https://pronosticapp-backend.onrender.com`. Vérifie que ça
répond en ouvrant `https://pronosticapp-backend.onrender.com/` dans le
navigateur — tu dois voir `{"status":"ok","service":"PronosticApp API"}`.

⚠️ **Sur le plan gratuit**, le service s'endort après 15 minutes sans
trafic et met ~30-50 secondes à se "réveiller" au premier appel suivant
— c'est normal, pas un bug. Pour un usage réel avec plusieurs
utilisateurs, il faudra passer sur un plan payant pour éviter ce délai.

### Alternative : Railway

Même principe (dépôt GitHub → connexion sur [railway.app](https://railway.app)
→ Railway détecte le `Procfile` automatiquement), pas besoin de
`render.yaml` dans ce cas. Railway n'a pas de mise en veille sur son
plan gratuit, mais celui-ci est limité en heures d'usage par mois plutôt
qu'illimité comme Render.

## 4. Connecter l'appli Android au backend déployé

Dans `RetrofitClient.kt`, remplace la ligne :
```kotlin
private const val BASE_URL = "http://10.0.2.2:8000/"
```
par ta vraie URL Render/Railway (avec le `/` final) :
```kotlin
private const val BASE_URL = "https://pronosticapp-backend.onrender.com/"
```

Comme c'est maintenant du HTTPS, tu peux **supprimer** l'exception
ajoutée dans `network_security_config.xml` (elle ne servait que pour le
test HTTP local via l'émulateur) — ou la laisser, elle est inoffensive
puisqu'elle ne vise que `10.0.2.2`.

`USE_MOCK_DATA` reste à `false` — pas de changement nécessaire côté
`MainActivity.kt`.

Teste ensuite sur un **vrai téléphone** (plus seulement l'émulateur) :
maintenant que le backend a une URL publique, ça doit fonctionner
depuis n'importe quel réseau, pas seulement en local.

## Base de données : Firestore (Firebase)

⚠️ Le projet est passé de Postgres (Render) à **Firestore** — plus de
limite d'expiration à 90 jours, plan gratuit (Spark) largement
suffisant pour ce volume.

**Configuration requise** :
1. Un fichier de clé de service Firebase (`firebase-service-account.json`)
2. En local : place-le à la racine du projet (déjà dans `.gitignore`)
3. En production sur Render : Dashboard → ton service → **Environment**
   → section **Secret Files** → ajoute un fichier nommé
   `firebase-service-account.json` avec le contenu JSON collé dedans.
   Render le monte automatiquement à `/etc/secrets/...`, le code le
   trouve tout seul à cet emplacement en prod.

Sans ce fichier (aucun des deux emplacements), l'app démarre quand même
mais toutes les fonctionnalités liées à la base (historique, VIP, quota)
répondent avec une erreur claire plutôt que de planter.

**Collections Firestore** : `pronostics`, `utilisateurs`, `codes_vip`
(pas de schéma à définir à l'avance, Firestore est sans schéma).

## 5. Historique et fiabilité des pronostics

Chaque appel à `/match/analyse` enregistre automatiquement le pronostic
en base (silencieusement — si la base n'est pas configurée ou a un
souci, ça n'empêche jamais de recevoir la réponse).

**Nouveaux endpoints** :

- `GET /historique?limite=20` — liste les derniers pronostics enregistrés
- `PATCH /historique/{id}?resultat_reel=V1` — renseigne le résultat réel
  une fois le match joué (`V1`, `NUL` ou `V2`)
- `GET /historique/stats` — taux de réussite global une fois des
  pronostics vérifiés

⚠️ **Vérification automatique disponible** : `POST /taches/verifier-resultats`
regarde, pour chaque pronostic en attente, si le dernier match TERMINÉ de
l'équipe 1 était bien contre l'équipe 2 — si oui, enregistre le résultat
tout seul. Render (plan gratuit) n'ayant pas de tâche planifiée intégrée,
il faut un déclencheur externe gratuit pour l'appeler périodiquement :

1. Va sur [cron-job.org](https://cron-job.org) (gratuit, inscription simple)
2. Crée un nouveau cron job :
   - URL : `https://pronosticapp-backend.onrender.com/taches/verifier-resultats`
   - Méthode : `POST`
   - Fréquence : toutes les 3-4 heures suffit largement
3. C'est tout — chaque appel externe vérifie ce qui peut l'être, et
   laisse le reste en attente pour le passage suivant

Bonus : cet appel périodique maintient aussi le service éveillé sur le
plan gratuit de Render (qui s'endort sinon après 15 min sans trafic).

`PATCH /historique/{id}?resultat_reel=...` reste disponible en
complément pour corriger un cas raté manuellement.

## Limites connues

- Le modèle statistique (Poisson) reste simplifié — il ne prend pas en
  compte la force du championnat, les confrontations directes
  historiques, ni la météo/le contexte du match. Une base solide,
  perfectible.
- Si aucune des deux sources ne trouve l'équipe (faute de frappe, club
  amateur peu référencé...), le pronostic retombe sur un résultat neutre
  et le dit explicitement à l'utilisateur plutôt que d'inventer des
  chiffres.
- Le comptage des indisponibles (blessures/suspensions) est une
  heuristique textuelle sur BeSoccer, pas un sélecteur garanti à 100% —
  donne un ordre de grandeur fiable dans la plupart des cas.
- Pas de cache : chaque requête relance les recherches en direct. À
  ajouter (ex. Redis, ou simple cache mémoire avec TTL) si le volume
  d'utilisateurs augmente, pour ne pas se faire bloquer par les sites
  sources à cause d'un trafic trop élevé.
- La vérification des résultats réels est manuelle (voir section 5) —
  pas encore d'automatisation.


## Sources de matchs (liste J / J+1)

`/matchs` fusionne CINQ fournisseurs en parallèle (priorité croissante : SportAPI7, OpenLigaDB,
TheSportsDB, free-api-live-football-data, football-data.org). Un même match remonté sous des
orthographes différentes ("Man City" / "Manchester City FC") est fusionné (`app/normalisation.py`,
coups d'envoi à moins de 90 min, équipes ≥ 82 % similaires, marqueurs féminines/jeunes/réserves
identiques). Chaque match porte `sources` et `nb_sources`.

Chaque source est protégée par un disjoncteur (`app/sources/sante.py`) : 3 échecs de suite = pause
de 30 min. État visible via `GET /debug/sources` (admin).

⚠️ `GET /debug/sportapi7` teste le chemin de liste par date de SportAPI7, **non vérifié** (voir
`app/sources/sportapi7.py`). Tests : `python -m unittest discover -s tests -t .`


## Module esport (ajout)

Package `app/esport/` + routes `/esport/*` (branchées dans main.py par une seule ligne ; le football n'est pas modifié).
Source : Esport API (RapidAPI, hôte esportapi1.p.rapidapi.com), même variable `RAPIDAPI_KEY`.
Jeux couverts (3) : Counter Strike (1572), Dota 2 (1570), LoL (1571).

⚠️ **Plan gratuit observé : 50 requêtes/jour.** Conception autour de ce budget :
- **Cache persistant** (collection `esport_cache`) : un redémarrage de l'hébergeur ne re-dépense plus rien.
- Requêtes envoyées par lots de 2 (évite le 429 « trop de requêtes d'un coup », qui ne bloque qu'1 minute).
- Direct : cache 10 min, plafonné à 10 appels réels par jour. Listes : cache 3 h (aujourd'hui) / 8 h (demain).
- Réserve : les tâches de fond s'arrêtent à 15 requêtes restantes. Rattrapage de l'historique en deux régimes :
  AMORÇAGE (5 derniers jours × 3 jeux ≈ 15 requêtes, une seule fois) à 4 jours par actualisation tant qu'il reste plus de
  15 requêtes ; puis ENTRETIEN (jusqu'à 45 jours) avec l'excédent seulement (≥ 30 restantes). Pause de 10 min après un échec.

**Pronostics sans appel API.** Chaque liste du jour contient aussi les matchs terminés : on les mémorise
(`esport_resultats`), on rattrape peu à peu 45 jours d'historique avec le quota excédentaire, et un classement Elo
(avec ajustement à la force des adversaires) en tire le pronostic de CHAQUE match, affiché directement dans la liste
(dès 2 matchs connus par équipe, avec la mention « confiance faible » tant que l'échantillon est petit).
Le pronostic est figé avant le coup d'envoi ; `GET /esport/bilan` le confronte au résultat, sans aucun appel API.

- `GET /esport/jeux`, `GET /esport/matchs?jeu=csgo` (matchs + `pronostic` + `diagnostic` + `historique`)
- `GET /esport/match/{id}?avec=cartes,duel,votes` (détail, 1 à 4 appels API : à la demande seulement)
- `GET /esport/recherche?q=nom`, `GET /esport/bilan`
- Admin : `POST /esport/equipes {teamId}` (favoris), `DELETE /esport/equipes/{id}`, `POST /esport/equipes/importer-connues`

`diagnostic` détaille chaque requête de la liste (ok, cache, vide, échec, ignoré) : à consulter dans /docs si une liste est incomplète.
Nettoyage d'anciennes données de test : `python scripts/nettoyer_tests_esport.py` (simulation par défaut).
Tests : `python -m unittest discover -s tests -t .`
