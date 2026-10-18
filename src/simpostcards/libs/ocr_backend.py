# -*- encoding: utf-8 -*-
"""
simpostcards/libs/ocr_backend.py - Extraction de texte (OCR) d'une
image, pour POST /api/compute_ocr.

pytesseract est volontairement importé localement (dans compute_ocr())
et non ici en tête de module : cela permet d'importer ce module sans
avoir pytesseract (ni le binaire tesseract-ocr sous-jacent) installés
-- même principe que ``simpostcards.libs.hasher``/``embedder``.

Contrairement à ``embedder.py`` (modèle CLIP chargé une fois et mis en
cache), il n'y a rien à "charger" ici : chaque appel invoque
directement le binaire ``tesseract`` via pytesseract, pas de singleton
ni de verrou nécessaires.
"""

from __future__ import annotations

import time

import numpy as np
from PIL import Image

from libpostcards.langs import tesseract_installed_languages, tesseract_missing_languages

try:
    import pytesseract  # noqa: F401
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


def compute_ocr(image: Image.Image, lang: str = "fra") -> str:
    """
    Extrait le texte de *image* (déjà en mémoire) via Tesseract OCR,
    dans la langue *lang* (code(s) tesseract, ex : "fra" ou "fra+eng").

    Nécessite pytesseract (et le binaire tesseract-ocr) installés --
    voir OCR_AVAILABLE ; l'appelant est responsable de ne pas appeler
    cette fonction sinon.
    """
    import pytesseract

    imgnp = np.array(image)
    return pytesseract.image_to_string(imgnp, lang=lang)


# Langues installées : interroger tesseract lance un sous-processus
# (``tesseract --list-langs``) ; une courte mémoire évite de le refaire à
# chaque carte d'un lot, tout en voyant vite un paquet de langue qu'on
# vient d'installer.
_LANGS_TTL = 30.0
_langs_cache: tuple[float, list[str]] | None = None


def installed_languages() -> list[str]:
    """Langues (``.traineddata``) installées pour tesseract sur ce serveur."""
    global _langs_cache
    now = time.monotonic()
    if _langs_cache is None or now - _langs_cache[0] > _LANGS_TTL:
        _langs_cache = (now, tesseract_installed_languages())
    return _langs_cache[1]


def missing_languages(lang: str) -> list[str]:
    """Codes de *lang* ("fra" ou "fra+eng") que tesseract n'a pas ici."""
    return tesseract_missing_languages(lang, installed_languages())


def reset_languages_cache() -> None:
    """Pour les tests."""
    global _langs_cache
    _langs_cache = None
