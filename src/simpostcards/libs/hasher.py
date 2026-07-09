# -*- encoding: utf-8 -*-
"""
simpostcards/libs/hasher.py - Calcul des hashs perceptuels d'une image.

Reprend les mêmes hashs (et la même bibliothèque ``imagehash``) que
``libpostcards.similar.PostcardSearcher.compute_hashes``, afin que
les valeurs renvoyées par l'API ``compute_hashes`` de simpostcards
restent directement comparables à celles stockées dans l'index de
recherche de cartes similaires (même ``hash_size`` par défaut = 8).

Volontairement, ce module ne calcule PAS l'embedding CLIP utilisé par
``PostcardSearcher`` (modèle lourd à charger, dépendance à torch /
open-clip) : simpostcards est une petite appli Flask dédiée au seul
calcul des hashs, appelée à la demande, et doit rester légère en
ressources. Le rapprochement CLIP reste du ressort de
``libpostcards.similar`` (indexation offline).
"""

from __future__ import annotations

import imagehash
from PIL import Image

# Hashs calculés, dans le même ordre / avec les mêmes noms que dans
# libpostcards.similar.PostcardSearcher.compute_hashes
_HASH_FUNCS = {
    "ahash": imagehash.average_hash,
    "dhash": imagehash.dhash,
    "phash": imagehash.phash,
    "whash": imagehash.whash,
}


def compute_hashes(image: Image.Image) -> dict[str, str]:
    """
    Calcule les hashs perceptuels d'une image PIL déjà en mémoire.

    Retourne un dict JSON-sérialisable :
      {"ahash": "...", "dhash": "...", "phash": "...", "whash": "..."}

    Chaque valeur est la représentation hexadécimale du hash
    (``str(imagehash.ImageHash)``), directement comparable (distance de
    Hamming via ``imagehash.hex_to_hash(...) - imagehash.hex_to_hash(...)``)
    à celles produites par ``PostcardSearcher.compute_hashes``.
    """
    rgb = image.convert("RGB")
    return {name: str(func(rgb)) for name, func in _HASH_FUNCS.items()}
