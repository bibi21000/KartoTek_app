"""
Blueprint home : page d'accueil avec recto/verso aléatoires en diaporama
et fiche détaillée d'une carte postale.
"""

from __future__ import annotations

import random
from html import escape
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from flpostcards.images import (
    SIZE_SMALL,
    SIZE_THUMB,
    ALLOWED_SIZE_DIRS,
    card_images,
    image_dimensions,
)
from flpostcards.icon_generator import find_uploaded_icon, get_or_generate_icon

bp = Blueprint("home", __name__, template_folder="../../templates")


def _no_cache(response, status: int | None = None):
    """
    Ajoute les en-têtes empêchant la mise en cache (navigateur, proxy nginx).

    Utilisé pour /api/random-card, qui doit toujours renvoyer une carte
    différente et ne doit donc jamais être servi depuis un cache.
    """
    if status is not None:
        response.status_code = status
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/")
def index():
    """Page d'accueil : diaporama recto/verso aléatoire, filtrable par collection."""
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    from flask_babel import gettext

    if collection:
        page_title = gettext(
            "Ma collection de cartes postales - %(collection)s",
            collection=collection,
        )
    else:
        page_title = gettext("Ma collection de cartes postales")

    # Carte "vedette" pour l'image Open Graph (statique, cohérente avec le filtre)
    og_image_url = None
    og_image_width = None
    og_image_height = None
    total = model.count_unique_cards(collection=collection or None, exclude_status="exchanged")
    if total:
        offset = random.randint(0, total - 1)
        featured = model.list_unique_cards(
            collection=collection or None, limit=1, offset=offset, exclude_status="exchanged"
        )
        if featured:
            featured_recto = card_images(featured[0]["id"])["recto"]
            og_image_url = url_for(
                "home.images",
                filename=featured_recto,
                _external=True,
            )
            dims = image_dimensions(current_app.config["DATADIR"], featured_recto)
            if dims:
                og_image_width, og_image_height = dims

    return render_template(
        "home/index.html",
        page_title=page_title,
        collections=collections,
        current_collection=collection,
        og_title=page_title,
        og_description=gettext(
            "Découvrez ma collection de cartes postales anciennes."
        ),
        og_image=og_image_url,
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="website",
    )


@bp.route("/images/<path:filename>")
def images(filename: str):
    """
    Sert les images PNG depuis datadir (size_div3, size_div10, size_div20).

    N'autorise que les sous-répertoires size_divX, conformément aux
    contraintes du projet (les images de cards/ ne sont pas exposées).

    Cache HTTP : ``Cache-Control: public, max-age=<IMAGE_CACHE_MAX_AGE_S>,
    immutable`` (30 jours par défaut, voir postcards.conf [flask]
    image_cache_max_age_s), en plus de l'ETag/Last-Modified déjà posés
    par Werkzeug (``send_from_directory`` est "conditional" par défaut :
    une requête avec ``If-None-Match``/``If-Modified-Since`` reçoit un
    304 sans re-télécharger l'image). Sans ce réglage, le client mobile
    retéléchargeait chaque image à chaque affichage de la galerie/vue
    "ici", faute de toute instruction de cache -- coûteux en données
    mobiles et en bande passante serveur pour un contenu qui ne change
    pour ainsi dire jamais une fois publié.

    Si une carte est malgré tout corrigée/republiée (nouvel export
    KartoTek App), le fichier change de date de modification : l'ETag/
    Last-Modified suivent, donc un client qui revalide (cache expiré,
    ou ``Cache-Control`` local à max-age=0) verra bien la nouvelle
    version -- seule une lecture strictement depuis le cache local
    (dans la fenêtre des `image_cache_max_age_s` secondes) resterait sur
    l'ancienne image le temps que ce cache expire.
    """
    allowed_dirs = ALLOWED_SIZE_DIRS
    parts = filename.split("/", 1)
    if len(parts) != 2 or parts[0] not in allowed_dirs:
        abort(404)

    datadir = current_app.config["DATADIR"]
    max_age = current_app.config.get("IMAGE_CACHE_MAX_AGE_S", 0)
    response = send_from_directory(datadir, filename, max_age=max_age or None)
    if max_age:
        # send_from_directory pose déjà un Cache-Control avec max-age via
        # le paramètre max_age, mais sans "public"/"immutable" -- on les
        # ajoute nous-mêmes plutôt que de dépendre du comportement par
        # défaut de Werkzeug (qui a varié selon les versions).
        response.headers["Cache-Control"] = f"public, max-age={max_age}, immutable"
    return response


