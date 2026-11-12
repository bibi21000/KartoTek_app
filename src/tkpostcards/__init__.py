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
    def __init__(self, conffile=None, datadir=None, importdir=None, tmpdir=None, debug=None,
                 torch_device=None, external_helper=None, external_helper_timeout=None):
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

        # torch_device : --torch-device en ligne de commande prévaut ;
        # sinon [DEFAULT] torch_device de postcards.conf ; sinon None,
        # qui signifie "laisser torch choisir" (cuda si disponible,
        # sinon cpu -- voir PostcardSearcher.__init__). Une chaîne vide
        # (clé présente mais sans valeur) est traitée comme absente.
        if torch_device is None:
            torch_device = self.conf.get('DEFAULT', 'torch_device', fallback=None)
        self.torch_device = (torch_device or '').strip() or None

        # external_helper : URI d'un serveur simpostcards (ex :
        # "http://simpostcards:8004") utilisé pour déléguer à ce
        # serveur distant, au lieu d'un calcul local, tout ce que
        # simpostcards sait faire à la place de cette machine :
        #   - PostcardSearcher (recherche par similarité, recherche de
        #     doublons -- comparaison de toutes les paires incluse, via
        #     POST /api/find_duplicates --, construction d'index) -- voir
        #     libpostcards.similar.PostcardSearcher.external_helper ;
        #     dispense d'installer torch/open-clip-torch/imagehash ici ;
        #   - ParcoursCartes (calcul de parcours optimisés) -- voir
        #     libpostcards.travel.ParcoursCartes.external_helper ;
        #     dispense d'installer ortools ici ;
        #   - PostcardOCR (extraction de texte) -- voir
        #     libpostcards.ocr.PostcardOCR.external_helper ; dispense
        #     d'installer pytesseract (et le binaire tesseract-ocr) ici.
        # Un seul et même réglage pour les trois, puisqu'il s'agit en
        # pratique toujours du même déploiement simpostcards.
        # --external-helper en ligne de commande prévaut ; sinon
        # [DEFAULT] external_helper de postcards.conf ; sinon None
        # (comportement historique : calcul local, ces libs requises).
        if external_helper is None:
            external_helper = self.conf.get('DEFAULT', 'external_helper', fallback=None)
        self.external_helper = (external_helper or '').strip() or None

        # external_helper_timeout : délai d'attente (en secondes) d'une
        # requête vers external_helper, avant d'abandonner côté client
        # (voir requests' propre paramètre "timeout"). Chaque classe a
        # son propre défaut raisonnable (30s pour PostcardSearcher/
        # PostcardOCR, 60s pour ParcoursCartes/PostcardDetection) --
        # None ici (valeur par défaut) laisse chacune utiliser le sien.
        # À augmenter si le serveur simpostcards est amené à retomber
        # sur cpu pour un calcul normalement fait sur GPU (voir
        # simpostcards.libs.embedder : un modèle CLIP sur cpu peut
        # largement dépasser 30s par image), ou plus généralement si le
        # réseau/la charge du serveur distant est un facteur limitant.
        # --external-helper-timeout en ligne de commande prévaut ;
        # sinon [DEFAULT] external_helper_timeout de postcards.conf ;
        # sinon None (chaque classe garde son propre défaut).
        if external_helper_timeout is None:
            external_helper_timeout = self.conf.get(
                'DEFAULT', 'external_helper_timeout', fallback=None)
        self.external_helper_timeout = (
            float(external_helper_timeout) if external_helper_timeout not in (None, '')
            else None
        )

        self.debug = debug

@click.group()
@click.option('--conffile', default='postcards.conf', help=_("Configuration file"))
@click.option('--datadir', default=None, help=_("Image and JSON storage directory"))
@click.option('--importdir', default=None, help=_("Scanned image import directory"))
@click.option('--tmpdir', default=None, help=_("Temporary directory"))
@click.option('--debug/--no-debug', default=False, help=_("Enable/disable debug"))
@click.option('--torch-device', default=None,
              help=_("Torch device to use for similarity search (see \"tktools devices\"). "
                     "Defaults to the [DEFAULT] torch_device setting in the configuration "
                     "file, or torch's own default (cuda if available, else cpu) if unset."))
@click.option('--external-helper', default=None,
              help=_("URI of a remote simpostcards server (e.g. http://simpostcards:8004) "
                     "used to delegate, instead of a local computation, whatever "
                     "simpostcards can do in place of this machine: similarity/doubles "
                     "search and index building (no torch/open-clip-torch/imagehash needed "
                     "here), travel route optimization (no ortools needed here), and OCR "
                     "text extraction (no pytesseract/tesseract-ocr needed here). Defaults "
                     "to the [DEFAULT] external_helper setting in the configuration file, "
                     "or unset (local computation, those libs required) if neither is "
                     "given."))
@click.option('--external-helper-timeout', default=None, type=float,
              help=_("Timeout, in seconds, for a single request to --external-helper, "
                     "before giving up client-side. Defaults to the [DEFAULT] "
                     "external_helper_timeout setting in the configuration file, or each "
                     "command's own built-in default (30-60s) if neither is given. Raise "
                     "this if the remote server has to fall back to cpu for a computation "
                     "normally done on GPU, which can easily take longer than the default."))
@click.pass_context
def cli(ctx, conffile, datadir, importdir, tmpdir, debug, torch_device, external_helper,
        external_helper_timeout):
    """Command group."""
    ctx.obj = Common(conffile, datadir, importdir, tmpdir, debug, torch_device,
                      external_helper, external_helper_timeout)
