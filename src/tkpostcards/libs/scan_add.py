# -*- encoding: utf-8 -*-
"""
libs.scan_add
--------------

Mutualizes the "add prepared scans to the collection" step that used to
live only in ``tktools scan add``. Both ``tktools`` (CLI) and ``tkimport``
(GUI) call :func:`add_pairs` so postcards are copied, OCR'ed, content-
detected (BLIP caption + DETR object detection on the recto, see
``libpostcards.detection``), exported and indexed the exact same way
whatever the front-end.
"""
import os
from pathlib import Path

from .importdir import PAIR_EXTENSIONS


class AddedPostcard(object):
    """Result of adding one postcard (recto + verso) to the collection."""

    # NB: ``ocr_updated`` is True if the JSON was rewritten because
    # either OCR *or* content detection updated a field (recto_ocr,
    # verso_ocr, detected_content, detected_objects) -- the name is
    # kept for backward compatibility, its scope simply broadened when
    # content detection was added alongside OCR.
    #
    # ``issues`` : ``libpostcards.langs.Issue`` raised while processing this
    # card (typically: the detection language is unavailable or the
    # translation failed, so ``detected_content`` / ``detected_objects`` were
    # stored in English -- see ``detected_*_lang`` and ``kttools
    # detect-translate``). Empty when all went well.
    __slots__ = ("pcid", "recto", "verso", "ocr_updated", "issues")

    def __init__(self, pcid, recto, verso, ocr_updated, issues=None):
        self.pcid = pcid
        self.recto = recto
        self.verso = verso
        self.ocr_updated = ocr_updated
        self.issues = list(issues or [])


def guess_ext(importdir, pcid, default="tiff"):
    """Guess the extension used by the prepared ``<pcid>_R.<ext>`` file.

    Falls back to *default* (``tiff``, the collection's storage format)
    when no prepared file is found, keeping the historical behaviour of
    ``tktools scan add``.
    """
    importdir = Path(importdir)
    for ext in PAIR_EXTENSIONS:
        if (importdir / ("%s_R.%s" % (pcid, ext))).exists():
            return ext
    return default


