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
("modèle d'import" -- see :mod:`tkpostcards.libs.scan_profiles`) rather
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

    :param profile: :class:`~.scan_profiles.ScanProfile` ("modèle
        d'import") controlling every correction parameter. Defaults to
        the ``cpa`` built-in profile when omitted.
    :param white_threshold: kept for quick one-off overrides (CLI
        ``--white-threshold``, legacy config key): when given, it
        overrides ``profile.white_threshold`` (and, unless the profile
        itself sets ``transparency_white_threshold``, the transparency
        threshold too).
    """
    import cv2
    from libpostcards.scan_corrector import ScanCorrector
    from .transparency import TiffBackgroundRemover

    if profile is None:
        profile = BUILTIN_PROFILES[DEFAULT_PROFILE_NAME]
    if white_threshold is not None:
        profile = profile.with_overrides(white_threshold=white_threshold)

    scanc = ScanCorrector(
        white_threshold=profile.white_threshold,
        white_ratio_threshold=profile.white_ratio_threshold,
        crop_margin=profile.crop_margin,
        angle_range=profile.angle_range,
        verbose=verbose,
    )
    bgtrans = TiffBackgroundRemover(white_threshold=profile.effective_transparency_threshold)

    def correct(infile, outfile):
        img = scanc.load_image(infile)
        img = scanc.process_image(img)
        img = bgtrans.make_border_white_transparent_cv2(img)
        ext = Path(infile).suffix.lower()
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
    :param profile: :class:`~.scan_profiles.ScanProfile` ("modèle
        d'import") passed to :func:`make_corrector` when *correct* is
        not provided. Defaults to the ``cpa`` built-in profile.
    :param white_threshold: one-off override passed to
        :func:`make_corrector` together with *profile* (see there).
    :param correct: optional ``correct(infile, outfile)`` callable used
        instead of the default one (mostly useful for tests). Built with
        :func:`make_corrector` when omitted.
    :param on_pair: optional ``on_pair(pair: PreparedPair)`` callback,
        invoked right after each pair has been corrected - handy for a
        progress bar (CLI) or to refresh a review list (GUI).
    :return: the list of :class:`PreparedPair` created, in order.
    :raises RuntimeError: if a destination file already exists.
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
        if recto_dst.exists():
            raise RuntimeError("%s exists" % recto_dst)
        correct(str(recto_src), str(recto_dst))

        verso_src = importdir / fl2[i + 1][1]
        verso_ext = verso_src.suffix.lower()
        verso_dst = importdir / ("%s_V%s" % (pcid, verso_ext))
        if verso_dst.exists():
            raise RuntimeError("%s exists" % verso_dst)
        correct(str(verso_src), str(verso_dst))

        pair = PreparedPair(pcid, recto_src, recto_dst, verso_src, verso_dst)
        pairs.append(pair)
        if on_pair is not None:
            on_pair(pair)

        pcid += 1

    return pairs
