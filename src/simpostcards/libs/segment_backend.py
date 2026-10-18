# -*- encoding: utf-8 -*-
"""
simpostcards/libs/segment_backend.py - Segmentation SAM d'une image, pour
POST /api/compute_segments.

Sert ``libpostcards.masks.StampDetector`` quand ``external_helper`` est
configuré (génération des masques COLMAP : voir ``kttools masks`` et le
menu « Masques » de ktmanager). Le serveur ne fait QUE la partie lourde,
la segmentation automatique ; le choix des segments qui sont des timbres
(forme, taille, position) reste côté client, avec les seuils de la section
[masks] de son postcards.conf -- même répartition que pour la détection
d'objets (voir ``libs.detection_backend``).

SAM est utilisé via le pipeline ``mask-generation`` de transformers (pas
besoin du paquet ``segment-anything``). Comme pour BLIP/DETR :

- transformers/torch sont importés localement, pas en tête de module ;
- le modèle est chargé à la première requête puis gardé en mémoire ;
- modèle et device sont fixés par la configuration serveur ([simpostcards]
  segment_model_name / segment_device), pas par requête (changer de modèle
  = rechargement complet), alors que la grille de points et les seuils de
  qualité SAM sont de simples paramètres d'inférence, donc par requête ;
- libération automatique après inactivité : voir [simpostcards]
  gpu_idle_timeout et ``libs._idle_unload``.

Un verrou distinct protège le chargement du modèle et l'inférence : le
pipeline n'est pas fait pour être appelé en parallèle, et deux
segmentations simultanées sur un GPU partagé risquent surtout de saturer
sa mémoire.
"""

from __future__ import annotations

import importlib.util
import threading

from PIL import Image

from simpostcards.libs._idle_unload import IdleUnloadTimer

# find_spec plutôt qu'un import : importer transformers/torch ici
# alourdirait le démarrage de tout déploiement, même sans SAM.
SEGMENT_AVAILABLE = all(
    importlib.util.find_spec(m) is not None for m in ("transformers", "torch"))

# Même valeur par défaut que libpostcards.masks.DEFAULT_SAM_MODEL.
DEFAULT_MODEL_NAME = "facebook/sam-vit-base"

# Nombre maximal de segments renvoyés (les mieux notés d'abord) : filet de
# sécurité contre une réponse démesurée, pas un réglage utile (SAM en
# produit rarement plus de quelques dizaines avec ces seuils).
MAX_SEGMENTS_CAP = 200


class _SegmentModels:
    """Singleton paresseux : charge et garde en cache le pipeline SAM."""

    def __init__(self) -> None:
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model_name = None
        self._pipe = None
        self._idle_timer = IdleUnloadTimer("SAM", self._unload)

    def configure_idle_timeout(self, seconds: float | None) -> None:
        """Voir [simpostcards] gpu_idle_timeout / le docstring du module."""
        self._idle_timer.configure(seconds)

    def _unload(self) -> bool:
        """Callback du minuteur (la mémoire est rendue ensuite par
        ``release_memory()``). Renvoie True si SAM était chargé."""
        with self._load_lock:
            if self._pipe is None:
                return False
            self._pipe = None
            self._model_name = None
        return True

    def _ensure(self, model_name: str, device: str | None):
        with self._load_lock:
            if self._pipe is not None and self._model_name == model_name:
                return self._pipe
            import torch
            from transformers import pipeline

            resolved = device or ("cuda" if torch.cuda.is_available() else "cpu")
            self._pipe = pipeline("mask-generation", model=model_name,
                                  device=resolved)
            self._model_name = model_name
            return self._pipe

    def segment(self, image: Image.Image, model_name: str, device: str | None,
                points_per_side: int, pred_iou_thresh: float,
                stability_thresh: float, max_masks: int):
        # busy() : SAM sur cpu peut durer longtemps ; pas de déchargement
        # en plein calcul (voir libs._idle_unload).
        with self._idle_timer.busy():
            return self._segment(image, model_name, device, points_per_side,
                                 pred_iou_thresh, stability_thresh, max_masks)

    def _segment(self, image: Image.Image, model_name: str, device: str | None,
                 points_per_side: int, pred_iou_thresh: float,
                 stability_thresh: float, max_masks: int):
        import numpy as np

        pipe = self._ensure(model_name, device)
        self._idle_timer.touch()
        with self._infer_lock:
            out = pipe(
                image,
                points_per_batch=64,
                points_per_crop=points_per_side,
                pred_iou_thresh=pred_iou_thresh,
                stability_score_thresh=stability_thresh,
            )
        self._idle_timer.touch()

        masks = [np.asarray(m, dtype=bool) for m in out["masks"]]
        scores = [float(x) for x in out.get("scores", [1.0] * len(masks))]
        ranked = sorted(zip(scores, masks), key=lambda t: -t[0])
        return [(s, m) for s, m in ranked if m.any()][:max_masks]


_models = _SegmentModels()


def configure_idle_timeout(seconds: float | None) -> None:
    """Appelé une fois au démarrage de l'appli (simpostcards.__init__)."""
    _models.configure_idle_timeout(seconds)


def compute_segments(
    image: Image.Image,
    model_name: str = DEFAULT_MODEL_NAME,
    device: str | None = None,
    points_per_side: int = 20,
    pred_iou_thresh: float = 0.86,
    stability_thresh: float = 0.90,
    max_masks: int = MAX_SEGMENTS_CAP,
) -> list[tuple[float, "object"]]:
    """
    Segmente *image* (RGB, déjà en mémoire) avec SAM : liste de
    ``(score, masque booléen HxW)`` triée par score décroissant, à la
    taille de *image*.

    Nécessite transformers + torch -- voir SEGMENT_AVAILABLE ; l'appelant
    est responsable de ne pas appeler cette fonction sinon.
    """
    return _models.segment(
        image, model_name=model_name, device=device,
        points_per_side=points_per_side, pred_iou_thresh=pred_iou_thresh,
        stability_thresh=stability_thresh,
        max_masks=min(max_masks, MAX_SEGMENTS_CAP))