def add_one(datadir, importdir, pcid, ext=None, ocr=None, pcs=None,
            searcher=None, detector=None, detect_lang=None, force=True,
            external_helper=None, external_helper_timeout=None):
    """Add a single postcard (recto + verso) to the collection.

    :param datadir: collection data directory (``common.datadir``).
    :param importdir: folder containing the reviewed ``<pcid>_R/_V.<ext>``
        files (``common.importdir``).
    :param pcid: postcard id to add (str or int).
    :param ext: extension of the prepared files; auto-detected with
        :func:`guess_ext` when omitted.
    :param ocr: a ``libpostcards`` ``PostcardOCR`` instance (built lazily
        when omitted, if pytesseract is installed or *external_helper* is
        given -- see ``libpostcards.ocr.PostcardOCR.external_helper``).
    :param pcs: a :class:`tkpostcards.libs.size.PostcardSize` instance
        (built lazily when omitted).
    :param searcher: optional ``PostcardSearcher`` instance; when given,
        the new postcard is added to the similarity index (the caller is
        responsible for loading/saving the index).
    :param detector: a ``libpostcards.detection`` ``PostcardDetection``
        instance (built lazily when omitted, if ``transformers`` is
        installed or *external_helper* is given); used to fill
        ``detected_content`` (BLIP caption) and ``detected_objects``
        (DETR object detection) from the recto.
    :param detect_lang: target language for the lazily-built ``detector``
        (ignored when ``detector`` is given explicitly).
    :param force: re-run OCR/detection even if already filled in the JSON.
    :param external_helper: URI of a remote simpostcards server used to
        build the lazy ``ocr``/``detector`` (ignored when *ocr*/*detector*
        are given explicitly) -- see
        ``libpostcards.ocr.PostcardOCR.external_helper`` and
        ``libpostcards.detection.PostcardDetection.external_helper``.
    :param external_helper_timeout: timeout (seconds) for a single
        request to *external_helper*, passed to the lazily-built
        ``ocr``/``detector``. ``None`` (default) lets each keep its own
        built-in default (30s/60s).
    :return: an :class:`AddedPostcard` instance.
    :raises RuntimeError: if the destination files already exist, or if
        the source files are missing from *importdir*.
    """
    from libpostcards.model import Model
    from .size import PostcardSize

    try:
        from libpostcards.ocr import PostcardOCR, PYTESSERACT_AVAILABLE
    except ImportError:
        PostcardOCR = None
        PYTESSERACT_AVAILABLE = False

    try:
        from libpostcards.detection import PostcardDetection, DETECTION_LIBS_AVAILABLE
    except ImportError:
        PostcardDetection = None
        DETECTION_LIBS_AVAILABLE = False

    datadir = Path(datadir)
    importdir = Path(importdir)
    pcid = str(pcid)

    if ext is None:
        ext = guess_ext(importdir, pcid)

    src_recto = importdir / ("%s_R.%s" % (pcid, ext))
    src_verso = importdir / ("%s_V.%s" % (pcid, ext))
    if not src_recto.exists():
        raise RuntimeError("%s does not exist" % src_recto)
    if not src_verso.exists():
        raise RuntimeError("%s does not exist" % src_verso)

    storage_ext = "tiff"
    dst_recto = datadir / "cards" / ("%s_R.%s" % (pcid, storage_ext))
    dst_verso = datadir / "cards" / ("%s_V.%s" % (pcid, storage_ext))
    if dst_recto.exists():
        raise RuntimeError("%s exists" % dst_recto)
    if dst_verso.exists():
        raise RuntimeError("%s exists" % dst_verso)

    import shutil
    shutil.copyfile(str(src_recto), str(dst_recto))
    shutil.copyfile(str(src_verso), str(dst_verso))

    if ocr is None and (PYTESSERACT_AVAILABLE or external_helper):
        ocr = PostcardOCR(external_helper=external_helper,
                           external_helper_timeout=external_helper_timeout)
    if detector is None and (DETECTION_LIBS_AVAILABLE or external_helper):
        detector_kwargs = {"external_helper": external_helper,
                            "external_helper_timeout": external_helper_timeout}
        if detect_lang:
            detector_kwargs["lang"] = detect_lang
        detector = PostcardDetection(**detector_kwargs)
    if pcs is None:
        pcs = PostcardSize(datadir)

    mod = Model(datadir)
    card = mod.load_json(pcid)
    updated = False
    issues = []
    if ocr is not None:
        if card.get("recto_ocr") is None or force is True:
            updated = True
            card["recto_ocr"] = ocr.to_string(str(dst_recto))
        if card.get("verso_ocr") is None or force is True:
            updated = True
            card["verso_ocr"] = ocr.to_string(str(dst_verso))
    if detector is not None:
        # La détection de contenu ne porte que sur le recto (voir
        # tkpostcards.libs.detection).
        # La langue du texte enregistré est mémorisée ("en" = non traduit :
        # langue indisponible, traduction en échec) pour que ``kttools
        # detect-translate`` sache quoi traduire plus tard.
        if card.get("detected_content") is None or force is True:
            updated = True
            described = detector.describe(str(dst_recto))
            card["detected_content"] = described.text
            card["detected_content_lang"] = described.lang
            issues.extend(described.issues)
        if card.get("detected_objects") is None or force is True:
            updated = True
            described = detector.describe_objects(str(dst_recto))
            card["detected_objects"] = described.text
            card["detected_objects_lang"] = described.lang
            issues.extend(described.issues)
    if updated is True:
        mod.write_json(card)

    pcs.export_one(dst_recto)
    pcs.export_one(dst_verso)

    if searcher is not None:
        output_original = datadir / "size_div1"
        base_name = dst_recto.stem
        searcher.build_index(output_original / ("%s.png" % base_name))

    return AddedPostcard(pcid, dst_recto, dst_verso, updated, issues)


