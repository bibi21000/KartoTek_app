"""
Blueprint API v1 : endpoints JSON pour une application mobile de
localisation de cartes postales.

Routes :
  GET  /api/v1/ping          → sonde de disponibilité minimale (aucun accès disque/DB)
  GET  /api/v1/dbid          → hash du fichier postcards.sqlite (détection de changement)
  GET  /api/v1/capabilities  → fonctionnalités activées sur ce serveur (similar, manager, collections, ...)
                                + gouvernance de version client (api_version, force_update, deprecations)
  GET  /api/v1/gps           → coordonnées GPS paginées (sans doublons, curseur after_id)
  GET  /api/v1/bounds        → zone GPS couverte par les cartes (rectangle)
  GET  /api/v1/nearby        → cartes dans un rayon autour d'une position
  GET  /api/v1/next-update   → délai recommandé avant le prochain poll
  POST /api/v1/update        → enregistre un repérage de carte sur le terrain (auth JWT requise)
  POST /api/v1/similar       → recherche de cartes similaires à une photo (auth JWT requise)
  POST /api/v1/report        → signale un problème sur une carte (mauvaise géoloc, contenu
                                inapproprié, doublon, ...) — public, aucune auth requise
  GET  /api/v1/reports       → liste les signalements (managers uniquement, auth JWT requise)
  POST /api/v1/reports/<id>/resolve → marque un signalement comme traité (managers uniquement)
  GET  /api/v1/metrics       → télémétrie légère (managers uniquement, auth JWT requise)
  # NB : /api/v1/push/register et /unregister vivent désormais sur le
  # master (kartotek.eu) — l'app mobile s'y inscrit une seule fois pour
  # tous les serveurs. Ce serveur appelle seulement le master en interne
  # (flpostcards.push.notify_master) quand une carte est ajoutée, voir
  # flpostcards.push_watch. Voir docs/07-PUSH_NOTIFICATIONS.md.
  GET  /api/v1/collections   → liste des collections (avec nombre de cartes)
  GET  /api/v1/card/<id>     → fiche détaillée d'une carte (titre, description, coord, collections, images pleine taille)
  GET  /api/v1/news          → dernières cartes ajoutées (comme la page d'accueil), filtrable par collection, paginable (page/per_page, voir docstring)
  GET  /api/v1/slideshow     → cartes pour un diaporama, filtrable par collection, paginable (page/per_page, voir docstring)
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
import importlib.resources as importlib_resources
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify, request, url_for

from flpostcards.auth import current_auth_email, issue_token_pair, require_auth
from flpostcards.extensions import limiter
from flpostcards.images import SIZE_MAIN, SIZE_SMALL, SIZE_THUMB, card_images
from flpostcards.jsonlock import LockedJsonFile, read_json
from flpostcards.blueprints.gallery import DEFAULT_PER_PAGE, PER_PAGE_CHOICES
from flask_limiter.util import get_remote_address

bp = Blueprint("api_v1", __name__)

# Motifs de signalement acceptés par POST /api/v1/report — liste fermée
# (plutôt qu'un texte libre) pour que l'appli mobile propose un menu et
# que les managers puissent trier/filtrer sans avoir à interpréter du
# texte libre. Exposée aussi via GET /api/v1/capabilities (clé
# "reporting") pour que le client construise son menu dynamiquement.
_VALID_REPORT_REASONS = {
    "wrong_location",
    "inappropriate_content",
    "duplicate",
    "copyright",
    "other",
}
_REPORT_COMMENT_MAX_LEN = 500


@bp.errorhandler(429)
def _rate_limit_exceeded(exc):
    """
    Par défaut flask-limiter renvoie un corps texte brut sur 429 ;
    l'API étant entièrement JSON, on aligne le format sur les autres
    erreurs ({"error": "..."}) plutôt que de laisser passer le texte
    brut par défaut.
    """
    return jsonify({"error": "too many requests, please retry later"}), 429


def _login_target_key() -> str:
    """
    Clé de rate limit basée sur le compte ciblé (email envoyé dans le
    corps JSON de /check_auth ou /auth/login), et non sur l'IP.

    Complète la limite par IP (get_remote_address, clé par défaut du
    limiter) : celle-ci ne suffit pas seule à empêcher un bruteforce
    ciblé sur un compte précis mené depuis plusieurs IP (botnet,
    rotation de proxy, ...). À l'inverse, la limite par IP protège
    contre un attaquant qui teste beaucoup d'emails différents depuis
    une même source. Les deux limites sont donc appliquées ensemble.
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    return f"login-email:{email}" if email else get_remote_address()


def _similar_user_key() -> str:
    """
    Clé de rate limit pour /api/v1/similar : email authentifié (le
    token JWT a nécessairement été validé par @require_auth avant que
    ce décorateur ne s'exécute, voir l'ordre des décorateurs sur la
    route) plutôt que l'IP, pour ne pas mélanger plusieurs
    utilisateurs derrière une même IP/NAT et ne pas être contournable
    en changeant simplement d'IP avec un token volé.
    """
    return current_auth_email() or get_remote_address()

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


def _pagination_params() -> tuple[int, int, bool]:
    """
    Lit les paramètres optionnels ``page``/``per_page`` pour
    /api/v1/news et /api/v1/slideshow — mêmes choix que /api/v1/gallery
    (voir PER_PAGE_CHOICES/DEFAULT_PER_PAGE, importés depuis
    blueprints.gallery pour ne pas dupliquer ces constantes).

    Retourne (page, per_page, explicit) où ``explicit`` indique si le
    client a lui-même demandé une pagination (``page`` ou ``per_page``
    présent dans la requête), pour distinguer ce cas du repli
    automatique de sécurité appliqué sur une grosse collection même
    quand le client n'a rien demandé de particulier (voir
    MOBILE_LIST_MAX_UNPAGINATED, utilisé par news()/slideshow()).
    """
    explicit = "page" in request.args or "per_page" in request.args

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

    return page, per_page, explicit


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

