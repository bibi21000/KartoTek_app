"""
flpostcards/data_cache.py - Cache pour les lectures répétées des trois
jeux de données "de référence" partagés par toutes les requêtes :
collections (collections.json), POIs (pois.json) et trajets
(travels.json).

Ces trois fichiers ne sont plus lus tels quels à chaque requête (voir
libpostcards.model.Model : ``get_collections()``, ``list_pois()`` et
``list_travels()`` interrogent en réalité les tables SQLite
``collections``/``pois``/``travels``, tenues à jour depuis ces JSON --
même principe que postcards.sqlite pour les cartes). Ce module ajoute
un étage de cache au-dessus de ces appels SQLite, pour épargner la
requête + désérialisation à chaque visite (``/map/pois.json``,
``/travel/``, ``/api/v1/collections``, page d'accueil, galerie,
diaporama, ``/api/v1/capabilities``, ... -- ce sont parmi les routes
les plus fréquemment appelées de l'application).

Durée de vie par défaut : 15 minutes (``[flask] json_cache_ttl_s``
dans postcards.conf, voir ``CACHE_DEFAULT_TIMEOUT`` dans
``flpostcards.load_config``). Backend Redis ou mémoire locale, voir
``flpostcards.extensions.cache``.

Invalidation : automatique à expiration du TTL, ou immédiate via
:func:`invalidate` (appelée par ``POST /api/v1/admin/restart``, voir
blueprints/api -- utile surtout avec un backend Redis partagé entre
plusieurs workers/process, qu'un simple redémarrage de processus ne
vide pas).
"""

from __future__ import annotations

from flask import current_app

from flpostcards.extensions import cache

_COLLECTIONS_KEY = "collections"
_POIS_KEY = "pois"
_TRAVELS_KEY = "travels"


def get_collections_cached() -> tuple[list[str], list[str]]:
    """Équivalent mis en cache de ``current_app.model.get_collections()``
    -- retourne ``(collections, collections_map)``."""
    value = cache.get(_COLLECTIONS_KEY)
    if value is None:
        value = current_app.model.get_collections()
        cache.set(_COLLECTIONS_KEY, value)
    return value


def list_pois_cached() -> list[dict]:
    """Équivalent mis en cache de ``current_app.model.list_pois()``."""
    value = cache.get(_POIS_KEY)
    if value is None:
        value = current_app.model.list_pois()
        cache.set(_POIS_KEY, value)
    return value


def list_travels_cached() -> list[dict]:
    """Équivalent mis en cache de ``current_app.model.list_travels()``."""
    value = cache.get(_TRAVELS_KEY)
    if value is None:
        value = current_app.model.list_travels()
        cache.set(_TRAVELS_KEY, value)
    return value


def get_travel_cached(travel_id: str) -> dict | None:
    """
    Équivalent mis en cache de ``current_app.model.read_travel(travel_id)``.

    Recherche dans :func:`list_travels_cached` plutôt que d'ouvrir un
    espace de cache séparé par id : le nombre de trajets reste modeste
    (parcours définis à la main dans travels.json), donc un balayage
    de la liste déjà en cache est largement suffisant, et évite
    d'avoir à invalider une clé par id.
    """
    travel_id = str(travel_id)
    for travel in list_travels_cached():
        if str(travel.get("id")) == travel_id:
            return travel
    return None


def invalidate() -> None:
    """Purge immédiatement les trois entrées (voir docstring du module)."""
    cache.delete(_COLLECTIONS_KEY)
    cache.delete(_POIS_KEY)
    cache.delete(_TRAVELS_KEY)
