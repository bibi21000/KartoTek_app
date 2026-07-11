"""
simpostcards - Micro-appli Flask de calcul de hashs de cartes postales.

Un seul point d'entrée API : POST /api/compute_hashes (voir
``simpostcards.blueprints.api``).

Étant donné une image de carte postale (recto ou verso), l'appli :
  1. la redresse et la détoure (``libpostcards.scan_corrector``) ;
  2. calcule ses hashs perceptuels (``simpostcards.libs.hasher``,
     mêmes hashs que ``libpostcards.similar``) ;
  3. renvoie le résultat en JSON.

Aucune donnée n'est persistée : simpostcards ne lit ni n'écrit dans
datadir/, ne touche pas à postcards.sqlite et n'a pas besoin de
``libpostcards.model.Model``. Elle est indépendante de flpostcards et
peut être déployée séparément (par exemple pour être appelée par
tkpostcards ou par un script de publication).
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path

from flask import Flask


def load_config(app: Flask, config_path: str | Path = "postcards.conf") -> None:
    """Charge postcards.conf (section [simpostcards], héritant de [DEFAULT])."""
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(config_path)

    if parser.has_section("simpostcards"):
        defaults = set(parser.defaults().keys())
        for key, value in parser.items("simpostcards"):
            if key in defaults:
                # Clé héritée de [DEFAULT], non pertinente ici
                continue
            if key == "debug":
                app.config["DEBUG"] = parser.getboolean("simpostcards", "debug")
            elif key == "port":
                app.config["PORT"] = parser.getint("simpostcards", "port")
            elif key == "host":
                app.config["HOST"] = value
            elif key == "secret_key":
                app.config["SECRET_KEY"] = value
            elif key == "white_threshold":
                app.config["SCAN_WHITE_THRESHOLD"] = parser.getint(
                    "simpostcards", "white_threshold"
                )
            elif key == "max_content_length_mb":
                app.config["MAX_CONTENT_LENGTH"] = (
                    parser.getint("simpostcards", "max_content_length_mb") * 1024 * 1024
                )
            else:
                app.config[key.upper()] = value

    app.config.setdefault("HOST", "127.0.0.1")
    app.config.setdefault("PORT", 8002)
    app.config.setdefault("SCAN_WHITE_THRESHOLD", 240)
    # Limite par défaut : 25 Mo, largement suffisant pour un scan de
    # carte postale (tiff non compressé compris) tout en évitant
    # qu'une requête abusive ne consomme trop de mémoire/CPU.
    app.config.setdefault("MAX_CONTENT_LENGTH", 25 * 1024 * 1024)


def create_app(config_path: str | Path = "postcards.conf") -> Flask:
    app = Flask(__name__)
    load_config(app, config_path)

    # Sans ça, seuls les WARNING+ remontent par défaut : les logs INFO
    # de compute_hashes (taille de l'image reçue, timing du
    # redressement + hashs) resteraient invisibles sous gunicorn.
    app.logger.setLevel(logging.DEBUG if app.debug else logging.INFO)

    from simpostcards.blueprints.api import bp as api_bp
    app.register_blueprint(api_bp)

    return app