@bp.route("/api/v1/ping")
# Limite large, par IP (clé par défaut du limiter) : ce endpoint ne fait
# ni accès disque ni requête base, le coût par appel est négligeable,
# mais on garde un plafond pour éviter qu'il ne devienne un vecteur de
# DoS trivial (aucune authentification, aucun paramètre à valider avant
# de répondre). Volontairement plus permissif que check_auth/login (qui
# protègent un bruteforce de mot de passe, pas un simple ping).
@limiter.limit("30 per minute;600 per hour")
def ping():
    """
    Sonde de disponibilité minimale : ne touche ni au disque ni à la
    base (contrairement à /api/v1/dbid, qui hashe postcards.sqlite), et
    ne dépend d'aucun paramètre. Destinée à un client qui a juste besoin
    de savoir "ce serveur répond-il encore ?" (ex : détection "serveur
    plus disponible" côté application mobile avant de basculer sur
    l'écran de sélection d'un autre serveur), sans payer le coût d'un
    endpoint plus riche.

    Toujours 200 si le processus Flask répond (une absence de réponse,
    un timeout ou une erreur réseau sont la façon dont le client détecte
    une réelle indisponibilité — ce endpoint ne peut pas, par nature,
    signaler sa propre absence).

    429 { "error": "too many requests, please retry later" } au-delà de
    30 appels/minute ou 600/heure pour une même IP (voir
    _rate_limit_exceeded, même format d'erreur que les autres endpoints).

    Réponse JSON : { "status": "ok" }
    """
    return jsonify({"status": "ok"})


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


# Rayon maximum accepté par /api/v1/nearby et /api/v1/next-update (voir ces
# routes) — dupliqué ici en constante nommée pour que /api/v1/capabilities
# puisse l'exposer sans que les deux valeurs ne divergent silencieusement.
_MAX_NEARBY_RADIUS_M = 50_000


def _load_deprecations() -> list[dict[str, Any]]:
    """
    Lit ``deprecations.json``, distribué à l'intérieur du paquet Python
    ``flpostcards`` lui-même (voir ``pyproject.toml`` →
    ``[tool.setuptools.package-data]``), pour signaler aux clients
    mobiles qu'un endpoint est en cours de retrait.

    Contrairement à ``collections.json`` (donnée d'exploitation, propre
    à chaque site, modifiable par l'admin sans toucher au code), une
    dépréciation d'endpoint est une information liée au CODE de
    l'API : elle apparaît et disparaît au fil des releases de
    flpostcards, donc versionnée dans git avec le reste du paquet et
    livrée avec chaque déploiement — pas dans ``datadir``.

    Absent ou invalide -> liste vide, jamais d'erreur 500 pour un
    simple fichier manquant (utile en particulier en développement,
    où le paquet peut ne pas être installé via le mécanisme
    package-data mais lancé directement depuis les sources).
    """
    try:
        raw = importlib_resources.files("flpostcards").joinpath(
            "deprecations.json"
        ).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _load_deprecations() -> list[dict[str, Any]]:
    """
    Lit ``deprecations.json``, distribué à l'intérieur du paquet Python
    ``flpostcards`` lui-même (voir ``pyproject.toml`` →
    ``[tool.setuptools.package-data]``), pour signaler aux clients
    mobiles qu'un endpoint est en cours de retrait.

    Contrairement à ``collections.json`` (donnée d'exploitation, propre
    à chaque site, modifiable par l'admin sans toucher au code), une
    dépréciation d'endpoint est une information liée au CODE de
    l'API : elle apparaît et disparaît au fil des releases de
    flpostcards, donc versionnée dans git avec le reste du paquet et
    livrée avec chaque déploiement — pas dans ``datadir``.

    Absent ou invalide -> liste vide, jamais d'erreur 500 pour un
    simple fichier manquant (utile en particulier en développement,
    où le paquet peut ne pas être installé via le mécanisme
    package-data mais lancé directement depuis les sources).
    """
    try:
        raw = importlib_resources.files("flpostcards").joinpath(
            "deprecations.json"
        ).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


