"""
Blueprint API v1 : endpoints JSON pour une application mobile de
localisation de cartes postales.

Routes :
  GET  /api/v1/dbid          → hash du fichier postcards.sqlite (détection de changement)
  GET  /api/v1/gps           → coordonnées GPS paginées (sans doublons, curseur after_id)
  GET  /api/v1/bounds        → zone GPS couverte par les cartes (rectangle)
  GET  /api/v1/nearby        → cartes dans un rayon autour d'une position
  GET  /api/v1/next-update   → délai recommandé avant le prochain poll
  POST /api/v1/update        → enregistre un repérage de carte sur le terrain (auth JWT requise)
  POST /api/v1/similar       → recherche de cartes similaires à une photo (auth JWT requise)
  GET  /api/v1/collections   → liste des collections (avec nombre de cartes)
  GET  /api/v1/news          → dernières cartes ajoutées (comme la page d'accueil), filtrable par collection
  GET  /api/v1/slideshow     → toutes les cartes pour un diaporama, filtrable par collection
  GET  /api/v1/gallery       → galerie paginée (collection, recherche texte, doublons)
  POST /api/v1/check_auth    → vérifie un couple email/password (table auths), sans émettre de token
  POST /api/v1/auth/login       → {email, password} -> {access_token, refresh_token}
  POST /api/v1/auth/refresh     → {refresh_token} -> {access_token, refresh_token} (rotation)
  POST /api/v1/auth/logout      → {refresh_token} -> révoque ce token
  POST /api/v1/auth/logout-all  → (auth JWT requise) révoque tous les refresh tokens de l'utilisateur

Authentification (endpoints protégés : /api/v1/similar, /api/v1/update,
/api/v1/auth/logout-all) :
  JWT access token, envoyé dans l'en-tête ``Authorization: Bearer <token>``,
  obtenu via ``POST /api/v1/auth/login`` puis renouvelé via
  ``POST /api/v1/auth/refresh`` (voir flpostcards/auth.py — access token
  courte durée stateless, refresh token longue durée stocké haché en
  base et révocable). Les comptes sont créés avec
  ``model.write_auth(email, password)`` (table ``auths``, mots de passe
  hashés PBKDF2-SHA256) ; ``/api/v1/check_auth`` reste disponible pour
  une simple vérification ponctuelle sans émettre de token (ex :
  validation d'un formulaire de saisie du mot de passe).
"""

from __future__ import annotations

import os
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify, request, url_for

from flpostcards.auth import issue_token_pair, require_auth
from flpostcards.images import SIZE_MAIN, SIZE_SMALL, SIZE_THUMB, card_images
from flpostcards.blueprints.gallery import DEFAULT_PER_PAGE, PER_PAGE_CHOICES

from flpostcards.images import SIZE_MAIN, SIZE_SMALL, SIZE_THUMB, card_images
from flpostcards.blueprints.gallery import DEFAULT_PER_PAGE, PER_PAGE_CHOICES

