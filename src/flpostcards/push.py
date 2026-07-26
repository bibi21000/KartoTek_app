"""
flpostcards/push.py - Client push : relaie au master centralisé
(kartotek.eu) la détection d'une nouvelle carte, plutôt que d'envoyer
soi-même vers FCM/APNs.

Historique : la première version de ce module gérait tout localement
(registre de tokens + envoi FCM/APNs par serveur). Ça obligeait à
distribuer les mêmes credentials FCM/APNs sur chaque flpostcards. Cette
version-ci délègue le registre ET l'envoi au master (voir
kartotek_master.push / kartotek_master.push_api) : ce module ne fait
plus qu'un appel HTTP interne, authentifié par un secret partagé.

Ce que ce module NE fait PLUS (déplacé côté master, voir
docs/07-PUSH_NOTIFICATIONS.md) :
  - le registre des tokens (POST /api/v1/push/register|unregister côté
    flpostcards a été retiré — l'app mobile s'inscrit directement
    auprès du master, une seule fois pour tous les serveurs) ;
  - l'envoi FCM/APNs lui-même, et les dépendances associées
    (google-auth, httpx[http2], PyJWT[crypto]) : plus nécessaires ici.

Configuration (postcards.conf, section [push]) :

    [push]
    enabled = true
    master_url = https://kartotek.eu
    notify_secret = <même valeur que [push] notify_secret côté master>
    watch_interval_s = 120
    http_timeout_s = 10
"""

from __future__ import annotations

from flask import current_app


def notify_master(card: dict) -> dict:
    """
    Signale une carte nouvellement ajoutée au master, qui se charge du
    filtrage par rayon et de l'envoi FCM/APNs (kartotek_master.push).

    `card` doit contenir `id`, `coord` = (lat, lon), et idéalement
    `title` (utilisé comme corps de la notification).

    Ne lève jamais d'exception : une erreur réseau/master ne doit pas
    interrompre le job de détection (flpostcards.push_watch) — juste
    être loguée. Voir push_watch.check_and_notify pour la gestion du
    curseur (avance uniquement sur les cartes déjà traitées, retente les
    échecs au prochain tick naturellement).

    Retourne un dict {"ok": bool, ...} — {"ok": False} si push non
    configuré, si le master est injoignable, ou en cas d'erreur HTTP.
    """
    enabled = current_app.config.get("PUSH_ENABLED", False)
    master_url = current_app.config.get("PUSH_MASTER_URL")
    secret = current_app.config.get("PUSH_NOTIFY_SECRET")

    if not enabled or not master_url or not secret:
        current_app.logger.debug(
            "push: notification ignorée (push non configuré : enabled=%s, master_url défini=%s, secret défini=%s)",
            enabled, bool(master_url), bool(secret),
        )
        return {"ok": False, "reason": "not_configured"}

    coord = card.get("coord")
    if not coord or coord[0] is None or coord[1] is None:
        return {"ok": False, "reason": "no_coord"}

    import requests

    timeout = current_app.config.get("PUSH_HTTP_TIMEOUT_S", 10.0)
    url = master_url.rstrip("/") + "/api/v1/push/notify"
    payload = {
        "server_url": current_app.config.get("SERVER_PUBLIC_URL", ""),
        "card_id": str(card.get("id", "")),
        "title": card.get("title") or card.get("title2") or "",
        "lat": coord[0],
        "lon": coord[1],
    }
    headers = {"X-Kartotek-Push-Secret": secret}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        current_app.logger.warning("push: échec de l'appel au master (%s) : %s", url, exc)
        return {"ok": False, "reason": "network_error"}

    if resp.status_code == 200:
        result = resp.json()
        current_app.logger.info(
            "push: carte %s signalée au master, %d appareil(s) ciblé(s)",
            card.get("id"), result.get("targeted", 0),
        )
        return {"ok": True, **result}

    current_app.logger.warning(
        "push: le master a renvoyé %d pour la carte %s : %s",
        resp.status_code, card.get("id"), resp.text[:300],
    )
    return {"ok": False, "reason": f"http_{resp.status_code}"}
