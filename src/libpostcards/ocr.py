# -*- encoding: utf-8 -*-
"""
libpostcards/ocr.py - Extraction de texte (OCR) d'une image de carte
postale (PostcardOCR).

Déplacé depuis tkpostcards.libs.ocr (même changement que
libpostcards.similar/libpostcards.travel en leur temps) : cette classe
ne dépend pas de Tk et est aussi utile côté serveur
(``simpostcards.blueprints.api``, qui expose POST /api/compute_ocr
pour ce module) que côté desktop
(``tkpostcards.libs.scan_add``/tkmanager/tktools).

Deux moteurs, choisis par le CLIENT (``engine=``, ou [tkimport] ocr_engine /
``--ocr-engine`` côté outils) :

- ``"tesseract"`` (défaut) : pytesseract + le binaire tesseract ;
- ``"paddleocr"`` : PaddleOCR (voir ``libpostcards.ocr_paddle``), meilleur sur
  le texte imprimé bruité ou posé sur une photo. Ni l'un ni l'autre ne lit
  l'écriture manuscrite.

Chacun tourne en local ou, avec ``external_helper``, sur le serveur
simpostcards qui doit alors avoir ce moteur. Le moteur ou la langue
indisponible est signalé par :class:`libpostcards.langs.EngineUnavailable` /
:class:`libpostcards.langs.LanguageUnavailable`.

pytesseract est volontairement importé localement (dans to_string()) et
non ici en tête de module : cela permet d'importer libpostcards.ocr
(ex : pour introspection, tests, ou pour PostcardOCR(external_helper=...) --
voir plus bas) sans avoir ce paquet (ni le binaire tesseract-ocr
sous-jacent) installé. Il n'est requis qu'au moment où l'OCR est
réellement effectué localement.

Un déploiement peut aussi s'en passer ENTIÈREMENT en pointant
PostcardOCR vers un serveur simpostcards distant via ``external_helper`` :
l'extraction de texte se fait alors côté serveur (POST
/api/compute_ocr), et pytesseract (+ le binaire tesseract-ocr) n'a pas
besoin d'être installé ici -- même principe que ``external_helper`` sur
``libpostcards.similar.PostcardSearcher``/
``libpostcards.travel.ParcoursCartes``.
"""
import numpy as np
import requests

from PIL import Image

from .langs import (
    EngineUnavailable, LanguageUnavailable, normalize_ocr_engine,
    ocr_language_message, paddle_missing_languages,
    raise_if_engine_unavailable, raise_if_language_unavailable,
    tesseract_installed_languages, tesseract_missing_languages,
)
from . import ocr_paddle

try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False


