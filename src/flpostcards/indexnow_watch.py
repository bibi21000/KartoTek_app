"""
flpostcards/indexnow_watch.py - Job de surveillance : détecte les
cartes et parcours nouveaux ou modifiés depuis le dernier passage, et
soumet leurs URLs à l'API IndexNow (flpostcards.indexnow.submit_urls,
voir https://www.bing.com/indexnow/getstarted) pour une réindexation
plus rapide côté Bing/Yandex/Seznam.cz/Naver, sans attendre leur
prochain crawl.

IMPORTANT — même principe que push_watch.py (voir son docstring pour
le détail) : à lancer comme process indépendant, PAS comme thread
démarré dans create_app(), pour éviter des soumissions en double si
gunicorn tourne avec plusieurs workers.

  # en continu (recommandé) :
  python -m flpostcards.indexnow_watch --config /path/postcards.conf

  # ou un seul passage, pour un déclenchement par cron :
  python -m flpostcards.indexnow_watch --config /path/postcards.conf --once

Sans [flask] indexnow_key configuré, chaque passage est un no-op
silencieux (voir indexnow.submit_urls) — inutile de désactiver ce job
autrement qu'en ne définissant pas la clé.

État persisté : datadir/indexnow_watch_state.json,
{"last_mdate": <int>} — date de modification (mdate, ou cdate à
défaut) la plus récente déjà soumise avec succès. À chaque tick, les
cartes et parcours dont mdate/cdate dépasse ce seuil sont soumis ;
l'état n'avance que si la soumission a réussi, pour retenter
naturellement au tick suivant en cas d'échec réseau/API.

Contrairement à push_watch (qui ne s'intéresse qu'aux cartes
nouvelles, via cdate et une fenêtre glissante récente), ce job doit
aussi capter les modifications d'une carte déjà ancienne (correction
d'un titre, ajout d'une coordonnée GPS, ...) : on part donc de la
liste complète des cartes/parcours (même source que le sitemap, voir
blueprints/home/__init__.py:sitemap()) plutôt que d'une fenêtre de
jours récents, et on filtre nous-mêmes sur mdate > last_mdate.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from flpostcards import create_app
from flpostcards.jsonlock import LockedJsonFile, acquire_lock, release_lock, read_json

logger = logging.getLogger("flpostcards.indexnow_watch")


def _state_path(app) -> Path:
    return Path(app.config["DATADIR"]) / "indexnow_watch_state.json"


def _lock_kwargs(app) -> dict:
    return {
        "lock_suffix": app.config.get("LOCK_SUFFIX", ".lck"),
        "timeout": app.config.get("LOCK_TIMEOUT", 60.0),
        "poll_interval": app.config.get("LOCK_POLL_INTERVAL", 2.0),
    }


def check_and_submit(app) -> int:
    """
    Un seul passage : cherche les cartes/parcours modifiés depuis le
    dernier ``last_mdate`` connu, soumet leurs URLs (+ les pages de
    listing principales, une fois, s'il y a du nouveau) à IndexNow, et
    avance l'état si la soumission a réussi. Retourne le nombre d'URLs
    soumises (0 si indexnow_key n'est pas configuré, ou si rien n'a
    changé).
    """
    if not app.config.get("INDEXNOW_KEY"):
        return 0

    # Ce job tourne hors d'une vraie requête HTTP (pas de Host: fourni
    # par un client/reverse proxy comme pour sitemap()) : il faut donc
    # un point de référence explicite pour que url_for(_external=True)
    # génère les bonnes URLs absolues -- [flask] public_url, déjà
    # utilisé pour SERVER_PUBLIC_URL ailleurs (voir load_config()).
    # Sans lui, mieux vaut ne rien soumettre que de soumettre à
    # IndexNow des URLs pointant vers le mauvais host (ex: localhost).
    base_url = app.config.get("SERVER_PUBLIC_URL") or None
    if not base_url:
        logger.warning(
            "indexnow_watch: [flask] public_url n'est pas défini -- "
            "impossible de générer des URLs absolues fiables, ce cycle "
            "est ignoré. Voir postcards.conf [flask] public_url."
        )
        return 0

    from flask import url_for

    from flpostcards import data_cache
    from flpostcards.indexnow import submit_urls

    with app.app_context(), app.test_request_context(base_url=base_url):
        state = read_json(_state_path(app), default={"last_mdate": 0})
        last_mdate = int(state.get("last_mdate", 0))

        all_cards = app.model.list_unique_cards(exclude_status="exchanged")
        changed_cards = sorted(
            (c for c in all_cards if int(c.get("mdate") or c.get("cdate") or 0) > last_mdate),
            key=lambda c: c.get("mdate") or c.get("cdate") or 0,
        )

        changed_travels = sorted(
            (
                t for t in data_cache.list_travels_cached()
                if int(t.get("mdate") or t.get("cdate") or 0) > last_mdate
            ),
            key=lambda t: t.get("mdate") or t.get("cdate") or 0,
        )

        if not changed_cards and not changed_travels:
            return 0

        max_mdate_seen = last_mdate
        urls = []
        for card in changed_cards:
            max_mdate_seen = max(max_mdate_seen, int(card.get("mdate") or card.get("cdate") or 0))
            urls.append(url_for("home.card_detail", card_id=card["id"], _external=True))
        for travel in changed_travels:
            max_mdate_seen = max(max_mdate_seen, int(travel.get("mdate") or travel.get("cdate") or 0))
            urls.append(url_for("travel.detail", travel_id=travel["id"], _external=True))

        # Les pages de listing (accueil, diaporama, galerie) changent à
        # chaque carte nouvelle/modifiée : les inclure aussi aide les
        # moteurs à re-crawler leur contenu vite, même principe que
        # l'entrée <lastmod> qui leur est déjà affectée dans le sitemap.
        urls.extend([
            url_for("home.index", _external=True),
            url_for("slideshow.index", _external=True),
            url_for("gallery.index", _external=True),
        ])

        ok = submit_urls(urls)
        logger.info(
            "indexnow_watch: %d carte(s)/parcours modifié(s), %d URL(s) soumise(s) (%s)",
            len(changed_cards) + len(changed_travels), len(urls),
            "ok" if ok else "échec, réessayera au prochain tick",
        )

        if ok and max_mdate_seen != last_mdate:
            with LockedJsonFile(_state_path(app), default={"last_mdate": 0}, **_lock_kwargs(app)) as f:
                f.data["last_mdate"] = max(int(f.data.get("last_mdate", 0)), max_mdate_seen)

        return len(urls) if ok else 0


def run_forever(app, interval_s: float) -> None:
    logger.info("indexnow_watch: démarrage, intervalle=%ss", interval_s)
    while True:
        try:
            check_and_submit(app)
        except Exception:
            logger.exception("indexnow_watch: erreur pendant un cycle, on continue au prochain tick")
        time.sleep(interval_s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Job de soumission IndexNow (flpostcards)")
    parser.add_argument("--config", default="postcards.conf", help="Chemin vers postcards.conf")
    parser.add_argument(
        "--once", action="store_true",
        help="Un seul passage puis quitte (pour un déclenchement externe par cron), "
             "protégé par un lockfile contre les exécutions concurrentes.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    app = create_app(args.config)

    if not app.config.get("INDEXNOW_KEY"):
        logger.warning(
            "indexnow_watch: [flask] indexnow_key n'est pas défini dans %s -- "
            "ce job n'aura rien à faire tant qu'une clé n'est pas configurée.",
            args.config,
        )

    interval_s = app.config.get("INDEXNOW_WATCH_INTERVAL_S", 300)

    if args.once:
        lock_path = _state_path(app).with_name("indexnow_watch.lck")
        if not acquire_lock(lock_path, timeout=5.0, poll_interval=1.0):
            logger.warning("indexnow_watch: une autre exécution est déjà en cours (lock présent), on quitte.")
            return
        try:
            check_and_submit(app)
        finally:
            release_lock(lock_path)
    else:
        run_forever(app, interval_s)


if __name__ == "__main__":
    main()
