"""
Blueprint gallery : galerie paginée des cartes postales, avec filtre par
collection, recherche textuelle, et affichage recto / verso / recto+verso.
"""

from __future__ import annotations

from flask import Blueprint, current_app, render_template, request, url_for
from flask_babel import gettext

from flpostcards.images import SIZE_THUMB, card_images

bp = Blueprint("gallery", __name__, template_folder="../../templates")

# Nombre de cartes par page (valeur par défaut + choix proposés)
DEFAULT_PER_PAGE = 24
PER_PAGE_CHOICES = (12, 24, 48)

# Modes d'affichage disponibles
DISPLAY_MODES = ("recto_verso", "recto", "verso")
DEFAULT_DISPLAY_MODE = "recto_verso"


# Modes du filtre "doublons" : "" (par défaut) exclut les doublons et les
# cartes échangées ; "all" affiche toutes les cartes (avec doublons), sauf
# les échangées ; "trade" / "exchanged" affichent uniquement les cartes du
# statut correspondant (y compris les doublons, car chaque exemplaire
# physique compte pour l'échange).
DOUBLES_MODES = ("", "all", "trade", "exchanged")
DEFAULT_DOUBLES_MODE = ""

# Statut exclu par défaut de toutes les listes flpostcards (une carte
# échangée n'a plus vocation à être montrée, sauf filtre explicite).
DEFAULT_EXCLUDED_STATUS = "exchanged"


@bp.route("/gallery/")
def index():
    """Galerie paginée, filtrable par collection et recherche textuelle."""
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    search = (request.args.get("q") or "").strip()

    display = request.args.get("display") or DEFAULT_DISPLAY_MODE
    if display not in DISPLAY_MODES:
        display = DEFAULT_DISPLAY_MODE
    if not current_app.config.get("POSTCARDS_VERSO", True):
        # Verso désactivé globalement ([DEFAULT] postcards_verso = false) :
        # on ignore le paramètre "display" éventuellement passé dans l'URL
        # et on force un affichage recto seul.
        display = "recto"

    # Filtre doublons / statut (cf. DOUBLES_MODES ci-dessus)
    doubles_mode = request.args.get("doubles") or DEFAULT_DOUBLES_MODE
    if doubles_mode not in DOUBLES_MODES:
        doubles_mode = DEFAULT_DOUBLES_MODE
    show_doubles = doubles_mode == "all"

    try:
        per_page = int(request.args.get("per_page", DEFAULT_PER_PAGE))
    except ValueError:
        per_page = DEFAULT_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        per_page = DEFAULT_PER_PAGE

    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    if doubles_mode in ("trade", "exchanged"):
        # Filtre par statut explicite : on regarde chaque exemplaire
        # physique (y compris les doublons), pas seulement la carte
        # "principale" d'un groupe de doublons.
        count_cards = model.count_cards
        list_cards = model.list_cards
        status_kwargs = {"status": doubles_mode}
    elif show_doubles:
        count_cards = model.count_cards
        list_cards = model.list_cards
        status_kwargs = {"exclude_status": DEFAULT_EXCLUDED_STATUS}
    else:
        count_cards = model.count_unique_cards
        list_cards = model.list_unique_cards
        status_kwargs = {"exclude_status": DEFAULT_EXCLUDED_STATUS}

    total = count_cards(collection=collection or None, search=search or None, **status_kwargs)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    offset = (page - 1) * per_page
    cards = list_cards(
        collection=collection or None,
        search=search or None,
        limit=per_page,
        offset=offset,
        **status_kwargs,
    )

    items = []
    for card in cards:
        images = card_images(card["id"], SIZE_THUMB)
        items.append(
            {
                "id": card["id"],
                "title": card.get("title"),
                "title2": card.get("title2"),
                "recto": images["recto"],
                "verso": images["verso"],
            }
        )

    # Calcul des numéros de pages à afficher dans la pagination :
    # toujours page 1, les 5 pages autour de la page courante, et la
    # dernière page. Les « trous » sont représentés par None (ellipse).
    WINDOW = 2  # pages de chaque côté de la page courante
    shown: set[int] = {1, pages}
    for p in range(max(1, page - WINDOW), min(pages, page + WINDOW) + 1):
        shown.add(p)
    sorted_pages = sorted(shown)
    page_range: list[int | None] = []
    prev: int | None = None
    for p in sorted_pages:
        if prev is not None and p - prev > 1:
            page_range.append(None)  # ellipse
        page_range.append(p)
        prev = p

    if collection:
        page_title = gettext(
            "Toutes les cartes postales - %(collection)s", collection=collection
        )
    else:
        page_title = gettext("Toutes les cartes postales")

    return render_template(
        "gallery/index.html",
        page_title=page_title,
        items=items,
        collections=collections,
        current_collection=collection,
        search=search,
        display=display,
        display_modes=DISPLAY_MODES,
        show_doubles=show_doubles,
        doubles_mode=doubles_mode,
        per_page=per_page,
        per_page_choices=PER_PAGE_CHOICES,
        page=page,
        pages=pages,
        page_range=page_range,
        total=total,
        og_title=page_title,
        og_description=gettext(
            "Parcourez ma collection de cartes postales anciennes."
        ),
        og_type="website",
    )
