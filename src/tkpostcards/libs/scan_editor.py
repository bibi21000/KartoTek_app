# -*- encoding: utf-8 -*-
"""
libs.scan_editor
-----------------

Step 2 of tkimport ("let the user validate the scan"): rotate a prepared
image by 90° steps, or open it in the user's preferred image editor.

The preferred application is remembered per operating system (Linux,
macOS, Windows) in the ``[tkimport]`` section of ``postcards.conf``, since
a config file is often shared between machines running different OSes.
"""
import os
import subprocess
import sys
from pathlib import Path

CONF_SECTION = "tkimport"

# One config key per OS family so the same postcards.conf can be shared
# between a Linux desktop, a Mac and a Windows laptop.
_PLATFORM_KEYS = {
    "linux": "editor_linux",
    "darwin": "editor_macos",
    "win32": "editor_windows",
}


def current_platform():
    """Return one of ``"linux"``, ``"darwin"`` or ``"win32"``."""
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def editor_conf_key(platform=None):
    """Config key (in ``[tkimport]``) storing the preferred app for *platform*."""
    return _PLATFORM_KEYS[platform or current_platform()]


def get_preferred_app(cfg, platform=None):
    """Return the preferred application path/command for *platform*.

    ``cfg`` is a :class:`configparser.ConfigParser`. Returns ``None`` when
    nothing is configured, meaning "use the OS default application".
    """
    if not cfg.has_section(CONF_SECTION):
        return None
    value = cfg.get(CONF_SECTION, editor_conf_key(platform), fallback="").strip()
    return value or None


def set_preferred_app(cfg, app_path, platform=None):
    """Persist *app_path* as the preferred application for *platform*.

    Does not save the file, only updates the in-memory ``cfg``; call
    ``tkpostcards`` config-saving helper (or ``cfg.write(fh)``) afterwards.
    """
    if not cfg.has_section(CONF_SECTION):
        cfg.add_section(CONF_SECTION)
    cfg.set(CONF_SECTION, editor_conf_key(platform), app_path or "")


def open_in_external_app(path, app_cmd=None):
    """Open *path* in *app_cmd*, or in the OS default application.

    :param app_cmd: full path (or name found in ``PATH``) of the
        application to launch. When ``None``/empty, falls back to the
        platform's default "open with" mechanism (``xdg-open`` on Linux,
        ``open`` on macOS, ``os.startfile`` on Windows).
    :raises RuntimeError: if the application could not be launched.
    """
    path = str(path)
    plat = current_platform()
    try:
        if app_cmd:
            subprocess.Popen([app_cmd, path])
            return
        if plat == "win32":
            os.startfile(path)  # noqa: S606 - user-triggered, not remote input
        elif plat == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except (OSError, FileNotFoundError) as exc:
        raise RuntimeError(str(exc)) from exc


# ── Rotation ────────────────────────────────────────────────────────────────

# Clockwise rotation -> PIL transpose op (transpose keeps full quality,
# unlike Image.rotate() which resamples).
def _transpose_op(degrees_cw):
    from PIL import Image
    ops = {
        90: Image.ROTATE_270,
        180: Image.ROTATE_180,
        270: Image.ROTATE_90,
    }
    return ops[degrees_cw]


def rotate_file(path, degrees_cw=90):
    """Rotate the image file at *path* clockwise by *degrees_cw* (a
    multiple of 90) and overwrite it in place, preserving its format.
    """
    from PIL import Image

    degrees_cw = degrees_cw % 360
    if degrees_cw == 0:
        return
    if degrees_cw not in (90, 180, 270):
        raise ValueError("degrees_cw must be a multiple of 90")

    path = Path(path)
    img = Image.open(path)
    img.load()
    fmt = img.format
    compression = img.info.get("compression")
    rotated = img.transpose(_transpose_op(degrees_cw))

    save_kwargs = {}
    if fmt == "TIFF" and compression:
        save_kwargs["compression"] = compression
    rotated.save(path, format=fmt, **save_kwargs)
