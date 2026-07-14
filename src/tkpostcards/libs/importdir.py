# -*- encoding: utf-8 -*-
"""
libs.importdir
---------------

Small, dependency-free helpers used to inspect the "importdir" (the folder
where raw scans land and where prepared ``<id>_R.<ext>`` / ``<id>_V.<ext>``
pairs wait to be reviewed and added to the collection).

This module has *no* heavy dependency (no PIL, no cv2, no libpostcards) so
it can be imported cheaply from the CLI (tktools), from tkscan and from
tkimport without slowing down start-up.
"""
import os
import re
from pathlib import Path

# Recognised image extensions for a "prepared" scan pair.
PAIR_EXTENSIONS = ("tiff", "tif", "png", "jpg", "jpeg")

# Matches "<int>_R.<ext>" or "<int>_V.<ext>" (recto / verso), e.g. "3_R.tiff"
RV_FILENAME_RE = re.compile(
    r"^(?P<id>\d+)_(?P<side>[RV])\.(?P<ext>%s)$" % "|".join(PAIR_EXTENSIONS),
    re.IGNORECASE,
)


def is_pair_file(name):
    """True if *name* looks like a prepared "<id>_R/_V.<ext>" file."""
    return RV_FILENAME_RE.match(name) is not None


def scan_importdir(importdir):
    """Inspect *importdir* and split its content in two lists.

    Returns a tuple ``(rv_files, other_files)`` where:

    - ``rv_files`` contains the files matching the "<id>_R.<ext>" /
      "<id>_V.<ext>" pattern (already prepared, ready for review/add),
    - ``other_files`` contains any other file (raw scans, junk, ...).

    Sub-directories are ignored. Both lists are sorted by name and contain
    :class:`~pathlib.Path` objects.
    """
    importdir = Path(importdir)
    rv_files = []
    other_files = []
    if not importdir.is_dir():
        return rv_files, other_files
    for entry in sorted(importdir.iterdir()):
        if not entry.is_file():
            continue
        if is_pair_file(entry.name):
            rv_files.append(entry)
        else:
            other_files.append(entry)
    return rv_files, other_files


def list_pairs(importdir):
    """Group the "<id>_R.<ext>" / "<id>_V.<ext>" files found in *importdir*.

    Returns an ordered ``dict`` mapping the postcard id (``str``) to a dict
    ``{"R": Path|None, "V": Path|None}``. Ids are sorted numerically.
    """
    rv_files, _other = scan_importdir(importdir)
    pairs = {}
    for f in rv_files:
        m = RV_FILENAME_RE.match(f.name)
        pid = m.group("id")
        side = m.group("side").upper()
        pairs.setdefault(pid, {"R": None, "V": None})[side] = f
    return {pid: pairs[pid] for pid in sorted(pairs, key=lambda x: int(x))}


def complete_pairs(importdir):
    """Like :func:`list_pairs` but only keeps ids having *both* R and V."""
    return {
        pid: sides
        for pid, sides in list_pairs(importdir).items()
        if sides["R"] is not None and sides["V"] is not None
    }


def group_raw_scans(importdir, prefix=""):
    """Reproduce the historical "prepare" grouping of raw scan files.

    Raw scans (straight out of the scanner, before correction) are named
    ``<prefix> (<n>).<ext>`` or ``<prefix>_<n>.<ext>``; when neither pattern
    matches, files are ordered as-is. Returns a sorted list of
    ``(index, filename)`` tuples, exactly as consumed by
    :func:`tkpostcards.libs.scan_prepare.prepare_pairs`.
    """
    importdir = Path(importdir)
    fl1 = [f for f in os.listdir(importdir) if re.match(r"%s.*" % re.escape(prefix), f)]
    fl2 = []
    for f in fl1:
        s = re.search(r"%s \((.*)\)\..*" % re.escape(prefix), f)
        if s is None:
            s = re.search(r"%s_(.*)\..*" % re.escape(prefix), f)
            if s is None:
                fl2.append((1, f))
            else:
                fl2.append((int(s.group(1)), f))
        else:
            fl2.append((int(s.group(1)), f))
    return sorted(fl2)
