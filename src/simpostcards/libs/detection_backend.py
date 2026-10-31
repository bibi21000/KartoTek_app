# -*- encoding: utf-8 -*-
"""
simpostcards/libs/detection_backend.py - Légende (BLIP) et détection
d'objets (DETR) d'une image, pour POST /api/compute_detection.

transformers/torch sont volontairement importés localement (dans
_ensure_*()) et non ici en tête de module : cela permet d'importer ce
module sans les avoir installés -- même principe que
``simpostcards.libs.embedder``/``ocr_backend``.

Reprend la même logique de traitement que
``libpostcards.detection.PostcardDetection`` (mots-artefacts BLIP
retirés, détections DETR dédoublonnées par libellé en gardant le
meilleur score, triées par confiance décroissante) -- SANS la
traduction : ce module ne produit que de l'anglais. La traduction
(``deep_translator``, qui ne nécessite pas torch) est faite par la route
(``POST /api/compute_detection``, champ ``lang``, voir
``simpostcards.blueprints.api``) et ``libpostcards.langs``, puis les
exclusions et la troncature restent appliquées côté client sur les
libellés anglais.

Modèles et device fixés par la configuration serveur (voir
[simpostcards] detection_model_name / detection_objects_model_name /
detection_device dans postcards.conf) : contrairement à ``threshold``
(filtre appliqué aux résultats du modèle déjà chargé, donc bon marché
à faire varier par requête), changer de modèle a un coût (rechargement
complet) et n'est donc PAS un paramètre de requête -- un client qui
veut un modèle différent doit pointer vers un déploiement simpostcards
configuré pour, pas demander un changement à la volée.

Un verrou distinct pour chaque modèle (BLIP / DETR) : une requête
"légende seule" ne doit pas attendre derrière le chargement (parfois
long) du modèle DETR demandé par une requête concurrente, et
inversement.

Libération automatique après inactivité (serveur GPU partagé)
----------------------------------------------------------------
Comme ``simpostcards.libs.embedder`` (CLIP) : chaque modèle (BLIP,
DETR) reste en mémoire GPU indéfiniment une fois chargé, sauf à
configurer [simpostcards] gpu_idle_timeout dans postcards.conf -- au-
delà de ce délai (en secondes) sans requête pour CE modèle
spécifiquement, il est déchargé et sa mémoire GPU rendue. BLIP et DETR
ont chacun leur propre minuteur (voir ``_DetectionModels._caption_idle_timer``/
``_objects_idle_timer``) : n'utiliser que la légende, par exemple,
finit par libérer la mémoire de DETR sans jamais toucher à celle de
BLIP, tant qu'on continue à demander des légendes. Désactivé par
défaut (0/absent : comportement historique, jamais déchargé).
"""

from __future__ import annotations

import logging
import re
import threading

from PIL import Image

from simpostcards.libs._idle_unload import IdleUnloadTimer

_logger = logging.getLogger(__name__)

try:
    from transformers import BlipProcessor, BlipForConditionalGeneration  # noqa: F401
    CAPTION_AVAILABLE = True
except ImportError:
    CAPTION_AVAILABLE = False

try:
    from transformers import DetrImageProcessor, DetrForObjectDetection  # noqa: F401
    OBJECTS_AVAILABLE = True
except ImportError:
    OBJECTS_AVAILABLE = False

# Mêmes valeurs par défaut que libpostcards.detection.PostcardDetection.
DEFAULT_MODEL_NAME = "Salesforce/blip-image-captioning-large"
DEFAULT_OBJECTS_MODEL_NAME = "facebook/detr-resnet-101"

# Nombre maximal d'objets renvoyés par compute_objects(), quel que soit
# le ``max_objects`` demandé côté client (voir libpostcards.detection,
# qui applique lui-même sa propre troncature APRÈS filtrage des
# exclusions -- ce plafond n'est qu'un filet de sécurité contre une
# réponse démesurée, pas la troncature "utile").
_MAX_OBJECTS_SAFETY_CAP = 50