@bp.route("/api/v1/capabilities")
# Route de découverte, appelée typiquement une fois par sélection de
# serveur (voir appli mobile KartoTek : écran paramètres / "ici") plutôt
# qu'à chaque action — pas besoin d'une limite serrée, mais on en garde
# une par précaution comme pour /ping.
@limiter.limit("30 per minute;600 per hour")
def capabilities():
    """
    Fonctionnalités effectivement activées/configurées sur CE serveur,
    à interroger une fois après sélection du serveur (ou mise en cache
    par l'appli) pour savoir quels boutons/écrans proposer côté client
    plutôt que de le découvrir par un 400/502/503 en essayant.

    Ne nécessite aucune authentification et ne révèle aucune donnée
    sensible : uniquement des booléens/nombres de configuration
    (ex. "y a-t-il au moins un compte manager créé sur ce serveur ?",
    jamais la liste des comptes eux-mêmes).

    Réponse JSON (200) :
      {
        "similar_search": {
          "enabled": true,                 -- SIMILAR_SERVER configuré (sinon /api/v1/similar échoue en 503)
          "default_threshold": 70.0,        -- seuil par défaut, voir /api/v1/similar
          "max_results": 20
        },
        "manager_accounts": {
          "enabled": true                   -- au moins un compte manager existe (model.list_auths()) ;
                                             -- si false, /api/v1/check_auth et /api/v1/auth/login
                                             -- échoueront systématiquement (aucun compte à authentifier)
        },
        "collections": {
          "enabled": true,
          "count": 2,
          "names": ["Louhans", "Seille"]
        },
        "push": {
          "enabled": true                   -- ce serveur notifie le master (kartotek_master) des nouvelles cartes
        },
        "reporting": {
          "enabled": true,                  -- toujours vrai (POST /api/v1/report est public, sans configuration)
          "reasons": ["wrong_location", "inappropriate_content", "duplicate", "copyright", "other"]
        },
        "gallery": {
          "per_page_choices": [12, 24, 48],
          "default_per_page": 24
        },
        "nearby": {
          "max_radius_m": 50000             -- plafond appliqué par /api/v1/nearby et /api/v1/next-update
        },
        "api_version": {
          "current": "1.3",                 -- version courante de l'API de CE serveur (config [app_version] api_version,
                                             -- null si non renseignée : ne pas en déduire une incompatibilité)
          "min_supported_client": "1.0.0",  -- version minimale de l'appli mobile acceptée par ce serveur ; si la
                                             -- version embarquée du client est strictement inférieure, le client DOIT
                                             -- afficher un écran de mise à jour bloquant avant tout autre appel réseau
                                             -- (null = aucun minimum imposé)
          "recommended_client": "1.4.0"     -- version conseillée, à titre informatif (ex : bandeau non bloquant),
                                             -- null si non renseignée
        },
        "force_update": {
          "required": false,                -- true = mise à jour immédiate exigée, indépendamment de
                                             -- min_supported_client (ex : faille de sécurité découverte sur une
                                             -- version déjà supérieure à min_supported_client) ; les deux
                                             -- mécanismes ont des déclencheurs différents et ne doivent pas être
                                             -- fusionnés côté client
          "reason": null,                   -- message à afficher à l'utilisateur si required=true
          "store_url": {
            "ios": null,
            "android": null
          }
        },
        "api_version": {
          "current": "1.3",                 -- version courante de l'API de CE serveur (config [app_version] api_version,
                                             -- null si non renseignée : ne pas en déduire une incompatibilité)
          "min_supported_client": "1.0.0",  -- version minimale de l'appli mobile acceptée par ce serveur ; si la
                                             -- version embarquée du client est strictement inférieure, le client DOIT
                                             -- afficher un écran de mise à jour bloquant avant tout autre appel réseau
                                             -- (null = aucun minimum imposé)
          "recommended_client": "1.4.0"     -- version conseillée, à titre informatif (ex : bandeau non bloquant),
                                             -- null si non renseignée
        },
        "force_update": {
          "required": false,                -- true = mise à jour immédiate exigée, indépendamment de
                                             -- min_supported_client (ex : faille de sécurité découverte sur une
                                             -- version déjà supérieure à min_supported_client) ; les deux
                                             -- mécanismes ont des déclencheurs différents et ne doivent pas être
                                             -- fusionnés côté client
          "reason": null,                   -- message à afficher à l'utilisateur si required=true
          "store_url": {
            "ios": null,
            "android": null
          }
        },
        "deprecations": [                   -- informatif, jamais bloquant : à logger/remonter en analytics côté
                                             -- client plutôt qu'à afficher à l'utilisateur. Distribué avec le
                                             -- paquet flpostcards lui-même (voir _load_deprecations, liste vide
                                             -- si le fichier est absent du paquet installé).
          {
            "endpoint": "GET /api/v1/gallery",
            "since": "2027-01-15",
            "removed_after": "2027-06-01",
            "replacement": "GET /api/v2/gallery",
            "message": "Le paramètre 'page' est remplacé par un curseur 'after_id'."
          }
        ],
        "privacy_policy_url": "https://server1.kartotek.eu/privacy/"
                                             -- URL absolue de la politique de confidentialité de CE serveur
                                             -- (voir flpostcards.blueprints.privacy), toujours renseignée : à
                                             -- lier depuis l'écran paramètres/à propos de l'app mobile. Apple et
                                             -- Google exigent que ce lien soit accessible depuis l'app elle-même,
                                             -- pas seulement depuis la fiche store.
       }
    Compatibilité ascendante : ce bloc a été ajouté après la première
    version de /api/v1/capabilities. Un client qui ne le connaît pas
    encore doit ignorer silencieusement toute clé JSON qu'il ne
    reconnaît pas (ne jamais faire d'analyse stricte du schéma) — c'est
    ce qui permettra d'ajouter d'autres champs plus tard sans passer,
    eux, par un cycle de dépréciation.
    """
    config = current_app.config
    model = current_app.model

    similar_enabled = bool(config.get("SIMILAR_SERVER"))
    manager_enabled = bool(model.list_auths())
    collection_names = config.get("COLLECTIONS", [])

    return jsonify({
        "similar_search": {
            "enabled": similar_enabled,
            "default_threshold": config.get("SIMILAR_DEFAULT_THRESHOLD") if similar_enabled else None,
            "max_results": config.get("SIMILAR_MAX_RESULTS") if similar_enabled else None,
        },
        "manager_accounts": {
            "enabled": manager_enabled,
        },
        "collections": {
            "enabled": bool(collection_names),
            "count": len(collection_names),
            "names": collection_names,
        },
        "push": {
            "enabled": bool(config.get("PUSH_ENABLED")),
        },
        "reporting": {
            "enabled": True,
            "reasons": sorted(_VALID_REPORT_REASONS),
        },
        "gallery": {
            "per_page_choices": list(PER_PAGE_CHOICES),
            "default_per_page": DEFAULT_PER_PAGE,
        },
        "nearby": {
            "max_radius_m": _MAX_NEARBY_RADIUS_M,
        },
        "api_version": {
            "current": config.get("API_VERSION_CURRENT"),
            "min_supported_client": config.get("MIN_SUPPORTED_CLIENT"),
            "recommended_client": config.get("RECOMMENDED_CLIENT"),
        },
        "force_update": {
            "required": bool(config.get("FORCE_UPDATE_REQUIRED", False)),
            "reason": config.get("FORCE_UPDATE_REASON"),
            "store_url": {
                "ios": config.get("STORE_URL_IOS"),
                "android": config.get("STORE_URL_ANDROID"),
            },
        },
        "deprecations": _load_deprecations(),
        "privacy_policy_url": url_for("privacy.index", _external=True),
    })