def add_pairs(datadir, importdir, ids, ext=None, force=True,
              use_searcher=True, on_progress=None, ocr_lang=None, torch_device=None,
              detect_lang=None, on_issue=None, model_name=None, objects_model_name=None,
              objects_threshold=None, max_objects=None, excluded_objects=None,
              external_helper=None, external_helper_timeout=None,
              ocr_engine=None, ocr_min_score=None, paddle_device=None):
    """Add several postcards at once, mirroring ``tktools scan add``.

    :param ids: iterable of postcard ids to add.
    :param use_searcher: try to load/update the similarity index
        (``libpostcards.similar.PostcardSearcher``) if it is available
        (installed locally, or *external_helper* is given).
    :param on_progress: optional ``on_progress(index, total, added)``
        callback, where ``added`` is the :class:`AddedPostcard` just
        created (or ``None`` if it raised, see below).
    :param ocr_engine: OCR engine, chosen by the caller: ``"tesseract"``
        (default) or ``"paddleocr"``; run here, or on *external_helper* when
        given (the server must then have this engine).
        ``libpostcards.langs.EngineUnavailable`` is raised BEFORE any file
        is copied when the chosen engine is not usable (PaddleOCR is never
        silently skipped the way a missing tesseract is).
    :param ocr_min_score: PaddleOCR only: minimum confidence of a line.
    :param paddle_device: PaddleOCR only, local runs only: ``"cpu"``,
        ``"gpu:0"``...
    :param ocr_lang: languages passed to ``PostcardOCR`` (tesseract
        ``lang`` argument, e.g. ``"fra"`` or ``"fra+eng"``). Falls back
        to ``PostcardOCR``'s own default (``"fra"``) when omitted.
    :param torch_device: torch device passed to ``PostcardSearcher`` and
        ``PostcardDetection`` (see ``[DEFAULT] torch_device`` in
        postcards.conf, and "tktools devices" for the list of available
        devices). ``None`` lets them pick their own default (cuda if
        available, else cpu). Ignored by both when *external_helper* is
        given (the remote server picks its own device).
    :param on_issue: callable receiving each ``libpostcards.langs.Issue``
        (at most once per code and language) -- typically "the detection
        language is not available on the server, the detected texts stay in
        English". Called from the same thread as ``add_pairs`` (and before
        the first card for what can be checked up front). The issues are
        also logged by ``libpostcards.detection``. An unavailable OCR
        language is not an ``Issue`` but fatal:
        ``libpostcards.langs.LanguageUnavailable`` is raised BEFORE any
        file is copied.
    :param detect_lang: target language passed to ``PostcardDetection``
        (translation of the BLIP caption / DETR object labels). Falls
        back to ``PostcardDetection``'s own default (``"fr"``) when
        omitted.
    :param model_name: BLIP model name passed to ``PostcardDetection``.
        Falls back to ``PostcardDetection``'s own default when omitted.
        Ignored when *external_helper* is given (the remote server uses
        its own configured model -- see [simpostcards] detection_model_name).
    :param objects_model_name: DETR model name passed to
        ``PostcardDetection``. Falls back to ``PostcardDetection``'s own
        default when omitted. Ignored when *external_helper* is given
        (see [simpostcards] detection_objects_model_name).
    :param objects_threshold: minimum confidence score (0-1) passed to
        ``PostcardDetection``. Falls back to ``PostcardDetection``'s own
        default when omitted.
    :param max_objects: maximum number of detected objects passed to
        ``PostcardDetection``. Falls back to ``PostcardDetection``'s own
        default when omitted.
    :param excluded_objects: iterable of object labels (English, case
        insensitive) always excluded from ``detected_objects``, passed
        to ``PostcardDetection``. Falls back to ``PostcardDetection``'s
        own default (empty) when omitted. Always applied locally, even
        when *external_helper* is given (see
        ``libpostcards.detection.PostcardDetection.detect_objects``).
    :param external_helper: URI of a remote simpostcards server (see
        ``[DEFAULT] external_helper`` in postcards.conf) used, instead
        of a local computation, for OCR (see
        ``libpostcards.ocr.PostcardOCR.external_helper`` -- lets OCR
        run without pytesseract/tesseract-ocr installed here, even if
        ``pytesseract`` is not installed locally, see
        ``PYTESSERACT_AVAILABLE`` below), content detection (see
        ``libpostcards.detection.PostcardDetection.external_helper`` --
        lets it run without transformers/torch installed here), and
        the similarity index (see
        ``libpostcards.similar.PostcardSearcher.external_helper`` --
        lets the index be updated without torch/open-clip-torch/
        imagehash installed here). Same server for all three, since in
        practice it is always the same simpostcards deployment.
    :param external_helper_timeout: timeout (seconds) for a single
        request to *external_helper*, passed to the lazily-built
        ``ocr``/``detector``/``searcher``. ``None`` (default) lets each
        keep its own built-in default (30s/60s).
    :return: the list of :class:`AddedPostcard` successfully added.

    OCR and content detection (BLIP caption + DETR object detection on
    the recto, see ``libpostcards.detection``) are both best-effort:
    each is silently skipped (without raising) when its dependencies
    (``pytesseract`` / ``transformers`` + ``torch``) are not installed
    AND no remote server is configured for it (*external_helper*),
    exactly like the original OCR-only behaviour otherwise.

    Errors while adding a single id are *not* swallowed: the exception
    propagates after ``on_progress`` has been called with ``added=None``,
    so the caller (CLI or GUI) can decide how to report it. Postcards
    already added before the error stay added.
    """
    from .size import PostcardSize

    try:
        from libpostcards.ocr import PostcardOCR, PYTESSERACT_AVAILABLE
    except ImportError:
        PostcardOCR = None
        PYTESSERACT_AVAILABLE = False

    try:
        from libpostcards.detection import PostcardDetection, DETECTION_LIBS_AVAILABLE
    except ImportError:
        PostcardDetection = None
        DETECTION_LIBS_AVAILABLE = False

    try:
        from libpostcards.similar import PostcardSearcher
        searcher_available = True
    except ImportError:
        searcher_available = False

    datadir = Path(datadir)
    from libpostcards.langs import normalize_ocr_engine
    engine = normalize_ocr_engine(ocr_engine)
    # Tesseract absent : l'OCR est ignoré (historique, "au mieux"). Un autre
    # moteur est un choix explicite de l'utilisateur : on le construit
    # toujours, pour que son indisponibilité soit signalée par le contrôle de
    # ``ocr.check_language()`` plus bas, au lieu d'être passée sous silence.
    if engine != "tesseract" or PYTESSERACT_AVAILABLE or external_helper:
        ocr_kwargs = {"external_helper": external_helper,
                      "external_helper_timeout": external_helper_timeout,
                      "engine": engine, "min_score": ocr_min_score,
                      "paddle_device": paddle_device}
        if ocr_lang:
            ocr_kwargs["lang"] = ocr_lang
        ocr = PostcardOCR(**ocr_kwargs)
    else:
        ocr = None

    if DETECTION_LIBS_AVAILABLE or external_helper:
        detector_kwargs = {"device": torch_device, "external_helper": external_helper,
                            "external_helper_timeout": external_helper_timeout}
        if detect_lang:
            detector_kwargs["lang"] = detect_lang
        if model_name:
            detector_kwargs["model_name"] = model_name
        if objects_model_name:
            detector_kwargs["objects_model_name"] = objects_model_name
        if objects_threshold is not None:
            detector_kwargs["objects_threshold"] = objects_threshold
        if max_objects is not None:
            detector_kwargs["max_objects"] = max_objects
        if excluded_objects is not None:
            detector_kwargs["excluded_objects"] = excluded_objects
        detector = PostcardDetection(**detector_kwargs)
    else:
        detector = None

    pcs = PostcardSize(datadir)

    searcher = None
    index_file = None
    if use_searcher and searcher_available:
        index_file = datadir / "postcards.pkl"
        searcher = PostcardSearcher(datadir=datadir, device=torch_device,
                                     external_helper=external_helper,
                                     external_helper_timeout=external_helper_timeout)
        searcher.load_index(index_file)

    ids = list(ids)
    results = []

    # Langues : vérifiées UNE fois avant de toucher à quoi que ce soit, pour
    # prévenir l'utilisateur tout de suite plutôt qu'à la première carte
    # (OCR : erreur fatale ; détection : le texte restera en anglais).
    if ocr is not None:
        ocr.check_language()                    # LanguageUnavailable
    reported = set()

    def _report(issue):
        key = (issue.code, issue.lang)
        if key not in reported:
            reported.add(key)
            if on_issue is not None:
                on_issue(issue)

    if detector is not None:
        preflight = detector.check_language()
        if preflight is not None:
            _report(preflight)

    try:
        for i, pcid in enumerate(ids):
            try:
                added = add_one(datadir, importdir, pcid, ext=ext, ocr=ocr,
                                 pcs=pcs, searcher=searcher, detector=detector,
                                 force=force)
            except Exception:
                if on_progress is not None:
                    on_progress(i, len(ids), None)
                raise
            for issue in added.issues:
                _report(issue)
            results.append(added)
            if on_progress is not None:
                on_progress(i, len(ids), added)
    finally:
        if searcher is not None and index_file is not None:
            searcher.save_index(index_file)

    return results
