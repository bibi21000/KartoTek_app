# -*- encoding: utf-8 -*-
"""
simpostcards/libs/duplicates.py - Recherche de doublons dans un index.

Équivalent côté serveur de
``libpostcards.similar.PostcardSearcher.find_similar_in_index`` : compare
toutes les paires d'entrées d'un index (hashs perceptuels + embedding CLIP
optionnel) et retourne celles dont le score combiné atteint un seuil. C'est
un calcul O(n²) : le déléguer à un serveur simpostcards évite de le faire
sur le poste (et d'y installer torch pour la similarité cosinus des
embeddings -- ici, numpy suffit).

Les scores sont ceux de ``PostcardSearcher._combined_score`` :

  - score hashs : moyenne pondérée des similarités ahash/phash/dhash/whash
    (0.15 / 0.40 / 0.20 / 0.25), chacune ``100 * (1 - distance_hamming / 64)`` ;
  - si LES DEUX entrées ont un embedding : ``hash_weight * score_hashs +
    clip_weight * max(0, cosinus * 100)`` ; sinon, score hashs seul.

Volontairement, ce module ne dépend PAS de torch / open-clip.
"""

from __future__ import annotations

import numpy as np

_HASH_WEIGHTS = (("ahash", 0.15), ("phash", 0.40), ("dhash", 0.20), ("whash", 0.25))

# Nombre de lignes de la matrice de scores calculées d'un coup : borne la
# mémoire (chunk x n flottants par hash) sans trop pénaliser la vitesse.
_CHUNK_ROWS = 256

_POPCOUNT_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


class InvalidIndexError(ValueError):
    """Entrée d'index malformée (champ manquant, hash non hexadécimal, ...)."""


def _popcount64(values: np.ndarray) -> np.ndarray:
    """Nombre de bits à 1 de chaque uint64 (forme conservée)."""
    if hasattr(np, "bitwise_count"):  # numpy >= 2.0
        return np.bitwise_count(values)
    as_bytes = np.ascontiguousarray(values).view(np.uint8).reshape(values.shape + (8,))
    return _POPCOUNT_LUT[as_bytes].sum(axis=-1, dtype=np.uint8)


def _hash_array(items: list, key: str) -> np.ndarray:
    try:
        return np.array([int(item[key], 16) for item in items], dtype=np.uint64)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise InvalidIndexError(
            "hash '%s' absent ou invalide (64 bits hexadécimaux attendus) : %s"
            % (key, exc)) from exc


def iter_similar_pairs(items, threshold=90, hash_weight=0.60, clip_weight=0.40):
    """
    Comme :func:`find_similar_pairs`, mais PROGRESSIF : retourne un
    itérateur de ``(lignes_traitées, total, paires)`` -- un tuple par bloc
    de ``_CHUNK_ROWS`` lignes de la matrice de scores --, où ``paires``
    (``[{"score", "file1", "file2"}, ...]``, triées par score décroissant
    à l'intérieur du bloc, éventuellement vide) contient les doublons
    trouvés dans CE bloc. Permet à un appelant d'afficher les résultats au
    fur et à mesure et de mesurer l'avancement, au lieu d'attendre la fin
    du calcul O(n²).

    La validation de l'index se fait immédiatement (``InvalidIndexError``
    levée à l'appel, avant la première itération), pas au premier
    ``next()`` : un serveur peut donc répondre 400 avant d'avoir commencé
    à émettre un flux.
    """
    n = len(items)
    if n < 2:
        return iter(())

    try:
        paths = [item["path"] for item in items]
    except (KeyError, TypeError) as exc:
        raise InvalidIndexError("champ 'path' absent d'une entrée : %s" % exc) from exc

    hashes = {key: _hash_array(items, key) for key, _ in _HASH_WEIGHTS}

    # Embeddings : matrice (n, d) + masque des entrées qui en ont un. Les
    # autres retombent sur les hashs seuls (voir docstring du module).
    has_emb = np.array([item.get("embedding") is not None for item in items])
    emb = None
    if has_emb.any():
        dims = {len(item["embedding"]) for item in items if item.get("embedding") is not None}
        if len(dims) != 1:
            raise InvalidIndexError(
                "embeddings de dimensions différentes : %s" % sorted(dims))
        emb = np.zeros((n, dims.pop()), dtype=np.float32)
        for k in np.flatnonzero(has_emb):
            emb[k] = items[k]["embedding"]

    columns = np.arange(n)

    def _blocks():
        for start in range(0, n - 1, _CHUNK_ROWS):
            stop = min(start + _CHUNK_ROWS, n)
            rows = slice(start, stop)

            hash_score = np.zeros((stop - start, n), dtype=np.float32)
            for key, weight in _HASH_WEIGHTS:
                col = hashes[key]
                distance = _popcount64(col[rows, None] ^ col[None, :])
                hash_score += weight * np.maximum(0.0, 100.0 * (1.0 - distance / 64.0))

            score = hash_score
            if emb is not None:
                clip_score = np.maximum(0.0, (emb[rows] @ emb.T) * 100.0)
                both = has_emb[rows, None] & has_emb[None, :]
                score = np.where(
                    both, hash_weight * hash_score + clip_weight * clip_score, hash_score)

            # Triangle supérieur strict seulement (j > i), comme la double
            # boucle d'origine.
            upper = columns[None, :] > np.arange(start, stop)[:, None]
            ii, jj = np.nonzero((score >= threshold) & upper)
            block = [{
                "score": round(float(score[i, j]), 2),
                "file1": paths[start + i],
                "file2": paths[j],
            } for i, j in zip(ii, jj)]
            block.sort(key=lambda m: m["score"], reverse=True)
            yield stop, n, block

    return _blocks()


def find_similar_pairs(items, threshold=90, hash_weight=0.60, clip_weight=0.40):
    """
    Retourne les paires ``(i < j)`` d'``items`` dont le score combiné est
    ``>= threshold``, sous la forme
    ``[{"score": float, "file1": path_i, "file2": path_j}, ...]`` triée par
    score décroissant (même format que ``find_similar_in_index``). Version
    non progressive de :func:`iter_similar_pairs`.

    :param items: liste de dicts ``{"path", "ahash", "dhash", "phash",
        "whash", "embedding"}`` -- ``embedding`` est une liste de float
        déjà normalisée, ou ``None``/absent.
    :raises InvalidIndexError: entrée malformée.
    """
    matches = []
    for _done, _total, block in iter_similar_pairs(
            items, threshold, hash_weight, clip_weight):
        matches.extend(block)
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches
