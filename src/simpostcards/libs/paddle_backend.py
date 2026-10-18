# -*- encoding: utf-8 -*-
"""
simpostcards/libs/paddle_backend.py - OCR PaddleOCR, pour POST
/api/compute_ocr avec ``engine=paddleocr``.

Même schéma que ``libs.segment_backend`` (SAM) :

- ``paddleocr`` / ``paddle`` ne sont pas importés en tête de module
  (``PADDLE_AVAILABLE`` se contente de les chercher) : ``import paddleocr``
  charge tout ``paddlex``, ce qui alourdirait le démarrage de tout
  déploiement, même sans PaddleOCR ;
- les moteurs (un par langue de reconnaissance, ``libpostcards.ocr_paddle
  .create_engine``) sont chargés à la première requête puis gardés en
  mémoire ;
- périphérique ("cpu", "gpu:0") et version de PP-OCR sont des choix du
  déploiement ([simpostcards] paddleocr_device / paddleocr_version), pas des
  paramètres de requête ; la langue et le seuil de confiance, eux, viennent
  du client ;
- libération automatique après inactivité ([simpostcards] gpu_idle_timeout,
  voir ``libs._idle_unload``), y compris le cache GPU de paddle ;
- un verrou protège le chargement, un autre l'inférence : le moteur n'est
  pas fait pour être appelé en parallèle.

Les modèles sont téléchargés depuis Internet à la première utilisation
(``~/.paddlex``) : un serveur sans accès réseau doit les avoir à l'avance.
"""

from __future__ import annotations

import threading

from PIL import Image

from libpostcards import ocr_paddle
from libpostcards.langs import resolve_paddle_language
from simpostcards.libs._idle_unload import IdleUnloadTimer

PADDLE_AVAILABLE = ocr_paddle.PADDLE_AVAILABLE

# Garde-fou : un moteur par langue de reconnaissance utilisée ; au-delà, on
# oublie le plus ancien (chaque moteur pèse quelques dizaines à centaines
# de Mo).
MAX_ENGINES = 3


class _PaddleModels:
    """Singleton paresseux : charge et garde en cache les moteurs PaddleOCR."""

    def __init__(self) -> None:
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._engines: dict = {}
        self._idle_timer = IdleUnloadTimer("PADDLEOCR", self._unload)

    def configure_idle_timeout(self, seconds: float | None) -> None:
        self._idle_timer.configure(seconds)

    def _unload(self) -> bool:
        """Callback du minuteur (la mémoire est rendue ensuite par
        ``release_memory()``). True si des moteurs étaient chargés."""
        with self._load_lock:
            if not self._engines:
                return False
            self._engines.clear()
        return True

    def _ensure(self, lang, device, ocr_version):
        # La clé est le code PaddleOCR résolu : "fra", "fra+eng" et "fr"
        # partagent le même moteur.
        code = resolve_paddle_language(lang) or lang
        key = (code, device, ocr_version)
        with self._load_lock:
            engine = self._engines.get(key)
            if engine is None:
                engine = ocr_paddle.create_engine(
                    lang, device=device, ocr_version=ocr_version)
                while len(self._engines) >= MAX_ENGINES:
                    self._engines.pop(next(iter(self._engines)))
                self._engines[key] = engine
            return engine

    def read(self, image, lang, device, ocr_version, min_score):
        # busy() : pas de déchargement pendant le chargement du modèle (qui
        # peut inclure un téléchargement) ni pendant la lecture.
        with self._idle_timer.busy():
            engine = self._ensure(lang, device, ocr_version)
            with self._infer_lock:
                return ocr_paddle.read_text(engine, image, min_score)


_models = _PaddleModels()


def configure_idle_timeout(seconds: float | None) -> None:
    """Appelé une fois au démarrage de l'appli (simpostcards.__init__)."""
    _models.configure_idle_timeout(seconds)


def compute_paddle_ocr(
    image: Image.Image,
    lang: str = "fra",
    device: str | None = None,
    ocr_version: str | None = None,
    min_score: float = ocr_paddle.DEFAULT_MIN_SCORE,
) -> str:
    """
    Extrait le texte de *image* (déjà en mémoire) avec PaddleOCR, dans la
    langue *lang* (codes tesseract ou PaddleOCR).

    Nécessite paddleocr + paddlepaddle -- voir PADDLE_AVAILABLE ; l'appelant
    est responsable de ne pas appeler cette fonction sinon. Lève
    ``libpostcards.langs.LanguageUnavailable`` (langue sans modèle) ou
    ``libpostcards.langs.EngineUnavailable`` (modèles introuvables...).
    """
    return _models.read(image, lang, device, ocr_version, min_score)