@bp.route("/favicon.ico")
@bp.route("/icon.png")
def icon():
    """
    Sert l'icône du site : static/icon.(png|jpg|jpeg) si présent,
    sinon un logo généré à partir du paramètre de config [flask] icon.
    """
    from flask import send_file

    # Un thème actif (voir flpostcards/theming.py) peut fournir son
    # propre static/icon.(png|jpg|jpeg) ; sinon on retombe sur celui
    # du cœur, comme sans thème.
    theme_static_dir = current_app.config.get("THEME_STATIC_DIR")
    if theme_static_dir is not None and find_uploaded_icon(theme_static_dir) is not None:
        static_dir = theme_static_dir
    else:
        static_dir = Path(current_app.static_folder)
    icon_config = current_app.config.get("ICON")

    icon_path = get_or_generate_icon(
        current_app.config["CACHEDIR"], static_dir, icon_config
    )
    if icon_path is None:
        abort(404)

    return send_file(icon_path, mimetype="image/png", max_age=86400)


@bp.route("/api/recent-cards")
def api_recent_cards():
    """
    Retourne la liste des cartes "récentes" à présenter dans le
    diaporama de la page d'accueil : celles ajoutées dans la fenêtre
    de RECENT_DAYS jours (cdate), ou à défaut les RECENT_FALLBACK_COUNT
    derniers ajouts si la fenêtre est vide.

    Le mélange et le parcours sans répétition sont effectués côté
    client (JS), à partir de cette liste complète : c'est ce qui
    permet de garantir que toutes les cartes sont vues avant qu'aucune
    ne soit répétée (un tirage aléatoire à chaque appel ne le garantit
    pas, certaines cartes pouvant alors apparaître plusieurs fois
    pendant que d'autres n'apparaissent jamais).
    """
    model = current_app.model

    collections = current_app.config.get("COLLECTIONS", [])
    collection = request.args.get("collection") or ""
    if collection not in collections:
        collection = ""

    days = current_app.config.get("RECENT_DAYS", 30)
    fallback_count = current_app.config.get("RECENT_FALLBACK_COUNT", 20)

    cards = model.list_recent_unique_cards(
        days=days, fallback_count=fallback_count, collection=collection or None,
        exclude_status="exchanged",
    )

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


