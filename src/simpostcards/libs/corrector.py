# -*- encoding: utf-8 -*-
"""
simpostcards/libs/corrector.py - Redressement et détourage d'une carte
postale scannée, avant calcul des hashs.

Réutilise directement ``libpostcards.scan_corrector.ScanCorrector``
(même logique que ``tktools prepare``) : pas de duplication de code,
simpostcards ne fait qu'orchestrer scan_corrector + hasher pour
exposer le résultat via une API JSON.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import cv2
import numpy as np
from PIL import Image

from libpostcards.scan_corrector import ScanCorrector


class ImageDecodeError(ValueError):
    """L'image envoyée est absente, vide ou illisible par OpenCV."""


def correct_image(
    data: bytes,
    white_threshold: int = 240,
) -> tuple[Image.Image, dict[str, Any]]:
    """
    Décode ``data`` (contenu brut d'un fichier image : tiff/jpg/png/...),
    applique le redressement + détourage de ``ScanCorrector``, et
    retourne :
      - l'image corrigée sous forme d'``Image.Image`` PIL (RGB)
      - le rapport de traitement (``CorrectionReport``) sous forme de dict

    Lève ``ImageDecodeError`` si ``data`` est vide ou ne peut pas être
    décodé comme une image par OpenCV.
    """
    if not data:
        raise ImageDecodeError("Fichier image vide")

    buffer = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if img is None:
        raise ImageDecodeError("Image illisible (format non reconnu)")

    corrector = ScanCorrector(white_threshold=white_threshold, verbose=False)
    corrected = corrector.process_image(img)

    rgb = cv2.cvtColor(corrected, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)

    report = asdict(corrector.report) if corrector.report is not None else {}
    return image, report
