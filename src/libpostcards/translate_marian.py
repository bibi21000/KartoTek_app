# -*- encoding: utf-8 -*-
"""
libpostcards/translate_marian.py - Traduction LOCALE (modèles Marian, anglais
vers une autre langue), alternative au service en ligne de Google.

Pourquoi : le service gratuit de Google limite le débit (HTTP 429) et peut
refuser une adresse IP pendant des minutes ou des heures (voir le disjoncteur
de ``libpostcards.langs``). Un modèle local n'a ni limite de débit, ni besoin
de réseau une fois téléchargé, et le texte ne quitte pas la machine.

Modèles : ``Helsinki-NLP/opus-mt-en-<langue>`` (``fr``, ``de``, ``es``...), de
l'ordre de 300 Mo chacun, téléchargés depuis Hugging Face à la première
utilisation de la langue (dans HF_HOME, comme les autres modèles du serveur).
Une langue sans modèle de ce nom est signalée comme indisponible
(:class:`libpostcards.langs.LanguageUnavailable`) ; le gabarit de nom se
règle ([simpostcards] translate_marian_model).

Ce module n'est utile que sur le serveur (qui a torch et transformers) ; rien
n'est importé à l'import du module. On utilise ``MarianTokenizer`` /
``MarianMTModel`` directement et non ``pipeline("translation")`` : transformers
5 n'a plus ce pipeline.

La traduction se fait mot-à-mot dans son contexte de phrase : un libellé isolé
et ambigu ("tie") peut être traduit à contresens, comme avec n'importe quel
traducteur automatique sans contexte.
"""
from __future__ import annotations

import importlib.util
import threading

from .langs import (
    LanguageUnavailable, TranslationError, TranslatorUnavailable,
)

MODEL_TEMPLATE = "Helsinki-NLP/opus-mt-en-{lang}"
REQUIRED_MODULES = ("transformers", "torch", "sentencepiece", "sacremoses")
INSTALL_HINT = ("pip install pypostcards[translate-local] (transformers, "
                "torch, sentencepiece, sacremoses)")


def missing_dependencies():
    """Modules nécessaires qui ne sont pas installés."""
    return [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]


class MarianEngine:
    """Modèles Marian chargés à la demande, un par langue cible."""

    def __init__(self, device=None, model_template=None, batch_size=16,
                 num_beams=4, max_tokens=256):
        self.device = device or "cpu"
        self.model_template = model_template or MODEL_TEMPLATE
        self.batch_size = batch_size
        self.num_beams = num_beams
        self.max_tokens = max_tokens
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._models: dict = {}

    def model_name(self, code):
        """Nom du modèle pour un code de langue ("fr", "pt-BR" -> "pt")."""
        return self.model_template.format(lang=code.split("-")[0].lower())

    def _load(self, code):
        name = self.model_name(code)
        with self._load_lock:
            if name in self._models:
                return self._models[name]
            missing = missing_dependencies()
            if missing:
                raise TranslatorUnavailable(
                    "the local translation engine needs %s: %s"
                    % (", ".join(missing), INSTALL_HINT))
            from transformers import MarianMTModel, MarianTokenizer
            try:
                tokenizer = MarianTokenizer.from_pretrained(name)
                model = MarianMTModel.from_pretrained(name)
            except OSError as exc:
                text = str(exc)
                if any(k in text for k in ("is not a valid model identifier",
                                           "Repository Not Found", "404")):
                    raise LanguageUnavailable(
                        "translate", code,
                        "no local translation model %r for language %r (the "
                        "name comes from translate_marian_model)" % (name, code)
                    ) from exc
                raise TranslationError(
                    "could not load or download the local translation model "
                    "%r (it is downloaded from Hugging Face on first use: "
                    "check the network): %s" % (name, exc)) from exc
            model.to(self.device)
            model.eval()
            self._models[name] = (tokenizer, model)
            return self._models[name]

    def prepare(self, code):
        """Charge (et au besoin télécharge) le modèle de *code*."""
        self._load(code)

    def translate(self, texts, code):
        """Traductions de *texts* (même longueur, même ordre)."""
        import torch
        tokenizer, model = self._load(code)
        out = []
        with self._infer_lock:
            for i in range(0, len(texts), self.batch_size):
                chunk = list(texts[i:i + self.batch_size])
                batch = tokenizer(chunk, return_tensors="pt", padding=True,
                                  truncation=True, max_length=self.max_tokens)
                batch = {k: v.to(self.device) for k, v in batch.items()}
                with torch.inference_mode():
                    generated = model.generate(
                        **batch, num_beams=self.num_beams,
                        max_new_tokens=self.max_tokens)
                out.extend(t.strip() for t in tokenizer.batch_decode(
                    generated, skip_special_tokens=True))
        if len(out) != len(texts) or not all(out):
            raise TranslationError(
                "the local translation model returned %d result(s) for %d "
                "text(s)" % (len(out), len(texts)))
        return out

    def unload(self):
        """Oublie les modèles ; True s'il y en avait."""
        with self._load_lock:
            had = bool(self._models)
            self._models.clear()
        return had
