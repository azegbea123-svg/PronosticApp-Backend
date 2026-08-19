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

## Limites connues de cette v1

- Le pronostic est une **heuristique simple** (forme récente + petit
  bonus domicile), pas un modèle statistique avancé — volontairement,
  pour rester explicable dans les "facteurs clés". À affiner plus tard
  si besoin (ex. pondération par force du championnat, confrontations
  directes réelles, blessures).
- Si aucune des deux sources ne trouve l'équipe (faute de frappe, club
  amateur peu référencé...), le pronostic retombe sur un résultat neutre
  et le dit explicitement à l'utilisateur plutôt que d'inventer des
  chiffres.
- Pas de cache : chaque requête relance les recherches en direct. À
  ajouter (ex. Redis, ou simple cache mémoire avec TTL) si le volume
  d'utilisateurs augmente, pour ne pas se faire bloquer par les sites
  sources à cause d'un trafic trop élevé.
