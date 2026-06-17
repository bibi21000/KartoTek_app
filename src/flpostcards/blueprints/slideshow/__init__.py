"""
Blueprint slideshow : diaporama affichant toutes les cartes de la
collection dans un ordre aléatoire, sans répétition avant qu'un tour
complet ne soit terminé (à la différence d'un tirage aléatoire pur à
chaque carte, qui peut répéter certaines cartes et en oublier d'autres).
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_babel import gettext

from flpostcards.images import SIZE_SMALL, card_images

bp = Blueprint("slideshow", __name__, template_folder="../../templates")


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

    return render_template(
        "slideshow/index.html",
        page_title=page_title,
        collections=collections,
        current_collection=collection,
        og_title=page_title,
        og_description=gettext(
            "Toutes mes cartes postales en diaporama."
        ),
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

    cards = model.list_unique_cards(collection=collection or None)

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
            }
        )

    return _no_cache(jsonify({"cards": items}))
