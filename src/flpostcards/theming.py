"""
Système de templates admin ("thèmes") pour flpostcards.

Un thème, choisi par l'admin via ``[flask] theme`` dans postcards.conf
(jamais par l'utilisateur final), peut venir de trois endroits, dans
cet ordre de priorité :

1. un dossier externe, propre au site (``THEMESDIR/<theme>``, par
   défaut ``./themes`` à côté de postcards.conf) -- pour un thème
   spécifique à une installation, ou pour surcharger localement un
   thème embarqué/tiers sans toucher au code installé ;
2. un thème embarqué dans le paquet flpostcards lui-même
   (``flpostcards/themes/<theme>``) -- livré avec l'application ;
3. un thème tiers, installé comme paquet Python séparé (ex. via pip),
   qui déclare un point d'entrée dans le groupe
   ``flpostcards.themes`` -- voir ``_entry_point_theme_dir`` plus bas.

Un thème peut, de façon complètement optionnelle (aucun sous-dossier
n'est obligatoire) :

- surcharger une, plusieurs ou toutes les pages : ``templates/`` reprend
  les mêmes chemins relatifs que ``flpostcards/templates/`` (ex :
  ``templates/home/index.html``) pour les fichiers à remplacer ;
- surcharger ou ajouter des fichiers statiques (CSS, JS, images) dans
  ``static/``, avec repli fichier par fichier sur le static du cœur ;
- ajouter ou surcharger des chaînes traduites dans ``translations/``,
  même domaine ``flpostcards`` que le cœur (structure Babel standard :
  ``translations/<lang>/LC_MESSAGES/flpostcards.po``) ;
- ajouter de nouvelles pages avec de la vraie logique Python, via un
  ``views.py`` définissant un Blueprint nommé ``bp`` -- enregistré tel
  quel, avec les mêmes droits que le reste de l'application. Un thème
  n'est donc pas un contenu "sandboxé" : c'est un choix de l'admin, au
  même titre qu'un plugin.

Structure d'un thème (tout est optionnel) ::

    mon_theme/
        theme.json          # métadonnées: name, description, author, url_prefix
        templates/           # fichiers qui surchargent/ajoutent des pages
        static/               # fichiers qui surchargent/ajoutent des assets
        translations/         # traductions supplémentaires ou surchargées
        views.py               # blueprint optionnel (`bp = Blueprint(...)`)

Voir ``create_app()`` dans ``flpostcards/__init__.py`` pour l'intégration.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, Flask, send_from_directory
from werkzeug.exceptions import NotFound
from werkzeug.utils import safe_join

# Groupe de points d'entrée (setuptools) sous lequel un paquet Python
# tiers déclare un thème installable séparément, ex. dans son
# pyproject.toml ::
#
#     [project.entry-points."flpostcards.themes"]
#     mon_theme = "flpostcards_theme_mon_theme:theme_dir"
#
# La cible du point d'entrée doit être un Path/str (chemin absolu vers
# le dossier du thème), ou un callable sans argument retournant un
# Path/str -- pratique si le chemin doit être résolu dynamiquement,
# par exemple via importlib.resources pour rester compatible avec un
# paquet zippé.
THEME_ENTRY_POINT_GROUP = "flpostcards.themes"


@dataclass
class Theme:
    name: str
    dir: Path
    source: str
    templates_dir: Path | None
    static_dir: Path | None
    translations_dir: Path | None
    views_path: Path | None
    metadata: dict = field(default_factory=dict)


def _entry_point_theme_dir(name: str) -> Path | None:
    """
    Résout un thème tiers installé comme paquet Python séparé, déclaré
    dans le groupe de points d'entrée ``flpostcards.themes`` (voir plus
    haut). Retourne ``None`` si aucun paquet installé ne déclare ce nom,
    ou si sa résolution échoue (loggué en warning par l'appelant via le
    contexte, pas ici, pour ne pas dupliquer les messages entre
    candidats testés).
    """
    try:
        eps = importlib.metadata.entry_points()
        if hasattr(eps, "select"):
            candidates = list(eps.select(group=THEME_ENTRY_POINT_GROUP, name=name))
        else:  # pragma: no cover - Python < 3.10
            candidates = [
                ep for ep in eps.get(THEME_ENTRY_POINT_GROUP, []) if ep.name == name
            ]
    except Exception:
        return None

    for ep in candidates:
        try:
            target = ep.load()
            path = target() if callable(target) else target
            return Path(path)
        except Exception:
            continue
    return None


def _iter_theme_candidates(app: Flask, name: str):
    """
    Génère les emplacements possibles pour le thème ``name``, dans
    l'ordre de priorité décrit en tête de module : dossier externe de
    l'admin, thème embarqué dans le paquet, thème tiers via point
    d'entrée. Chaque élément est un couple (dossier, libellé de source)
    -- le dossier n'est pas garanti d'exister, à vérifier par l'appelant.
    """
    themes_dir = Path(app.config.get("THEMESDIR", "themes"))
    yield themes_dir / name, "dossier externe"

    yield Path(__file__).parent / "themes" / name, "embarqué"

    entry_point_dir = _entry_point_theme_dir(name)
    if entry_point_dir is not None:
        yield entry_point_dir, "paquet tiers (point d'entrée)"


def load_theme(app: Flask) -> Theme | None:
    """
    Résout le thème actif défini par l'admin (``[flask] theme`` dans
    postcards.conf), en cherchant successivement un dossier externe, un
    thème embarqué, puis un thème tiers installé (voir
    ``_iter_theme_candidates``). Retourne ``None`` si aucun thème n'est
    configuré, ou si aucune des trois sources ne le fournit -- dans ce
    dernier cas avec un warning listant les emplacements essayés, pour
    ne pas échouer silencieusement sur une faute de frappe ou un
    paquet tiers manquant.
    """
    name = (app.config.get("THEME") or "").strip()
    if not name:
        return None

    theme_dir: Path | None = None
    source = ""
    tried: list[str] = []
    for candidate_dir, candidate_source in _iter_theme_candidates(app, name):
        tried.append(f"{candidate_dir} ({candidate_source})")
        if candidate_dir.is_dir():
            theme_dir = candidate_dir
            source = candidate_source
            break

    if theme_dir is None:
        app.logger.warning(
            "postcards.conf [flask] theme=%r : introuvable -- essayé : %s -- "
            "aucun thème appliqué, retour aux templates/static/traductions "
            "par défaut du cœur.",
            name,
            "; ".join(tried),
        )
        return None

    def _existing_dir(sub: str) -> Path | None:
        candidate = theme_dir / sub
        return candidate if candidate.is_dir() else None

    metadata: dict = {}
    metadata_path = theme_dir / "theme.json"
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            app.logger.warning(
                "Thème %r : theme.json illisible (%s), métadonnées ignorées.",
                name,
                exc,
            )

    views_path = theme_dir / "views.py"
    if not views_path.is_file():
        views_path = None

    theme = Theme(
        name=name,
        dir=theme_dir,
        source=source,
        templates_dir=_existing_dir("templates"),
        static_dir=_existing_dir("static"),
        translations_dir=_existing_dir("translations"),
        views_path=views_path,
        metadata=metadata,
    )

    description = metadata.get("description")
    app.logger.info(
        "Thème actif : %r (%s, source=%s)%s",
        name,
        theme_dir,
        source,
        f" -- {description}" if description else "",
    )
    return theme


def list_available_themes(app: Flask) -> dict[str, str]:
    """
    Recense les thèmes disponibles (nom -> libellé de source), en
    combinant les trois sources : dossier externe (THEMESDIR),
    thèmes embarqués (flpostcards/themes/), et thèmes tiers installés
    (points d'entrée du groupe ``flpostcards.themes``). Utile pour du
    diagnostic (log au démarrage, future commande CLI listant les
    thèmes utilisables) -- n'est pas utilisé par ``load_theme`` lui-même,
    qui ne regarde que le thème effectivement sélectionné.

    En cas de nom en doublon entre sources, la source la plus
    prioritaire l'emporte dans le résultat (même ordre que
    ``_iter_theme_candidates``).
    """
    found: dict[str, str] = {}

    themes_dir = Path(app.config.get("THEMESDIR", "themes"))
    if themes_dir.is_dir():
        for entry in sorted(themes_dir.iterdir()):
            if entry.is_dir():
                found.setdefault(entry.name, "dossier externe")

    bundled_dir = Path(__file__).parent / "themes"
    if bundled_dir.is_dir():
        for entry in sorted(bundled_dir.iterdir()):
            if entry.is_dir():
                found.setdefault(entry.name, "embarqué")

    try:
        eps = importlib.metadata.entry_points()
        candidates = (
            eps.select(group=THEME_ENTRY_POINT_GROUP)
            if hasattr(eps, "select")
            else eps.get(THEME_ENTRY_POINT_GROUP, [])
        )
        for ep in candidates:
            found.setdefault(ep.name, "paquet tiers (point d'entrée)")
    except Exception:
        pass

    return found


def translation_directories(app: Flask, theme: Theme | None) -> str:
    """
    Construit la valeur de BABEL_TRANSLATION_DIRECTORIES : le dossier
    translations/ du thème (s'il existe) en premier, celui du cœur en
    second. Flask-Babel/Babel cherche, pour chaque entrée de la liste
    dans l'ordre, un catalogue du domaine "flpostcards" pour la langue
    courante et s'arrête au premier trouvé -- mettre le thème en tête
    permet donc à ses traductions (mêmes clés que le cœur, ou clés
    propres à ses nouvelles pages) de prendre le dessus.
    """
    core_dir = Path(app.root_path) / "translations"
    dirs = [str(core_dir)]
    if theme is not None and theme.translations_dir is not None:
        dirs.insert(0, str(theme.translations_dir))
    return ";".join(dirs)


def apply_templates(app: Flask, theme: Theme | None) -> None:
    """
    Fait passer le loader Jinja de l'app par le dossier templates/ du
    thème en priorité (surcharge), avec repli automatique sur les
    templates du cœur/des blueprints si le fichier n'existe pas côté
    thème (ajout ou page non surchargée).
    """
    if theme is None or theme.templates_dir is None:
        return

    from jinja2 import ChoiceLoader, FileSystemLoader

    app.jinja_env.loader = ChoiceLoader(
        [FileSystemLoader(str(theme.templates_dir)), app.jinja_env.loader]
    )


def register_static_route(app: Flask, theme: Theme | None) -> None:
    """
    Enregistre "à la main" la route ``/static/<path:filename>`` --
    l'app est construite avec ``static_folder=None`` (voir
    ``create_app``) pour qu'on puisse faire un repli fichier par
    fichier entre le static/ du thème actif (prioritaire) et le
    static/ du cœur : un thème qui ne surcharge que ``style.css``
    continue de servir les images/JS du cœur sans les dupliquer, et
    peut aussi ajouter des fichiers absents du cœur.
    """
    core_static_dir = Path(app.root_path) / "static"
    theme_static_dir = theme.static_dir if theme is not None else None

    app.config["CORE_STATIC_DIR"] = core_static_dir
    app.config["THEME_STATIC_DIR"] = theme_static_dir

    # Conservé pour le code existant qui lit current_app.static_folder
    # (ex : icon_generator.find_uploaded_icon) : reste le dossier du
    # cœur -- l'éventuelle surcharge de l'icône par le thème est gérée
    # séparément dans blueprints/home/icon() (voir THEME_STATIC_DIR).
    app.static_folder = str(core_static_dir)

    def _serve_static(filename: str):
        if theme_static_dir is not None:
            safe_path = safe_join(str(theme_static_dir), filename)
            if safe_path is not None and Path(safe_path).is_file():
                return send_from_directory(theme_static_dir, filename)
        try:
            return send_from_directory(core_static_dir, filename)
        except NotFound:
            raise

    app.add_url_rule(
        f"{app.static_url_path}/<path:filename>",
        endpoint="static",
        view_func=_serve_static,
    )


def register_theme_blueprint(app: Flask, theme: Theme) -> None:
    """
    Charge dynamiquement ``themes/<theme>/views.py`` et enregistre le
    Blueprint ``bp`` qui y est défini, s'il existe. C'est le mécanisme
    permettant à un thème d'ajouter des pages avec de la vraie logique
    Python (pas seulement des templates statiques). Un thème place ses
    propres templates dans son dossier ``templates/`` : le
    ChoiceLoader mis en place par ``apply_templates`` les y trouve déjà,
    ``bp`` n'a donc pas besoin de déclarer son propre ``template_folder``.

    Erreurs de chargement (fichier invalide, exception à l'import, pas
    de ``bp``) : on logue un warning/exception et on ignore le
    blueprint plutôt que de faire planter tout le serveur pour un
    thème mal formé.
    """
    if theme.views_path is None:
        return

    module_name = f"flpostcards._themes.{theme.name}"
    spec = importlib.util.spec_from_file_location(module_name, theme.views_path)
    if spec is None or spec.loader is None:
        app.logger.warning(
            "Thème %r : impossible de charger views.py, ignoré.", theme.name
        )
        return

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        app.logger.exception(
            "Thème %r : erreur au chargement de views.py, blueprint ignoré.",
            theme.name,
        )
        sys.modules.pop(module_name, None)
        return

    bp = getattr(module, "bp", None)
    if not isinstance(bp, Blueprint):
        app.logger.warning(
            "Thème %r : views.py ne définit pas de Blueprint nommé `bp`, ignoré.",
            theme.name,
        )
        return

    url_prefix = theme.metadata.get("url_prefix") or None
    try:
        app.register_blueprint(bp, url_prefix=url_prefix)
    except Exception:
        app.logger.exception(
            "Thème %r : erreur à l'enregistrement du blueprint %r, ignoré.",
            theme.name,
            bp.name,
        )
        return

    app.logger.info(
        "Thème %r : blueprint %r enregistré (url_prefix=%r).",
        theme.name,
        bp.name,
        url_prefix,
    )
