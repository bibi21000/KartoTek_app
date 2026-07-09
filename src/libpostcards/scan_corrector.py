#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
# ~ from gettext import gettext as _

import cv2
import numpy as np


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
        Pixels conservés autour du contenu lors du rognage (défaut 20).
    angle_range : float
        Plage de recherche ±X° pour les cas extrêmes (défaut 10.0).
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
        angle_range:   float = 10.0,
        verbose:       bool  = True,
    ) -> None:
        self.white_threshold   = white_threshold
        self.white_ratio_threshold     = white_ratio_threshold
        self.crop_margin = crop_margin
        self.angle_range   = angle_range
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
        """Charge une image depuis le disque (tous formats OpenCV supportés)."""
        img = cv2.imread(chemin)
        if img is None:
            raise FileNotFoundError(f"Impossible de load_image : {chemin}")
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

    def detect_angle(self, img: np.ndarray) -> float:
        """
        Détection d'angle robuste par pics locaux + guidage Hough.

        Étapes :
        1. Projection classique sur ±5° (pas 0.05°) sur image réduite.
        2. Lissage + détection des pics locaux.
        3. Hough quasi-horizontal → sélectionner le pic le plus proche.
        4. Passe fine ±0.3° à 0.01° autour du pic retenu.
        5. Fallback si courbe monotone et résultat aux bornes :
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

        if angles_pics:
            if a_hough is not None:
                # Prendre le pic le plus proche du Hough
                best_g = min(angles_pics, key=lambda x: abs(x - a_hough))
                mode   = "pic+Hough"
            else:
                # Prendre le pic avec le meilleur score
                best_g = float(angles[sorted(pics, key=lambda p: -scores[p])[0]])
                mode   = "pic_max"
        else:
            # Courbe monotone : prendre le maximum global
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
        img = self.crop_borders(img, marge=5)
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
        Format déduit de l'extension (.jpg→JPEG 95, .png→PNG 3, .tiff→sans perte).
        """
        self._log(f"\n📂 Chargement : {chemin_entree}")
        img = self.load_image(chemin_entree)
        img = self.process_image(img)

        ext    = Path(chemin_sortie).suffix.lower()
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