@bp.route("/robots.txt")
def robots():
    """robots.txt minimal, pointant vers le sitemap pour faciliter sa découverte."""
    from flask import Response

    lines = [
        "User-agent: *",
        "Allow: /",
        f"Sitemap: {url_for('home.sitemap', _external=True)}",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@bp.route("/<string:filename>")
def verification_file(filename: str):
    """
    Sert les fichiers de vérification de propriété de site déposés dans
    htmldir/verification/ (Google Search Console, Yandex via .html/.txt ;
    Bing via BingSiteAuth.xml). Seuls les fichiers .html, .txt et .xml sont
    autorisés, et uniquement depuis ce répertoire dédié — aucun autre
    fichier du serveur n'est exposé.
    Usage : déposer le fichier fourni par le moteur de recherche dans
    htmldir/verification/ puis accéder à /<nom-du-fichier>.

    Cas particulier : la clé IndexNow (https://www.bing.com/indexnow/getstarted,
    [flask] indexnow_key dans postcards.conf) doit être servie telle
    quelle sur /<clé>.txt pour que l'API puisse vérifier le contrôle du
    site avant d'accepter des soumissions (voir flpostcards.indexnow) —
    générée directement depuis la config, sans dépôt manuel de fichier.
    """
    indexnow_key = current_app.config.get("INDEXNOW_KEY")
    if indexnow_key and filename == f"{indexnow_key}.txt":
        from flask import Response

        return Response(indexnow_key, mimetype="text/plain")

    if not filename.endswith((".html", ".txt", ".xml")):
        abort(404)
    verification_dir = Path(current_app.config["DATADIR"]) / "verification"
    if not verification_dir.exists():
        abort(404)
    file_path = verification_dir / filename
    if not file_path.exists():
        abort(404)
    if filename.endswith(".html"):
        mimetype = "text/html"
    elif filename.endswith(".xml"):
        mimetype = "application/xml"
    else:
        mimetype = "text/plain"
    return send_from_directory(verification_dir, filename, mimetype=mimetype)


@bp.route("/sitemap.xml")
def sitemap():
    """
    Sitemap XML listant les pages principales, toutes les fiches cartes
    (avec lastmod basé sur mdate) et les parcours.

    Les pages de "listing" (accueil, diaporama, galerie, index des
    parcours, carte) n'ont pas de date de mise à jour qui leur est
    propre : on leur affecte la date de modification la plus récente
    parmi toutes les cartes, ce qui donne à Google une info utile pour
    prioriser le re-crawl plutôt que d'omettre <lastmod>.
    """
    from flask import Response

    model = current_app.model

    urls = []

    def add(loc: str, lastmod: int | None = None, changefreq: str | None = None,
            image_loc: str | None = None, image_caption: str | None = None):
        urls.append({
            "loc": loc, "lastmod": lastmod, "changefreq": changefreq,
            "image_loc": image_loc, "image_caption": image_caption,
        })

    all_cards = model.list_unique_cards(exclude_status="exchanged")

    card_mdates = [c.get("mdate") for c in all_cards if c.get("mdate")]
    last_card_update = max(card_mdates) if card_mdates else None

    add(url_for("home.index", _external=True), lastmod=last_card_update, changefreq="daily")
    add(url_for("slideshow.index", _external=True), lastmod=last_card_update, changefreq="daily")
    add(url_for("gallery.index", _external=True), lastmod=last_card_update, changefreq="weekly")
    add(url_for("travel.index", _external=True), lastmod=last_card_update, changefreq="weekly")
    add(url_for("map.index", _external=True), lastmod=last_card_update, changefreq="weekly")

    for card in all_cards:
        # Extension "image sitemap" (voir
        # https://developers.google.com/search/docs/crawling-indexing/sitemaps/image-sitemaps) :
        # le site étant essentiellement composé de scans de cartes
        # postales, indiquer explicitement l'image de chaque fiche aide
        # Google Images à découvrir et indexer le fonds, plutôt que de
        # compter uniquement sur le crawl HTML classique. On ne référence
        # que le recto (og:image), pas le verso.
        recto_path = card_images(card["id"])["recto"]
        image_loc = url_for("home.images", filename=recto_path, _external=True)
        image_caption = (
            card.get("title") or card.get("detected_content") or None
        )
        add(
            url_for("home.card_detail", card_id=card["id"], _external=True),
            lastmod=card.get("mdate"),
            changefreq="monthly",
            image_loc=image_loc,
            image_caption=image_caption,
        )

    from flpostcards import data_cache

    for travel in data_cache.list_travels_cached():
        add(
            url_for("travel.detail", travel_id=travel["id"], _external=True),
            lastmod=travel.get("mdate") or last_card_update,
            changefreq="monthly",
        )

    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml_parts.append(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
    )
    for entry in urls:
        xml_parts.append("  <url>")
        xml_parts.append(f"    <loc>{entry['loc']}</loc>")
        if entry["lastmod"]:
            from datetime import datetime, timezone

            lastmod_str = datetime.fromtimestamp(
                entry["lastmod"], tz=timezone.utc
            ).strftime("%Y-%m-%d")
            xml_parts.append(f"    <lastmod>{lastmod_str}</lastmod>")
        if entry["changefreq"]:
            xml_parts.append(f"    <changefreq>{entry['changefreq']}</changefreq>")
        if entry["image_loc"]:
            xml_parts.append("    <image:image>")
            xml_parts.append(f"      <image:loc>{escape(entry['image_loc'])}</image:loc>")
            if entry["image_caption"]:
                xml_parts.append(
                    f"      <image:caption>{escape(entry['image_caption'])}</image:caption>"
                )
            xml_parts.append("    </image:image>")
        xml_parts.append("  </url>")
    xml_parts.append("</urlset>")

    return Response("\n".join(xml_parts), mimetype="application/xml")


@bp.route("/card/<card_id>")
def card_detail(card_id: str):
    """Fiche détaillée d'une carte postale (recto/verso, métadonnées)."""
    model = current_app.model
    card = model.get_card(card_id)
    if card is None:
        abort(404)

    from flask_babel import gettext

    images = card_images(card["id"])
    images_small = card_images(card["id"], SIZE_SMALL)

    card_title = card.get("title") or gettext("Carte #%(id)s", id=card["id"])
    og_description = (
        card.get("description")
        or card.get("title2")
        # Contenu détecté automatiquement (BLIP, voir tkpostcards.libs.detection)
        # : à défaut de description/titre secondaire renseignés à la main,
        # donne un texte unique et pertinent par carte plutôt que de
        # retomber sur le titre générique "Carte #123".
        or card.get("detected_content")
        or card_title
    )

    # Lien de retour contextuel (ex: vers la galerie, page/filtres conservés).
    # On n'accepte que des chemins locaux (commençant par '/' et pas '//'
    # pour éviter toute redirection ouverte vers un autre domaine).
    back_url = request.args.get("back") or ""
    if not back_url.startswith("/") or back_url.startswith("//"):
        back_url = ""

    back_label = None
    if back_url.startswith("/gallery/"):
        back_label = gettext("Retour à la galerie")
    elif back_url.startswith("/travel/"):
        back_label = gettext("Retour à la balade")
    elif back_url.startswith("/map/") or back_url.startswith("/map?"):
        back_label = gettext("Retour à la carte")
    elif back_url.startswith("/slideshow/") or back_url.startswith("/slideshow?"):
        back_label = gettext("Retour au diaporama")

    dims = image_dimensions(current_app.config["DATADIR"], images["recto"])
    og_image_width, og_image_height = dims if dims else (None, None)

    # Texte alternatif du recto : on concatène, une par ligne, les
    # informations disponibles (titre, titre secondaire, description,
    # contenu détecté automatiquement par BLIP - voir
    # tkpostcards.libs.detection), sans libellé ni mention "Recto de la
    # carte x", pour un alt à la fois concis, descriptif, utile pour
    # l'accessibilité et le référencement (recherche d'images).
    recto_alt_parts = [
        part for part in (
            card.get("title"),
            card.get("title2"),
            card.get("description"),
            card.get("detected_content"),
        ) if part
    ]
    recto_alt = "\n".join(recto_alt_parts)

    # URL canonique sans le paramètre ?back= (état de navigation interne,
    # pas une variation de contenu) : évite tout signal de contenu dupliqué
    # entre les différentes façons d'arriver sur cette fiche.
    canonical_url = url_for("home.card_detail", card_id=card["id"], _external=True)

    # Données structurées schema.org (JSON-LD), pour les rich results
    # Google (aperçu image, fil d'Ariane) - voir
    # https://developers.google.com/search/docs/appearance/structured-data/image-license-metadata
    # et .../breadcrumb.
    from datetime import datetime, timezone

    def _iso_date(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None

    structured_data = {
        "@context": "https://schema.org",
        "@type": "ImageObject",
        "contentUrl": url_for("home.images", filename=images["recto"], _external=True),
        "url": canonical_url,
        "name": card_title,
        "description": og_description,
    }
    if og_image_width and og_image_height:
        structured_data["width"] = og_image_width
        structured_data["height"] = og_image_height
    upload_date = _iso_date(card.get("cdate"))
    if upload_date:
        structured_data["uploadDate"] = upload_date
    modified_date = _iso_date(card.get("mdate"))
    if modified_date:
        structured_data["dateModified"] = modified_date

    breadcrumb_data = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem", "position": 1,
                "name": gettext("Accueil"),
                "item": url_for("home.index", _external=True),
            },
            {
                "@type": "ListItem", "position": 2,
                "name": card_title,
                "item": canonical_url,
            },
        ],
    }

    # Section "Points d'intérêt" (bas de fiche) : pour chaque POI
    # référencé par la carte (card["poi"], liste d'ids), son nom et les
    # autres cartes uniques (hors doublons/échangées) qui référencent ce
    # même POI -- la carte courante elle-même est exclue de cette liste,
    # puisqu'elle est déjà affichée en haut de la page. Un POI sans autre
    # carte associée n'est pas affiché : la section sert à naviguer vers
    # d'autres cartes du même lieu, pas à lister les POIs pour eux-mêmes.
    points_of_interest = []
    for poi_id in card.get("poi") or []:
        poi = model.get_poi(poi_id)
        if poi is None:
            continue

        poi_name = poi.get("description") or gettext(
            "Point d'intérêt #%(id)s", id=poi["id"]
        )

        poi_cards = model.list_unique_cards(poi=poi_id, exclude_status="exchanged")
        related = []
        for poi_card in poi_cards:
            if poi_card["id"] == card["id"]:
                continue
            poi_card_images = card_images(poi_card["id"], SIZE_THUMB)
            related.append(
                {
                    "id": poi_card["id"],
                    "title": poi_card.get("title"),
                    "title2": poi_card.get("title2"),
                    "recto": poi_card_images["recto"],
                }
            )

        if not related:
            continue

        points_of_interest.append({"id": poi["id"], "name": poi_name, "cards": related})

    return render_template(
        "card/detail.html",
        card=card,
        images=images,
        images_small=images_small,
        back_url=back_url,
        back_label=back_label,
        recto_alt=recto_alt,
        canonical_url=canonical_url,
        points_of_interest=points_of_interest,
        og_title=card_title,
        og_description=og_description,
        og_image=url_for("home.images", filename=images["recto"], _external=True),
        og_image_width=og_image_width,
        og_image_height=og_image_height,
        og_type="article",
        structured_data=structured_data,
        breadcrumb_data=breadcrumb_data,
    )