bp = Blueprint("api_v1", __name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Rayon de la Terre en mètres (WGS-84 approx.)
_EARTH_R = 6_371_000.0

# Délai minimum entre deux polls (secondes), même en mouvement rapide
_POLL_MIN_S = 10
# Délai maximum entre deux polls quand aucune carte n'est proche
_POLL_MAX_S = 300


# ---------------------------------------------------------------------------
# Helpers géographiques
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance en mètres entre deux points (formule de Haversine)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def _cards_with_coord(model) -> list[dict]:
    """Retourne toutes les cartes uniques ayant des coordonnées GPS."""
    return [
        c for c in model.list_unique_cards()
        if c.get("coord") and c["coord"][0] is not None and c["coord"][1] is not None
    ]


def _get_searcher():
    """
    Retourne (et met en cache sur ``current_app``) un
    ``libpostcards.similar.PostcardSearcher`` avec son index
    (``datadir/postcards.pkl``) chargé.

    Le modèle CLIP n'est jamais chargé ici (voir
    ``PostcardSearcher._ensure_model``) : seule la comparaison de hashs
    perceptuels (``search_hashes``) est utilisée, ce qui garde
    flpostcards léger.

    Comme pour ``Model._get_conn`` (libpostcards/model.py), l'index est
    rechargé automatiquement si le fichier ``postcards.pkl`` a été
    remplacé depuis le dernier chargement (mtime différente) — utile
    après une republication de l'index sans redémarrer gunicorn.
    """
    pkl_path = Path(current_app.config["DATADIR"]) / "postcards.pkl"
    try:
        mtime = pkl_path.stat().st_mtime
    except OSError:
        mtime = None

    cached = getattr(current_app, "_similar_searcher", None)
    cached_mtime = getattr(current_app, "_similar_searcher_mtime", None)

    if cached is None or cached_mtime != mtime:
        from libpostcards.similar import PostcardSearcher

        searcher = PostcardSearcher(datadir=current_app.config["DATADIR"])
        if mtime is not None:
            searcher.load_index(pkl_path)
        current_app._similar_searcher = searcher
        current_app._similar_searcher_mtime = mtime

    return current_app._similar_searcher


def _no_cache(response):
    """
    Ajoute les en-têtes empêchant la mise en cache (navigateur, proxy) —
    utilisé pour /api/v1/news et /api/v1/slideshow, dont le contenu doit
    toujours refléter l'état courant de la collection (mêmes en-têtes
    que les endpoints équivalents côté web, voir home/slideshow).
    """
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _validated_collection(raw: str | None) -> str:
    """Retourne ``raw`` si c'est une collection connue, sinon ``""``."""
    collections = current_app.config.get("COLLECTIONS", [])
    collection = (raw or "").strip()
    return collection if collection in collections else ""


def _image_uri(card_id: str, size_dir: str, side: str) -> str:
    """URL absolue (via home.images) du recto/verso d'une carte, pour un répertoire de taille donné."""
    filename = card_images(card_id, size_dir)[side]
    return url_for("home.images", filename=filename, _external=True)


def _card_summary(card: dict) -> dict:
    """
    Représentation JSON d'une carte pour /api/v1/news et
    /api/v1/slideshow : mêmes champs que les endpoints équivalents
    côté web (/api/recent-cards, /api/slideshow-cards), mais avec des
    URLs absolues (recto/verso/verso_small) puisque destinées à un
    client mobile plutôt qu'au JS de la même origine.
    """
    cid = card["id"]
    return {
        "id": cid,
        "title": card.get("title"),
        "title2": card.get("title2"),
        "cdate": card.get("cdate"),
        "recto": _image_uri(cid, SIZE_MAIN, "recto"),
        "verso": _image_uri(cid, SIZE_MAIN, "verso"),
        "verso_small": _image_uri(cid, SIZE_SMALL, "verso"),
    }


def _card_thumb(card: dict) -> dict:
    """
    Représentation JSON d'une carte pour /api/v1/gallery : vignettes
    (size_div10, comme la galerie web) plutôt que les images pleine
    taille utilisées par _card_summary.
    """
    cid = card["id"]
    return {
        "id": cid,
        "title": card.get("title"),
        "title2": card.get("title2"),
        "recto": _image_uri(cid, SIZE_THUMB, "recto"),
        "verso": _image_uri(cid, SIZE_THUMB, "verso"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/api/v1/dbid")
def dbid():
    """
    Retourne un hash SHA1 (12 premiers caractères) du fichier
    postcards.sqlite (et de son fichier WAL s'il existe), permettant à
    un client de détecter si la base a changé depuis son dernier appel
    (nouvelles cartes, coordonnées GPS mises à jour, etc.) sans
    télécharger ni interroger la base entière.

    Le fichier -wal est inclus dans le hash car SQLite en mode WAL
    n'écrit pas immédiatement dans le fichier principal.

    Réponse JSON : { "hash": "abc123def456", "mtime": 1234567890 }
    """
    db_path = Path(current_app.config["DATADIR"]) / "postcards.sqlite"
    if not db_path.exists():
        return jsonify({"error": "database not found"}), 404

    h = hashlib.sha1()
    candidates = [db_path, Path(str(db_path) + "-wal")]
    latest_mtime = 0

    for path in candidates:
        if not path.exists():
            continue
        stat = path.stat()
        latest_mtime = max(latest_mtime, int(stat.st_mtime))
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)

    return jsonify({"hash": h.hexdigest()[:12], "mtime": latest_mtime})


@bp.route("/api/v1/gps")
def gps():
    """
    Liste paginée des coordonnées GPS des cartes uniques (sans doublons)
    ayant une position renseignée, triées par id numérique croissant.

    Paramètres de requête (optionnels) :
      after_id : id numérique de la dernière carte reçue à la page
                 précédente (défaut 0 pour démarrer). Pagination par
                 curseur : stable même si la base change pendant le
                 parcours des pages (ajout/màj/suppression de cartes).
      offset   : nombre de résultats à retourner (défaut 500, max 2000)

      start    : (déprécié) index OFFSET dans la liste. Conservé pour
                 compatibilité ascendante uniquement si `after_id`
                 n'est pas fourni. À éviter : une pagination OFFSET
                 peut renvoyer des cartes en double ou en sauter si la
                 base est modifiée entre deux appels (tri non figé
                 entre requêtes distinctes). Migrer vers `after_id`.

    Réponse JSON :
      {
        "count": 42,             -- nombre de résultats dans cette page
        "total": 150,            -- nombre total de cartes GPS sans doublons
        "next_after_id": "187",  -- à repasser en after_id pour la page suivante (null si fin)
        "cards": [
          { "id": "1", "lat": 46.749, "lon": 5.620 },
          ...
        ]
      }

    Utilisation typique (recommandée) : appeler avec after_id=0, puis
    reprendre avec after_id=next_after_id jusqu'à ce que
    next_after_id soit null (ou count < offset).
    """
    try:
        limit = min(int(request.args.get("offset", 500)), 2000)
    except ValueError:
        return jsonify({"error": "offset doit être un entier"}), 400

    model = current_app.model

    if "start" in request.args and "after_id" not in request.args:
        # Ancien mode OFFSET/LIMIT — conservé pour compatibilité
        # ascendante, mais déconseillé : instable si la base change
        # pendant la pagination (doublons / cartes manquantes).
        try:
            start = max(int(request.args.get("start", 0)), 0)
        except ValueError:
            return jsonify({"error": "start doit être un entier"}), 400
        page_cards = [
            c for c in model.list_unique_cards(limit=limit, offset=start)
            if c.get("coord") and c["coord"][0] is not None and c["coord"][1] is not None
        ]
    else:
        try:
            after_id = max(int(request.args.get("after_id", 0)), 0)
        except ValueError:
            return jsonify({"error": "after_id doit être un entier"}), 400
        page_cards = model.list_unique_cards_with_coord(
            after_id=after_id, limit=limit
        )

    cards = [
        {"id": c["id"], "lat": c["coord"][0], "lon": c["coord"][1]}
        for c in page_cards
    ]

    # Total calculé avec exactement le même filtre (unique + GPS) que
    # la pagination, pour que "total" corresponde à ce qui peut
    # effectivement être renvoyé (auparavant ce total comptait aussi
    # les cartes marquées doublons, qui ne sortent jamais des pages).
    total = model.count_unique_cards_with_coord()

    next_after_id = None
    if len(cards) == limit:
        try:
            next_after_id = str(max(int(c["id"]) for c in cards))
        except ValueError:
            next_after_id = cards[-1]["id"]

    return jsonify({
        "count": len(cards),
        "total": total,
        "next_after_id": next_after_id,
        "cards": cards,
    })


@bp.route("/api/v1/bounds")
def bounds():
    """
    Zone GPS couverte par l'ensemble des cartes postales géolocalisées.

    Retourne le rectangle englobant (bounding box) sous la forme :
      { "min_lat", "max_lat", "min_lon", "max_lon", "count" }
    """
    model = current_app.model
    conn = model._get_conn()

    row = conn.execute(
        "SELECT COUNT(*), MIN(coord_lat), MAX(coord_lat), "
        "       MIN(coord_lon), MAX(coord_lon) "
        "FROM cards WHERE coord_lat IS NOT NULL AND coord_lon IS NOT NULL"
    ).fetchone()

    count = row[0] if row else 0
    if not count:
        return jsonify({"count": 0, "bounds": None})

    return jsonify({
        "count": count,
        "bounds": {
            "min_lat": row[1],
            "max_lat": row[2],
            "min_lon": row[3],
            "max_lon": row[4],
        },
    })


@bp.route("/api/v1/nearby")
def nearby():
    """
    Cartes postales dans un rayon autour d'une position GPS.

    Paramètres de requête (tous obligatoires) :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float, max 50 000)

    Retourne la liste des cartes triées par distance croissante, avec
    pour chaque carte : id, title, coord, distance_m, recto (size_div10).
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon et radius sont obligatoires (float)"}), 400

    model = current_app.model
    cards = _cards_with_coord(model)

    results = []
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            results.append({
                "id": card["id"],
                "title": card.get("title"),
                "coord": card["coord"],
                "distance_m": round(dist, 1),
                "recto": f"size_div10/{card['id']}_R.png",
            })

    results.sort(key=lambda x: x["distance_m"])

    return jsonify({"count": len(results), "cards": results})


@bp.route("/api/v1/next-update")
def next_update():
    """
    Délai recommandé (en secondes) avant le prochain appel à /api/v1/nearby.

    Paramètres de requête :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float)
      speed  : vitesse de déplacement en m/s (float, 0 = immobile)
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
        speed = max(float(request.args.get("speed", 0)), 0.0)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon, radius (et optionnellement speed) sont obligatoires"}), 400

    if speed <= 0:
        return jsonify({"next_update_s": _POLL_MAX_S, "reason": "immobile"})

    model = current_app.model
    cards = _cards_with_coord(model)

    min_dist_in_radius: float | None = None
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            if min_dist_in_radius is None or dist < min_dist_in_radius:
                min_dist_in_radius = dist

    effective_distance = min_dist_in_radius if min_dist_in_radius is not None else radius
    remaining = max(radius - effective_distance, 0)
    delay = max(_POLL_MIN_S, min(remaining / speed, _POLL_MAX_S))

    return jsonify({
        "next_update_s": round(delay, 1),
        "reason": "moving",
        "speed_ms": speed,
        "radius_m": radius,
        "nearest_card_m": round(min_dist_in_radius, 1) if min_dist_in_radius is not None else None,
    })


# ---------------------------------------------------------------------------
# Endpoints pour l'application mobile : collections, news, slideshow, gallery, auth
# ---------------------------------------------------------------------------

@bp.route("/api/v1/collections")
def collections():
    """
    Liste des collections connues (paramètre [DEFAULT] collections de
    postcards.conf), avec le nombre de cartes uniques (sans doublons)
    dans chacune.

    Réponse JSON :
      {
        "collections": [
          {"name": "Louhans", "count": 42},
          {"name": "Seille", "count": 12},
          ...
        ],
        "collections_map": ["Louhans", "Seille"]
          -- sous-ensemble proposé comme filtre sur /map/ (= collections
             si [DEFAULT] collections_map n'est pas défini)
      }
    """
    model = current_app.model
    names = current_app.config.get("COLLECTIONS", [])

    items = [
        {"name": name, "count": model.count_unique_cards(collection=name)}
        for name in names
    ]

    return jsonify({
        "collections": items,
        "collections_map": current_app.config.get("COLLECTIONS_MAP", names),
    })


@bp.route("/api/v1/news")
def news():
    """
    Dernières cartes postales ajoutées (même contenu que le diaporama
    de la page d'accueil) : celles ajoutées dans la fenêtre de
    RECENT_DAYS jours (cdate), ou à défaut les RECENT_FALLBACK_COUNT
    derniers ajouts si la fenêtre est vide.

    Paramètres de requête (optionnels) :
      collection : filtre sur une collection connue (ignoré si inconnue,
                   auquel cas toutes les collections sont renvoyées)

    Réponse JSON :
      {
        "collection": "Louhans" | null,
        "count": 12,
        "cards": [
          {
            "id": "423", "title": "...", "title2": "...", "cdate": 1234567890,
            "recto": "https://.../images/size_div3/423_R.png",
            "verso": "https://.../images/size_div3/423_V.png",
            "verso_small": "https://.../images/size_div10/423_V.png"
          },
          ...
        ]
      }

    Comme /api/recent-cards côté web, le mélange et le parcours sans
    répétition sont à faire côté client à partir de cette liste
    complète.
    """
    model = current_app.model
    collection = _validated_collection(request.args.get("collection"))

    days = current_app.config.get("RECENT_DAYS", 30)
    fallback_count = current_app.config.get("RECENT_FALLBACK_COUNT", 20)

    cards = model.list_recent_unique_cards(
        days=days, fallback_count=fallback_count, collection=collection or None
    )
    items = [_card_summary(c) for c in cards]

    return _no_cache(jsonify({
        "collection": collection or None,
        "count": len(items),
        "cards": items,
    }))


@bp.route("/api/v1/slideshow")
def slideshow():
    """
    Liste complète des cartes uniques (sans doublons), pour alimenter
    un diaporama côté mobile — mêmes cartes que /slideshow/ côté web.
    Le mélange et le parcours sans répétition sont à faire côté client
    à partir de cette liste complète (un tirage aléatoire à chaque
    appel ne garantit pas de voir toutes les cartes avant répétition).

    Paramètres de requête (optionnels) :
      collection : filtre sur une collection connue (ignoré si inconnue)

    Réponse JSON : { "collection": ..., "count": ..., "cards": [...] }
    (mêmes champs par carte que /api/v1/news)
    """
    model = current_app.model
    collection = _validated_collection(request.args.get("collection"))

    cards = model.list_unique_cards(collection=collection or None)
    items = [_card_summary(c) for c in cards]

    return _no_cache(jsonify({
        "collection": collection or None,
        "count": len(items),
        "cards": items,
    }))


@bp.route("/api/v1/gallery")
def gallery():
    """
    Galerie paginée — mêmes filtres que /gallery/ côté web.

    Paramètres de requête (tous optionnels) :
      collection : filtre sur une collection connue (ignoré si inconnue)
      q          : recherche texte (titre, description, adresse, POI, ...)
      doubles    : "all" pour inclure les doublons (défaut : exclus)
      page       : numéro de page, défaut 1
      per_page   : 12, 24 ou 48 (défaut 24) — toute autre valeur retombe sur 24

    Réponse JSON :
      {
        "collection": "Louhans" | null,
        "search": "château" | null,
        "show_doubles": false,
        "page": 2, "per_page": 24, "pages": 10, "total": 230,
        "cards": [
          {"id": "423", "title": "...", "title2": "...",
           "recto": "https://.../images/size_div10/423_R.png",
           "verso": "https://.../images/size_div10/423_V.png"},
          ...
        ]
      }

    Les images sont en size_div10 (vignettes), comme la galerie web.
    """
    model = current_app.model
    collection = _validated_collection(request.args.get("collection"))
    search = (request.args.get("q") or "").strip()
    show_doubles = request.args.get("doubles") == "all"

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

    if show_doubles:
        count_cards = model.count_cards
        list_cards = model.list_cards
    else:
        count_cards = model.count_unique_cards
        list_cards = model.list_unique_cards

    total = count_cards(collection=collection or None, search=search or None)
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages

    offset = (page - 1) * per_page
    cards = list_cards(
        collection=collection or None,
        search=search or None,
        limit=per_page,
        offset=offset,
    )

    items = [_card_thumb(c) for c in cards]

    return jsonify({
        "collection": collection or None,
        "search": search or None,
        "show_doubles": show_doubles,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
        "cards": items,
    })


@bp.route("/api/v1/check_auth", methods=["POST"])
def check_auth():
    """
    Vérifie un couple email/password (table ``auths``, mots de passe
    hashés PBKDF2-SHA256 — voir ``model.check_auth``), sans émettre de
    token. Utile pour une simple vérification ponctuelle (ex : écran de
    changement de mot de passe). Pour obtenir un access/refresh token
    JWT permettant d'appeler les endpoints protégés
    (/api/v1/similar, /api/v1/update), utiliser plutôt
    ``POST /api/v1/auth/login``.

    Corps JSON (Content-Type: application/json) :
      { "email": "utilisateur@example.com", "password": "mot de passe" }

    Codes de retour :
      200 { "status": "ok", "email": "utilisateur@example.com" }
      401 { "error": "unauthorized" }
      400 { "error": "..." }  -- email ou password manquant
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))

    if not email or not password:
        return jsonify({"error": "email et password sont obligatoires"}), 400

    if not current_app.model.check_auth(email, password):
        current_app.logger.info("check_auth : échec pour %s (from=%s)", email, request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401

    return jsonify({"status": "ok", "email": email})


# ---------------------------------------------------------------------------
# Authentification JWT (access token + refresh token) — voir flpostcards/auth.py
# ---------------------------------------------------------------------------

def _device_info() -> str | None:
    """User-Agent tronqué, à titre indicatif pour un futur écran "sessions actives"."""
    ua = request.headers.get("User-Agent", "")
    return ua[:255] or None


@bp.route("/api/v1/auth/login", methods=["POST"])
def auth_login():
    """
    Authentifie un utilisateur (table ``auths``) et délivre une
    nouvelle paire de tokens.

    Corps JSON (Content-Type: application/json) :
      { "email": "utilisateur@example.com", "password": "mot de passe" }

    Réponse :
      200 {
        "access_token": "<JWT>", "refresh_token": "<chaîne opaque>",
        "token_type": "Bearer", "expires_in": 900
      }
      401 { "error": "unauthorized" }
      400 { "error": "..." }  -- email ou password manquant

    ``access_token`` : à envoyer dans ``Authorization: Bearer <token>``
    pour /api/v1/similar, /api/v1/update, /api/v1/auth/logout-all.
    Expire après ``expires_in`` secondes (15 min par défaut).

    ``refresh_token`` : à conserver côté client (stockage sécurisé,
    ex. Keychain/Keystore) et à utiliser uniquement avec
    ``POST /api/v1/auth/refresh`` pour obtenir un nouvel access_token
    sans redemander le mot de passe. Valide 30 jours par défaut.
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))

    if not email or not password:
        return jsonify({"error": "email et password sont obligatoires"}), 400

    if not current_app.model.check_auth(email, password):
        current_app.logger.info("auth/login : échec pour %s (from=%s)", email, request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401

    current_app.logger.info("auth/login : succès pour %s (from=%s)", email, request.remote_addr)
    return jsonify(issue_token_pair(email, _device_info()))


@bp.route("/api/v1/auth/refresh", methods=["POST"])
def auth_refresh():
    """
    Échange un refresh token contre une nouvelle paire de tokens.

    Rotation : l'ancien refresh token est révoqué immédiatement et
    remplacé par un nouveau — limite l'impact d'un refresh token
    intercepté (une seule utilisation possible par token).

    Corps JSON : { "refresh_token": "..." }

    Réponse :
      200 { "access_token": "...", "refresh_token": "...", "token_type": "Bearer", "expires_in": 900 }
      401 { "error": "unauthorized" }  -- refresh token invalide, expiré ou déjà révoqué
      400 { "error": "..." }           -- refresh_token manquant
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    refresh_token = str(data.get("refresh_token", "")).strip()
    if not refresh_token:
        return jsonify({"error": "refresh_token est obligatoire"}), 400

    entry = current_app.model.verify_refresh_token(refresh_token)
    if entry is None:
        current_app.logger.info("auth/refresh : token invalide/expiré (from=%s)", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401

    current_app.model.revoke_refresh_token(refresh_token)

    return jsonify(issue_token_pair(entry["email"], _device_info()))


@bp.route("/api/v1/auth/logout", methods=["POST"])
def auth_logout():
    """
    Révoque un refresh token (déconnexion de cet appareil uniquement).

    Corps JSON : { "refresh_token": "..." }

    Idempotent : renvoie 200 même si le token était déjà révoqué,
    inconnu, ou absent du corps de la requête — un client qui appelle
    logout deux fois (ex : retry réseau) n'a pas de cas d'erreur
    particulier à gérer.

    Réponse : 200 { "status": "ok" }
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    refresh_token = str(data.get("refresh_token", "")).strip()
    if refresh_token:
        current_app.model.revoke_refresh_token(refresh_token)
    return jsonify({"status": "ok"})


@bp.route("/api/v1/auth/logout-all", methods=["POST"])
@require_auth
def auth_logout_all(auth_email: str):
    """
    Révoque tous les refresh tokens actifs de l'utilisateur authentifié
    (déconnexion de tous les appareils — ex : téléphone volé).

    Authentification requise : ``Authorization: Bearer <access_token>``.

    Réponse : 200 { "status": "ok", "revoked": 3 }
    """
    count = current_app.model.revoke_all_refresh_tokens(auth_email)
    current_app.logger.info(
        "auth/logout-all : %d token(s) révoqué(s) pour %s (from=%s)",
        count, auth_email, request.remote_addr,
    )
    return jsonify({"status": "ok", "revoked": count})


# ---------------------------------------------------------------------------
# Recherche de cartes similaires (photo prise par l'appli mobile)
# ---------------------------------------------------------------------------

@bp.route("/api/v1/similar", methods=["POST"])
@require_auth
def similar(auth_email: str):
    """
    Recherche les cartes postales de la collection ressemblant à une
    photo envoyée par l'appli mobile (ex : une carte trouvée dans une
    brocante).

    Authentification requise : ``Authorization: Bearer <access_token>``
    (obtenu via ``POST /api/v1/auth/login``, voir flpostcards/auth.py).

    Déroulé :
      1. La photo est transmise telle quelle au service ``simpostcards``
         (``SIMILAR_SERVER``, voir postcards.conf [flask]) qui la
         redresse/détoure et renvoie ses hashs perceptuels.
      2. Ces hashs sont comparés (``PostcardSearcher.search_hashes``,
         sans embedding CLIP) à l'index ``datadir/postcards.pkl``.
      3. Chaque carte trouvée au-dessus du seuil est mise en
         correspondance avec ses PNG ``size_div3``/``size_div10``
         (le chemin indexé, ``<cardid>_R.tiff``, donne l'id de carte).

    Requête (multipart/form-data) :
      image     : fichier image (obligatoire) — la photo à identifier
      threshold : seuil de similarité 0-100 (optionnel, défaut
                  configurable via SIMILAR_DEFAULT_THRESHOLD, 70 par défaut)

    Réponse (200) — liste triée par score décroissant :
      [
        {"id": "423", "score": "91%",
         "uri_div3": "http://.../images/size_div3/423_R.png",
         "uri_div10": "http://.../images/size_div10/423_R.png"},
        ...
      ]

    Erreurs :
      401 { "error": "unauthorized" }  — access token absent, invalide ou expiré
      400 { "error": "..." }  — pas d'image envoyée, threshold invalide,
                                 ou image rejetée par simpostcards
      502 { "error": "..." }  — service simpostcards injoignable / en erreur
    """
    file_storage = request.files.get("image")
    if file_storage is None or not file_storage.filename:
        current_app.logger.warning(
            "similar : aucune image reçue (user=%s, from=%s)", auth_email, request.remote_addr
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image')"}), 400

    threshold_raw = request.form.get("threshold", request.args.get("threshold"))
    if threshold_raw is None:
        threshold = current_app.config["SIMILAR_DEFAULT_THRESHOLD"]
    else:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            current_app.logger.warning(
                "similar : threshold invalide %r (user=%s, from=%s)",
                threshold_raw, auth_email, request.remote_addr,
            )
            return jsonify({"error": "threshold doit être un nombre (0-100)"}), 400

    start = time.perf_counter()
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    # request.content_length couvre tout le corps multipart (image +
    # champs additionnels type "threshold"), donc légèrement supérieur
    # à la taille du seul fichier — suffisant pour diagnostiquer un
    # problème de taille/timeout.
    current_app.logger.info(
        "similar : requête reçue, user=%s filename=%s content_length=%s threshold=%s (from=%s)",
        auth_email, file_storage.filename, request.content_length, threshold, remote,
    )

    similar_server = current_app.config["SIMILAR_SERVER"].rstrip("/")
    timeout = current_app.config["SIMILAR_TIMEOUT_S"]

    image_bytes = file_storage.read()
    current_app.logger.info(
        "similar : image lue, %d octets, appel de %s (timeout=%.1fs)",
        len(image_bytes), similar_server, timeout,
    )

    try:
        upstream = requests.post(
            f"{similar_server}/api/compute_hashes",
            files={"image": (file_storage.filename, image_bytes, file_storage.mimetype)},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        current_app.logger.error(
            "similar : simpostcards injoignable après %.2fs (%s) : %s",
            time.perf_counter() - start, similar_server, exc,
        )
        return jsonify({"error": f"Service simpostcards injoignable : {exc}"}), 502

    current_app.logger.info(
        "similar : réponse simpostcards en %.2fs, status=%d",
        time.perf_counter() - start, upstream.status_code,
    )

    if upstream.status_code != 200:
        try:
            upstream_error = upstream.json().get("error", upstream.text)
        except ValueError:
            upstream_error = upstream.text
        current_app.logger.warning(
            "similar : simpostcards a renvoyé %d : %s", upstream.status_code, upstream_error
        )
        status = 400 if upstream.status_code < 500 else 502
        return jsonify({"error": f"simpostcards : {upstream_error}"}), status

    try:
        hashes = upstream.json()["hashes"]
    except (ValueError, KeyError):
        current_app.logger.error("similar : réponse simpostcards invalide : %r", upstream.text[:500])
        return jsonify({"error": "Réponse invalide du service simpostcards"}), 502

    searcher = _get_searcher()
    max_results = current_app.config["SIMILAR_MAX_RESULTS"]
    matches = searcher.search_hashes(hashes, threshold=threshold, max_results=max_results)

    results = []
    for match in matches:
        card_id = searcher.extract_card_id(match["path"])
        results.append({
            "id": card_id,
            "score": f"{round(match['score'])}%",
            "uri_div3": url_for(
                "home.images",
                filename=card_images(card_id, SIZE_MAIN)["recto"],
                _external=True,
            ),
            "uri_div10": url_for(
                "home.images",
                filename=card_images(card_id, SIZE_SMALL)["recto"],
                _external=True,
            ),
        })

    current_app.logger.info(
        "similar : terminé en %.2fs, %d résultat(s) (threshold=%s)",
        time.perf_counter() - start, len(results), threshold,
    )

    return jsonify(results)


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: Path) -> bool:
    """
    Tente d'acquérir un verrou exclusif via un fichier .lck.

    Utilise ``O_CREAT | O_EXCL`` qui est atomique sur POSIX : seul le
    processus qui crée le fichier en premier obtient le verrou.

    Attend jusqu'à ``LOCK_TIMEOUT`` secondes (config) que le fichier
    disparaisse si quelqu'un d'autre le tient, par sondages espacés de
    ``LOCK_POLL_INTERVAL`` secondes (config).
    Retourne True si le verrou est acquis, False en cas de timeout.
    """
    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    poll = current_app.config.get("LOCK_POLL_INTERVAL", 2.0)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)


def _release_lock(lock_path: Path) -> None:
    """Relâche le verrou en supprimant le fichier .lck."""
    try:
        lock_path.unlink()
    except OSError:
        pass


@bp.route("/api/v1/update", methods=["POST"])
@require_auth
def update(auth_email: str):
    """
    Enregistre le repérage d'une carte postale sur le terrain.

    Authentification requise : ``Authorization: Bearer <access_token>``
    (obtenu via ``POST /api/v1/auth/login``, voir flpostcards/auth.py).
    L'email associé au repérage est celui du token, pas un champ du
    corps de la requête.

    Corps JSON (Content-Type: application/json) :
      {
        "card_id" : "123",
        "lat"     : 46.749,
        "lon"     : 5.620
      }

    L'écriture dans ``updates.json`` est protégée par un lockfile
    ``updates.json.lck`` : si ce fichier existe, on attend jusqu'à 10
    secondes qu'il disparaisse avant d'écrire (protection contre les
    écritures concurrentes depuis plusieurs workers gunicorn).

    En cas de succès, enregistre le repérage dans datadir/updates.json :
      { "card_id", "email", "lat", "lon", "ts" (timestamp UNIX) }

    Codes de retour :
      200 { "status": "ok", "card_id": "...", "ts": ... }
      401 { "error": "unauthorized" }  — access token absent, invalide ou expiré
      400 { "error": "..." }   — champ manquant ou invalide
      503 { "error": "..." }   — timeout sur le lockfile (rare)
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    email = auth_email

    card_id = str(data.get("card_id", "")).strip()
    if not card_id:
        return jsonify({"error": "card_id est obligatoire"}), 400

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lat et lon sont obligatoires (float)"}), 400

    ts = int(time.time())
    entry = {"card_id": card_id, "email": email, "lat": lat, "lon": lon, "ts": ts}

    datadir = Path(current_app.config["DATADIR"])
    updates_path = datadir / "updates.json"
    lock_suffix = current_app.config.get("LOCK_SUFFIX", ".lck")
    lock_path = Path(str(updates_path) + lock_suffix)

    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    if not _acquire_lock(lock_path):
        return jsonify({
            "error": f"verrou {lock_path.name} toujours présent après {timeout:.0f}s"
        }), 503

    try:
        try:
            updates: list[dict] = json.loads(updates_path.read_text(encoding="utf-8"))
            if not isinstance(updates, list):
                updates = []
        except (OSError, json.JSONDecodeError):
            updates = []

        updates.append(entry)

        tmp_path = updates_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(updates_path)
    finally:
        _release_lock(lock_path)

    return jsonify({"status": "ok", "card_id": card_id, "ts": ts})
