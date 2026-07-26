"""
flpostcards/push_watch.py - Job de surveillance : détecte les cartes
nouvellement ajoutées à la base et signale chacune au master centralisé
(flpostcards.push.notify_master(), qui appelle kartotek_master's
POST /api/v1/push/notify — c'est le master qui filtre par rayon et
envoie via FCM/APNs, voir docs/07-PUSH_NOTIFICATIONS.md).

IMPORTANT — pourquoi un process séparé et pas un thread démarré dans
create_app() (contrairement au Poller de kartotek_master) :

  gunicorn lance en général PLUSIEURS workers (processus). Un thread de
  fond démarré dans create_app() serait démarré autant de fois qu'il y a
  de workers -> chaque carte ajoutée serait notifiée en double, triple,
  etc. selon le nombre de workers. Le poller de kartotek_master a le
  même risque théorique ; il n'est simplement pas gênant pour LUI car
  relancer un GET vers des serveurs distants plusieurs fois est sans
  conséquence visible, alors qu'ici ça enverrait plusieurs notifications
  push au même utilisateur pour la même carte.

  Ce module est donc pensé pour tourner comme un **process indépendant,
  unique**, lancé à côté de gunicorn (systemd timer/service, cron, ou
  simple supervisord) :

    # en continu (recommandé) :
    python -m flpostcards.push_watch --config /path/postcards.conf

    # ou un seul passage, pour un déclenchement par cron :
    python -m flpostcards.push_watch --config /path/postcards.conf --once

En mode --once, un lockfile (push_watch.lck) empêche deux exécutions
cron de se chevaucher si un run précédent est encore en cours (ex :
FCM/APNs lents à répondre).

État persisté : datadir/push_watch_state.json, {"last_cdate": <int>} —
horodatage (cdate) de la dernière carte déjà traitée. À chaque tick, on
récupère toutes les cartes dont cdate > last_cdate (fenêtre large,
volontairement redondante avec le tick précédent en cas de redémarrage)
et on notifie celles qui ont des coordonnées GPS.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from flpostcards import create_app
from flpostcards.jsonlock import LockedJsonFile, acquire_lock, release_lock, read_json

logger = logging.getLogger("flpostcards.push_watch")

# Fenêtre de recherche des cartes "récentes" (jours) à chaque tick : large
# par rapport à l'intervalle réel entre deux ticks (quelques minutes),
# pour absorber un redémarrage du job sans rien manquer -- le filtre
# précis reste le cdate > last_cdate ci-dessous, pas cette fenêtre.
_SCAN_WINDOW_DAYS = 3


def _state_path(app) -> Path:
    return Path(app.config["DATADIR"]) / "push_watch_state.json"


def _lock_kwargs(app) -> dict:
    return {
        "lock_suffix": app.config.get("LOCK_SUFFIX", ".lck"),
        "timeout": app.config.get("LOCK_TIMEOUT", 60.0),
        "poll_interval": app.config.get("LOCK_POLL_INTERVAL", 2.0),
    }


def check_and_notify(app) -> int:
    """
    Un seul passage : cherche les cartes nouvelles depuis le dernier
    ``last_cdate`` connu, les notifie, met à jour l'état. Retourne le
    nombre de cartes notifiées.
    """
    from flpostcards.push import notify_master

    with app.app_context():
        state = read_json(_state_path(app), default={"last_cdate": 0})
        last_cdate = int(state.get("last_cdate", 0))

        candidates = app.model.list_recent_unique_cards(
            days=_SCAN_WINDOW_DAYS, fallback_count=0, collection=None
        )
        new_cards = sorted(
            (c for c in candidates if int(c.get("cdate") or 0) > last_cdate),
            key=lambda c: c.get("cdate") or 0,
        )

        notified = 0
        max_cdate_seen = last_cdate
        for card in new_cards:
            max_cdate_seen = max(max_cdate_seen, int(card.get("cdate") or 0))
            coord = card.get("coord")
            if not coord or coord[0] is None or coord[1] is None:
                continue
            try:
                result = notify_master(card)
                if result.get("ok") and result.get("targeted"):
                    notified += 1
            except Exception:
                # Une erreur d'envoi sur une carte ne doit pas bloquer les
                # suivantes ni empêcher la progression de l'état pour les
                # cartes déjà traitées avec succès.
                logger.exception("push_watch: échec de notification pour la carte %s", card.get("id"))

        if max_cdate_seen != last_cdate:
            with LockedJsonFile(_state_path(app), default={"last_cdate": 0}, **_lock_kwargs(app)) as f:
                f.data["last_cdate"] = max(int(f.data.get("last_cdate", 0)), max_cdate_seen)

        if new_cards:
            logger.info(
                "push_watch: %d carte(s) neuve(s) examinée(s), %d notification(s) envoyée(s)",
                len(new_cards), notified,
            )
        return notified


def run_forever(app, interval_s: float) -> None:
    logger.info("push_watch: démarrage, intervalle=%ss", interval_s)
    while True:
        try:
            check_and_notify(app)
        except Exception:
            logger.exception("push_watch: erreur pendant un cycle, on continue au prochain tick")
        time.sleep(interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Job de notification push KartoTek (flpostcards)")
    parser.add_argument("--config", default="postcards.conf", help="Chemin vers postcards.conf")
    parser.add_argument(
        "--once", action="store_true",
        help="Un seul passage puis quitte (pour un déclenchement externe par cron), "
             "protégé par un lockfile contre les exécutions concurrentes.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    app = create_app(args.config)
    interval_s = app.config.get("PUSH_WATCH_INTERVAL_S", 120)

    if args.once:
        lock_path = _state_path(app).with_name("push_watch.lck")
        if not acquire_lock(lock_path, timeout=5.0, poll_interval=1.0):
            logger.warning("push_watch: une autre exécution est déjà en cours (lock présent), on quitte.")
            return
        try:
            check_and_notify(app)
        finally:
            release_lock(lock_path)
    else:
        run_forever(app, interval_s)


if __name__ == "__main__":
    main()
