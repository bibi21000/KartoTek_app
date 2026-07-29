"""
Exemple de blueprint de thème : ajoute une page qui n'existe pas dans
le cœur (voir flpostcards/theming.py). Doit définir un Blueprint
nommé `bp` -- c'est la seule contrainte.
"""
from flask import Blueprint, render_template

bp = Blueprint("theme_exemple", __name__)


@bp.route("/nouvelle-page")
def nouvelle_page():
    # Le template "custom/nouvelle_page.html" doit exister dans
    # themes/exemple/templates/ (à créer, non fourni dans cet exemple
    # minimal).
    return "<h1>Nouvelle page ajoutée par le thème exemple</h1>"
