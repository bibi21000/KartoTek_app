"""
flpostcards/indexnow.py - Soumission de nouvelles/modifiées URLs à
l'API IndexNow (https://www.bing.com/indexnow/getstarted), qui relaie
ensuite la notification à l'ensemble des moteurs de recherche
participants (Bing, Yandex, Seznam.cz, Naver, ...) au lieu d'attendre
leur prochain crawl naturel.

Fonctionnement résumé :
  - Un secret ("clé" IndexNow, [flask] indexnow_key dans
    postcards.conf) identifie ce site auprès de l'API. Il doit être
    exposé tel quel sur https://<host>/<indexnow_key>.txt : voir le
    cas particulier de flpostcards.blueprints.home.verification_file,
    qui sert ce fichier directement depuis la config, sans dépôt
    manuel.
  - submit_urls() ci-dessous fait l'appel HTTP réel vers l'API.
  - flpostcards/indexnow_watch.py détecte automatiquement les
    cartes/parcours nouveaux ou modifiés (même principe que
    push_watch.py) et appelle submit_urls() — mais rien n'empêche un
    autre appelant (ex : depuis un futur hook de publication) de
    l'appeler directement.

Sans [flask] indexnow_key défini, submit_urls() est un no-op
silencieux (retourne False) — l'app fonctionne normalement, IndexNow
est simplement désactivé.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import current_app

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"

# L'API limite chaque appel à 10 000 URLs ; on découpe nous-mêmes au cas
# où un run rattraperait un historique important (première activation
# sur un site déjà bien rempli, redémarrage après une longue coupure...).
_MAX_URLS_PER_CALL = 10000


def submit_urls(urls: list[str]) -> bool:
    """
    Soumet une liste d'URLs (absolues, http(s)://...) à l'API
    IndexNow, découpée en paquets de 10 000 si besoin.

    Ne lève jamais d'exception : une erreur réseau ou une réponse HTTP
    d'erreur ne doit jamais faire échouer l'appelant (job de
    surveillance périodique, appel ponctuel...) — juste être loguée.
    Retourne True si tous les paquets ont été acceptés (HTTP 200/202),
    False si IndexNow n'est pas configuré ([flask] indexnow_key
    absent), s'il n'y a aucune URL, ou si un paquet a échoué.

    Doit être appelée dans un contexte applicatif Flask (current_app).
    """
    key = current_app.config.get("INDEXNOW_KEY")
    if not key:
        current_app.logger.debug(
            "indexnow: soumission ignorée ([flask] indexnow_key non défini)"
        )
        return False

    # Dédoublonne en conservant l'ordre, retire les entrées vides.
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return False

    host = urlparse(urls[0]).netloc
    key_location = f"https://{host}/{key}.txt"
    timeout = current_app.config.get("INDEXNOW_HTTP_TIMEOUT_S", 10.0)

    import requests

    all_ok = True
    for i in range(0, len(urls), _MAX_URLS_PER_CALL):
        batch = urls[i : i + _MAX_URLS_PER_CALL]
        payload = {
            "host": host,
            "key": key,
            "keyLocation": key_location,
            "urlList": batch,
        }
        try:
            response = requests.post(INDEXNOW_ENDPOINT, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            all_ok = False
            current_app.logger.warning(
                "indexnow: échec réseau lors de la soumission de %d URL(s) : %s",
                len(batch), exc,
            )
            continue

        if response.status_code in (200, 202):
            current_app.logger.info(
                "indexnow: %d URL(s) soumise(s) (HTTP %s)",
                len(batch), response.status_code,
            )
        else:
            all_ok = False
            current_app.logger.warning(
                "indexnow: soumission refusée/erreur (HTTP %s) pour %d URL(s) : %s",
                response.status_code, len(batch), response.text[:200],
            )

    return all_ok


def submit_url(url: str) -> bool:
    """Raccourci pour soumettre une seule URL, voir submit_urls()."""
    return submit_urls([url])
