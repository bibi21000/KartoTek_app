# -*- encoding: utf-8 -*-
"""
libpostcards/contour_utils.py
------------------------------

Détection de contour partagée entre :
- ``ScanCorrector.crop_borders_by_contour`` (rognage final)
- ``TiffBackgroundRemover.make_border_transparent_by_contour_cv2`` (détourage)

Les deux méthodes ci-dessus détectent le contour physique réel de la carte
par gradient (Canny), pour s'affranchir des limites d'une approche par
couleur/seuil de blanc sur une carte non rectangulaire ou un fond très
proche en couleur du papier de la carte (voir leurs docstrings).

Sur certains scans, le bord réel (souvent un bord dentelé) ne produit
qu'un gradient très faible -- à peine plus fort que le bruit de grain du
papier -- et Canny, même avec des seuils bas, ne trace alors qu'un contour
troué (le bruit et le signal utile sont trop proches en amplitude pour
qu'un simple seuil les sépare). ``enhance_and_detect_edges`` ajoute des
étapes de prétraitement optionnelles, chacune activable indépendamment
(voir :class:`~tkpostcards.libs.scan_profiles.ScanProfile`), pour muscler
ce cas :

- ``denoise`` : lissage préservant les bords (filtre bilatéral) --
  contrairement à un flou gaussien classique, il réduit le bruit de grain
  sans étaler le bord recherché, ce qui est important puisqu'on va ensuite
  amplifier le contraste (voir ``clahe``) : amplifier du bruit non filtré
  serait pire que de ne rien faire.
- ``clahe`` : égalisation d'histogramme adaptative *locale* (contrairement
  à un simple étirement de contraste global, qui ne change presque rien
  quand toute l'image est déjà proche du blanc) : une légère décoloration
  du bord dans une zone globalement très claire ressort alors beaucoup
  mieux.
- ``auto_canny`` : calcule les seuils de Canny à partir de la médiane
  d'intensité de l'image plutôt que d'utiliser des seuils fixes, puisque
  l'exposition/l'éclairage du scan varie d'une carte à l'autre.
"""
from __future__ import annotations

import cv2
import numpy as np


def find_content_mask(
    gray: np.ndarray,
    denoise: bool = False,
    clahe: bool = False,
    auto_canny: bool = False,
    canny_low: int = 20,
    canny_high: int = 60,
    min_content_ratio: float = 0.25,
    max_content_ratio: float = 0.999,
    kernel_percents=(0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05),
):
    """Détecte les contours puis étiquette fond/contenu par composantes
    connexes (voir ``ScanCorrector.crop_borders_by_contour`` et
    ``TiffBackgroundRemover.make_border_transparent_by_contour_cv2``),
    en essayant plusieurs tailles de noyau de dilatation par ordre
    croissant plutôt qu'une taille fixe.

    Pourquoi : un bord dentelé net et bien contrasté n'a besoin que d'un
    petit noyau (voire aucun) pour donner un contour fermé -- utiliser
    d'emblée un grand noyau lisse alors sa texture fine (chaque pointe du
    dentelé) en quelques grosses bosses arrondies, méconnaissables. À
    l'inverse, un bord à faible contraste (voir
    ``libpostcards.contour_utils``) peut avoir besoin d'un noyau bien
    plus grand pour combler ses brèches. Comme la bonne taille dépend du
    contraste réel de CHAQUE scan (pas seulement de sa résolution), on
    part petit (préserve le détail) et on n'agrandit que si nécessaire
    (le contour reste troué, le fond "fuit" dans le contenu).

    :return: tuple ``(background_mask, content_ratio, edge_dilate)`` du
        premier essai dont le ``content_ratio`` retombe dans
        ``[min_content_ratio, max_content_ratio]`` (fond fiable), ou
        ``(None, best_ratio, None)`` si aucun essai n'y parvient --
        l'appelant doit alors se rabattre sur une méthode plus robuste.
    """
    h, w = gray.shape[:2]
    edges = enhance_and_detect_edges(
        gray, denoise=denoise, clahe=clahe, auto_canny=auto_canny,
        canny_low=canny_low, canny_high=canny_high,
    )

    best_ratio = None
    for pct in kernel_percents:
        k = max(3, round(min(h, w) * pct))
        if k % 2 == 0:
            k += 1

        # Noyau elliptique (et non carré) : un noyau carré dilate en
        # "escalier" avec des angles à 90° près d'un virage marqué du
        # contour (typiquement un coin de carte).
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        dilated = cv2.dilate(edges, kernel, iterations=1)

        passable = (dilated == 0).astype(np.uint8)
        num_labels, labels = cv2.connectedComponents(passable, connectivity=4)
        border_labels = set(labels[0, :].tolist())
        border_labels |= set(labels[-1, :].tolist())
        border_labels |= set(labels[:, 0].tolist())
        border_labels |= set(labels[:, -1].tolist())
        border_labels.discard(0)
        background = np.isin(labels, list(border_labels))

        content_ratio = 1.0 - (background.sum() / background.size)
        if best_ratio is None or abs(content_ratio - 0.5) < abs(best_ratio - 0.5):
            best_ratio = content_ratio

        if min_content_ratio <= content_ratio <= max_content_ratio:
            return background, content_ratio, k

    return None, best_ratio, None


def enhance_and_detect_edges(
    gray: np.ndarray,
    denoise: bool = False,
    clahe: bool = False,
    auto_canny: bool = False,
    canny_low: int = 20,
    canny_high: int = 60,
    denoise_d: int = 9,
    denoise_sigma_color: float = 50.0,
    denoise_sigma_space: float = 50.0,
    clahe_clip_limit: float = 3.0,
    clahe_tile_grid_size: int = 8,
    auto_canny_sigma: float = 0.33,
) -> np.ndarray:
    """Prépare une image en niveaux de gris puis détecte ses contours
    (Canny), avec des étapes de prétraitement optionnelles pour muscler
    la détection sur un bord à faible contraste (voir le docstring du
    module pour le détail de chaque option).

    :param gray: image source en niveaux de gris (un seul canal).
    :param denoise: applique un filtre bilatéral (préserve les bords,
        contrairement à un flou gaussien) avant tout le reste.
    :param clahe: applique une égalisation d'histogramme adaptative
        locale (CLAHE) avant Canny.
    :param auto_canny: calcule ``canny_low``/``canny_high`` à partir de
        la médiane d'intensité de l'image (au lieu des valeurs fixes
        passées en paramètre), voir ``auto_canny_sigma``.
    :return: masque binaire des contours détectés (uint8, 0/255), même
        dimensions que ``gray``.
    """
    work = gray

    if denoise:
        work = cv2.bilateralFilter(
            work, denoise_d, denoise_sigma_color, denoise_sigma_space)

    if clahe:
        clahe_obj = cv2.createCLAHE(
            clipLimit=clahe_clip_limit,
            tileGridSize=(clahe_tile_grid_size, clahe_tile_grid_size))
        work = clahe_obj.apply(work)

    # Léger flou gaussien final : Canny y est sensible, et CLAHE en
    # particulier peut réintroduire un peu de grain à haute fréquence.
    work = cv2.GaussianBlur(work, (5, 5), 0)

    if auto_canny:
        median = float(np.median(work))
        canny_low = int(max(0, (1.0 - auto_canny_sigma) * median))
        canny_high = int(min(255, (1.0 + auto_canny_sigma) * median))

    return cv2.Canny(work, canny_low, canny_high)


def detect_card_quad(
    image: np.ndarray,
    target_width: int = 1500,
    white_threshold: int = 245,
    morph_kernel_size: int = 15,
):
    """Détecte l'enveloppe quadrilatère approximative de la carte, en
    travaillant sur une copie réduite de ``image`` (BGR), puis renvoie
    les 4 coins dans le système de coordonnées de l'image ORIGINALE.

    Principe : réduire d'abord l'image lisse le bruit de numérisation
    (chaque pixel réduit moyenne de nombreux pixels d'origine), ce qui
    rend une simple distinction par seuil de couleur entre le blanc du
    fond du scanner et le blanc -- généralement légèrement teinté -- du
    papier de la carte bien plus fiable qu'à pleine résolution, où des
    pixels bruités individuels franchissent le seuil de façon
    imprévisible et fragmentent le contour en un semis de petits trous
    (voir le reste de ce module). Travailler en petit est aussi
    beaucoup plus rapide.

    Le quadrilatère obtenu n'est qu'une enveloppe approximative (voir
    ``cv2.approxPolyDP``) -- il ne suit PAS un bord fin comme un
    dentelé. Il est destiné à cadrer/positionner un rognage *généreux*
    de l'original en PLEINE résolution (pour que le bord dentelé soit
    intégralement inclus comme contenu, sans être rogné), sur lequel un
    détourage fin peut ensuite être appliqué séparément (voir
    ``enhance_and_detect_edges`` / ``find_content_mask``).

    :param target_width: largeur (px) de la copie réduite utilisée pour
        la détection.
    :param white_threshold: niveau de gris (0-255) en dessous duquel un
        pixel est considéré comme faisant partie de la carte (pas du
        fond), sur l'image réduite.
    :param morph_kernel_size: taille du noyau (elliptique) utilisé pour
        nettoyer le masque binaire (``cv2.morphologyEx``) avant de
        chercher son contour.
    :return: tuple ``(quad, scale)`` où ``quad`` est un tableau (4, 2)
        de points (x, y) dans le système de coordonnées de l'image
        ORIGINALE, ou ``None`` si aucun contour n'a été trouvé ;
        ``scale`` est le facteur de réduction utilisé
        (original / réduit).
    """
    h0, w0 = image.shape[:2]

    if w0 > target_width:
        scale = w0 / float(target_width)
        small = cv2.resize(
            image, (target_width, max(1, round(h0 / scale))),
            interpolation=cv2.INTER_AREA)
    else:
        scale = 1.0
        small = image

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, white_threshold, 255, cv2.THRESH_BINARY_INV)

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (morph_kernel_size, morph_kernel_size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, scale

    largest = max(contours, key=cv2.contourArea)

    perimeter = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.02 * perimeter, True)

    if len(approx) != 4:
        # Repli : rectangle tourné englobant (toujours 4 points, même
        # si le contour détecté n'est pas un quadrilatère net).
        rect = cv2.minAreaRect(largest)
        approx = cv2.boxPoints(rect).reshape(-1, 1, 2)

    quad_small = approx.reshape(-1, 2).astype(np.float64)
    quad_full = quad_small * scale

    return quad_full, scale
