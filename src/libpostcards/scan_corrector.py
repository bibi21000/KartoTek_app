#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
# ~ from gettext import gettext as _

import cv2
import numpy as np

from .contour_utils import enhance_and_detect_edges, find_content_mask, detect_card_quad


# ─────────────────────────────────────────────────────────────────────────────
# Rapport de traitement
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CorrectionReport:
    """Résumé complet d'un traitement."""
    original_size:     tuple[int, int]  = (0, 0)
    size_after_crop: tuple[int, int]  = (0, 0)
    final_size:        tuple[int, int]  = (0, 0)
    removed_borders:    dict[str, int]   = field(default_factory=dict)
    white_threshold_used:  int              = 240
    projection_angle:     float            = 0.0
    hough_angle:          Optional[float]  = None
    final_angle:          float            = 0.0
    methodes_fusionnees:  int              = 1
    detection_mode:       str              = "pic+Hough"

    def __str__(self) -> str:
        b = self.removed_borders
        lignes = [
            "─── Rapport ScanCorrector ───────────────────────────",
            f"  Taille originale    : {self.original_size[0]}×{self.original_size[1]} px",
            f"  Après rognage       : {self.size_after_crop[0]}×{self.size_after_crop[1]} px",
            f"  Taille finale       : {self.final_size[0]}×{self.final_size[1]} px",
        ]
        if b:
            lignes.append(
                f"  Bandes supprimées   : haut={b.get('haut',0)}px  bas={b.get('bas',0)}px  "
                f"gauche={b.get('gauche',0)}px  droite={b.get('droite',0)}px"
            )
        lignes += [
            f"  Seuil blanc utilisé : {self.white_threshold_used}",
            f"  Mode détection      : {self.detection_mode}",
            f"  Angle projection    : {self.projection_angle:+.3f}°",
            (f"  Angle Hough         : {self.hough_angle:+.3f}°"
             if self.hough_angle is not None else
             "  Angle Hough         : —"),
            f"  Angle final         : {self.final_angle:+.2f}°",
            "─────────────────────────────────────────────────────",
        ]
        return "\n".join(lignes)


# ─────────────────────────────────────────────────────────────────────────────
# Classe principale
# ─────────────────────────────────────────────────────────────────────────────

