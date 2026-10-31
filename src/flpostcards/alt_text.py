# -*- encoding: utf-8 -*-
"""
flpostcards/alt_text.py - Texte alternatif (``alt``) du recto d'une carte.

Source unique pour la fiche carte, la galerie, la carte (map), le diaporama
et les balades, afin que le même texte soit produit partout.

Le texte réunit, une information par ligne, le titre, le titre secondaire, la
description et le contenu détecté automatiquement (BLIP, voir
``libpostcards.detection``). Pour le référencement (recherche d'images) et
l'accessibilité, il commence toujours par « Carte postale » (dans la langue
courante) -- sauf s'il le mentionne déjà : c'est le cas du contenu détecté,
qui commence normalement par « une carte postale de... » (voir
``PostcardDetection.caption_prompt``), ce qui évite un doublon.
"""
from __future__ import annotations

from flask_babel import gettext

# Formes reconnues comme "le texte parle déjà de carte postale", en plus du
# libellé traduit dans la langue courante : le contenu détecté peut être resté
# en anglais (traduction indisponible, voir ``kttools detect-translate``) ou
# avoir été enregistré dans une autre langue que celle de la page.
_ALREADY_MENTIONED = ("carte postale", "cartes postales", "postcard")


def build_recto_alt(card) -> str:
    """Texte alternatif du recto de ``card`` (dict, ``id`` obligatoire).

    Jamais vide : sans aucune information, retombe sur « Carte postale <id> »
    (un ``alt`` vide signale une image décorative et la retire de l'indexation).
    """
    label = gettext("Carte postale")
    parts = [
        part.strip() for part in (
            card.get("title"),
            card.get("title2"),
            card.get("description"),
            card.get("detected_content"),
        ) if part and part.strip()
    ]
    if not parts:
        return ("%s %s" % (label, card.get("id", ""))).strip()

    text = "\n".join(parts)
    lowered = text.lower()
    if label.lower() in lowered or any(m in lowered for m in _ALREADY_MENTIONED):
        return text
    return "%s - %s" % (label, text)
