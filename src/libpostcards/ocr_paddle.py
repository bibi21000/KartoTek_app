# -*- encoding: utf-8 -*-
"""
libpostcards/ocr_paddle.py - Moteur OCR PaddleOCR (alternative à tesseract).

Utilisé par ``libpostcards.ocr.PostcardOCR`` (calcul local, ``engine=
"paddleocr"``) et par le serveur simpostcards (``simpostcards.libs.
paddle_backend``, qui tient les modèles en mémoire et les décharge après
inactivité).

Ni ``paddleocr`` ni ``paddle`` ne sont importés ici en tête de module : le
paquet ``paddleocr`` charge tout ``paddlex`` (lent, lourd), et un client qui
délègue au serveur (``external_helper``) ne les a pas du tout. Ils ne sont
importés que dans :func:`create_engine`.

Ce que PaddleOCR fait bien, et moins bien
-----------------------------------------
Meilleur que tesseract sur le texte bruité, courbe ou posé sur une photo
(légendes des cartes postales, mentions de l'éditeur). Les modèles PP-OCR
sont des lecteurs de texte IMPRIMÉ : pour l'écriture manuscrite (le message
au verso) ne vous attendez pas à des miracles ; voir le comparatif dans
la documentation du projet.

Premier lancement
-----------------
Les modèles (quelques dizaines de Mo par langue) sont téléchargés depuis
Internet à la première utilisation, dans ``~/.paddlex``. Une machine sans
accès réseau doit les y avoir déposés à l'avance.
"""
from __future__ import annotations

import importlib.util
import statistics

from .langs import (
    EngineUnavailable, LanguageUnavailable, ocr_language_message,
    paddle_missing_languages, resolve_paddle_language,
)

# find_spec plutôt qu'un import : voir le docstring du module.
PADDLE_AVAILABLE = all(
    importlib.util.find_spec(m) is not None for m in ("paddleocr", "paddle"))

# Seuil de confiance par défaut : en dessous, PaddleOCR a surtout "lu" des
# textures (pierres, feuillage, grain du papier). Plus bas = plus de texte
# mais plus de bruit.
DEFAULT_MIN_SCORE = 0.5

INSTALL_HINT = (
    "PaddleOCR is not installed: it needs the PaddlePaddle framework AND the "
    "paddleocr package. 1) PaddlePaddle: \"pip install paddlepaddle\" (PyPI "
    "only has wheels for CPython 3.9-3.13 on Linux x86_64, macOS arm64 and "
    "Windows; elsewhere, or for a GPU, use PaddlePaddle's own index, e.g. "
    "pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/ "
    "or paddlepaddle-gpu with .../stable/cu126/). 2) Then: "
    "pip install pypostcards[paddleocr]")


def check_device(device):
    """Vérifie que la build de PaddlePaddle installée sait utiliser *device*.

    Les roues pip de PaddlePaddle n'existent qu'en CPU et en NVIDIA CUDA : il
    n'y en a pas pour AMD (ROCm), qui passe par une compilation depuis les
    sources. Sans ce contrôle, ``paddleocr_device = gpu:0`` sur une build CPU
    échoue dans le constructeur de PaddleOCR avec une erreur qui, une fois
    enveloppée par :func:`create_engine`, ferait croire à un problème de réseau
    ou de modèles. Lève :class:`EngineUnavailable` avec la vraie cause.
    """
    kind = (device or "").split(":")[0].strip().lower()
    if kind in ("", "cpu"):
        return
    try:
        import paddle
    except ImportError:      # paddle absent : create_engine l'a déjà signalé
        return
    cuda = bool(getattr(paddle, "is_compiled_with_cuda", lambda: False)())
    rocm = bool(getattr(paddle, "is_compiled_with_rocm", lambda: False)())
    if cuda or rocm:
        return               # build GPU : PaddlePaddle jugera le reste
    raise EngineUnavailable(
        "paddleocr",
        "paddleocr_device = %r asks for the GPU, but the installed PaddlePaddle "
        "was built without GPU support (CPU only). PaddlePaddle's pip wheels "
        "exist for the CPU and for NVIDIA CUDA only, not for AMD ROCm (that "
        "needs a build from source): set paddleocr_device = cpu." % (device,))


def create_engine(lang, device=None, ocr_version=None):
    """Instance de ``paddleocr.PaddleOCR`` prête à lire du français (ou la
    langue demandée).

    ``lang`` : codes tesseract ou PaddleOCR (voir
    :func:`libpostcards.langs.resolve_paddle_language`). ``device`` : "cpu",
    "gpu:0"... (défaut : celui que choisit PaddlePaddle, GPU s'il y en a un
    -- mettre "cpu" pour laisser le GPU à d'autres usages). ``ocr_version``
    : "PP-OCRv5", "PP-OCRv6"... (défaut : le meilleur de la version installée
    de paddleocr pour cette langue).

    Lève :class:`LanguageUnavailable` (aucun modèle pour cette langue) ou
    :class:`EngineUnavailable` (paquet absent, modèles impossibles à
    obtenir : réseau coupé à la première utilisation...).

    Orientation du document et dépliage, utiles pour des photos de feuilles,
    sont désactivés : une carte postale scannée est droite, et ces étapes
    chargent deux modèles de plus pour rien. Le seuil de confiance n'est PAS
    fixé ici (``text_rec_score_thresh`` = 0) mais appliqué à la lecture
    (:func:`read_text`) : un même moteur en mémoire sert ainsi des demandes
    qui n'ont pas le même seuil.
    """
    code = resolve_paddle_language(lang)
    if code is None:
        missing = paddle_missing_languages(lang)
        raise LanguageUnavailable(
            "ocr", lang,
            ocr_language_message(lang, missing, [], engine="paddleocr"),
            missing=missing)
    if not PADDLE_AVAILABLE:
        raise EngineUnavailable("paddleocr", INSTALL_HINT)
    check_device(device)
    from paddleocr import PaddleOCR

    kwargs = {
        "lang": code,
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "text_rec_score_thresh": 0.0,
    }
    if device:
        kwargs["device"] = device
    if ocr_version:
        kwargs["ocr_version"] = ocr_version
    try:
        return PaddleOCR(**kwargs)
    except ValueError as exc:
        # "No models are available for lang=..." : la version installée de
        # paddleocr ne connaît pas cette langue (notre liste en avance ou en
        # retard sur elle). Toute autre ValueError (ocr_version invalide...)
        # est une erreur de configuration.
        if "No models are available" in str(exc):
            missing = paddle_missing_languages(lang) or [lang]
            raise LanguageUnavailable(
                "ocr", lang,
                ocr_language_message(lang, missing, [], engine="paddleocr"),
                missing=missing) from exc
        raise EngineUnavailable("paddleocr", "PaddleOCR rejected its "
                                "configuration: %s" % exc) from exc
    except Exception as exc:
        raise EngineUnavailable(
            "paddleocr",
            "PaddleOCR could not load its models for %r (they are downloaded "
            "on first use: check the network, or put them in ~/.paddlex "
            "beforehand): %s" % (lang, exc)) from exc


def to_bgr_array(image):
    """Image PIL -> tableau numpy BGR (convention OpenCV, celle qu'attend
    PaddleOCR). Le fond transparent des cartes détourées est aplati sur du
    blanc : sinon il serait lu comme noir."""
    import numpy as np
    from PIL import Image

    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    else:
        image = image.convert("RGB")
    return np.ascontiguousarray(np.asarray(image)[:, :, ::-1])


def assemble_lines(items):
    """Regroupe des morceaux de texte en lignes lisibles.

    ``items`` : liste de ``(x0, y0, x1, y1, texte)``. Les morceaux dont le
    centre vertical est assez proche (moins d'une demi-hauteur de ligne
    médiane) sont mis sur la même ligne, de gauche à droite ; les lignes sont
    séparées par un saut de ligne. Une légende en deux blocs côte à côte
    reste ainsi sur une seule ligne, au lieu d'être lue colonne par colonne.
    """
    items = [i for i in items if i[4] and i[4].strip()]
    if not items:
        return ""
    heights = [max(1.0, i[3] - i[1]) for i in items]
    tolerance = 0.5 * statistics.median(heights)
    ordered = sorted(items, key=lambda i: ((i[1] + i[3]) / 2, i[0]))
    lines: list[list] = []
    centers: list[float] = []
    for item in ordered:
        center = (item[1] + item[3]) / 2
        if lines and abs(center - centers[-1]) <= tolerance:
            lines[-1].append(item)
            centers[-1] = (centers[-1] * (len(lines[-1]) - 1) + center) / len(lines[-1])
        else:
            lines.append([item])
            centers.append(center)
    return "\n".join(
        " ".join(i[4].strip() for i in sorted(line, key=lambda i: i[0]))
        for line in lines)


def _items_from_result(result, min_score):
    """``(x0, y0, x1, y1, texte)`` d'un résultat ``predict()`` (un par image).

    Un résultat se comporte comme un dictionnaire : ``rec_texts``,
    ``rec_scores`` et ``rec_boxes`` (tableau Nx4 ``x0, y0, x1, y1``, vide si
    rien n'a été lu) -- vérifié sur paddleocr 3.7 / paddlex.
    """
    texts = list(result["rec_texts"])
    scores = list(result["rec_scores"]) if "rec_scores" in result else [1.0] * len(texts)
    boxes = result["rec_boxes"] if "rec_boxes" in result else []
    items = []
    for i, text in enumerate(texts):
        if i < len(scores) and float(scores[i]) < min_score:
            continue
        if i < len(boxes):
            x0, y0, x1, y1 = (float(v) for v in boxes[i])
        else:   # pas de boîtes : on garde au moins l'ordre de lecture
            x0, y0, x1, y1 = 0.0, float(i), 1.0, float(i) + 1.0
        items.append((x0, y0, x1, y1, text))
    return items


def read_text(engine, image, min_score=DEFAULT_MIN_SCORE):
    """Texte de *image* (PIL), lu avec *engine* (voir :func:`create_engine`),
    une ligne de la page par ligne de texte."""
    items = []
    for result in engine.predict(to_bgr_array(image)):
        items.extend(_items_from_result(result, min_score))
    return assemble_lines(items)