# Voir libpostcards.detection._BLIP_ARTIFACT_WORDS : dupliqué
# volontairement (pas importé, pour ne pas faire dépendre simpostcards
# de libpostcards pour un si petit bout de logique) -- garder les deux
# synchronisés en cas de modification.
_BLIP_ARTIFACT_WORDS = ("arafed", "araffed", "araffe", "araffes")
_BLIP_ARTIFACT_RE = re.compile(
    r"\b(?:an?\s+)?(?:" + "|".join(_BLIP_ARTIFACT_WORDS) + r")\b", re.IGNORECASE
)


def _strip_blip_artifacts(text: str) -> str:
    if not text:
        return text

    def _replace(match):
        return "a" if text[match.end():].strip() else ""

    cleaned = _BLIP_ARTIFACT_RE.sub(_replace, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or text


class _DetectionModels:
    """Singleton paresseux : charge et garde en cache les modèles BLIP
    et DETR, chacun protégé par son propre verrou. Voir le docstring du
    module pour le détail des garanties (paresseux, mis en cache,
    thread-safe, verrous indépendants)."""

    def __init__(self) -> None:
        self._caption_lock = threading.Lock()
        self._caption_model_name = None
        self._processor = None
        self._model = None
        self._device = None
        self._caption_idle_timer = IdleUnloadTimer("BLIP", self._unload_caption)

        self._objects_lock = threading.Lock()
        self._objects_model_name = None
        self._objects_processor = None
        self._objects_model = None
        self._objects_device = None
        self._objects_idle_timer = IdleUnloadTimer("DETR", self._unload_objects)

    def configure_idle_timeout(self, seconds: float | None) -> None:
        """Voir [simpostcards] gpu_idle_timeout / le docstring du module."""
        self._caption_idle_timer.configure(seconds)
        self._objects_idle_timer.configure(seconds)


    def _unload_caption(self) -> bool:
        """Callback de ``self._caption_idle_timer`` : voir
        ``_ClipEmbedder._unload`` dans embedder.py pour la note sur la
        sûreté vis-à-vis d'une requête BLIP déjà en cours."""
        with self._caption_lock:
            if self._model is None:
                return False
            self._model = None
            self._processor = None
            self._device = None
            self._caption_model_name = None
        return True

    def _unload_objects(self) -> bool:
        """Callback de ``self._objects_idle_timer`` (voir _unload_caption)."""
        with self._objects_lock:
            if self._objects_model is None:
                return False
            self._objects_model = None
            self._objects_processor = None
            self._objects_device = None
            self._objects_model_name = None
        return True

    def _ensure_caption(self, model_name: str, device: str | None) -> None:
        with self._caption_lock:
            if self._model is not None and self._caption_model_name == model_name:
                return

            import torch
            from transformers import BlipProcessor, BlipForConditionalGeneration

            resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")

            processor = BlipProcessor.from_pretrained(model_name)
            model = BlipForConditionalGeneration.from_pretrained(model_name)
            model = model.to(resolved_device)
            model.eval()

            self._processor = processor
            self._model = model
            self._device = resolved_device
            self._caption_model_name = model_name

    def caption(self, image: Image.Image, model_name: str, device: str | None,
                prompt: str | None = None) -> str:
        # busy() : voir libs._idle_unload (pas de déchargement en plein calcul).
        with self._caption_idle_timer.busy():
            return self._caption(image, model_name, device, prompt)

    def _caption(self, image: Image.Image, model_name: str, device: str | None,
                 prompt: str | None = None) -> str:
        import torch

        self._ensure_caption(model_name, device)
        self._caption_idle_timer.touch()

        if prompt:
            # Légende conditionnée : BLIP complète ``prompt`` (ex : "a postcard of").
            inputs = self._processor(image, text=prompt, return_tensors="pt").to(self._device)
            gen_kwargs = {"max_new_tokens": 40, "num_beams": 3}
        else:
            inputs = self._processor(image, return_tensors="pt").to(self._device)
            gen_kwargs = {}
        with torch.no_grad():
            out = self._model.generate(**inputs, **gen_kwargs)

        raw = self._processor.decode(out[0], skip_special_tokens=True)
        self._caption_idle_timer.touch()
        return _strip_blip_artifacts(raw)

    def _ensure_objects(self, model_name: str, device: str | None) -> None:
        with self._objects_lock:
            if self._objects_model is not None and self._objects_model_name == model_name:
                return

            import torch
            from transformers import DetrImageProcessor, DetrForObjectDetection

            resolved_device = device or ("cuda" if torch.cuda.is_available() else "cpu")

            processor = DetrImageProcessor.from_pretrained(model_name)
            model = DetrForObjectDetection.from_pretrained(model_name)
            model = model.to(resolved_device)
            model.eval()

            self._objects_processor = processor
            self._objects_model = model
            self._objects_device = resolved_device
            self._objects_model_name = model_name

    def detect_objects(self, image: Image.Image, model_name: str, device: str | None,
                        threshold: float) -> list[tuple[str, float]]:
        with self._objects_idle_timer.busy():
            return self._detect_objects(image, model_name, device, threshold)

    def _detect_objects(self, image: Image.Image, model_name: str, device: str | None,
                        threshold: float) -> list[tuple[str, float]]:
        import torch

        self._ensure_objects(model_name, device)
        self._objects_idle_timer.touch()

        inputs = self._objects_processor(images=image, return_tensors="pt").to(
            self._objects_device)
        with torch.no_grad():
            outputs = self._objects_model(**inputs)

        # (height, width) attendu par post_process_object_detection ;
        # image.size est (width, height), d'où l'inversion.
        target_sizes = torch.tensor([image.size[::-1]])
        results = self._objects_processor.post_process_object_detection(
            outputs, target_sizes=target_sizes, threshold=threshold
        )[0]

        best_by_label = {}
        for score, label_id in zip(results["scores"], results["labels"]):
            label = self._objects_model.config.id2label[label_id.item()]
            score = score.item()
            if label not in best_by_label or score > best_by_label[label]:
                best_by_label[label] = score

        detections = sorted(best_by_label.items(), key=lambda item: item[1], reverse=True)
        self._objects_idle_timer.touch()
        return detections[:_MAX_OBJECTS_SAFETY_CAP]


_models = _DetectionModels()


def configure_idle_timeout(seconds: float | None) -> None:
    """Voir [simpostcards] gpu_idle_timeout dans postcards.conf --
    appelé une fois au démarrage de l'appli (simpostcards.__init__)."""
    _models.configure_idle_timeout(seconds)


def compute_caption(
    image: Image.Image,
    model_name: str = DEFAULT_MODEL_NAME,
    device: str | None = None,
    prompt: str | None = None,
) -> str:
    """
    Génère une légende (en anglais, non traduite) décrivant *image*
    (déjà en mémoire) via BLIP. Nécessite transformers + torch
    installés -- voir CAPTION_AVAILABLE ; l'appelant est responsable de
    ne pas appeler cette fonction sinon.
    """
    return _models.caption(image, model_name=model_name, device=device,
                           prompt=prompt)


def compute_objects(
    image: Image.Image,
    model_name: str = DEFAULT_OBJECTS_MODEL_NAME,
    device: str | None = None,
    threshold: float = 0.92,
) -> list[tuple[str, float]]:
    """
    Détecte les objets présents dans *image* (déjà en mémoire) via
    DETR : liste de ``(label, score)`` (libellés anglais), dédoublonnée
    par libellé (meilleur score conservé) et triée par confiance
    décroissante -- PAS filtrée par exclusions ni tronquée à un
    ``max_objects`` "utile" (voir libpostcards.detection.detect_objects,
    qui applique ces deux étapes côté client, identiquement que ce
    résultat vienne d'ici ou d'un calcul local).

    Nécessite transformers + torch installés -- voir OBJECTS_AVAILABLE ;
    l'appelant est responsable de ne pas appeler cette fonction sinon.
    """
    return _models.detect_objects(image, model_name=model_name, device=device,
                                   threshold=threshold)