@bp.route("/api/v1/gps")
# Endpoint public, sans authentification, permettant de paginer
# l'intégralité des cartes géolocalisées : plafonné pour empêcher un
# scraping complet répété de la base par un tiers, tout en restant
# largement au-dessus du besoin réel (le master ne le rappelle qu'une
# fois par cycle de poller, voir kartotek_master.poller).
@limiter.limit("30 per minute;600 per hour")
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
# Route de découverte peu coûteuse mais publique : même limite que
# /api/v1/capabilities (appelée avec la même fréquence, typiquement une
# fois par sélection de serveur côté app mobile, ou une fois par cycle
# de poller côté master).
@limiter.limit("30 per minute;600 per hour")
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
        "api_version": {
            "current": config.get("API_VERSION_CURRENT"),
            "min_supported_client": config.get("MIN_SUPPORTED_CLIENT"),
            "recommended_client": config.get("RECOMMENDED_CLIENT"),
        },
        "force_update": {
            "required": bool(config.get("FORCE_UPDATE_REQUIRED", False)),
            "reason": config.get("FORCE_UPDATE_REASON"),
            "store_url": {
                "ios": config.get("STORE_URL_IOS"),
                "android": config.get("STORE_URL_ANDROID"),
            },
        },
        "deprecations": _load_deprecations(),
    })


