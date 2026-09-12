# -*- encoding: utf-8 -*-
"""
libs.scan_prepare
------------------

Mutualizes the "analyze and correct scans" step (dewarp / crop / make the
white background transparent) that used to live only in
``tktools scan prepare``. Both ``tktools`` (CLI) and ``tkimport`` (GUI)
call :func:`prepare_pairs` so the exact same correction is applied whatever
the front-end.

Every parameter controlling that correction (white threshold, crop
margin, ...) is grouped in a named :class:`~.scan_profiles.ScanProfile`
("profil d'import" -- see :mod:`tkpostcards.libs.scan_profiles`) rather
than passed around individually: :func:`make_corrector` /
:func:`prepare_pairs` accept a ``profile`` (defaulting to the
``cpa`` built-in profile), plus an optional ``white_threshold``
kept for quick one-off overrides (CLI ``--white-threshold``, legacy
config).

The raw scans found in ``importdir`` are expected two-by-two (recto then
verso) and are renamed/corrected into ``<id>_R.<ext>`` / ``<id>_V.<ext>``,
*staying in importdir* until they are reviewed (tkimport step 2) and added
to the collection (``tktools scan add`` / tkimport step 3).
"""
import os
from pathlib import Path

from .importdir import group_raw_scans
from .scan_profiles import BUILTIN_PROFILES, DEFAULT_PROFILE_NAME


class PreparedPair(object):
    """Result of preparing one postcard (recto + verso)."""

    __slots__ = ("pcid", "recto_src", "recto_dst", "verso_src", "verso_dst")

    def __init__(self, pcid, recto_src, recto_dst, verso_src, verso_dst):
        self.pcid = pcid
        self.recto_src = recto_src
        self.recto_dst = recto_dst
        self.verso_src = verso_src
        self.verso_dst = verso_dst


def make_corrector(profile=None, white_threshold=None, verbose=False):
    """Build the default ``correct(infile, outfile)`` callable.

    Uses ``libpostcards.scan_corrector.ScanCorrector`` (dewarp / crop) then
    :class:`tkpostcards.libs.transparency.TiffBackgroundRemover` to make the
    white background transparent, exactly like the historical
    ``tktools scan prepare`` command.

    :param profile: :class:`~.scan_profiles.ScanProfile` ("profil
        d'import") controlling every correction parameter. Defaults to
        the ``cpa`` built-in profile when omitted.
    :param white_threshold: kept for quick one-off overrides (CLI
        ``--white-threshold``, legacy config key): when given, it
        overrides ``profile.white_threshold`` (and, unless the profile
        itself sets ``transparency_white_threshold``, the transparency
        threshold too). Ignored when ``profile.skip_processing`` is set
        (see below), since no threshold is used in that case.

    If ``profile.skip_processing`` is set (the ``null`` built-in
    profile), the returned ``correct()`` callable does not touch the
    image at all: it byte-for-byte copies *infile* to *outfile* --
    no crop, no deskew, no transparency. Meant for scans already fully
    corrected by hand (e.g. in GIMP) before being imported.

    If ``profile.skip_transparency`` is set (the ``draft`` built-in
    profile) without ``skip_processing``, crop/deskew still run
    normally but the background-transparency step is skipped, so no
    pixel risks being wrongly turned transparent before a manual
    review/crop.
    """
    import shutil

    import cv2
    from libpostcards.scan_corrector import ScanCorrector
    from .transparency import TiffBackgroundRemover

    if profile is None:
        profile = BUILTIN_PROFILES[DEFAULT_PROFILE_NAME]
    if white_threshold is not None:
        profile = profile.with_overrides(white_threshold=white_threshold)

    if profile.skip_processing:
        def correct(infile, outfile):
            shutil.copyfile(infile, outfile)
        return correct

    scanc = ScanCorrector(
        white_threshold=profile.white_threshold,
        white_ratio_threshold=profile.white_ratio_threshold,
        crop_margin=profile.crop_margin,
        final_crop_margin=profile.final_crop_margin,
        angle_range=profile.angle_range,
        contour_final_crop=profile.use_contour_geometry,
        quad_final_crop=profile.use_quad_geometry,
        contour_denoise=profile.contour_denoise,
        contour_clahe=profile.contour_clahe,
        contour_auto_canny=profile.contour_auto_canny,
        verbose=verbose,
    )
    bgtrans = TiffBackgroundRemover(white_threshold=profile.effective_transparency_threshold)

    def correct(infile, outfile):
        img = scanc.load_image(infile)
        img = scanc.process_image(img)
        if not profile.skip_transparency:
            if profile.use_contour_geometry:
                img = bgtrans.make_border_transparent_by_contour_cv2(
                    img, band=profile.transparency_band,
                    denoise=profile.contour_denoise,
                    clahe=profile.contour_clahe,
                    auto_canny=profile.contour_auto_canny,
                )
            else:
                img = bgtrans.make_border_white_transparent_cv2(
                    img, band=profile.transparency_band)
        ext = Path(infile).suffix.lower()

        if ext in (".tif", ".tiff"):
            # cv2.imwrite() écrit les TIFF 4 canaux (BGRA) en LZW *sans*
            # déclarer le 4e canal comme alpha (balise TIFF "ExtraSamples"
            # absente -- OpenCV le relit quand même par tolérance, avec un
            # avertissement, mais tout lecteur plus strict -- tifffile,
            # ImageMagick, etc. -- peut alors mal interpréter ce canal, ou
            # échouer purement et simplement si le décodeur LZW installé
            # ne suffit pas (le paquet "imagecodecs", requis par tifffile
            # pour décoder du LZW, n'est pas toujours présent). Écrire via
            # PIL évite les deux problèmes : balise alpha correcte, et
            # compression "tiff_deflate" (zlib, décodable par tifffile
            # sans dépendance additionnelle) au lieu de LZW.
            from PIL import Image
            import cv2 as _cv2
            rgba = _cv2.cvtColor(img, _cv2.COLOR_BGRA2RGBA) if img.shape[2] == 4 \
                else _cv2.cvtColor(img, _cv2.COLOR_BGR2RGB)
            Image.fromarray(rgba).save(str(outfile), compression="tiff_deflate")
            return

        params = []
        if ext in (".jpg", ".jpeg"):
            params = [cv2.IMWRITE_JPEG_QUALITY, 95]
        elif ext == ".png":
            params = [cv2.IMWRITE_PNG_COMPRESSION, 3]
        cv2.imwrite(str(outfile), img, params)

    return correct


