# -*- encoding: utf-8 -*-
import os
import sys
import gettext
import locale
import configparser
from pathlib import Path

import click

# ──────────────────────────────────────────────────────────────────────────────
# i18n setup
# ──────────────────────────────────────────────────────────────────────────────
#
# NOTE: ``_()`` is used inside ``click`` decorator arguments below (e.g.
# ``help=_("...")``), which are evaluated once, at import time. This means
# the translation catalog MUST be bound to the "tkpostcards" domain *before*
# this module finishes loading, otherwise every caller (including
# ``tkpostcards.scripts.tktools``, which does ``from .. import cli``) would
# freeze on the untranslated (English) strings for the whole process
# lifetime. Previously this module (and ``scripts/tktools.py``) used
# ``from gettext import gettext as _``, which binds ``_`` to the *global*
# default domain ("messages"); since that domain was never bound to our own
# catalog via ``bindtextdomain``/``textdomain``, it always returned the
# original msgid untranslated, regardless of the user's language or of the
# content of the .mo files. Mirrors the pattern used in tkscan.py /
# tkimport.py / tkmanager.py, so all four entry points share the same
# "tkpostcards" catalog.

APP_DIR = Path(__file__).parent.resolve()
TRANSLATIONS_DIR = APP_DIR / "translations"

I18N_DOMAIN = "tkpostcards"


def _detect_system_lang() -> str:
    """Best-effort detection of the user's preferred language code (e.g. "fr")."""
    lang = os.environ.get("TKPOSTCARDS_LANG")
    if lang:
        return lang[:2].lower()

    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            first = value.split(":")[0].split(".")[0]
            if first and first.upper() not in ("C", "POSIX"):
                return first.split("_")[0].lower()

    if sys.platform.startswith("win"):
        try:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            win_lang = locale.windows_locale.get(lcid)
            if win_lang:
                return win_lang.split("_")[0].lower()
        except Exception:
            pass

    try:
        locale.setlocale(locale.LC_ALL, "")
        lc_category = getattr(locale, "LC_MESSAGES", locale.LC_CTYPE)
        lc, _enc = locale.getlocale(lc_category)
        if lc:
            return lc.split("_")[0].lower()
    except (locale.Error, ValueError):
        pass

    return "en"


def setup_i18n(lang: str | None = None) -> gettext.NullTranslations:
    """Return a translation object bound to the "tkpostcards" domain."""
    if lang is None:
        lang = _detect_system_lang()
    try:
        translation = gettext.translation(
            I18N_DOMAIN,
            localedir=str(TRANSLATIONS_DIR),
            languages=[lang],
        )
    except FileNotFoundError:
        translation = gettext.NullTranslations()
    return translation


_TRANSLATION = setup_i18n()
_ = _TRANSLATION.gettext

def config(confile=None):
    if confile is None:
        confile = 'postcards.conf'
    config = configparser.ConfigParser()
    config.read(confile)
    return config

class Common(object):
    def __init__(self, conffile=None, datadir=None, importdir=None, tmpdir=None, debug=None):
        self.conffile = conffile
        self.conf = config(self.conffile)

        if datadir is None:
            datadir = self.conf.get('DEFAULT', 'datadir', fallback=None)
        self.datadir = os.path.abspath(datadir or 'data')

        if importdir is None:
            importdir = self.conf.get('DEFAULT', 'importdir', fallback=None)
        self.importdir = os.path.abspath(importdir or 'import')

        if tmpdir is None:
            tmpdir = self.conf.get('DEFAULT', 'tmpdir', fallback=None)
        self.tmpdir = os.path.abspath(tmpdir or 'tmp')

        self.file_format = self.conf.get('DEFAULT', 'file_format', fallback='tiff')

        self.debug = debug

@click.group()
@click.option('--conffile', default='postcards.conf', help=_("Configuration file"))
@click.option('--datadir', default=None, help=_("Image and JSON storage directory"))
@click.option('--importdir', default=None, help=_("Scanned image import directory"))
@click.option('--tmpdir', default=None, help=_("Temporary directory"))
@click.option('--debug/--no-debug', default=False, help=_("Enable/disable debug"))
@click.pass_context
def cli(ctx, conffile, datadir, importdir, tmpdir, debug):
    """Command group."""
    ctx.obj = Common(conffile, datadir, importdir, tmpdir, debug)
