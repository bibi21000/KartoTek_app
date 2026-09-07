"""
Blueprint slideshow : diaporama affichant toutes les cartes de la
collection dans un ordre aléatoire, sans répétition avant qu'un tour
complet ne soit terminé (à la différence d'un tirage aléatoire pur à
chaque carte, qui peut répéter certaines cartes et en oublier d'autres).
"""

from __future__ import annotations

import random

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from flask_babel import gettext

from flpostcards.extensions import cache
from flpostcards.images import SIZE_SMALL, card_images, image_dimensions

bp = Blueprint("slideshow", __name__, template_folder="../../templates")

# Durée du cache (par collection) du choix de la carte utilisée comme
# image Open Graph de /slideshow/ -- voir _pick_og_image.
_OG_IMAGE_CACHE_TTL = 30 * 60


def _og_image_cache_key(collection: str) -> str:
    return f"slideshow_og_image:{collection}"


def _pick_og_image(model, collection: str) -> dict | None:
    """Choisit une carte au hasard dans la collection (ou dans toute la
    collection si ``collection`` est vide) pour servir d'image Open
    Graph à /slideshow/, et met ce choix en cache 30 minutes -- par
    collection, puisque le tirage doit rester cohérent avec le filtre
    ``?collection=`` -- pour éviter de refaire la recherche en base à
    chaque requête."""
    cache_key = _og_image_cache_key(collection)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    total = model.count_unique_cards(collection=collection or None, exclude_status="exchanged")
    if not total:
        return None

    offset = random.randint(0, total - 1)
    featured = model.list_unique_cards(
        collection=collection or None, limit=1, offset=offset, exclude_status="exchanged"
    )
    if not featured:
        return None

    featured_recto = card_images(featured[0]["id"])["recto"]
    og_image_url = url_for("home.images", filename=featured_recto, _external=True)
    dims = image_dimensions(current_app.config["DATADIR"], featured_recto)
    og_image_width, og_image_height = dims if dims else (None, None)

    value = {
        "url": og_image_url,
        "width": og_image_width,
        "height": og_image_height,
    }
    cache.set(cache_key, value, timeout=_OG_IMAGE_CACHE_TTL)
    return value


def invalidate() -> None:
    """Purge le choix d'image Open Graph mis en cache pour toutes les
    collections (dont "" = toutes les cartes). Voir data_cache.invalidate."""
    collections = current_app.config.get("COLLECTIONS", [])
    for collection in ["", *collections]:
        cache.delete(_og_image_cache_key(collection))


def _no_cache(response, status: int | None = None):
    """
    Ajoute les en-têtes empêchant la mise en cache (navigateur, proxy
    nginx). Le mélange de l'ordre se fait côté client à partir de la
    liste complète, mais cette liste elle-même doit toujours refléter
    l'état courant de la collection (nouvelles cartes, suppressions).
    """
    if status is not None:
        response.status_code = status
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/slideshow/")
def index():
    """Page diaporama : toutes les cartes, ordre aléatoire sans répétition."""
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    if collection:
        page_title = gettext(
            "Diaporama - %(collection)s", collection=collection
        )
    else:
        page_title = gettext("Diaporama")

    og_image = _pick_og_image(model, collection)

    return render_template(
        "slideshow/index.html",
        page_title=page_title,
        collections=collections,
        current_collection=collection,
        og_title=page_title,
        og_description=gettext(
            "Toutes mes cartes postales en diaporama."
        ),
        og_image=og_image["url"] if og_image else None,
        og_image_width=og_image["width"] if og_image else None,
        og_image_height=og_image["height"] if og_image else None,
        og_type="website",
    )


@bp.route("/api/slideshow-cards")
def api_slideshow_cards():
    """
    Retourne la liste complète des cartes uniques (sans doublons) de la
    collection (ou de la collection filtrée), pour alimenter le
    diaporama. Le mélange et le parcours sans répétition sont effectués
    côté client (JS), à partir de cette liste complète.
    """
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    cards = model.list_unique_cards(collection=collection or None, exclude_status="exchanged")

    items = []
    for card in cards:
        images = card_images(card["id"])
        images_small = card_images(card["id"], SIZE_SMALL)
        items.append(
            {
                "id": card["id"],
                "title": card.get("title"),
                "title2": card.get("title2"),
                "recto": images["recto"],
                "verso": images["verso"],
                "verso_small": images_small["verso"],
                "cdate": card.get("cdate"),
                "detected_content": card.get("detected_content"),
            }
        )

    return _no_cache(jsonify({"cards": items}))
