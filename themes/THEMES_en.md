# Admin template system ("themes") — flpostcards

A system letting the admin (never the end user) choose, via `postcards.conf`,
a theme that overrides/extends pages, CSS/JS/images, and translations from
the core, and can also add brand-new pages with real Python logic.

## Configuration

```ini
[flask]
theme = my_theme         ; theme name ; empty = no theme
themesdir = themes       ; optional, defaults to "themes" (external folder, relative to the working directory)
```

## Three possible sources, in this priority order

1. **External folder** (`THEMESDIR/my_theme`, defaults to `./themes` next to
   `postcards.conf`): a theme specific to one site, or a local override of a
   bundled/third-party theme without touching the installed code.
2. **Theme bundled with the package** (`flpostcards/themes/my_theme/`):
   shipped with the application, available with nothing extra to install. An
   example (`exemple_embarque`) is included.
3. **Third-party theme, installed as a separate Python package** (`pip
   install flpostcards-theme-xxx`), which registers itself via an *entry
   point* in the `flpostcards.themes` group. See `exemple-paquet-tiers/` for
   a complete skeleton, distributable independently of flpostcards.

The same name can exist in more than one place: the highest-priority source
wins (handy for testing a local tweak to a third-party theme before pushing
it back into its own package, for instance).

## Structure of a theme

Everything is optional: a theme only provides what it wants to change.

```
my_theme/
    theme.json          # metadata: name, description, author, url_prefix
    templates/           # files that override/add pages
        home/
            index.html    # overrides flpostcards/templates/home/index.html
    static/               # files that override/add assets
        theme.css          # referenced from an overridden template
        icon.png            # overrides the favicon (see blueprints/home/icon())
    translations/         # additional or overridden translations
        fr/LC_MESSAGES/flpostcards.po
    views.py               # optional blueprint: adds new pages
```

### Overriding a page

Recreate the same relative path as in `flpostcards/templates/`:
`my_theme/templates/home/index.html` overrides
`flpostcards/templates/home/index.html`. Pages not provided by the theme
keep using the core file (automatic fallback).

### Adding static files (CSS/JS/images)

Same logic in `my_theme/static/`: a file present on the theme's side is
served first (`/static/<path>`), otherwise the core one is served. Reference
these files normally with `url_for('static', filename=...)` in the theme's
templates.

### Adding translations

Standard Babel layout, same `flpostcards` domain:
`my_theme/translations/<lang>/LC_MESSAGES/flpostcards.po` (compile to `.mo`
with `pybabel compile`, or `msgfmt`). Useful for translating strings from
the theme's new pages, or for overriding a core translation.

### Adding pages with Python logic

`my_theme/views.py` must define a `Blueprint` named `bp`:

```python
from flask import Blueprint, render_template

bp = Blueprint("my_theme_pages", __name__)

@bp.route("/my-new-page")
def my_new_page():
    return render_template("custom/my_page.html")
```

This blueprint is registered automatically after the core blueprints.
Important: a theme **adds** pages, it cannot redefine a route already
registered by the core (that would fail) — to change the behavior of an
existing page, only override its template.

Note: `views.py` is Python code executed with the same privileges as the
rest of the application (no sandboxing) — a theme should therefore be
treated as a plugin chosen by the admin, not as untrusted third-party
content.

`theme.json` (`url_prefix`) lets you prefix all of the blueprint's routes,
e.g. `"url_prefix": "/my-theme"`.

## Distributing a theme as a third-party package

See `exemple-paquet-tiers/`: a minimal Python package (`pyproject.toml` +
module) that registers its theme via:

```toml
[project.entry-points."flpostcards.themes"]
noel = "flpostcards_theme_noel:theme_dir"
```

`theme_dir` is a no-argument function that returns the `Path` of the folder
containing `templates/`, `static/`, `translations/`, `views.py` (a function
rather than a static path, to stay reliable once the package is actually
installed). Once `pip install`ed, just write `theme = noel` in
`postcards.conf` — flpostcards finds it automatically, with nothing to copy
into a `themes/` folder.

## Files added/modified in flpostcards

- `flpostcards/theming.py` (new): all the logic (resolving the theme across
  the 3 sources in priority order, Jinja fallback, static fallback,
  translations, blueprint loading, `list_available_themes` for
  diagnostics).
- `flpostcards/themes/exemple_embarque/` (new): example of a theme bundled
  with the package.
- `flpostcards/__init__.py`: reads `[flask] theme` / `themesdir`, app built
  with `static_folder=None` then the `/static` route registered via
  `theming.register_static_route`, Jinja loader extended via
  `theming.apply_templates`, theme blueprint registered after the core
  ones.
- `flpostcards/blueprints/home/__init__.py`: the favicon route
  (`/favicon.ico`, `/icon.png`) first checks `static/icon.(png|jpg|jpeg)` on
  the theme's side before falling back to the core one.
- `pyproject.toml`: added `"themes/**/*"` to the `flpostcards` package data,
  so bundled themes are properly included in the distribution.

Tested (manual scripts, outside the project's test suite — no `tests/`
folder was present in the supplied archive): page override, an
unoverridden page falling back to the core, an overridden static file plus
an added one, no theme configured (identical behavior to before), an
invalid theme name (warning listing the 2 locations tried + clean
fallback), a new page via a theme blueprint, a bundled theme resolved
correctly, external-folder-over-bundled priority (same name on both sides,
external wins), a third-party theme resolved via a simulated entry point,
and `list_available_themes` correctly combining all three sources without
duplicates.

## Known limitations / roads not taken

- A theme cannot redefine the logic of an existing route, only its
  template, CSS/JS/images, translations, or add new ones — see discussion
  above.
- The translation merge order (theme before core) works in tests but
  deserves to be re-verified with real, differing translated strings
  between theme and core in your exact Flask-Babel environment, before
  going to production.
- No schema validation on `theme.json`: unknown keys are silently ignored,
  worth watching if you expand the metadata later.
- Third-party theme resolution relies on `importlib.metadata.entry_points()`:
  it assumes a normally installed package (not tested in a zipapp/zipped
  install); the recommended `theme_dir()` callable (rather than a static
  `Path` hardcoded in `pyproject.toml`) reduces this risk but doesn't
  eliminate it entirely for unusual installs.
