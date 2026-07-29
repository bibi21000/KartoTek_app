# Système de templates admin ("thèmes") — flpostcards

Système permettant à l'admin (jamais l'utilisateur final) de choisir, via
`postcards.conf`, un thème qui surcharge/complète les pages, le CSS/JS/images
et les traductions du cœur, et peut aussi ajouter de nouvelles pages avec de
la vraie logique Python.

## Configuration

```ini
[flask]
theme = mon_theme        ; nom du thème ; vide = aucun thème
themesdir = themes       ; optionnel, défaut "themes" (dossier externe, relatif au dossier de travail)
```

## Trois sources possibles, dans cet ordre de priorité

1. **Dossier externe** (`THEMESDIR/mon_theme`, par défaut `./themes` à côté
   de `postcards.conf`) : thème propre à un site, ou override local d'un
   thème embarqué/tiers sans toucher au code installé.
2. **Thème embarqué dans le paquet** (`flpostcards/themes/mon_theme/`) :
   livré avec l'application, disponible sans rien installer de plus. Un
   exemple (`exemple_embarque`) est fourni.
3. **Thème tiers, installé comme paquet Python séparé** (`pip install
   flpostcards-theme-xxx`), qui se déclare via un *entry point* du groupe
   `flpostcards.themes`. Voir `exemple-paquet-tiers/` pour un squelette
   complet, distribuable indépendamment de flpostcards.

Le même nom peut exister à plusieurs endroits : la source la plus prioritaire
gagne (utile pour tester une modification locale d'un thème tiers avant de
la reverser dans son paquet, par exemple).

## Structure d'un thème

Tout est optionnel : un thème ne fournit que ce qu'il veut changer.

```
mon_theme/
    theme.json          # métadonnées : name, description, author, url_prefix
    templates/           # fichiers qui surchargent/ajoutent des pages
        home/
            index.html    # écrase flpostcards/templates/home/index.html
    static/               # fichiers qui surchargent/ajoutent des assets
        theme.css          # référencé par un template surchargé
        icon.png            # surcharge le favicon (voir blueprints/home/icon())
    translations/         # traductions supplémentaires ou surchargées
        fr/LC_MESSAGES/flpostcards.po
    views.py               # blueprint optionnel : ajoute de nouvelles pages
```

### Surcharger une page

Recréer le même chemin relatif que dans `flpostcards/templates/` :
`mon_theme/templates/home/index.html` écrase
`flpostcards/templates/home/index.html`. Les pages non fournies par le thème
continuent d'utiliser le fichier du cœur (repli automatique).

### Ajouter des fichiers statiques (CSS/JS/images)

Même logique dans `mon_theme/static/` : un fichier présent côté thème est
servi en priorité (`/static/<chemin>`), sinon celui du cœur est servi.
Référencer ces fichiers normalement avec `url_for('static', filename=...)`
dans les templates du thème.

### Ajouter des traductions

Structure Babel standard, même domaine `flpostcards` :
`mon_theme/translations/<langue>/LC_MESSAGES/flpostcards.po` (à compiler en
`.mo` avec `pybabel compile`, ou `msgfmt`). Utile pour traduire les chaînes
des nouvelles pages du thème, ou pour surcharger une traduction du cœur.

### Ajouter des pages avec de la logique Python

`mon_theme/views.py` doit définir un `Blueprint` nommé `bp` :

```python
from flask import Blueprint, render_template

bp = Blueprint("mon_theme_pages", __name__)

@bp.route("/ma-nouvelle-page")
def ma_nouvelle_page():
    return render_template("custom/ma_page.html")
```

Ce blueprint est enregistré automatiquement après les blueprints du cœur.
Important : un thème **ajoute** des pages, il ne peut pas redéfinir une route
déjà enregistrée par le cœur (ça échouerait) — pour changer le comportement
d'une page existante, ne surcharger que son template.