@bp.route("/api/v1/nearby")
# Endpoint le plus sollicité par l'écran "ici" de l'app mobile (boucle
# nearby/next-update en tâche de fond) : limite plus généreuse que les
# routes de découverte, mais toujours bornée — sans authentification, et
# chaque appel recalcule une distance haversine sur toutes les cartes
# géolocalisées (_cards_with_coord), donc pas gratuit à répéter sans
# limite pour un tiers qui n'aurait pas de contrainte de mouvement réel
# (contrairement au client légitime, dont next_update() plafonne déjà le
# rythme via next_update_s).
@limiter.limit("60 per minute;1200 per hour")
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
        radius = min(float(request.args["radius"]), _MAX_NEARBY_RADIUS_M)
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
# Même palier que /api/v1/nearby : appelée dans la même boucle par
# l'écran "ici", pour les mêmes raisons (voir commentaire ci-dessus).
@limiter.limit("60 per minute;1200 per hour")
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
        radius = min(float(request.args["radius"]), _MAX_NEARBY_RADIUS_M)
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
# Route de découverte peu coûteuse mais publique, appelée typiquement
# une fois par sélection de serveur — même palier que
# /api/v1/capabilities et /api/v1/bounds.
@limiter.limit("30 per minute;600 per hour")
def collections():
    """
    Liste des collections connues (paramètre ``collections`` de
    ``<datadir>/collections.json``), avec le nombre de cartes uniques
    (sans doublons) dans chacune.

    Réponse JSON :
      {
        "collections": [
          {"name": "Louhans", "count": 42},
          {"name": "Seille", "count": 12},
          ...
        ],
        "collections_map": ["Louhans", "Seille"]
          -- sous-ensemble proposé comme filtre sur /map/ (= collections
             si "collections_map" n'est pas défini dans collections.json)
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
# Écran consulté régulièrement mais pas en boucle serrée (contrairement
# à nearby/next-update) : palier intermédiaire.
@limiter.limit("30 per minute;600 per hour")
def news():
    """
    Dernières cartes postales ajoutées (même contenu que le diaporama
    de la page d'accueil) : celles ajoutées dans la fenêtre de
    RECENT_DAYS jours (cdate), ou à défaut les RECENT_FALLBACK_COUNT
    derniers ajouts si la fenêtre est vide.

    Paramètres de requête (optionnels) :
      collection : filtre sur une collection connue (ignoré si inconnue,
                   auquel cas toutes les collections sont renvoyées)
      page       : numéro de page (défaut 1) — voir "Pagination" ci-dessous
      per_page   : 12, 24 ou 48 (défaut 24) — toute autre valeur retombe sur 24

    Réponse JSON (liste complète, comportement historique — toujours le
    cas si le nombre de cartes de la fenêtre RECENT_DAYS/collection ne
    dépasse pas MOBILE_LIST_MAX_UNPAGINATED, 200 par défaut) :
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
        ],
        "truncated": false
      }

    Pagination : déclenchée soit explicitement (client envoyant `page`
    et/ou `per_page`), soit automatiquement si le nombre de cartes de la
    fenêtre dépasse MOBILE_LIST_MAX_UNPAGINATED (repli de sécurité pour
    un gros site, sur un lien mobile : évite d'envoyer d'un bloc un
    payload qui grossirait sans limite avec la collection). Dans les
    deux cas, la réponse gagne les champs suivants (mêmes noms que
    /api/v1/gallery) :
      "page", "per_page", "pages", "total"
    et "truncated" vaut true UNIQUEMENT dans le cas du repli automatique
    (page=1 implicite, alors que le client n'avait rien demandé) — false
    si le client a lui-même demandé une page, y compris au-delà de la
    dernière (auquel cas `page` est ramené à `pages`).

    Un client qui ignore ces champs (ancien client déployé avant cette
    évolution) continue de fonctionner sans changement tant que la
    collection reste sous le seuil ; au-delà, il ne verra plus que la
    première page mais avec `truncated: true` explicite plutôt qu'une
    troncature silencieuse.

    Comme /api/recent-cards côté web, le mélange et le parcours sans
    répétition sont à faire côté client à partir des cartes reçues (sur
    l'ensemble des pages, si la pagination est utilisée).
    """
    model = current_app.model
    collection = _validated_collection(request.args.get("collection"))

    days = current_app.config.get("RECENT_DAYS", 30)
    fallback_count = current_app.config.get("RECENT_FALLBACK_COUNT", 20)

    # list_recent_unique_cards() ne supporte pas de limit/offset côté
    # modèle (fenêtre RECENT_DAYS + repli RECENT_FALLBACK_COUNT, pas une
    # simple liste ordonnée) : la pagination ci-dessous découpe donc la
    # liste déjà matérialisée en Python. Ça réduit le payload HTTP
    # renvoyé au client (le problème signalé), mais pas le travail fait
    # côté base — un site avec une fenêtre RECENT_DAYS réellement énorme
    # gagnerait à un vrai limit/offset dans libpostcards.model, hors
    # scope de ce correctif.
    cards = model.list_recent_unique_cards(
        days=days, fallback_count=fallback_count, collection=collection or None
    )
    total = len(cards)
    page, per_page, explicit = _pagination_params()
    max_unpaginated = current_app.config["MOBILE_LIST_MAX_UNPAGINATED"]

    if not explicit and total <= max_unpaginated:
        items = [_card_summary(c) for c in cards]
        return _no_cache(jsonify({
            "collection": collection or None,
            "count": len(items),
            "cards": items,
            "truncated": False,
        }))

    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page
    items = [_card_summary(c) for c in cards[offset:offset + per_page]]

    return _no_cache(jsonify({
        "collection": collection or None,
        "count": len(items),
        "cards": items,
        "truncated": not explicit,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
    }))


@bp.route("/api/v1/slideshow")
# Renvoie la collection entière (pas de pagination) : plus coûteux par
# appel que les autres endpoints de liste, mais normalement rappelé au
# mieux une fois par lancement du diaporama côté client — limite plus
# stricte que /api/v1/news pour refléter ce coût.
@limiter.limit("20 per minute;300 per hour")
def slideshow():
    """
    Liste des cartes uniques (sans doublons), pour alimenter un
    diaporama côté mobile — mêmes cartes que /slideshow/ côté web.

    Paramètres de requête (optionnels) :
      collection : filtre sur une collection connue (ignoré si inconnue)
      page       : numéro de page (défaut 1) — voir "Pagination" ci-dessous
      per_page   : 12, 24 ou 48 (défaut 24) — toute autre valeur retombe sur 24

    Réponse JSON (liste complète, comportement historique — toujours le
    cas si la collection/filtre ne dépasse pas MOBILE_LIST_MAX_UNPAGINATED
    cartes, 200 par défaut) :
      { "collection": ..., "count": ..., "cards": [...], "truncated": false }
    (mêmes champs par carte que /api/v1/news)

    Pagination : mêmes règles que /api/v1/news (voir sa docstring pour le
    détail) — déclenchée explicitement par le client (`page`/`per_page`)
    ou automatiquement au-delà du seuil, avec alors "page", "per_page",
    "pages", "total" en plus, et "truncated": true uniquement dans le cas
    du repli automatique.

    Le mélange et le parcours sans répétition restent à faire côté
    client, à partir de l'ensemble des cartes reçues : si la pagination
    est utilisée, cela veut dire à partir de l'ensemble des pages
    parcourues (le classement par id étant stable d'un appel à l'autre,
    parcourir toutes les pages reconstitue la même collection complète
    qu'un appel non paginé).
    """
    model = current_app.model
    collection = _validated_collection(request.args.get("collection"))

    total = model.count_unique_cards(collection=collection or None)
    page, per_page, explicit = _pagination_params()
    max_unpaginated = current_app.config["MOBILE_LIST_MAX_UNPAGINATED"]

    if not explicit and total <= max_unpaginated:
        cards = model.list_unique_cards(collection=collection or None)
        items = [_card_summary(c) for c in cards]
        return _no_cache(jsonify({
            "collection": collection or None,
            "count": len(items),
            "cards": items,
            "truncated": False,
        }))

    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page

    cards = model.list_unique_cards(collection=collection or None, limit=per_page, offset=offset)
    items = [_card_summary(c) for c in cards]

    return _no_cache(jsonify({
        "collection": collection or None,
        "count": len(items),
        "cards": items,
        "truncated": not explicit,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
    }))


@bp.route("/api/v1/gallery")
# Parcourue de façon interactive (pagination, filtres, recherche texte)
# côté galerie mobile : palier plus généreux qu'un endpoint de
# découverte, mais toujours borné.
@limiter.limit("60 per minute;1000 per hour")
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


@bp.route("/api/v1/card/<card_id>")
# Consultée à chaque ouverture de fiche détaillée (galerie, "ici",
# résultats de /api/v1/similar) : même palier que /api/v1/gallery.
@limiter.limit("60 per minute;1000 per hour")
def card_detail(card_id: str):
    """
    Fiche détaillée d'une carte postale — équivalent JSON de la page web
    ``/card/<id>`` (voir flpostcards.blueprints.home.card_detail), à
    l'usage des vues de détail de l'appli mobile (galerie, « ici »,
    résultats de /api/v1/similar, ...).

    Contrairement à /api/v1/news, /api/v1/slideshow et /api/v1/gallery
    qui ne renvoient que des résumés, cette route inclut la description
    complète, les collections d'appartenance et les trois tailles
    d'image (utile pour passer d'une vignette à un affichage plein
    écran sans requête supplémentaire).

    Champs renvoyés (voir libpostcards/model.py, table ``cards``) :
      id, title, title2, description : identiques à /api/v1/news
      date          : époque/date manuscrite de la carte (texte libre,
                       ex. "1910" ou "circa 1905"), à ne pas confondre
                       avec cdate/mdate qui sont les timestamps
                       d'ajout/modification en base.
      cdate, mdate  : timestamps UNIX (ajout / dernière modification).
      address       : liste de chaînes (adresse/lieu, tel que renseigné
                       lors de l'import) — peut être vide.
      collections   : liste des collections auxquelles la carte appartient.
      coord         : {"lat", "lon"} ou null si non géolocalisée.
      recto_text / verso_text : texte reconnu (OCR nettoyé) sur chaque
                       face, s'il y en a un — absent du JSON si vide,
                       plutôt que "" ou null, pour ne pas laisser croire
                       à une valeur exploitable.
      recto / verso : {"main", "small", "thumb"} — URLs absolues des
                       trois tailles d'image (voir flpostcards.images).
      poi           : liste des points d'intérêt liés, chacun résolu en
                       {"id", "description", "coord"} (voir model.get_poi)
                       plutôt que de simples identifiants bruts.
      doubles       : autres exemplaires connus de la même carte
                       (même photo/tirage scanné plusieurs fois), résolus
                       en résumé léger {"id", "title", "thumb_recto"} —
                       utile pour proposer "voir l'autre exemplaire"
                       plutôt que de dupliquer l'affichage dans galerie/ici.
      web_url       : page web équivalente (repli / bouton "voir sur le site").

    NB : ``date``, ``address``, ``poi`` et ``doubles`` existent dans le
    modèle mais ne sont actuellement affichés nulle part côté web (voir
    templates/card/detail.html) — ils sont exposés ici car potentiellement
    utiles à l'appli mobile, mais leur contenu peut être incomplet ou
    vide selon la façon dont chaque serveur a été importé/renseigné.

    404 { "error": "carte introuvable" } si card_id est inconnu.
    """
    model = current_app.model
    card = model.get_card(card_id)
    if card is None:
        return jsonify({"error": "carte introuvable"}), 404

    cid = card["id"]
    coord = card.get("coord")
    has_coord = coord and coord[0] is not None and coord[1] is not None

    def _sizes(side: str) -> dict:
        return {
            "main": _image_uri(cid, SIZE_MAIN, side),
            "small": _image_uri(cid, SIZE_SMALL, side),
            "thumb": _image_uri(cid, SIZE_THUMB, side),
        }

    pois = []
    for poi_id in card.get("poi") or []:
        poi = model.get_poi(poi_id)
        if poi is None:
            continue
        poi_coord = poi.get("coord")
        pois.append({
            "id": poi["id"],
            "description": poi.get("description"),
            "coord": (
                {"lat": poi_coord[0], "lon": poi_coord[1]}
                if poi_coord and poi_coord[0] is not None and poi_coord[1] is not None
                else None
            ),
        })

    doubles = []
    for double_id in card.get("doubles") or []:
        double = model.get_card(double_id)
        if double is None:
            continue
        doubles.append({
            "id": double["id"],
            "title": double.get("title"),
            "thumb_recto": _image_uri(double["id"], SIZE_THUMB, "recto"),
        })

    result = {
        "id": cid,
        "title": card.get("title"),
        "title2": card.get("title2"),
        "description": card.get("description"),
        "date": card.get("date"),
        "cdate": card.get("cdate"),
        "mdate": card.get("mdate"),
        "address": card.get("address") or [],
        "collections": card.get("collections") or [],
        "coord": {"lat": coord[0], "lon": coord[1]} if has_coord else None,
        "recto": _sizes("recto"),
        "verso": _sizes("verso"),
        "poi": pois,
        "doubles": doubles,
        "web_url": url_for("home.card_detail", card_id=cid, _external=True),
    }

    recto_text = (card.get("recto_text") or "").strip()
    verso_text = (card.get("verso_text") or "").strip()
    if recto_text:
        result["recto_text"] = recto_text
    if verso_text:
        result["verso_text"] = verso_text

    return jsonify(result)


@bp.route("/api/v1/check_auth", methods=["POST"])
# Bruteforce : limite par IP (défaut du limiter) et par compte ciblé
# (voir _login_target_key) -- les deux sont nécessaires, voir sa
# docstring. Les mêmes valeurs que /auth/login : check_auth vérifie
# le même mot de passe (model.check_auth), donc présente le même
# risque de bruteforce.
@limiter.limit("10 per minute;100 per hour")
@limiter.limit("8 per minute;50 per hour", key_func=_login_target_key)
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
# Bruteforce : voir la même remarque que check_auth ci-dessus.
@limiter.limit("10 per minute;100 per hour")
@limiter.limit("8 per minute;50 per hour", key_func=_login_target_key)
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
# Abus : endpoint coûteux (appel réseau à simpostcards, calcul de
# hashs perceptuels sur l'image envoyée) -- limite par utilisateur
# authentifié (voir _similar_user_key) plutôt que par IP, appliquée
# après @require_auth pour ne pas consommer de budget de requêtes
# non authentifiées.
@limiter.limit("20 per minute;200 per hour", key_func=_similar_user_key)
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
         redresse/détoure et renvoie ses hashs perceptuels pour ses 4
         rotations (0°, 90°, 180°, 270°) — la photo peut avoir été prise
         dans n'importe quel sens.
      2. Chaque jeu de hashs est comparé (``PostcardSearcher.search_hashes``,
         sans embedding CLIP) à l'index ``datadir/postcards.pkl`` ; pour
         chaque carte, seul le meilleur score toutes rotations confondues
         est conservé.
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
        rotations = upstream.json()["hashes"]
    except (ValueError, KeyError):
        current_app.logger.error("similar : réponse simpostcards invalide : %r", upstream.text[:500])
        return jsonify({"error": "Réponse invalide du service simpostcards"}), 502

    # Depuis la mise à jour de simpostcards, "hashes" est une liste de 4
    # entrées {"angle": 0|90|180|270, "hashes": {...}}, une par rotation
    # de la photo — la carte a pu être photographiée dans n'importe quel
    # sens. On interroge l'index pour chaque rotation, puis on fusionne
    # les résultats en ne gardant, par carte, que le meilleur score
    # obtenu (celui de l'orientation qui correspond réellement).
    searcher = _get_searcher()
    max_results = current_app.config["SIMILAR_MAX_RESULTS"]

    best_by_path: dict[str, dict] = {}
    for rotation in rotations:
        angle = rotation.get("angle")
        rotation_hashes = rotation.get("hashes")
        if not rotation_hashes:
            continue
        rotation_matches = searcher.search_hashes(
            rotation_hashes, threshold=threshold, max_results=max_results
        )
        for match in rotation_matches:
            path = match["path"]
            best = best_by_path.get(path)
            if best is None or match["score"] > best["score"]:
                match["angle"] = angle
                best_by_path[path] = match

    matches = sorted(best_by_path.values(), key=lambda m: m["score"], reverse=True)[:max_results]

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
        "card_id"  : "123",
        "lat"      : 46.749,
        "lon"      : 5.620,
        "accuracy" : 12.5   (optionnel, précision GPS en mètres)
      }

    L'écriture dans ``updates.json`` est protégée par un lockfile
    ``updates.json.lck`` : si ce fichier existe, on attend jusqu'à 10
    secondes qu'il disparaisse avant d'écrire (protection contre les
    écritures concurrentes depuis plusieurs workers gunicorn).

    En cas de succès, enregistre le repérage dans datadir/updates.json :
      { "card_id", "email", "lat", "lon", "accuracy", "ts" (timestamp UNIX) }
      ("accuracy" vaut ``None`` si absent ou invalide dans la requête)

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

    try:
        accuracy = float(data["accuracy"])
    except (KeyError, ValueError, TypeError):
        accuracy = None

    ts = int(time.time())
    entry = {
        "card_id": card_id,
        "email": email,
        "lat": lat,
        "lon": lon,
        "accuracy": accuracy,
        "ts": ts,
    }

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


# ---------------------------------------------------------------------------
# Signalement / modération de contenu
# ---------------------------------------------------------------------------

def _report_rate_limit_key() -> str:
    """
    Limite par IP : POST /api/v1/report est public (aucun compte requis,
    pour ne pas décourager un signalement légitime), donc le seul frein
    aux abus est le débit par adresse — comme pour les routes push du
    master (voir kartotek_master.push_api._push_token_key).
    """
    return get_remote_address()


@bp.route("/api/v1/report", methods=["POST"])
@limiter.limit("10 per minute;60 per hour", key_func=_report_rate_limit_key)
def report_card():
    """
    Signale un problème sur une carte postale (mauvaise géolocalisation,
    contenu inapproprié, doublon, atteinte à des droits, ...) — bouton
    "signaler" côté appli mobile, sur la fiche carte ou la vue "ici".

    Public, sans authentification : n'importe quel utilisateur peut
    signaler une carte, y compris un utilisateur non-manager. Seule la
    consultation/le traitement des signalements (voir GET /api/v1/reports
    et POST /api/v1/reports/<id>/resolve) est réservée aux managers.

    Corps JSON :
      {
        "card_id": "123",
        "reason": "wrong_location" | "inappropriate_content" | "duplicate" | "copyright" | "other",
        "comment": "..."     (optionnel, 500 caractères max)
      }
    (voir GET /api/v1/capabilities -> "reporting.reasons" pour la liste
    à jour, à ne jamais coder en dur côté client mobile)

    Le signalement est ajouté à datadir/reports.json (protégé par un
    verrou reports.json.lck, voir flpostcards.jsonlock.LockedJsonFile —
    même mécanisme que push_tokens.json), pour être traité par
    l'administrateur du site (via GET /api/v1/reports, ou plus tard
    KartoTek App). Aucune adresse IP ni identifiant de l'auteur n'est
    conservé dans ce fichier : seule une limite de débit en mémoire
    s'appuie sur l'IP, un signalement n'engage pas son auteur.

    Réponse (201) : { "status": "ok", "report_id": "<uuid4 hex>" }
    Erreurs :
      400 { "error": "..." }  — card_id/reason manquant ou invalide, commentaire trop long
      404 { "error": "..." }  — card_id inconnu sur ce serveur
      503 { "error": "..." }  — verrou reports.json toujours pris après le délai (voir LOCK_TIMEOUT)
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    card_id = str(data.get("card_id", "")).strip()
    if not card_id:
        return jsonify({"error": "card_id est obligatoire"}), 400

    reason = str(data.get("reason", "")).strip().lower()
    if reason not in _VALID_REPORT_REASONS:
        return jsonify({
            "error": f"reason doit être l'un de {sorted(_VALID_REPORT_REASONS)}"
        }), 400

    comment = str(data.get("comment") or "").strip()
    if len(comment) > _REPORT_COMMENT_MAX_LEN:
        return jsonify({
            "error": f"comment dépasse {_REPORT_COMMENT_MAX_LEN} caractères"
        }), 400

    if current_app.model.get_card(card_id) is None:
        return jsonify({"error": f"carte {card_id!r} inconnue sur ce serveur"}), 404

    entry = {
        "id": uuid.uuid4().hex,
        "card_id": card_id,
        "reason": reason,
        "comment": comment or None,
        "ts": int(time.time()),
        "status": "pending",
        "resolved_by": None,
        "resolved_ts": None,
    }

    datadir = Path(current_app.config["DATADIR"])
    lock_suffix = current_app.config.get("LOCK_SUFFIX", ".lck")
    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    poll = current_app.config.get("LOCK_POLL_INTERVAL", 2.0)

    try:
        with LockedJsonFile(
            datadir / "reports.json", default={"reports": []},
            lock_suffix=lock_suffix, timeout=timeout, poll_interval=poll,
        ) as f:
            f.data.setdefault("reports", []).append(entry)
    except TimeoutError as exc:
        current_app.logger.error("report : %s", exc)
        return jsonify({"error": str(exc)}), 503

    current_app.logger.info(
        "report : carte %s signalée (%s), report_id=%s (from=%s)",
        card_id, reason, entry["id"], request.remote_addr,
    )

    return jsonify({"status": "ok", "report_id": entry["id"]}), 201


@bp.route("/api/v1/reports")
@require_auth
def list_reports(auth_email: str):
    """
    Liste les signalements enregistrés sur ce serveur (managers
    uniquement — protégé par @require_auth, comme /api/v1/similar et
    /api/v1/update ; il n'existe pas de rôle "manager" séparé du rôle
    "compte authentifié", voir model.list_auths()).

    Query string :
      status : "pending" (défaut) | "resolved" | "all"

    Réponse (200) : liste des signalements (du plus récent au plus
    ancien), chacun sous la forme produite par POST /api/v1/report,
    avec en plus "status"/"resolved_by"/"resolved_ts" tenus à jour par
    POST /api/v1/reports/<id>/resolve.

    Erreurs :
      401 { "error": "unauthorized" }  — access token absent, invalide ou expiré
      400 { "error": "..." }           — status invalide
    """
    status_filter = request.args.get("status", "pending").strip().lower()
    if status_filter not in {"pending", "resolved", "all"}:
        return jsonify({"error": "status doit être 'pending', 'resolved' ou 'all'"}), 400

    datadir = Path(current_app.config["DATADIR"])
    reports = read_json(datadir / "reports.json", {"reports": []}).get("reports", [])

    if status_filter != "all":
        reports = [r for r in reports if r.get("status") == status_filter]

    return jsonify(sorted(reports, key=lambda r: r.get("ts", 0), reverse=True))


@bp.route("/api/v1/reports/<report_id>/resolve", methods=["POST"])
@require_auth
def resolve_report(report_id: str, auth_email: str):
    """
    Marque un signalement comme traité (managers uniquement). Aucun
    effet de bord sur la carte elle-même : c'est à l'administrateur de
    corriger la géolocalisation/le contenu via KartoTek App, puis de
    "clore" le signalement ici une fois l'action faite en local.

    Réponse (200) : { "status": "ok" }
    Erreurs :
      401 { "error": "unauthorized" }  — access token absent, invalide ou expiré
      404 { "error": "..." }           — report_id inconnu
      503 { "error": "..." }           — verrou reports.json toujours pris après le délai
    """
    datadir = Path(current_app.config["DATADIR"])
    lock_suffix = current_app.config.get("LOCK_SUFFIX", ".lck")
    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    poll = current_app.config.get("LOCK_POLL_INTERVAL", 2.0)

    try:
        with LockedJsonFile(
            datadir / "reports.json", default={"reports": []},
            lock_suffix=lock_suffix, timeout=timeout, poll_interval=poll,
        ) as f:
            reports = f.data.setdefault("reports", [])
            match = next((r for r in reports if r.get("id") == report_id), None)
            if match is None:
                return jsonify({"error": f"report {report_id!r} inconnu"}), 404
            match["status"] = "resolved"
            match["resolved_by"] = auth_email
            match["resolved_ts"] = int(time.time())
    except TimeoutError as exc:
        return jsonify({"error": str(exc)}), 503

    current_app.logger.info(
        "report : %s résolu par %s", report_id, auth_email,
    )
    return jsonify({"status": "ok"})


@bp.route("/api/v1/metrics")
@require_auth
def metrics(auth_email: str):
    """
    Instantané de télémétrie légère (managers uniquement — mêmes
    accès que /api/v1/reports) : nombre de requêtes par endpoint,
    répartition par classe de statut HTTP, latence moyenne/max.

    Voir flpostcards.metrics pour la portée et les limites (compteurs
    en mémoire, propres à CE worker, remis à zéro à chaque redémarrage
    — pas un remplacement d'un vrai APM).

    Réponse (200) : voir flpostcards.metrics.snapshot().
    Erreurs :
      401 { "error": "unauthorized" }  — access token absent, invalide ou expiré
    """
    from flpostcards import metrics as metrics_module

    return jsonify(metrics_module.snapshot())
