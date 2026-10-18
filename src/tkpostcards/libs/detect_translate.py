# -*- encoding: utf-8 -*-
"""
tkpostcards.libs.detect_translate
---------------------------------

Traduit les textes de détection (``detected_content``, la légende BLIP, et
``detected_objects``, les libellés DETR) restés en anglais, via le serveur
simpostcards (``external_helper``) : voir ``kttools detect-translate``.

Comment on sait qu'un texte n'est pas traduit
=============================================
Chaque texte a un marqueur de langue (``detected_content_lang`` /
``detected_objects_lang``), écrit au moment de la détection :

- ``"en"``      : texte resté en anglais (langue indisponible, traduction en
  échec...) -> à traduire ;
- le code cible ("fr"...) : déjà traduit -> on n'y touche pas ;
- un autre code : écrit dans une autre langue -> on n'y touche pas ;
- ``"manual"``  : modifié à la main dans ktmanager -> jamais retraduit ;
- absent        : carte antérieure aux marqueurs. Rien ne dit alors si le
  texte a été traduit ; on le devine (``libpostcards.langs.looks_english``)
  sauf avec ``guess=False``.

Les cartes sont traitées par lots : chaque lot est écrit avant de passer au
suivant, une interruption (réseau, quota du service de traduction) ne perd
donc pas ce qui a déjà été fait, et relancer la commande reprend où elle en
était (les textes traduits portent maintenant le marqueur de la langue cible).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from libpostcards.langs import (
    TranslationError, TranslatorUnavailable, is_english, looks_english,
    normalize_code,
)

# (champ du texte, champ du marqueur, "caption" | "objects")
FIELDS = (
    ("detected_content", "detected_content_lang", "caption"),
    ("detected_objects", "detected_objects_lang", "objects"),
)

CARDS_PER_BATCH = 20
UNKNOWN_SAMPLES = 8


def split_labels(text):
    """"person, car" -> ["person", "car"] (le séparateur de to_objects_string)."""
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def classify(card, text_key, lang_key, target_code, guess=True):
    """``(à_traduire, raison)`` pour un champ d'une carte.

    Raisons : ``"marker"`` (marqueur "en"), ``"guessed"`` (pas de
    marqueur, texte qui a l'air anglais) si à traduire ; sinon ``"empty"``,
    ``"manual"``, ``"done"`` (déjà dans la langue cible), ``"other"`` (autre
    langue), ``"unknown"`` (pas de marqueur et pas l'air d'anglais, ou
    ``guess`` désactivé).
    """
    text = (card.get(text_key) or "").strip()
    if not text:
        return False, "empty"
    marker = card.get(lang_key)
    if marker:
        marker = normalize_code(marker)
        if marker == "manual":
            return False, "manual"
        if marker == normalize_code(target_code):
            return False, "done"
        if is_english(marker):
            return True, "marker"
        return False, "other"
    if guess and looks_english(text):
        return True, "guessed"
    return False, "unknown"


@dataclass
class Summary:
    """Bilan d'une exécution."""
    cards_seen: int = 0
    cards_changed: int = 0                  # écrites (ou à écrire en dry-run)
    fields_translated: int = 0
    skipped: dict = field(default_factory=dict)      # raison -> nombre de champs
    planned: list = field(default_factory=list)      # [(id, champ, raison)]
    missing: list = field(default_factory=list)      # ids sans fichier JSON
    # Quelques textes écartés comme "unknown" (id, champ, texte) : de quoi
    # juger, avec --dry-run, si l'heuristique se trompe.
    unknown_samples: list = field(default_factory=list)
    error: str | None = None                # interruption (TranslationError...)

    def skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def plan_cards(model, ids, target_code, guess=True):
    """Parcourt les cartes ; renvoie ``(travail, summary)`` où ``travail``
    est une liste ``(id, carte, [(champ_texte, champ_marqueur, genre,
    raison), ...])`` des seules cartes ayant quelque chose à traduire."""
    summary = Summary()
    work = []
    for cid in ids:
        # load_json() d'une carte inexistante renvoie un squelette vide
        # (sans lever d'exception) : on teste donc le fichier.
        if not (model.cards_dir / f"{cid}.json").is_file():
            summary.missing.append(str(cid))
            continue
        card = model.load_json(cid)
        summary.cards_seen += 1
        todo = []
        for text_key, lang_key, kind in FIELDS:
            needed, reason = classify(card, text_key, lang_key, target_code, guess)
            if needed:
                todo.append((text_key, lang_key, kind, reason))
                summary.planned.append((cid, text_key, reason))
            else:
                summary.skip(reason)
                if reason == "unknown" and len(summary.unknown_samples) < UNKNOWN_SAMPLES:
                    summary.unknown_samples.append((cid, text_key, card.get(text_key)))
        if todo:
            work.append((cid, card, todo))
    return work, summary


def translate_cards(model, ids, translator, lang, target_code, *, guess=True,
                    dry_run=False, on_progress=None):
    """Traduit les textes de détection en anglais des cartes ``ids``.

    ``translator`` : un :class:`libpostcards.langs.HelperTranslator` (ou tout
    objet ayant ``translate(texts, lang)``). ``target_code`` : le code de
    langue retenu par le serveur (``HelperTranslator.check_language``), qui
    est aussi le marqueur enregistré. Voir le docstring du module pour le
    traitement par lots et les erreurs : une interruption est renvoyée dans
    ``Summary.error`` (rien n'est levé), ce qui a déjà été écrit reste.
    """
    work, summary = plan_cards(model, ids, target_code, guess)
    if dry_run or not work:
        summary.cards_changed = len(work)
        summary.fields_translated = len(summary.planned)
        return summary

    label_cache: dict[str, str] = {}
    for start in range(0, len(work), CARDS_PER_BATCH):
        batch = work[start:start + CARDS_PER_BATCH]

        captions = sorted({(card.get(tk) or "").strip()
                           for _cid, card, todo in batch
                           for tk, _lk, kind, _r in todo if kind == "caption"})
        labels = sorted({label
                         for _cid, card, todo in batch
                         for tk, _lk, kind, _r in todo if kind == "objects"
                         for label in split_labels(card.get(tk))
                         if label not in label_cache})
        try:
            if captions:
                translated = translator.translate(captions, lang)
                caption_map = dict(zip(captions, translated))
            else:
                caption_map = {}
            if labels:
                label_cache.update(zip(labels, translator.translate(labels, lang)))
        except (TranslationError, TranslatorUnavailable) as exc:
            summary.error = str(exc)
            return summary

        for cid, card, todo in batch:
            for text_key, lang_key, kind, _reason in todo:
                original = (card.get(text_key) or "").strip()
                if kind == "caption":
                    card[text_key] = caption_map[original]
                else:
                    card[text_key] = ", ".join(
                        label_cache[l] for l in split_labels(original))
                card[lang_key] = target_code
                summary.fields_translated += 1
            model.write_json(card)
            summary.cards_changed += 1
        if on_progress is not None:
            on_progress(min(start + CARDS_PER_BATCH, len(work)), len(work))
    return summary