class PostcardOCR:

    def __init__(
        self,
        lang="fra",
        debug=False,
        external_helper=None,
        external_helper_timeout=None,
        engine="tesseract",
        min_score=None,
        paddle_device=None,
    ):

        self.lang = lang
        self.debug = debug

        # Moteur OCR : "tesseract" (défaut) ou "paddleocr" ; le CHOIX est fait
        # ici, par le client, que le calcul ait lieu en local ou sur le
        # serveur (``external_helper``, qui doit alors avoir ce moteur). Le
        # code de langue peut être celui de tesseract ("fra", "fra+eng") ou
        # celui de PaddleOCR ("fr") : chaque moteur comprend les deux (voir
        # libpostcards.langs.resolve_paddle_language).
        # ``min_score`` (PaddleOCR seulement) : confiance minimale d'une
        # ligne lue ; ``paddle_device`` ("cpu", "gpu:0"...) : calcul LOCAL
        # seulement -- côté serveur, c'est [simpostcards] paddleocr_device.
        self.engine = normalize_ocr_engine(engine)
        self.min_score = (ocr_paddle.DEFAULT_MIN_SCORE
                          if min_score is None else float(min_score))
        self.paddle_device = paddle_device or None
        self._paddle = None   # PaddleOCR local, créé à la demande

        # URI d'un serveur simpostcards (ex : "http://simpostcards:8004",
        # voir [DEFAULT] external_helper dans postcards.conf) utilisé pour
        # déléguer l'extraction de texte à ce serveur AU LIEU d'un
        # calcul local -- voir to_string() et _to_string_remote(). Vide/
        # None (par défaut) : calcul local, comme avant (pytesseract +
        # le binaire tesseract-ocr requis).
        self.external_helper = (external_helper or "").strip() or None
        self.external_helper_timeout = (
            30.0 if external_helper_timeout is None else external_helper_timeout)
        self._language_checked = False

    def check_language(self):
        """Vérifie que le moteur choisi existe là où l'OCR sera fait (ici, ou
        sur ``external_helper``) ET qu'il a la langue ``self.lang`` ; lève
        :class:`libpostcards.langs.EngineUnavailable` ou
        :class:`libpostcards.langs.LanguageUnavailable` sinon, avec le détail
        (codes manquants, langues disponibles, quoi installer). Appelé
        automatiquement par :meth:`to_string` (une seule vérification par
        instance) ; les appelants qui traitent un lot peuvent l'appeler
        d'abord pour échouer tout de suite, avant d'avoir commencé à copier
        ou modifier quoi que ce soit.

        Un serveur trop ancien pour avoir ``/api/ocr_languages`` (404) n'est
        pas vérifié avec tesseract : l'erreur éventuelle viendra de l'OCR
        lui-même. Avec PaddleOCR, il est refusé : un serveur ancien ignorerait
        le choix du moteur et lirait avec tesseract.
        """
        if self._language_checked:
            return
        if self.external_helper:
            self._check_language_remote()
        elif self.engine == "paddleocr":
            if not ocr_paddle.PADDLE_AVAILABLE:
                raise EngineUnavailable("paddleocr", ocr_paddle.INSTALL_HINT)
            missing = paddle_missing_languages(self.lang)
            if missing:
                raise LanguageUnavailable(
                    "ocr", self.lang,
                    ocr_language_message(self.lang, missing, [], engine="paddleocr"),
                    missing=missing)
        else:
            if not PYTESSERACT_AVAILABLE:
                raise ImportError(
                    "pytesseract is required to run OCR. "
                    "Install it with: pip install pypostcards[ocr]"
                )
            installed = tesseract_installed_languages()
            missing = tesseract_missing_languages(self.lang, installed)
            if missing:
                raise LanguageUnavailable(
                    "ocr", self.lang,
                    ocr_language_message(self.lang, missing, installed),
                    missing=missing, available=installed)
        self._language_checked = True

    def _check_language_remote(self):
        response = requests.get(
            f"{self.external_helper.rstrip('/')}/api/ocr_languages",
            params={"lang": self.lang, "engine": self.engine},
            timeout=self.external_helper_timeout,
        )
        raise_if_language_unavailable(response, "ocr")
        raise_if_engine_unavailable(response)
        if response.status_code == 404:
            if self.engine != "tesseract":
                raise EngineUnavailable(
                    self.engine, "the server %s is too old to choose the OCR "
                    "engine (no /api/ocr_languages): upgrade it, or use the "
                    "tesseract engine" % self.external_helper)
            return
        response.raise_for_status()
        self._require_engine(response)

    def _require_engine(self, response):
        """Un serveur qui ne sait pas choisir le moteur l'ignorerait et
        lirait avec tesseract : avec un autre moteur demandé, on exige que la
        réponse dise lequel l'a produite."""
        if self.engine == "tesseract":
            return
        answered = response.json().get("engine")
        if answered != self.engine:
            raise EngineUnavailable(
                self.engine, "the server %s did not run the %s engine "
                "(answered %r): it is probably too old, upgrade it"
                % (self.external_helper, self.engine, answered))

    def to_string(self, fname):
        """
        Extrait le texte de l'image *fname* (chemin de fichier), dans
        la langue ``self.lang`` (code(s) tesseract, ex : "fra" ou
        "fra+eng").

        Si ``self.external_helper`` est configuré, délègue à ce serveur
        simpostcards (_to_string_remote) au lieu d'un calcul local --
        pytesseract n'est alors pas nécessaire ici.

        Lève :class:`libpostcards.langs.LanguageUnavailable` si la langue
        n'est pas disponible pour le moteur, et
        :class:`libpostcards.langs.EngineUnavailable` si le moteur lui-même
        l'est (voir :meth:`check_language`).
        """
        self.check_language()
        if self.external_helper:
            return self._to_string_remote(fname)
        if self.engine == "paddleocr":
            return self._to_string_paddle(fname)

        if not PYTESSERACT_AVAILABLE:
            raise ImportError(
                "pytesseract is required to run OCR. "
                "Install it with: pip install pypostcards[ocr]"
            )
        import pytesseract

        imgnp = np.array(Image.open(fname))
        imgtext = pytesseract.image_to_string(imgnp, lang=self.lang)
        # ~ imgtext = pytesseract.image_to_string(imgnp)
        return imgtext

    def _to_string_paddle(self, fname) -> str:
        """Lecture locale avec PaddleOCR (moteur créé à la première carte :
        c'est là que les modèles se chargent, voire se téléchargent)."""
        if self._paddle is None:
            self._paddle = ocr_paddle.create_engine(
                self.lang, device=self.paddle_device)
        with Image.open(fname) as image:
            image.load()
            return ocr_paddle.read_text(self._paddle, image, self.min_score)

    def _to_string_remote(self, fname) -> str:
        """
        Extrait le texte de *fname* via
        ``POST {self.external_helper}/api/compute_ocr`` (voir
        simpostcards.blueprints.api.compute_ocr_route), au lieu d'un
        calcul local -- pytesseract n'est alors pas requis sur cette
        machine.
        """
        with open(fname, "rb") as fh:
            response = requests.post(
                f"{self.external_helper.rstrip('/')}/api/compute_ocr",
                files={"image": (str(fname), fh)},
                data={"lang": self.lang, "engine": self.engine,
                      "min_score": self.min_score},
                timeout=self.external_helper_timeout,
            )
        raise_if_language_unavailable(response, "ocr")
        raise_if_engine_unavailable(response)
        response.raise_for_status()
        self._require_engine(response)
        return response.json()["text"]
