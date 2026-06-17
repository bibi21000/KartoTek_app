"""
Blueprint map : carte OpenStreetMap affichant les cartes postales
géolocalisées (table cards), filtrable par collection.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from flask_babel import gettext

from flpostcards.images import SIZE_SMALL, card_images, image_dimensions
from flpostcards.osm_static_map import get_or_generate_map_image

bp = Blueprint("map", __name__, template_folder="../../templates")


@bp.route("/map/")
def index():
    """Page carte : affiche toutes les cartes géolocalisées d'une collection."""
    collections = current_app.config.get("COLLECTIONS_MAP", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    if collection:
        page_title = gettext("Carte - %(collection)s", collection=collection)
    else:
        page_title = gettext("Carte")

    # Image og:image : vue statique OpenStreetMap générée depuis le
    # paramètre de configuration [flask] osm_map (zoom/lat/lon).
    og_image_url = None
    og_image_width = None
    og_image_height = None
    osm_map = current_app.config.get("OSM_MAP")
    if osm_map:
        image_path = get_or_generate_map_image(
            current_app.config["DATADIR"], osm_map
        )
        if image_path is not None:
            og_image_url = url_for("map.og_map_image", _external=True)
            dims = image_dimensions(image_path.parent, image_path.name)
            if dims:
                og_image_width, og_image_height = dims

    return render_template(
        "map/index.html",
        page_title=page_title,
        collections=collections,
        current_collection=collection,
        og_title=page_title,
        og_description=gettext(
            "Localisez ma collection de cartes postales sur la carte."
        ),
        og_image=og_image_url,
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="website",
    )


@bp.route("/map/og-image.png")
def og_map_image():
    """Sert l'image OSM statique générée pour og:image (mise en cache sur disque)."""
    from flask import abort, send_file

    osm_map = current_app.config.get("OSM_MAP")
    if not osm_map:
        abort(404)

    image_path = get_or_generate_map_image(current_app.config["DATADIR"], osm_map)
    if image_path is None:
        abort(404)

    return send_file(image_path, mimetype="image/png", max_age=86400)


@bp.route("/map/cards.json")
def cards_json():
    """
    Cartes géolocalisées (sans doublons), filtrées par collection.

    Retourne id, titre, coordonnées et chemin de la vignette recto
    (utilisée pour l'aperçu au survol du marqueur).
    """
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS_MAP", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    cards = model.list_unique_cards(collection=collection or None)

    items = []
    for card in cards:
        coord = card.get("coord")
        if not coord or coord[0] is None or coord[1] is None:
            continue

        images = card_images(card["id"], SIZE_SMALL)
        items.append(
            {
                "id": card["id"],
                "title": card.get("title"),
                "coord": coord,
                "recto": images["recto"],
            }
        )

    return jsonify({"cards": items})
