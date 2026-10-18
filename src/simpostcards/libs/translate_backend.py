# -*- encoding: utf-8 -*-
"""
simpostcards/libs/translate_backend.py - Traduction locale (Marian) du
serveur, pour ``POST /api/translate`` et le champ ``lang`` de
``/api/compute_detection``.

Choix du moteur ([simpostcards] translate_engine) :

- ``google`` (défaut) : service en ligne, comportement historique ;
- ``marian``          : modèle local uniquement, sans limite de débit
  (``libpostcards.translate_marian``) ;
- ``auto``            : Google d'abord ; dès qu'il refuse ce serveur (le
  disjoncteur de ``libpostcards.langs`` s'est déclenché), le modèle local
  prend le relais, sans que le client ait quoi que ce soit à faire.

Comme les autres modèles du serveur (CLIP, BLIP, DETR, SAM, PaddleOCR) : chargé
à la première requête, gardé en mémoire, déchargé après inactivité
([simpostcards] gpu_idle_timeout, voir ``libs._idle_unload``) ; il tourne sur
le CPU par défaut ([simpostcards] translate_device), pour laisser le GPU à
d'autres usages.
"""

from __future__ import annotations

import logging

from libpostcards import langs
from libpostcards.translate_marian import (
    MarianEngine, missing_dependencies, INSTALL_HINT,
)
from simpostcards.libs._idle_unload import IdleUnloadTimer

LOG = logging.getLogger(__name__)


class _MarianBackend:
    """Enveloppe de MarianEngine : minuteur d'inactivité, requêtes en cours."""

    def __init__(self) -> None:
        self.engine = MarianEngine()
        self._idle_timer = IdleUnloadTimer("MARIAN", self._unload)

    def configure(self, device, model_template) -> None:
        self.engine.device = device or "cpu"
        if model_template:
            self.engine.model_template = model_template

    def configure_idle_timeout(self, seconds) -> None:
        self._idle_timer.configure(seconds)

    def _unload(self) -> bool:
        return self.engine.unload()

    # Interface attendue par libpostcards.langs.configure_translation
    def translate(self, texts, code):
        # busy() : pas de déchargement pendant le chargement du modèle (qui
        # peut inclure un téléchargement) ni pendant la traduction.
        with self._idle_timer.busy():
            return self.engine.translate(texts, code)

    def prepare(self, code) -> None:
        with self._idle_timer.busy():
            self.engine.prepare(code)


_backend = _MarianBackend()


def configure(engine, device=None, model_template=None) -> str:
    """Applique ``translate_engine`` (appelé au démarrage de l'appli).
    Renvoie le moteur effectivement retenu."""
    engine = (engine or "google").strip().lower()
    if engine not in langs.TRANSLATE_ENGINES:
        LOG.warning("unknown translate_engine %r (expected one of: %s): "
                    "using google", engine, ", ".join(langs.TRANSLATE_ENGINES))
        engine = "google"
    marian = None
    if engine in ("marian", "auto"):
        missing = missing_dependencies()
        if missing:
            LOG.warning(
                "translate_engine = %s but the local translation engine needs "
                "%s (%s)%s", engine, ", ".join(missing), INSTALL_HINT,
                ": Google only" if engine == "auto" else
                ": translation requests will answer 501")
        else:
            _backend.configure(device, model_template)
            marian = _backend
    langs.configure_translation(engine, marian)
    return engine


def configure_idle_timeout(seconds) -> None:
    """Appelé une fois au démarrage de l'appli (simpostcards.__init__)."""
    _backend.configure_idle_timeout(seconds)