Attention : `views.py` est du code Python exécuté avec les mêmes droits que
le reste de l'application (pas de bac à sable) — un thème doit donc être
traité comme un plugin choisi par l'admin, pas comme un contenu tiers
non fiable.

`theme.json` (`url_prefix`) permet de préfixer toutes les routes du
blueprint, ex. `"url_prefix": "/mon-theme"`.

## Distribuer un thème comme paquet tiers

Voir `exemple-paquet-tiers/` : un mini-paquet Python (`pyproject.toml` +
module) qui déclare son thème via :

```toml
[project.entry-points."flpostcards.themes"]
noel = "flpostcards_theme_noel:theme_dir"
```

`theme_dir` est une fonction sans argument qui retourne le `Path` du dossier
contenant `templates/`, `static/`, `translations/`, `views.py` (une fonction
plutôt qu'un chemin statique, pour rester fiable une fois le paquet
réellement installé). Une fois `pip install`é, il suffit d'écrire `theme =
noel` dans `postcards.conf` — flpostcards le trouve automatiquement, sans
rien copier dans un dossier `themes/`.

## Fichiers modifiés/ajoutés dans flpostcards

- `flpostcards/theming.py` (nouveau) : toute la logique (résolution du
  thème sur les 3 sources par ordre de priorité, repli Jinja, repli static,
  traductions, chargement du blueprint, `list_available_themes` pour du
  diagnostic).
- `flpostcards/themes/exemple_embarque/` (nouveau) : exemple de thème
  embarqué dans le paquet.
- `flpostcards/__init__.py` : lecture de `[flask] theme` / `themesdir`,
  app construite avec `static_folder=None` puis route `/static` enregistrée
  via `theming.register_static_route`, loader Jinja étendu via
  `theming.apply_templates`, blueprint du thème enregistré après ceux du
  cœur.
- `flpostcards/blueprints/home/__init__.py` : la route favicon
  (`/favicon.ico`, `/icon.png`) regarde d'abord `static/icon.(png|jpg|jpeg)`
  côté thème avant de retomber sur celui du cœur.
- `pyproject.toml` : ajout de `"themes/**/*"` aux données de paquet
  `flpostcards`, pour que les thèmes embarqués soient bien inclus dans la
  distribution.

Testé (scripts manuels, hors suite de tests du projet — aucun dossier
`tests/` n'était présent dans l'archive fournie) : surcharge de page, page
non surchargée qui retombe sur le cœur, fichier statique surchargé et
ajouté, thème absent (comportement identique à avant), nom de thème invalide
(warning listant les 2 emplacements essayés + repli propre), nouvelle page
via blueprint de thème, thème embarqué résolu correctement, priorité
dossier externe > embarqué (même nom des deux côtés, l'externe gagne), thème
tiers résolu via un point d'entrée simulé, et `list_available_themes`
combinant bien les trois sources sans doublon.

## Limites connues / pistes non retenues

- Un thème ne peut pas redéfinir la logique d'une route existante, seulement
  son template, son CSS/JS/images, ses traductions, ou en ajouter de
  nouvelles — cf. discussion plus haut.
- L'ordre de fusion des traductions (thème avant cœur) fonctionne dans les
  tests mais mérite d'être revérifié avec de vraies chaînes traduites
  différentes entre thème et cœur dans votre environnement Flask-Babel
  précis, avant mise en prod.
- Pas de validation de schéma sur `theme.json` : clés inconnues ignorées
  silencieusement, à surveiller si vous étoffez les métadonnées plus tard.
- Résolution des thèmes tiers basée sur `importlib.metadata.entry_points()` :
  suppose un paquet installé normalement (pas testé en zipapp/zippé) ; le
  callable `theme_dir()` recommandé (plutôt qu'un `Path` statique en dur
  dans le `pyproject.toml`) limite ce risque mais ne l'élimine pas
  totalement pour des installations exotiques.

