"""
Source : Flashscore — NON implémentée, volontairement.

Flashscore charge la quasi-totalité de son contenu via JavaScript et est
protégé par une détection anti-bot (Incapsula). De simples requêtes HTTP
(httpx/requests), même avec des en-têtes imitant un navigateur, sont
quasi systématiquement bloquées ou reçoivent une page vide/CAPTCHA.

Pour scraper Flashscore de façon réellement fiable, il faudrait :
  - un navigateur headless piloté (Playwright ou Selenium),
  - un vrai profil de navigateur (fingerprint, cookies, délais "humains"),
  - potentiellement des proxies rotatifs si le volume de requêtes grandit,
  - un entretien régulier, car la protection anti-bot évolue.

C'est une architecture sensiblement plus lourde à déployer (le backend
ne serait plus un simple conteneur Python léger) et plus fragile dans le
temps. Plutôt que de fournir un scraper qui ferait illusion en local mais
casserait silencieusement en prod, cette source renvoie `None` : l'appli
continue de fonctionner avec Sofascore + BeSoccer.

Si tu veux vraiment l'ajouter, dis-le et on part sur une implémentation
Playwright dédiée (avec son propre coût d'hébergement/maintenance).
"""

from typing import Optional, Dict, Any


async def get_team_stats(nom_equipe: str) -> Optional[Dict[str, Any]]:
    return None
