# -*- encoding: utf-8 -*-
"""
libs.scan_add
--------------

Mutualizes the "add prepared scans to the collection" step that used to
live only in ``tktools scan add``. Both ``tktools`` (CLI) and ``tkimport``
(GUI) call :func:`add_pairs` so postcards are copied, OCR'ed, exported and
indexed the exact same way whatever the front-end.
"""
import os
from pathlib import Path

from .importdir import PAIR_EXTENSIONS


class AddedPostcard(object):
    """Result of adding one postcard (recto + verso) to the collection."""

    __slots__ = ("pcid", "recto", "verso", "ocr_updated")

    def __init__(self, pcid, recto, verso, ocr_updated):
        self.pcid = pcid
        self.recto = recto
        self.verso = verso
        self.ocr_updated = ocr_updated


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
            searcher=None, force=True):
    """Add a single postcard (recto + verso) to the collection.

    :param datadir: collection data directory (``common.datadir``).
    :param importdir: folder containing the reviewed ``<pcid>_R/_V.<ext>``
        files (``common.importdir``).
    :param pcid: postcard id to add (str or int).
    :param ext: extension of the prepared files; auto-detected with
        :func:`guess_ext` when omitted.
    :param ocr: a ``libpostcards`` ``PostcardOCR`` instance (built lazily
        when omitted).
    :param pcs: a :class:`tkpostcards.libs.size.PostcardSize` instance
        (built lazily when omitted).
    :param searcher: optional ``PostcardSearcher`` instance; when given,
        the new postcard is added to the similarity index (the caller is
        responsible for loading/saving the index).
    :param force: re-run OCR even if it is already filled in the JSON.
    :return: an :class:`AddedPostcard` instance.
    :raises RuntimeError: if the destination files already exist, or if
        the source files are missing from *importdir*.
    """
    from libpostcards.model import Model
    from .size import PostcardSize

    try:
        from .ocr import PostcardOCR, PYTESSERACT_AVAILABLE
    except ImportError:
        PostcardOCR = None
        PYTESSERACT_AVAILABLE = False

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

    if ocr is None and PYTESSERACT_AVAILABLE:
        ocr = PostcardOCR()
    if pcs is None:
        pcs = PostcardSize(datadir)

    mod = Model(datadir)
    card = mod.load_json(pcid)
    updated = False
    if ocr is not None:
        if card.get("recto_ocr") is None or force is True:
            updated = True
            card["recto_ocr"] = ocr.to_string(str(dst_recto))
        if card.get("verso_ocr") is None or force is True:
            updated = True
            card["verso_ocr"] = ocr.to_string(str(dst_verso))
    if updated is True:
        mod.write_json(card)

    pcs.export_one(dst_recto)
    pcs.export_one(dst_verso)

    if searcher is not None:
        output_original = datadir / "size_div1"
        base_name = dst_recto.stem
        searcher.build_index(output_original / ("%s.png" % base_name))

    return AddedPostcard(pcid, dst_recto, dst_verso, updated)


def add_pairs(datadir, importdir, ids, ext=None, force=True,
              use_searcher=True, on_progress=None, ocr_lang=None):
    """Add several postcards at once, mirroring ``tktools scan add``.

    :param ids: iterable of postcard ids to add.
    :param use_searcher: try to load/update the similarity index
        (``libpostcards.similar.PostcardSearcher``) if it is available.
    :param on_progress: optional ``on_progress(index, total, added)``
        callback, where ``added`` is the :class:`AddedPostcard` just
        created (or ``None`` if it raised, see below).
    :param ocr_lang: languages passed to ``PostcardOCR`` (tesseract
        ``lang`` argument, e.g. ``"fra"`` or ``"fra+eng"``). Falls back
        to ``PostcardOCR``'s own default (``"fra"``) when omitted.
    :return: the list of :class:`AddedPostcard` successfully added.

    Errors while adding a single id are *not* swallowed: the exception
    propagates after ``on_progress`` has been called with ``added=None``,
    so the caller (CLI or GUI) can decide how to report it. Postcards
    already added before the error stay added.
    """
    from .size import PostcardSize

    try:
        from .ocr import PostcardOCR, PYTESSERACT_AVAILABLE
    except ImportError:
        PostcardOCR = None
        PYTESSERACT_AVAILABLE = False

    try:
        from libpostcards.similar import PostcardSearcher
        searcher_available = True
    except ImportError:
        searcher_available = False

    datadir = Path(datadir)
    if PYTESSERACT_AVAILABLE:
        ocr = PostcardOCR(lang=ocr_lang) if ocr_lang else PostcardOCR()
    else:
        ocr = None
    pcs = PostcardSize(datadir)

    searcher = None
    index_file = None
    if use_searcher and searcher_available:
        index_file = datadir / "postcards.pkl"
        searcher = PostcardSearcher(datadir=datadir)
        searcher.load_index(index_file)

    ids = list(ids)
    results = []
    try:
        for i, pcid in enumerate(ids):
            try:
                added = add_one(datadir, importdir, pcid, ext=ext, ocr=ocr,
                                 pcs=pcs, searcher=searcher, force=force)
            except Exception:
                if on_progress is not None:
                    on_progress(i, len(ids), None)
                raise
            results.append(added)
            if on_progress is not None:
                on_progress(i, len(ids), added)
    finally:
        if searcher is not None and index_file is not None:
            searcher.save_index(index_file)

    return results