def prepare_pairs(importdir, next_id, prefix="", profile=None, white_threshold=None,
                   correct=None, on_pair=None):
    """Analyze and correct the raw scans found in *importdir*.

    :param importdir: folder containing the raw scans (recto/verso pairs,
        two consecutive files per postcard once sorted).
    :param next_id: first postcard id to attribute (subsequent pairs get
        ``next_id + 1``, ``next_id + 2``, ...).
    :param prefix: only consider files whose name starts with *prefix*.
    :param profile: :class:`~.scan_profiles.ScanProfile` ("profil
        d'import") passed to :func:`make_corrector` when *correct* is
        not provided. Defaults to the ``cpa`` built-in profile.
    :param white_threshold: one-off override passed to
        :func:`make_corrector` together with *profile* (see there).
    :param correct: optional ``correct(infile, outfile)`` callable used
        instead of the default one (mostly useful for tests). Built with
        :func:`make_corrector` when omitted.
    :param on_pair: optional ``on_pair(pair: PreparedPair)`` callback,
        invoked right after each pair has been examined (whether or not
        anything was actually (re)corrected) - handy for a progress bar
        (CLI) or to refresh a review list (GUI).
    :return: the list of :class:`PreparedPair` seen, in order (not just
        the ones actually (re)corrected).

    A destination file (``recto_dst``/``verso_dst``) that already exists
    is left untouched and is *not* passed to *correct* again: this
    function only fills in whatever is currently missing. This makes it
    safe -- and idempotent -- to call repeatedly on the same *importdir*
    as raw scans keep coming in, and is what lets tkimport's "Annuler" /
    "Annuler tout" buttons work: deleting one side's prepared file (to
    retry it with a different :class:`~.scan_profiles.ScanProfile`)
    followed by a new "Analyze and correct scans" run regenerates only
    that missing side, leaving every other already-prepared file (and
    postcard id) exactly as it was.
    """
    importdir = Path(importdir)
    if correct is None:
        correct = make_corrector(profile=profile, white_threshold=white_threshold)

    fl2 = group_raw_scans(importdir, prefix=prefix)

    pairs = []
    pcid = next_id
    for i in range(0, len(fl2) - 1, 2):
        recto_src = importdir / fl2[i][1]
        recto_ext = recto_src.suffix.lower()
        recto_dst = importdir / ("%s_R%s" % (pcid, recto_ext))
        if not recto_dst.exists():
            correct(str(recto_src), str(recto_dst))

        verso_src = importdir / fl2[i + 1][1]
        verso_ext = verso_src.suffix.lower()
        verso_dst = importdir / ("%s_V%s" % (pcid, verso_ext))
        if not verso_dst.exists():
            correct(str(verso_src), str(verso_dst))

        pair = PreparedPair(pcid, recto_src, recto_dst, verso_src, verso_dst)
        pairs.append(pair)
        if on_pair is not None:
            on_pair(pair)

        pcid += 1

    return pairs
