# -*- encoding: utf-8 -*-
"""
simpostcards/libs/corrector.py - Redressement et détourage d'une carte
postale scannée.

Réutilise directement ``libpostcards.scan_corrector.ScanCorrector`` (+
``libpostcards.transparency.TiffBackgroundRemover``, pour le détourage)
: pas de duplication de code, simpostcards ne fait qu'orchestrer ces
briques pour les exposer via une API HTTP.

Deux fonctions, pour deux usages distincts :
  - :func:`correct_image` -- redressement seul (pas de détourage),
    piloté par un simple ``white_threshold`` : utilisée par
    /api/compute_hashes pour une photo quelconque (recherche par URL/
    presse-papiers, photo prise par l'appli mobile) avant d'en calculer
    les hashs -- le détourage n'a pas de sens là, l'image n'est jamais
    stockée telle quelle.
  - :func:`correct_scan` -- pipeline complet (redressement + rognage +
    détourage), piloté par les mêmes paramètres qu'un
    ``ScanProfile`` (voir ``tkpostcards.libs.scan_profiles``) : utilisée
    par /api/correct_scan pour déléguer l'étape "Analyze and correct
    scans" de tkimport/tktools (voir
    ``tkpostcards.libs.scan_prepare.make_corrector``) à ce serveur.
"""

from __future__ import annotations

import io
from dataclasses import asdict
from typing import Any

import cv2
import numpy as np
from PIL import Image

from libpostcards.scan_corrector import ScanCorrector
from libpostcards.transparency import TiffBackgroundRemover


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


def correct_scan(
    data: bytes,
    white_threshold: int = 240,
    white_ratio_threshold: float = 0.98,
    crop_margin: int = 20,
    final_crop_margin: int = 5,
    angle_range: float = 20.0,
    use_contour_geometry: bool = False,
    use_quad_geometry: bool = False,
    contour_denoise: bool = False,
    contour_clahe: bool = False,
    contour_auto_canny: bool = False,
    skip_transparency: bool = False,
    transparency_white_threshold: int | None = None,
    transparency_band: int | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    Décode ``data`` (contenu brut d'un fichier image), applique le
    pipeline complet -- redressement/rognage (``ScanCorrector
    .process_image``) puis, sauf ``skip_transparency``, détourage du
    fond (``TiffBackgroundRemover``) -- avec les mêmes paramètres qu'un
    ``ScanProfile`` (voir ``tkpostcards.libs.scan_profiles``), et
    reproduit exactement le pipeline local de
    ``tkpostcards.libs.scan_prepare.make_corrector()``.

    Contrairement à :func:`correct_image` (redressement seul, pas de
    détourage, RGB), celle-ci gère aussi le détourage et rend un
    tableau numpy BGR (3 canaux) ou BGRA (4 canaux, dès que le
    détourage a été appliqué) -- PAS encore encodé dans un format
    d'image : voir :func:`encode_scan`, la conversion dépend du format
    de sortie voulu (l'encodage TIFF, notamment, doit passer par PIL
    plutôt que cv2.imwrite -- voir son docstring).

    ``transparency_white_threshold`` doit être déjà résolu par
    l'appelant (voir ``ScanProfile.effective_transparency_threshold``) ;
    ``None`` retombe sur ``white_threshold``.

    Lève ``ImageDecodeError`` si ``data`` est vide ou ne peut pas être
    décodé comme une image par OpenCV.
    """
    if not data:
        raise ImageDecodeError("Fichier image vide")

    buffer = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if img is None:
        raise ImageDecodeError("Image illisible (format non reconnu)")

    corrector = ScanCorrector(
        white_threshold=white_threshold,
        white_ratio_threshold=white_ratio_threshold,
        crop_margin=crop_margin,
        final_crop_margin=final_crop_margin,
        angle_range=angle_range,
        contour_final_crop=use_contour_geometry,
        quad_final_crop=use_quad_geometry,
        contour_denoise=contour_denoise,
        contour_clahe=contour_clahe,
        contour_auto_canny=contour_auto_canny,
        verbose=False,
    )
    corrected = corrector.process_image(img)

    if not skip_transparency:
        bgtrans = TiffBackgroundRemover(
            white_threshold=(
                transparency_white_threshold if transparency_white_threshold is not None
                else white_threshold
            )
        )
        if use_contour_geometry:
            corrected = bgtrans.make_border_transparent_by_contour_cv2(
                corrected, band=transparency_band,
                denoise=contour_denoise, clahe=contour_clahe,
                auto_canny=contour_auto_canny,
            )
        else:
            corrected = bgtrans.make_border_white_transparent_cv2(
                corrected, band=transparency_band)

    report = asdict(corrector.report) if corrector.report is not None else {}
    return corrected, report


def encode_scan(image: np.ndarray, ext: str) -> bytes:
    """
    Encode *image* (tableau numpy BGR ou BGRA, tel que renvoyé par
    :func:`correct_scan`) au format demandé par *ext* (avec ou sans le
    point, ex : "tiff" ou ".tiff"), avec exactement les mêmes réglages
    que le pipeline local historique
    (``tkpostcards.libs.scan_prepare.make_corrector``) :

      - TIFF via PIL (pas ``cv2.imwrite``) avec balise alpha correcte
        et compression "tiff_deflate" (zlib) plutôt que LZW -- voir le
        commentaire détaillé dans ``make_corrector`` pour le pourquoi ;
      - JPEG qualité 95 ;
      - PNG compression 3 (aussi le repli pour toute autre extension).

    Lève ``ValueError`` si l'encodage échoue.
    """
    ext = ext.lower()
    if not ext.startswith("."):
        ext = "." + ext

    if ext in (".tif", ".tiff"):
        rgba_or_rgb = (
            cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA) if image.shape[2] == 4
            else cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        )
        buf = io.BytesIO()
        Image.fromarray(rgba_or_rgb).save(buf, format="TIFF", compression="tiff_deflate")
        return buf.getvalue()

    params: list[int] = []
    if ext in (".jpg", ".jpeg"):
        params = [cv2.IMWRITE_JPEG_QUALITY, 95]
    elif ext == ".png":
        params = [cv2.IMWRITE_PNG_COMPRESSION, 3]

    ok, buf = cv2.imencode(ext if ext in (".jpg", ".jpeg", ".png") else ".png", image, params)
    if not ok:
        raise ValueError("Échec de l'encodage de l'image corrigée (format %r)" % ext)
    return buf.tobytes()
