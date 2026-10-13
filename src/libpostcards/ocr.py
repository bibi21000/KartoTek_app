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
    ):

        self.lang = lang
        self.debug = debug

        # URI d'un serveur simpostcards (ex : "http://simpostcards:8004",
        # voir [DEFAULT] external_helper dans postcards.conf) utilisé pour
        # déléguer l'extraction de texte à ce serveur AU LIEU d'un
        # calcul local -- voir to_string() et _to_string_remote(). Vide/
        # None (par défaut) : calcul local, comme avant (pytesseract +
        # le binaire tesseract-ocr requis).
        self.external_helper = (external_helper or "").strip() or None
        self.external_helper_timeout = (
            30.0 if external_helper_timeout is None else external_helper_timeout)

    def to_string(self, fname):
        """
        Extrait le texte de l'image *fname* (chemin de fichier), dans
        la langue ``self.lang`` (code(s) tesseract, ex : "fra" ou
        "fra+eng").

        Si ``self.external_helper`` est configuré, délègue à ce serveur
        simpostcards (_to_string_remote) au lieu d'un calcul local --
        pytesseract n'est alors pas nécessaire ici.
        """
        if self.external_helper:
            return self._to_string_remote(fname)

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
                data={"lang": self.lang},
                timeout=self.external_helper_timeout,
            )
        response.raise_for_status()
        return response.json()["text"]
