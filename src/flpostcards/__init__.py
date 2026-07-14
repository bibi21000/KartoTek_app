"""
flpostcards - Application Flask de consultation des cartes postales.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path

from flask import Flask, request
from flask_babel import Babel
from werkzeug.middleware.proxy_fix import ProxyFix

from libpostcards.model import Model
from flpostcards.extensions import limiter

# Langues disponibles pour l'application Flask
LANGUAGES = ["fr", "en"]


def select_locale() -> str:
    """Détermine la langue à utiliser pour la requête courante."""
    return request.accept_languages.best_match(LANGUAGES) or "fr"


def load_config(app: Flask, config_path: str | Path = "postcards.conf") -> None:
    """Charge postcards.conf (section DEFAULT + section [flask])."""
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(config_path)

    datadir = parser.get("DEFAULT", "datadir", fallback="datadir")
    app.config["DATADIR"] = Path(datadir).resolve()

    # Paramètres de verrouillage fichier (lockfile) pour updates.json
    app.config["LOCK_SUFFIX"] = parser.get(
        "DEFAULT", "lock_suffix", fallback=".lck"
    ).strip()
    app.config["LOCK_POLL_INTERVAL"] = parser.getfloat(
        "DEFAULT", "lock_poll_interval", fallback=2.0
    )
    app.config["LOCK_TIMEOUT"] = parser.getfloat(
        "DEFAULT", "lock_timeout", fallback=60.0
    )

    # Liste des collections connues (définie dans [DEFAULT], accessible
    # depuis [flask] via l'héritage configparser)
    collections_raw = parser.get("DEFAULT", "collections", fallback="")
    app.config["COLLECTIONS"] = [
        c.strip() for c in collections_raw.split(",") if c.strip()
    ]

    # Sous-ensemble des collections proposées comme filtre sur la carte
    # (/map/) ; à défaut, retombe sur la liste complète des collections.
    # Accepte aussi "collections_maps" (variante avec 's') par tolérance.
    collections_map_raw = parser.get(
        "DEFAULT", "collections_map", fallback=""
    ) or parser.get("DEFAULT", "collections_maps", fallback="")
    if collections_map_raw.strip():
        app.config["COLLECTIONS_MAP"] = [
            c.strip() for c in collections_map_raw.split(",") if c.strip()
        ]
    else:
        app.config["COLLECTIONS_MAP"] = app.config["COLLECTIONS"]

    if parser.has_section("flask"):
        defaults = set(parser.defaults().keys())
        for key, value in parser.items("flask"):
            if key in defaults:
                # Clé héritée de [DEFAULT] (ex: datadir), déjà traitée
                continue
            if key == "debug":
                app.config["DEBUG"] = parser.getboolean("flask", "debug")
            elif key == "port":
                app.config["PORT"] = parser.getint("flask", "port")
            elif key == "secret_key":
                app.config["SECRET_KEY"] = value
            elif key == "recent_days":
                app.config["RECENT_DAYS"] = parser.getint("flask", "recent_days")
            elif key == "recent_fallback_count":
                app.config["RECENT_FALLBACK_COUNT"] = parser.getint(
                    "flask", "recent_fallback_count"
                )
            elif key == "smtp_port":
                app.config["SMTP_PORT"] = parser.getint("flask", "smtp_port")
            elif key == "similar_default_threshold":
                app.config["SIMILAR_DEFAULT_THRESHOLD"] = parser.getfloat(
                    "flask", "similar_default_threshold"
                )
            elif key == "similar_max_results":
                app.config["SIMILAR_MAX_RESULTS"] = parser.getint(
                    "flask", "similar_max_results"
                )
            elif key == "similar_timeout_s":
                app.config["SIMILAR_TIMEOUT_S"] = parser.getfloat(
                    "flask", "similar_timeout_s"
                )
            elif key == "jwt_access_ttl_s":
                app.config["JWT_ACCESS_TTL_S"] = parser.getint(
                    "flask", "jwt_access_ttl_s"
                )
            elif key == "jwt_refresh_ttl_s":
                app.config["JWT_REFRESH_TTL_S"] = parser.getint(
                    "flask", "jwt_refresh_ttl_s"
                )
            elif key == "trusted_proxies":
                app.config["TRUSTED_PROXIES"] = parser.getint(
                    "flask", "trusted_proxies"
                )
            elif key == "rate_limit_storage_uri":
                app.config["RATELIMIT_STORAGE_URI"] = value
            elif key == "rate_limit_key_prefix":
                app.config["RATELIMIT_KEY_PREFIX"] = value
            else:
                app.config[key.upper()] = value

    # Identifiant du site (siteId) pour le suivi Matomo, utilisé dans
    # base.html avec [flask] site_matomo. Clé de configuration :
    # [flask] id_matomo (1 par défaut si non renseigné).
    app.config.setdefault("ID_MATOMO", "1")

    app.config.setdefault("RECENT_DAYS", 30)
    app.config.setdefault("RECENT_FALLBACK_COUNT", 20)
    app.config.setdefault("SMTP_PORT", 587)
    # URL du service simpostcards (POST /api/compute_hashes), utilisé
    # par /api/v1/similar pour obtenir les hashs d'une photo envoyée
    # par l'appli mobile, sans avoir à embarquer OpenCV côté flpostcards.
    # Clé de configuration : [flask] similar_server
    app.config.setdefault("SIMILAR_SERVER", "http://simpostcards:8004")
    app.config.setdefault("SIMILAR_DEFAULT_THRESHOLD", 70.0)
    app.config.setdefault("SIMILAR_MAX_RESULTS", 20)
    app.config.setdefault("SIMILAR_TIMEOUT_S", 30.0)
    # Auth JWT (flpostcards/auth.py) : durée de vie de l'access token
    # (15 min) et du refresh token (30 jours) par défaut.
    app.config.setdefault("JWT_ACCESS_TTL_S", 15 * 60)
    app.config.setdefault("JWT_REFRESH_TTL_S", 30 * 86400)
    # Nombre de reverse proxies "de confiance" en amont de flpostcards
    # (ex : BunkerWeb = 1) — voir ProxyFix dans create_app(). Détermine
    # combien de valeurs, en partant de la droite, sont prises en
    # compte dans des en-têtes potentiellement à plusieurs valeurs
    # comme X-Forwarded-Proto/X-Forwarded-For.
    app.config.setdefault("TRUSTED_PROXIES", 1)
    # Backend de comptage pour le rate limiting (flpostcards/extensions.py) :
    # "memory://" (défaut) suffit en dev ou avec un seul worker, mais
    # chaque worker gunicorn a alors ses propres compteurs en mémoire
    # -> la limite réelle est multipliée par le nombre de workers.
    # En production avec plusieurs workers/instances, définir
    # [flask] rate_limit_storage_uri = redis://host:6379/0 (nécessite
    # le paquet Python "redis") pour un comptage partagé et fiable.
    app.config.setdefault("RATELIMIT_STORAGE_URI", "memory://")
    # Préfixe des clés de rate limiting (flask-limiter, config
    # RATELIMIT_KEY_PREFIX) : indispensable si plusieurs serveurs
    # flpostcards distincts (plusieurs sites KartoTek) partagent la
    # même instance Redis pour RATELIMIT_STORAGE_URI -- sans préfixe
    # différent par serveur, leurs compteurs se mélangeraient (ex :
    # un attaquant bloqué sur le serveur A épuiserait aussi le quota
    # du serveur B). Définir [flask] rate_limit_key_prefix = <nom du
    # site> pour chaque serveur.
    app.config.setdefault("RATELIMIT_KEY_PREFIX", "")

    secret_key = app.config.get("SECRET_KEY")
    if secret_key in (None, "", "secret"):
        app.logger.warning(
            "postcards.conf [flask] secret_key n'est pas défini (ou vaut "
            "encore la valeur d'exemple 'secret') : les access tokens JWT "
            "seraient signés avec un secret faible/public. À changer avant "
            "toute mise en production de l'authentification."
        )
    elif len(secret_key) < 32:
        app.logger.warning(
            "postcards.conf [flask] secret_key ne fait que %d caractères : "
            "trop court pour signer des JWT en HMAC-SHA256 en toute "
            "sécurité (32 caractères aléatoires minimum recommandés, ex. "
            "`python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"`).",
            len(secret_key),
        )


def create_app(config_path: str | Path = "postcards.conf") -> Flask:
    app = Flask(__name__)
    load_config(app, config_path)

    # Sans ça, seuls les WARNING+ remontent par défaut (que ce soit en
    # dev ou sous gunicorn) : les logs INFO de /api/v1/similar (taille
    # de la photo, timings, nombre de résultats — utiles pour
    # diagnostiquer un 502/timeout côté reverse proxy) resteraient
    # invisibles sans ce réglage explicite.
    app.logger.setLevel(logging.DEBUG if app.debug else logging.INFO)

    # Sans ça, Flask ignore les en-têtes X-Forwarded-* posés par un
    # reverse proxy (BunkerWeb, nginx, ...) qui termine le TLS devant
    # flpostcards : request.is_secure reste False et url_for(...,
    # _external=True) génère des URLs en http:// au lieu de https://
    # (cassait uri_div3/uri_div10 dans /api/v1/similar, /api/v1/news,
    # etc.). TRUSTED_PROXIES (postcards.conf [flask] trusted_proxies,
    # 1 par défaut) doit correspondre au nombre de proxies en amont ;
    # une valeur trop élevée permettrait à un en-tête X-Forwarded-*
    # forgé par le client d'être pris en compte à tort.
    trusted_proxies = app.config["TRUSTED_PROXIES"]
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxies,
        x_proto=trusted_proxies,
        x_host=trusted_proxies,
        x_port=trusted_proxies,
        x_prefix=trusted_proxies,
    )

    app.config.setdefault("LANGUAGES", LANGUAGES)
    app.config.setdefault("BABEL_DEFAULT_LOCALE", "fr")
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", "translations")
    app.config.setdefault("BABEL_DOMAIN", "flpostcards")

    babel = Babel(app, locale_selector=select_locale)

    @app.context_processor
    def inject_locale():
        from flask_babel import get_locale
        return {"get_locale": get_locale}

    @app.context_processor
    def inject_current_path():
        """Chemin courant (avec query string) sans le '?' final superflu."""
        path = request.full_path
        if path.endswith("?"):
            path = path[:-1]
        return {"current_path": path}

    limiter.init_app(app)

    if app.config["RATELIMIT_STORAGE_URI"] == "memory://" and not app.debug:
        app.logger.warning(
            "rate limiting : stockage en mémoire (memory://) utilisé hors "
            "debug -- avec plusieurs workers gunicorn, les compteurs ne "
            "sont pas partagés entre processus, ce qui affaiblit la "
            "limite réelle. Définir [flask] rate_limit_storage_uri "
            "(ex : redis://host:6379/0) pour un déploiement multi-workers."
        )
    elif (
        app.config["RATELIMIT_STORAGE_URI"].startswith("redis")
        and not app.config["RATELIMIT_KEY_PREFIX"]
    ):
        app.logger.warning(
            "rate limiting : rate_limit_storage_uri pointe vers Redis mais "
            "[flask] rate_limit_key_prefix n'est pas défini -- si cette "
            "instance Redis est partagée entre plusieurs serveurs "
            "flpostcards, leurs compteurs de rate limiting vont se "
            "mélanger. Définir un préfixe distinct par serveur (ex : nom "
            "du site) si c'est le cas."
        )

    # Modèle partagé (lecture uniquement côté Flask)
    app.model = Model(app.config["DATADIR"])

    from flpostcards.blueprints.home import bp as home_bp
    app.register_blueprint(home_bp)

    from flpostcards.blueprints.gallery import bp as gallery_bp
    app.register_blueprint(gallery_bp)

    from flpostcards.blueprints.travel import bp as travel_bp
    app.register_blueprint(travel_bp)

    from flpostcards.blueprints.map import bp as map_bp
    app.register_blueprint(map_bp)

    from flpostcards.blueprints.slideshow import bp as slideshow_bp
    app.register_blueprint(slideshow_bp)

    from flpostcards.blueprints.api import bp as api_bp
    app.register_blueprint(api_bp)

    from flpostcards.blueprints.contact import bp as contact_bp
    app.register_blueprint(contact_bp)

    return app
