"""
Blueprint travel : parcours personnalisés (table travels), affichés sous
forme de diaporama suivant l'ordre des cartes du trajet.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, jsonify, render_template, url_for
from flask_babel import gettext

from flpostcards.images import SIZE_MAIN, SIZE_SMALL, card_images, image_dimensions

bp = Blueprint("travel", __name__, template_folder="../../templates")


@bp.route("/travel/")
def index():
    """Liste des parcours disponibles."""
    model = current_app.model
    travels = model.list_travels()

    page_title = gettext("Balades dans le temps au fil des cartes postales")

    return render_template(
        "travel/index.html",
        page_title=page_title,
        travels=travels,
        og_title=page_title,
        og_description=gettext(
            "Suivez mes parcours à travers ma collection de cartes postales."
        ),
        og_type="website",
    )


@bp.route("/travel/<travel_id>")
def detail(travel_id: str):
    """Diaporama d'un parcours, suivant l'ordre des cartes du trajet."""
    model = current_app.model
    travel = model.read_travel(travel_id)
    if travel is None:
        abort(404)

    cards = travel.get("cards") or []
    if not cards:
        abort(404)

    page_title = travel.get("title") or gettext(
        "Parcours #%(id)s", id=travel["id"]
    )

    # Image vedette (première carte du parcours) pour Open Graph
    featured_recto = card_images(cards[0]["id"])["recto"]
    og_image_url = url_for(
        "home.images",
        filename=featured_recto,
        _external=True,
    )
    dims = image_dimensions(current_app.config["DATADIR"], featured_recto)
    og_image_width, og_image_height = dims if dims else (None, None)

    return render_template(
        "travel/detail.html",
        page_title=page_title,
        travel=travel,
        cards=cards,
        og_title=page_title,
        og_description=travel.get("title2")
        or gettext(
            "Suivez mes parcours à travers ma collection de cartes postales."
        ),
        og_image=og_image_url,
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="website",
    )


@bp.route("/travel/<travel_id>/cards.json")
def cards_json(travel_id: str):
    """
    Données JSON des cartes du parcours (id, titre, images recto/verso),
    utilisées par le diaporama JS.
    """
    model = current_app.model
    travel = model.read_travel(travel_id)
    if travel is None:
        abort(404)

    items = []
    for entry in travel.get("cards") or []:
        card = model.get_card(entry["id"])
        title = (card.get("title") if card else None) or entry.get("title")
        coord = (card.get("coord") if card else None) or None

        images = card_images(entry["id"], SIZE_MAIN)
        images_small = card_images(entry["id"], SIZE_SMALL)
        items.append(
            {
                "id": entry["id"],
                "title": title,
                "recto": images["recto"],
                "verso": images["verso"],
                "verso_small": images_small["verso"],
                "coord": coord,
            }
        )

    return jsonify({"id": travel["id"], "title": travel.get("title"), "cards": items})