class ScanCorrector:
    """
    Corrige l'inclinaison et supprime les bandes blanches d'un scan.

    Paramètres
    ----------
    white_threshold : int
        Valeur de gris considérée comme "blanche" (défaut 240).
        Fallback automatique jusqu'à 200 pour les fonds grisés.
    white_ratio_threshold : float
        Fraction minimale de pixels blancs pour qu'une ligne soit "blanche"
        (défaut 0.98 = 98 %).
    crop_margin : int
        Pixels conservés autour du contenu lors du rognage initial (avant
        détection d'angle/redressement) (défaut 20).
    final_crop_margin : int
        Pixels conservés autour du contenu lors du rognage final, une fois
        l'image redressée (supprime les bandes blanches ajoutées par la
        rotation). Distinct de ``crop_margin`` : pour un bord dentelé, une
        marge trop faible ici laisse les pointes du dentelé toucher le bord
        du canevas, sans aucune sécurité en cas de léger écart d'angle
        (défaut 5, comportement historique).
    angle_range : float
        Plage de recherche ±X° pour les cas extrêmes (défaut 20.0).
    contour_final_crop : bool
        Si True, le rognage final utilise le contour physique réel de la
        carte (détection par gradient, voir ``crop_borders_by_contour``)
        plutôt que la détection ligne/colonne classique de
        ``crop_borders``. Indispensable pour une carte qui n'est pas
        parfaitement rectangulaire (coin coupé, forme légèrement
        trapézoïdale) : la détection ligne/colonne classique, en
        moyennant sur toute la largeur/hauteur, peut alors laisser une
        marge quasi nulle à l'endroit le plus défavorable (voire
        rogner dans le contenu) alors qu'elle en laisse beaucoup
        ailleurs. Défaut : False (comportement historique).
    quad_final_crop : bool
        Si True (prioritaire sur ``contour_final_crop``), le rognage
        final utilise l'enveloppe quadrilatère approximative de la
        carte (détection par seuil de couleur sur une copie réduite,
        voir ``crop_borders_by_quad``) plutôt que Canny. Plus robuste
        que ``contour_final_crop`` sur un bord dentelé à faible
        contraste (le bruit de numérisation, qui casse un simple seuil
        de couleur à pleine résolution, est lissé par la réduction
        préalable), au prix d'un cadrage moins précis (une enveloppe,
        pas le contour exact) -- compensé par une marge généreuse.
        Défaut : False (comportement historique).
    contour_denoise, contour_clahe, contour_auto_canny : bool
        Prétraitements optionnels appliqués avant la détection de
        contour (voir ``contour_final_crop``), pour muscler la détection
        sur un bord à faible contraste (voir
        ``libpostcards.contour_utils.enhance_and_detect_edges``).
        Sans effet si ``contour_final_crop`` est False. Défaut : False
        (comportement historique).
    verbose : bool
        Affiche les étapes et angles intermédiaires (défaut True).

    Stratégie de détection d'angle
    --------------------------------
    1. Calcul des scores de projection classique sur ±5° (pas fin 0.05°).
    2. Lissage de la courbe et détection des pics locaux.
       → Robuste aux images mixtes texte+photo qui créent un double pic :
         le premier pic (petit angle) est le vrai, le second est un artefact.
    3. Hough quasi-horizontal pour choisir le pic le plus proche de la réalité.
       → Si Hough disponible : pic le plus proche du Hough = angle retenu.
       → Sinon : pic avec le meilleur score.
    4. Passe fine ±0.3° à 0.01° autour du pic retenu.
    5. Si aucun pic local (courbe monotone) et résultat aux bornes ≥ 2.75° :
       Hough direct ou masque texte ±angle_range en fallback.
    """

    def __init__(
        self,
        white_threshold:   int   = 240,
        white_ratio_threshold:     float = 0.98,
        crop_margin: int   = 20,
        final_crop_margin: int   = 5,
        angle_range:   float = 20.0,
        contour_final_crop: bool  = False,
        quad_final_crop: bool = False,
        contour_denoise: bool = False,
        contour_clahe: bool = False,
        contour_auto_canny: bool = False,
        verbose:       bool  = True,
    ) -> None:
        self.white_threshold   = white_threshold
        self.white_ratio_threshold     = white_ratio_threshold
        self.crop_margin = crop_margin
        self.final_crop_margin = final_crop_margin
        self.angle_range   = angle_range
        self.contour_final_crop = contour_final_crop
        self.quad_final_crop = quad_final_crop
        self.contour_denoise = contour_denoise
        self.contour_clahe = contour_clahe
        self.contour_auto_canny = contour_auto_canny
        self.verbose       = verbose

        self.detected_angle: float = 0.0
        self.report: Optional[CorrectionReport] = None
        self._reset_state()

    def _reset_state(self) -> None:
        self._bandes:              dict[str, int]  = {}
        self._white_threshold_used: int             = self.white_threshold
        self._angle_proj:          float           = 0.0
        self._hough_angle:         Optional[float] = None
        self._mode:                str             = "pic+Hough"

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # ── 1. Chargement ────────────────────────────────────────────────────────

    def load_image(self, chemin: str) -> np.ndarray:
        """Charge une image depuis le disque (tous formats OpenCV supportés).

        Gère explicitement le cas d'une source déjà en RGBA (ex : un
        ancien fichier de sortie réutilisé comme entrée, ou un scanner
        qui exporte un canal alpha même sur un scan "brut") : le reste du
        pipeline (``crop_borders``, ``detect_angle``...) suppose une
        image 3 canaux sans transparence. Lire avec ``cv2.imread``
        classique (3 canaux par défaut) ignorerait purement le canal
        alpha et laisserait apparaître la couleur RGB brute qui se
        trouve dessous dans les zones transparentes -- souvent du noir,
        pas du blanc -- ce qui perturberait ensuite toute la détection
        de fond blanc. On aplatit donc explicitement un éventuel canal
        alpha sur un fond blanc avant de poursuivre.
        """
        img = cv2.imread(chemin, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise FileNotFoundError(f"Impossible de load_image : {chemin}")

        if img.ndim == 2:
            # Image en niveaux de gris : uniformiser vers 3 canaux BGR.
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            self._log("  ⚠️  Source RGBA : aplatissement du canal alpha sur fond blanc.")
            bgr = img[:, :, :3].astype(np.float32)
            alpha = img[:, :, 3:4].astype(np.float32) / 255.0
            blanc = np.full_like(bgr, 255.0)
            img = (bgr * alpha + blanc * (1.0 - alpha)).astype(np.uint8)

        self._log(f"  Chargée : {img.shape[1]}×{img.shape[0]} px")
        return img

    # ── 2. Rognage ───────────────────────────────────────────────────────────

    def crop_borders(self, img: np.ndarray, marge: Optional[int] = None) -> np.ndarray:
        """
        Supprime les bandes blanches par analyse ligne/colonne.
        Fallback automatique si le fond n'est pas parfaitement blanc :
        descend de 240 → 230 → 220 → 210 → 200.
        """
        if marge is None:
            marge = self.crop_margin

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        seuil_retenu = self.white_threshold
        lignes = cols = np.array([])

        for seuil in [self.white_threshold, 230, 220, 210, 200]:
            pct_ligne = (gray > seuil).mean(axis=1)
            pct_col   = (gray > seuil).mean(axis=0)
            lignes = np.where(pct_ligne < self.white_ratio_threshold)[0]
            cols   = np.where(pct_col   < self.white_ratio_threshold)[0]

            if len(lignes) and len(cols):
                marge_h = lignes[0] + (h - lignes[-1])
                marge_w = cols[0]   + (w - cols[-1])
                if marge_h > h * 0.01 or marge_w > w * 0.01:
                    seuil_retenu = seuil
                    break
        else:
            self._log("  Aucune bande blanche significative détectée.")
            return img

        if seuil_retenu < self.white_threshold:
            self._log(f"  ⚠️  Fond non-blanc détecté, seuil adapté à {seuil_retenu}.")

        y1 = max(0, int(lignes[0])  - marge)
        y2 = min(h, int(lignes[-1]) + marge)
        x1 = max(0, int(cols[0])    - marge)
        x2 = min(w, int(cols[-1])   + marge)

        self._log(
            f"  Bandes supprimées → haut:{lignes[0]}px  bas:{h-lignes[-1]}px  "
            f"gauche:{cols[0]}px  droite:{w-cols[-1]}px"
        )
        self._bandes             = dict(haut=int(lignes[0]), bas=int(h - lignes[-1]),
                                        gauche=int(cols[0]), droite=int(w - cols[-1]))
        self._white_threshold_used = seuil_retenu
        return img[y1:y2, x1:x2]

    def crop_borders_by_contour(self, img: np.ndarray, marge: Optional[int] = None,
                                 canny_low: int = 20, canny_high: int = 60,
                                 min_content_ratio: float = 0.25) -> np.ndarray:
        """
        Rogne en détectant le contour physique réel de la carte (par
        gradient, indépendamment de la couleur -- même principe que
        ``TiffBackgroundRemover.make_border_transparent_by_contour_cv2``
        côté transparence), plutôt qu'en scannant ligne par ligne/colonne
        par colonne.

        Là où ``crop_borders`` calcule une marge moyenne par ligne/colonne
        sur toute la largeur/hauteur de l'image -- ce qui, pour une carte
        non rectangulaire (coin coupé, forme légèrement trapézoïdale),
        peut laisser une marge quasi nulle au point le plus défavorable
        même si la marge moyenne semble correcte -- cette méthode calcule
        la boîte englobante du contenu réellement détecté (son contour),
        garantissant une marge d'au moins ``marge`` px partout autour de
        la forme réelle de la carte. La taille du noyau de dilatation
        utilisé pour combler les brèches du contour est choisie
        automatiquement par essais croissants (voir
        ``libpostcards.contour_utils.find_content_mask``).

        Se rabat sur ``crop_borders`` (comportement classique) si le
        contour détecté n'est pas fiable (voir ``min_content_ratio``).
        """
        if marge is None:
            marge = self.final_crop_margin

        h, w = img.shape[:2]

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        background, content_ratio, used_dilate = find_content_mask(
            gray,
            denoise=self.contour_denoise,
            clahe=self.contour_clahe,
            auto_canny=self.contour_auto_canny,
            canny_low=canny_low,
            canny_high=canny_high,
            min_content_ratio=min_content_ratio,
        )

        if background is None:
            self._log(
                "  ⚠️  Contour non fiable pour le rognage "
                f"(contenu détecté: {content_ratio:.0%}), repli sur le rognage ligne/colonne."
            )
            return self.crop_borders(img, marge=marge)

        content = ~background

        # On élimine seulement les résidus isolés clairement minuscules
        # (poussière, bruit de numérisation, petite tache non reliée au
        # fond) qui pousseraient sinon la marge calculée à zéro dès
        # qu'un pixel isolé touche un bord de l'image -- mais SANS se
        # limiter à la seule plus grande composante : un coin du carton
        # peut légitimement se retrouver séparé du corps principal de la
        # carte (la dilatation appliquée au contour, voir "edge_dilate",
        # peut sectionner la fine bande de papier qui relie la pointe
        # d'un coin dentelé au reste du carton) sans être du bruit pour
        # autant -- l'exclure couperait ce coin de la carte au rognage.
        content_u8 = content.astype(np.uint8)
        num_content_labels, content_labels, stats, _ = cv2.connectedComponentsWithStats(
            content_u8, connectivity=8)
        if num_content_labels > 1:
            # Label 0 = fond ("non-contenu") ; stats[1:, CC_STAT_AREA] =
            # aire de chaque composante de contenu. Seuil relatif à la
            # plus grande composante (le corps principal de la carte) :
            # une composante minuscule en comparaison est presque
            # certainement du bruit, une composante d'une taille
            # comparable (ex : un coin séparé) ne l'est presque
            # certainement pas.
            areas = stats[1:, cv2.CC_STAT_AREA]
            min_area = max(50, areas.max() * 0.01)
            significant_labels = [i + 1 for i, a in enumerate(areas) if a >= min_area]
            content = np.isin(content_labels, significant_labels)

        ys, xs = np.where(content)
        y0, y1 = int(ys.min()), int(ys.max())
        x0, x1 = int(xs.min()), int(xs.max())

        # Filet de sécurité supplémentaire : sur un bord dentelé à très
        # faible contraste, le contour tracé peut rester ouvert malgré la
        # dilatation, laissant le "fond" se scinder en îlots déconnectés
        # non reliés au bord de l'image -- la boîte englobante du
        # contenu peut alors, à tort, toucher un bord de l'image (marge
        # nulle) même après filtrage de la plus grande composante. Un
        # contour fiable doit laisser de la place tout autour ; si ce
        # n'est pas le cas nulle part, mieux vaut se rabattre sur le
        # rognage ligne/colonne (moins précis, mais pas trompeur) que de
        # produire un rognage à ras du contenu.
        if min(y0, h - 1 - y1, x0, w - 1 - x1) < 3:
            self._log(
                "  ⚠️  Boîte de contour non fiable pour le rognage "
                "(touche un bord de l'image), repli sur le rognage ligne/colonne."
            )
            return self.crop_borders(img, marge=marge)

        y0 = max(0, y0 - marge)
        y1 = min(h, y1 + marge + 1)
        x0 = max(0, x0 - marge)
        x1 = min(w, x1 + marge + 1)

        self._log(
            f"  Rognage par contour → haut:{y0}px  bas:{h-y1}px  "
            f"gauche:{x0}px  droite:{w-x1}px"
        )
        self._bandes = dict(haut=y0, bas=h - y1, gauche=x0, droite=w - x1)
        return img[y0:y1, x0:x1]

    # ── 3. Détection d'angle ─────────────────────────────────────────────────

    def _score(self, gray: np.ndarray, angle: float, seuil: int = 200) -> float:
        """Variance de projection horizontale pour un angle donné."""
        h, w = gray.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        rot = cv2.warpAffine(gray, M, (w, h), borderValue=255)
        _, b = cv2.threshold(rot, seuil, 255, cv2.THRESH_BINARY_INV)
        return float(b.sum(axis=1).var())

    def _hough_horizontal(self, gray_roi: np.ndarray) -> Optional[float]:
        """
        Angle médian des lignes quasi-horizontales via Hough probabiliste.
        Seuil bas (threshold=50) pour capturer même peu de lignes.
        Retourne None si moins de 5 lignes détectées après filtrage IQR.
        """
        blur  = cv2.GaussianBlur(gray_roi, (5, 5), 0)
        bords = cv2.Canny(blur, 30, 100, apertureSize=3)
        lignes = cv2.HoughLinesP(
            bords, rho=1, theta=np.pi / 180,
            threshold=50, minLineLength=100, maxLineGap=10,
        )
        if lignes is None:
            return None

        # cv2.HoughLinesP renvoie un tableau (N, 1, 4) sur les anciennes
        # versions d'OpenCV, et (N, 4) depuis OpenCV 5. On aplatit dans
        # tous les cas pour rester compatible avec les deux formes.
        lignes = np.asarray(lignes).reshape(-1, 4)

        angles = []
        for x1, y1, x2, y2 in lignes:
            if x2 == x1:
                continue
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(a) < 5.0:
                angles.append(a)

        if len(angles) < 5:
            return None

        q25, q75 = np.percentile(angles, 25), np.percentile(angles, 75)
        iqr      = q75 - q25
        filtres  = [a for a in angles if q25 - 1.5 * iqr <= a <= q75 + 1.5 * iqr]
        return float(np.median(filtres)) if len(filtres) >= 5 else None

    def _hough_fort(self, gray_roi: np.ndarray) -> Optional[float]:
        """
        Hough avec seuil élevé pour les images à forte rotation (>2.75°).
        Cherche les lignes longues (>200px) avec angle jusqu'à angle_range.
        """
        blur  = cv2.GaussianBlur(gray_roi, (5, 5), 0)
        bords = cv2.Canny(blur, 50, 150, apertureSize=3)
        lignes = cv2.HoughLinesP(
            bords, rho=1, theta=np.pi / 180,
            threshold=150, minLineLength=200, maxLineGap=15,
        )
        if lignes is None:
            return None

        # Voir _hough_horizontal : compatibilité (N, 1, 4) / (N, 4).
        lignes = np.asarray(lignes).reshape(-1, 4)

        angles = []
        for x1, y1, x2, y2 in lignes:
            if x2 == x1:
                continue
            a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(a) < self.angle_range:
                angles.append(a)

        if len(angles) < 5:
            return None

        q25, q75 = np.percentile(angles, 25), np.percentile(angles, 75)
        iqr      = q75 - q25
        filtres  = [a for a in angles if q25 - 1.5 * iqr <= a <= q75 + 1.5 * iqr]
        return float(np.median(filtres)) if len(filtres) >= 5 else None

    def _local_peaks(self, scores: np.ndarray, fenetre: int = 7) -> list[int]:
        """
        Détecte les indices des pics locaux dans la courbe de scores après lissage.
        Un pic local = score localement maximal (dérivée change de + à -).
        """
        scores_s = np.convolve(scores, np.ones(fenetre) / fenetre, mode='same')
        diff     = np.diff(scores_s)
        return [i + 1 for i in range(len(diff) - 1)
                if diff[i] > 0 and diff[i + 1] <= 0]

    def crop_borders_by_quad(self, img: np.ndarray, marge: Optional[int] = None,
                              target_width: int = 1500,
                              white_threshold: int = 245,
                              min_area_ratio: float = 0.15) -> np.ndarray:
        """
        Rogne en détectant l'enveloppe quadrilatère approximative de la
        carte sur une copie réduite de ``img`` (voir
        ``libpostcards.contour_utils.detect_card_quad``), puis en
        rognant l'image en PLEINE résolution à la boîte englobante de ce
        quadrilatère + ``marge``.

        Contrairement à ``crop_borders_by_contour`` (Canny + composantes
        connexes, sensible au bruit sur un bord à faible contraste),
        cette méthode travaille par seuil de couleur sur une image
        réduite : réduire d'abord lisse le bruit de numérisation, ce qui
        rend la distinction blanc du fond / blanc (généralement
        légèrement teinté) du papier bien plus fiable. Le quadrilatère
        obtenu n'est qu'une enveloppe approximative -- il ne suit pas le
        détail fin d'un bord dentelé -- d'où une marge généreuse pour
        garantir que ce bord reste intégralement inclus comme contenu ;
        le détourage fin (qui, lui, suit le bord réel) s'applique
        ensuite séparément sur cette image déjà correctement cadrée.

        N'apporte rien sur un côté dont la couleur est réellement
        identique entre carte et fond, pas seulement bruitée (un verso
        vierge très blanc, par exemple) : aucune réduction ne peut faire
        apparaître une différence de couleur qui n'existe pas. D'où
        ``min_area_ratio`` : si la boîte englobante détectée est plus
        petite que cette fraction de l'image de travail, la détection
        est jugée peu fiable (a presque certainement accroché une tache
        interne -- écriture, tampon... -- plutôt que le bord réel de la
        carte) et on se rabat sur ``crop_borders`` (comportement
        classique).
        """
        if marge is None:
            marge = self.final_crop_margin

        h, w = img.shape[:2]

        quad, _scale = detect_card_quad(
            img, target_width=target_width, white_threshold=white_threshold)

        if quad is not None:
            area_ratio = (
                (quad[:, 0].max() - quad[:, 0].min())
                * (quad[:, 1].max() - quad[:, 1].min())
                / (h * w)
            )
        else:
            area_ratio = 0.0

        if quad is None or area_ratio < min_area_ratio:
            self._log(
                "  ⚠️  Quadrilatère non détecté ou implausible pour le rognage "
                f"(aire: {area_ratio:.0%}), repli sur le rognage ligne/colonne."
            )
            return self.crop_borders(img, marge=marge)

        x0 = max(0, int(quad[:, 0].min()) - marge)
        x1 = min(w, int(quad[:, 0].max()) + marge + 1)
        y0 = max(0, int(quad[:, 1].min()) - marge)
        y1 = min(h, int(quad[:, 1].max()) + marge + 1)

        self._log(
            f"  Rognage par quadrilatère → haut:{y0}px  bas:{h-y1}px  "
            f"gauche:{x0}px  droite:{w-x1}px"
        )
        self._bandes = dict(haut=y0, bas=h - y1, gauche=x0, droite=w - x1)
        return img[y0:y1, x0:x1]

    def detect_angle(self, img: np.ndarray) -> float:
        """
        Détection d'angle robuste par pics locaux + guidage Hough.

        Étapes :
        1. Projection classique sur ±5° (pas 0.05°) sur image réduite.
        2. Lissage + détection des pics locaux.
        3. Restriction des pics à la plage plausible ±angle_range (un pic
           hors plage vient presque toujours du contenu de la scène et
           non d'un vrai désalignement du carton).
        4. Hough quasi-horizontal → sélectionner le pic le plus proche ;
           à défaut, retenir le pic restant le plus proche de 0°.
        5. Passe fine ±0.3° à 0.01° autour du pic retenu.
        6. Fallback si courbe monotone (ou tous les pics hors plage) :
           Hough fort → masque texte → zéro.
        """
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2))

        # ── Étape 1 : scores sur ±5° (pas 0.05°) ─────────────────────────────
        angles = np.arange(-5.0, 5.05, 0.05)
        scores = np.array([self._score(small, a) for a in angles])

        # ── Étape 2 : pics locaux ─────────────────────────────────────────────
        pics = self._local_peaks(scores)
        angles_pics = [float(angles[p]) for p in pics]

        # ── Étape 3 : Hough quasi-horizontal ─────────────────────────────────
        a_hough = self._hough_horizontal(gray)
        self._hough_angle = a_hough

        self._log(f"  Pics locaux détectés        : {[f'{a:+.2f}°' for a in angles_pics]}")
        self._log(
            f"  Hough quasi-horizontal      : {a_hough:+.3f}°"
            if a_hough is not None else
            "  Hough quasi-horizontal      : —"
        )

        # Ne garder que les pics à l'intérieur de la plage jugée plausible
        # pour un simple défaut de placement du carton sur le scanner
        # (angle_range) : un pic hors de cette plage vient presque
        # toujours d'une ligne dominante de la scène photographiée (toit,
        # berge, horizon...) plutôt que d'un vrai désalignement du carton,
        # et l'appliquer ferait pivoter le carton lui-même hors de son
        # cadre plutôt que de le redresser.
        angles_pics_ok = [a for a in angles_pics if abs(a) <= self.angle_range]
        if len(angles_pics_ok) != len(angles_pics):
            hors_plage = [a for a in angles_pics if abs(a) > self.angle_range]
            self._log(
                f"  Pics hors plage ±{self.angle_range}° ignorés : "
                + ", ".join(f"{a:+.2f}°" for a in hors_plage)
            )

        if angles_pics_ok:
            if a_hough is not None:
                # Prendre le pic le plus proche du Hough
                best_g = min(angles_pics_ok, key=lambda x: abs(x - a_hough))
                mode   = "pic+Hough"
            else:
                # Sans Hough pour trancher entre plusieurs pics, impossible
                # de distinguer un vrai désalignement du carton d'une
                # ligne dominante de la scène (toit, berge, horizon...) :
                # on retient le pic le plus proche de 0°, hypothèse la
                # plus sûre pour un carton habituellement posé à peu près
                # droit sur le scanner. Retenir le pic au score maximal
                # (comportement précédent) reviendrait à laisser le
                # contenu de la photo dicter la rotation.
                best_g = min(angles_pics_ok, key=lambda x: abs(x))
                mode   = "pic_proche_zéro"
        else:
            # Courbe monotone, ou tous les pics hors plage ±angle_range :
            # prendre le maximum global et laisser les fallbacks trancher.
            best_g = float(angles[np.argmax(scores)])
            mode   = "max_global"

            # Si aux bornes → fallbacks
            if abs(best_g) >= 2.75:
                a_fort = self._hough_fort(gray)
                self._log(
                    f"  ⚠️  Pic aux bornes ({best_g:+.2f}°) — Hough fort : "
                    + (f"{a_fort:+.3f}°" if a_fort is not None else "—")
                )
                if a_fort is not None and abs(a_fort) < self.angle_range:
                    best_g = a_fort
                    mode   = "Hough fort"
                else:
                    # Masque texte
                    masque = small.copy()
                    masque[small >= 120] = 255
                    ag10   = np.arange(-self.angle_range, self.angle_range + 0.25, 0.25)
                    best_m = float(ag10[np.argmax(
                        [self._score(masque, a, 120) for a in ag10]
                    )])
                    self._log(f"  Masque texte ±{self.angle_range}°         : {best_m:+.2f}°")
                    if abs(best_m) < self.angle_range - 1.0:
                        best_g = best_m
                        mode   = f"masque texte ±{self.angle_range}°"
                    else:
                        best_g = 0.0
                        mode   = "zéro fallback"

        # ── Étape 4 : passe fine ±0.3° à 0.01° ──────────────────────────────
        angles_f = np.arange(best_g - 0.3, best_g + 0.3 + 0.01, 0.01)
        a_proj   = float(angles_f[np.argmax([self._score(small, a) for a in angles_f])])

        self._log(f"  Pic retenu                  : {best_g:+.2f}°  [{mode}]")
        self._log(f"  Angle après passe fine      : {a_proj:+.3f}°")

        self._angle_proj  = a_proj
        self._mode        = mode
        self.detected_angle = round(a_proj, 2)
        return self.detected_angle

    # ── 4. Redressement ──────────────────────────────────────────────────────

    def deskew(self, img: np.ndarray, angle: float) -> np.ndarray:
        """
        Applique la rotation et agrandit le canevas pour ne rien couper.
        Bords ajoutés remplis en blanc.
        """
        if abs(angle) < 0.05:
            self._log("  Angle négligeable, pas de rotation appliquée.")
            return img

        h, w = img.shape[:2]
        M    = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        cos  = abs(M[0, 0])
        sin  = abs(M[0, 1])
        new_w = int(h * sin + w * cos)
        new_h = int(h * cos + w * sin)
        M[0, 2] += (new_w - w) / 2
        M[1, 2] += (new_h - h) / 2

        return cv2.warpAffine(
            img, M, (new_w, new_h),
            flags=cv2.INTER_CUBIC,
            borderValue=(255, 255, 255),
        )

    # ── 5. Pipeline ──────────────────────────────────────────────────────────

    def process_image(self, img: np.ndarray) -> np.ndarray:
        """
        Pipeline complet sur tableau numpy BGR.
        Peuple `self.detected_angle` et `self.report`.
        """
        self._reset_state()
        taille_orig = (img.shape[1], img.shape[0])

        self._log("\n✂️  Rognage initial des bandes blanches...")
        img = self.crop_borders(img)
        taille_apres = (img.shape[1], img.shape[0])
        self._log(f"  Taille après rognage : {taille_apres[0]}×{taille_apres[1]} px")

        self._log("\n🔍 Détection de l'angle...")
        angle = self.detect_angle(img)

        self._log("\n↩️  Redressement...")
        img = self.deskew(img, angle)

        self._log("\n✂️  Rognage final (résidus de rotation)...")
        if self.quad_final_crop:
            img = self.crop_borders_by_quad(img, marge=self.final_crop_margin)
        elif self.contour_final_crop:
            img = self.crop_borders_by_contour(img, marge=self.final_crop_margin)
        else:
            img = self.crop_borders(img, marge=self.final_crop_margin)
        final_size = (img.shape[1], img.shape[0])
        self._log(f"  Taille finale : {final_size[0]}×{final_size[1]} px")

        self.report = CorrectionReport(
            original_size     = taille_orig,
            size_after_crop = taille_apres,
            final_size        = final_size,
            removed_borders    = dict(self._bandes),
            white_threshold_used  = self._white_threshold_used,
            projection_angle     = self._angle_proj,
            hough_angle          = self._hough_angle,
            final_angle          = self.detected_angle,
            detection_mode       = self._mode,
        )
        return img

    def process_file(self, chemin_entree: str, chemin_sortie: str) -> None:
        """
        Charge, traite et sauvegarde une image.
        Format déduit de l'extension (.jpg→JPEG 95, .png→PNG 3, .tiff→sans perte deflate).
        """
        self._log(f"\n📂 Chargement : {chemin_entree}")
        img = self.load_image(chemin_entree)
        img = self.process_image(img)

        ext = Path(chemin_sortie).suffix.lower()

        if ext in (".tif", ".tiff"):
            # PIL plutôt que cv2.imwrite : compression "tiff_deflate"
            # (zlib, sans perte, décodable par tifffile sans dépendance
            # additionnelle -- contrairement au LZW utilisé par défaut
            # par cv2.imwrite, qui nécessite le paquet "imagecodecs")
            # et balise correctement un éventuel canal alpha (voir
            # tkpostcards.libs.scan_prepare.make_corrector).
            from PIL import Image
            if img.shape[2] == 4:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
            else:
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            Image.fromarray(rgb).save(chemin_sortie, compression="tiff_deflate")
        else:
            params: list[int] = []
            if ext in (".jpg", ".jpeg"):
                params = [cv2.IMWRITE_JPEG_QUALITY, 95]
            elif ext == ".png":
                params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
            cv2.imwrite(chemin_sortie, img, params)

        self._log(f"\n✅ Image sauvegardée : {chemin_sortie}")

        if self.verbose and self.report:
            print()
            print(self.report)


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python -m libpostcards.scan_corrector <image_entrée> [image_sortie]")
        print("Formats supportés : tiff, jpg, jpeg, png, bmp")
        print("Exemple : python -m libpostcards.scan_corrector scan.tiff scan_corrige.jpg")
        sys.exit(1)

    entree = sys.argv[1]
    sortie = (sys.argv[2] if len(sys.argv) >= 3 else
              str(Path(entree).with_stem(Path(entree).stem + "_corrige").with_suffix(".jpg")))

    ScanCorrector().process_file(entree, sortie)
