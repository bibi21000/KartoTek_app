#!/usr/bin/env python3
"""
tkimport - Review application sitting between tkscan and tkmanager.

    1. Analyze and correct the raw scans found in importdir
       (shared with ``tktools scan prepare``).
    2. Let the user validate each prepared postcard: rotate it by 90°
       steps, or open it in their preferred image editor (remembered per
       OS in the ``[tkimport]`` section of the configuration file).
    3. Add the validated postcards to the collection
       (shared with ``tktools scan add``).
"""
from __future__ import annotations

import configparser
import gettext
import locale
import logging
import sys
import threading
from pathlib import Path

import click
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

from . import cli
from .libs.importdir import scan_importdir, complete_pairs, list_pairs
from .libs.scan_prepare import prepare_pairs
from .libs.scan_add import add_pairs
from .libs.scan_editor import (
    rotate_file,
    open_in_external_app,
    get_preferred_app,
    set_preferred_app,
    editor_conf_key,
)

# ──────────────────────────────────────────────────────────────────────────────
# Paths & i18n setup
# ──────────────────────────────────────────────────────────────────────────────

APP_DIR = Path(__file__).parent.resolve()
TRANSLATIONS_DIR = APP_DIR / "translations"
CONFIG_FILE = APP_DIR / "postcards.conf"

I18N_DOMAIN = "tkpostcards"


def setup_i18n(lang: str | None = None) -> gettext.NullTranslations:
    """Return a translation object for the requested language."""
    if lang is None:
        lc, _enc = locale.getdefaultlocale()
        lang = (lc or "en")[:2]
    try:
        translation = gettext.translation(
            I18N_DOMAIN,
            localedir=str(TRANSLATIONS_DIR),
            languages=[lang],
        )
    except FileNotFoundError:
        translation = gettext.NullTranslations()
    return translation


# ──────────────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────────────

# Default values used when a key is missing from the [tkimport] section of
# postcards.conf. importdir/datadir are *not* part of this dict: they come
# from the [DEFAULT] section, inherited automatically by every section
# thanks to configparser (same convention as tkscan).
DEFAULT_CONFIG = {
    "prefix": "",
    "white_threshold": "240",
    "language": "",
    "remove_after_add": "false",
    "editor_linux": "",
    "editor_macos": "",
    "editor_windows": "",
}


def load_config() -> configparser.ConfigParser:
    """Load postcards.conf and ensure a [tkimport] section with defaults exists."""
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(str(CONFIG_FILE))
    if not cfg.has_section("tkimport"):
        cfg.add_section("tkimport")
    for key, value in DEFAULT_CONFIG.items():
        if not cfg.has_option("tkimport", key):
            cfg.set("tkimport", key, value)
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    with open(str(CONFIG_FILE), "w", encoding="utf-8") as f:
        cfg.write(f)


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────

THUMB_SIZE = (170, 120)


def _load_thumb(path: Path, size=THUMB_SIZE) -> ImageTk.PhotoImage | None:
    try:
        img = Image.open(path)
        img.thumbnail(size, Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception as exc:
        logging.warning("thumbnail error for %s: %s", path, exc)
        return None


def _show_full_image(parent: tk.Widget, path: Path, title: str) -> None:
    try:
        img = Image.open(path)
    except Exception as exc:
        messagebox.showerror("Error", str(exc), parent=parent)
        return
    win = tk.Toplevel(parent)
    win.title(title)
    win.geometry("1024x768")
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    max_w, max_h = sw - 40, sh - 80
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    lbl = tk.Label(win, image=photo, bg="black")
    lbl.image = photo  # keep a reference
    lbl.pack(fill=tk.BOTH, expand=True)
    ttk.Button(win, text="Close", command=win.destroy).pack(pady=4)


def _disable_recursive(widget) -> None:
    """Recursively disable *widget* and all its children (ttk or tk)."""
    try:
        widget.state(["disabled"])
    except Exception:
        try:
            widget.configure(state=tk.DISABLED)
        except Exception:
            pass
    for child in widget.winfo_children():
        _disable_recursive(child)


class ScrollableFrame(ttk.Frame):
    """A vertically-scrollable container. Add children to ``self.body``."""

    def __init__(self, parent):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.body = ttk.Frame(self._canvas)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self._canvas.bind_all("<Button-4>", self._on_wheel, add="+")
        self._canvas.bind_all("<Button-5>", self._on_wheel, add="+")

    def _on_body_configure(self, _event=None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self._canvas.itemconfig(self._window, width=event.width)

    def _on_wheel(self, event) -> None:
        if not str(self._canvas.winfo_containing(event.x_root, event.y_root) or "") \
                .startswith(str(self._canvas)):
            return
        if event.num == 4:
            self._canvas.yview_scroll(-2, "units")
        elif event.num == 5:
            self._canvas.yview_scroll(2, "units")
        elif getattr(event, "delta", 0):
            self._canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


# ──────────────────────────────────────────────────────────────────────────────
# One row per prepared postcard (recto + verso)
# ──────────────────────────────────────────────────────────────────────────────

class PairRow(ttk.Frame):
    """Review row for a single prepared postcard (id, recto, verso)."""

    def __init__(self, parent, app: "PostcardImportApp", pcid: str, paths: dict):
        super().__init__(parent, padding=6, relief=tk.RIDGE, borderwidth=1)
        self.app = app
        self.pcid = pcid
        self.paths = dict(paths)  # {"R": Path, "V": Path}
        self._photo_refs: dict = {}
        self.included_var = tk.BooleanVar(value=True)
        self.added = False
        self.inactive = False

        self._build_ui()

    def _build_ui(self) -> None:
        _ = self.app._

        left = ttk.Frame(self)
        left.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(left, variable=self.included_var).pack(anchor="n")
        ttk.Label(left, text=f"#{self.pcid}", font=("TkDefaultFont", 11, "bold")
                  ).pack(anchor="n", pady=(4, 0))
        self._status_lbl = ttk.Label(left, text="", foreground="#2a7d2a")
        self._status_lbl.pack(anchor="n", pady=(4, 0))

        self._side_frames = {}
        for side, label in (("R", _("Recto")), ("V", _("Verso"))):
            side_frame = ttk.Frame(self)
            side_frame.pack(side=tk.LEFT, padx=8)
            self._side_frames[side] = side_frame

            ttk.Label(side_frame, text=label).pack()
            img_lbl = tk.Label(side_frame, bg="#222222", cursor="hand2")
            img_lbl.pack()
            img_lbl.bind("<Button-1>", lambda _e, s=side: self._view_full(s))
            setattr(self, f"_img_lbl_{side}", img_lbl)

            btns = ttk.Frame(side_frame)
            btns.pack(pady=(4, 0))
            ttk.Button(btns, text="⟲", width=3,
                       command=lambda s=side: self._rotate(s, 270)).pack(side=tk.LEFT)
            ttk.Button(btns, text="⟳", width=3,
                       command=lambda s=side: self._rotate(s, 90)).pack(side=tk.LEFT)
            ttk.Button(btns, text=_("Open…"),
                       command=lambda s=side: self._open_external(s)).pack(side=tk.LEFT, padx=(4, 0))
            ttk.Button(btns, text=_("Reload"),
                       command=lambda s=side: self._refresh_thumb(s)).pack(side=tk.LEFT, padx=(4, 0))

            self._refresh_thumb(side)

    # ── actions ─────────────────────────────────────────────────────────────

    def _refresh_thumb(self, side: str) -> None:
        path = self.paths.get(side)
        img_lbl = getattr(self, f"_img_lbl_{side}")
        if path is None or not path.exists():
            img_lbl.config(image="", text=self.app._("missing"))
            return
        photo = _load_thumb(path)
        if photo is None:
            img_lbl.config(image="", text="?")
            return
        self._photo_refs[side] = photo  # keep a reference
        img_lbl.config(image=photo, text="")

    def _rotate(self, side: str, degrees_cw: int) -> None:
        path = self.paths.get(side)
        if path is None:
            return
        try:
            rotate_file(path, degrees_cw)
        except Exception as exc:
            messagebox.showerror(self.app._("Error"), str(exc), parent=self)
            return
        self._refresh_thumb(side)

    def _open_external(self, side: str) -> None:
        path = self.paths.get(side)
        if path is None:
            return
        app_cmd = get_preferred_app(self.app.cfg)
        try:
            open_in_external_app(path, app_cmd)
        except RuntimeError as exc:
            messagebox.showerror(self.app._("Error"), str(exc), parent=self)

    def _view_full(self, side: str) -> None:
        path = self.paths.get(side)
        if path is None or not path.exists():
            return
        _show_full_image(self, path, f"#{self.pcid} - {side}")

    def mark_added(self) -> None:
        self.added = True
        self._status_lbl.config(text=self.app._("Added"))
        for child in self.winfo_children():
            _disable_recursive(child)

    def set_inactive(self) -> None:
        """Grey out this row: the pair is already in the collection."""
        self.included_var.set(False)
        self.inactive = True
        self._status_lbl.config(text=self.app._("Already in collection"))
        for child in self.winfo_children():
            _disable_recursive(child)


# ──────────────────────────────────────────────────────────────────────────────
# Main application window
# ──────────────────────────────────────────────────────────────────────────────

class PostcardImportApp(tk.Tk):
    """Main tkimport window: three steps, one screen."""

    def __init__(self, cfg: configparser.ConfigParser, gettext_func) -> None:
        super().__init__()
        self.cfg = cfg
        self._ = gettext_func

        self.importdir = Path(cfg["tkimport"].get("importdir", "import"))
        self.datadir = Path(cfg["tkimport"].get("datadir", "data"))
        self.importdir.mkdir(parents=True, exist_ok=True)

        self.title(self._("Postcard Import"))
        self.minsize(760, 560)
        self.geometry("980x720")

        self.tk.call("wm", "iconname", ".", "tkimport")
        try:
            self.tk.call("tk", "appname", "tkimport")
        except Exception:
            pass

        _icon_path = APP_DIR / "images" / "ktimport_256.png"
        if _icon_path.exists():
            try:
                _icon = ImageTk.PhotoImage(Image.open(_icon_path))
                self.iconphoto(True, _icon)
                self._icon = _icon
            except Exception as exc:
                logging.warning("Could not load icon: %s", exc)

        self._rows: dict[str, PairRow] = {}
        self._busy = False

        self._build_ui()
        self._load_settings_to_ui()
        self._load_existing_pairs()
        self._refresh_pending_count()

        self.protocol("WM_DELETE_WINDOW", self._on_quit)

        # Make sure the window is actually mapped with its final geometry
        # before popping up any message box, otherwise Tk cannot center
        # the dialog over it.
        self.update_idletasks()
        self.update()

        if not self._check_importdir_at_startup():
            # Nothing to do (or an inconsistency was found): show the
            # window just long enough to close it right away.
            self.after_idle(self.destroy)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        _ = self._

        # ── Step 1 ───────────────────────────────────────────────────────
        step1 = ttk.LabelFrame(self, text=_("1. Analyze and correct scans"), padding=10)
        step1.pack(fill=tk.X, padx=10, pady=(10, 4))
        step1.columnconfigure(3, weight=1)
        self._step1_frame = step1

        ttk.Label(step1, text=_("Prefix:")).grid(row=0, column=0, sticky="w")
        self._prefix_var = tk.StringVar()
        ttk.Entry(step1, textvariable=self._prefix_var, width=16).grid(
            row=0, column=1, sticky="w", padx=(4, 12))

        ttk.Label(step1, text=_("White threshold:")).grid(row=0, column=2, sticky="w")
        self._threshold_var = tk.StringVar()
        ttk.Spinbox(step1, textvariable=self._threshold_var, from_=0, to=255,
                    width=6).grid(row=0, column=3, sticky="w", padx=(4, 12))

        self._pending_var = tk.StringVar()
        ttk.Label(step1, textvariable=self._pending_var, foreground="gray").grid(
            row=0, column=4, sticky="e")

        self._prepare_btn = ttk.Button(step1, text=_("Analyze and correct scans"),
                                        command=self._start_prepare)
        self._prepare_btn.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        # ── Step 2 ───────────────────────────────────────────────────────
        step2 = ttk.LabelFrame(self, text=_("2. Validate scans"), padding=10)
        step2.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        toolbar = ttk.Frame(step2)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        self._editor_var = tk.StringVar()
        self._settings_btn = ttk.Button(
            toolbar, text="⚙", width=3, command=self._open_editor_settings)
        self._settings_btn.pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text=_("Select all"), command=lambda: self._select_all(True)
                   ).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text=_("Select none"), command=lambda: self._select_all(False)
                   ).pack(side=tk.RIGHT, padx=2)

        self._scroll = ScrollableFrame(step2)
        self._scroll.pack(fill=tk.BOTH, expand=True)

        # ── Step 3 ───────────────────────────────────────────────────────
        step3 = ttk.LabelFrame(self, text=_("3. Add to collection"), padding=10)
        step3.pack(fill=tk.X, padx=10, pady=4)

        self._remove_after_var = tk.BooleanVar()
        ttk.Checkbutton(step3, variable=self._remove_after_var,
                         text=_("Empty the import folder once postcards are added "
                                 "(deletes every file in the folder)")
                         ).pack(anchor="w")

        self._add_btn = ttk.Button(step3, text=_("Add validated postcards to collection"),
                                    command=self._start_add, style="Accent.TButton")
        self._add_btn.pack(anchor="w", pady=(6, 0))

        # ── Status + log ────────────────────────────────────────────────
        self._status_var = tk.StringVar(value=f"{_('Status:')} {_('Ready')}")
        ttk.Label(self, textvariable=self._status_var).pack(anchor="w", padx=12)

        log_frame = ttk.LabelFrame(self, text=_("Log"), padding=6)
        log_frame.pack(fill=tk.BOTH, padx=10, pady=(4, 10))
        self._log_text = tk.Text(log_frame, height=6, state=tk.DISABLED,
                                  wrap=tk.WORD, font=("Courier", 9))
        sb = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    # ── Startup analysis ────────────────────────────────────────────────────

    def _card_side_exists(self, pcid: str, side: str) -> bool:
        """True if *side* ("R" or "V") of postcard *pcid* is already stored
        in ``datadir/cards`` (the collection's storage extension is always
        ``tiff``, see :func:`libs.scan_add.add_one`)."""
        return (self.datadir / "cards" / f"{pcid}_{side}.tiff").exists()

    def _grey_step1(self) -> None:
        """Grey out step 1 and disable the 'Analyze and correct scans' button."""
        self._prepare_btn.config(state=tk.DISABLED)
        _disable_recursive(self._step1_frame)

    def _check_importdir_at_startup(self) -> bool:
        """Inspect ``importdir`` right after start-up.

        Returns ``True`` if the application should carry on normally, or
        ``False`` (after showing an explanatory message box) if it has
        nothing to do and must quit immediately.
        """
        _ = self._
        rv_files, other_files = scan_importdir(self.importdir)
        n = len(rv_files)
        m = len(other_files)

        if n == 0 and m == 0:
            messagebox.showinfo(_("Info"), _("Aucun scan à traiter"), parent=self)
            return False

        if m % 2 != 0:
            messagebox.showerror(
                _("Error"), _("Nombre de scan(s) à traiter incohérent"), parent=self)
            return False

        if n > 0 and m > n:
            messagebox.showerror(
                _("Error"),
                _("La précédente analyse n'a pas été terminée ... "
                  "je ne sais pas quoi faire"),
                parent=self)
            return False

        if n > 0 and m == n:
            self._grey_step1()

            inconsistent = []
            something_to_do = False
            for pcid, sides in list_pairs(self.importdir).items():
                has_r = self._card_side_exists(pcid, "R")
                has_v = self._card_side_exists(pcid, "V")
                if has_r and has_v:
                    row = self._rows.get(pcid)
                    if row is not None:
                        row.set_inactive()
                elif has_r or has_v:
                    inconsistent.append(pcid)
                else:
                    something_to_do = True

            if inconsistent:
                messagebox.showwarning(
                    _("Warning"),
                    _("Incohérence entre importdir et la base de données")
                    + " (#" + ", #".join(inconsistent) + ")",
                    parent=self)

            if not something_to_do:
                messagebox.showinfo(
                    _("Info"), _("Rien à faire ... je quitte"), parent=self)
                return False

        return True

    # ── Settings ────────────────────────────────────────────────────────────

    def _load_settings_to_ui(self) -> None:
        s = self.cfg["tkimport"]
        self._prefix_var.set(s.get("prefix", ""))
        self._threshold_var.set(s.get("white_threshold", "240"))
        self._remove_after_var.set(s.get("remove_after_add", "false").lower() == "true")
        app_cmd = get_preferred_app(self.cfg)
        self._editor_var.set(app_cmd or self._("(system default)"))

    def _save_settings_from_ui(self) -> None:
        s = self.cfg["tkimport"]
        s["prefix"] = self._prefix_var.get()
        s["white_threshold"] = self._threshold_var.get() or "240"
        s["remove_after_add"] = str(self._remove_after_var.get()).lower()
        save_config(self.cfg)

    def _browse_editor(self, parent=None) -> None:
        parent = parent or self
        path = filedialog.askopenfilename(
            title=self._("Choose your preferred image editor"), parent=parent)
        if not path:
            return
        set_preferred_app(self.cfg, path)
        self._editor_var.set(path)
        save_config(self.cfg)
        self._log(self._("Preferred image editor set to {app}").format(app=path))

    def _clear_editor(self, parent=None) -> None:
        set_preferred_app(self.cfg, "")
        self._editor_var.set(self._("(system default)"))
        save_config(self.cfg)

    def _open_editor_settings(self) -> None:
        """Modal dialog to pick/reset the preferred image editor."""
        _ = self._
        win = tk.Toplevel(self)
        win.title(_("Preferred image editor"))
        win.transient(self)
        win.resizable(False, False)

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frm, text=_("Preferred image editor:")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(frm, textvariable=self._editor_var, width=44, state="readonly"
                  ).grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 10))

        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(btns, text=_("Browse…"),
                   command=lambda: self._browse_editor(parent=win)
                   ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text=_("Use system default"),
                   command=lambda: self._clear_editor(parent=win)
                   ).pack(side=tk.LEFT)

        ttk.Button(frm, text=_("Close"), command=win.destroy).grid(
            row=3, column=0, columnspan=2, sticky="e", pady=(16, 0))

        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        win.grab_set()
        win.wait_window()

    # ── Step 1: prepare ────────────────────────────────────────────────────

    def _refresh_pending_count(self) -> None:
        _rv, other = scan_importdir(self.importdir)
        self._pending_var.set(
            self._("Raw scans waiting: {count}").format(count=len(other)))

    def _start_prepare(self) -> None:
        if self._busy:
            return
        self._save_settings_from_ui()
        try:
            threshold = int(self._threshold_var.get())
        except ValueError:
            threshold = 240
        prefix = self._prefix_var.get()

        self._set_busy(True)
        self._set_status(self._("Analyzing scans..."))
        threading.Thread(
            target=self._prepare_worker, args=(prefix, threshold), daemon=True,
        ).start()

    def _prepare_worker(self, prefix: str, threshold: int) -> None:
        try:
            from libpostcards.model import Model
            next_id = Model(str(self.datadir)).next_id()

            def _on_pair(pair):
                self.after(0, self._on_pair_prepared, pair)

            pairs = prepare_pairs(
                self.importdir, next_id,
                prefix=prefix, white_threshold=threshold,
                on_pair=_on_pair,
            )
            self.after(0, self._on_prepare_done, len(pairs), None)
        except Exception as exc:
            self.after(0, self._on_prepare_done, 0, str(exc))

    def _on_pair_prepared(self, pair) -> None:
        self._log(f"+ #{pair.pcid}: {pair.recto_dst.name} / {pair.verso_dst.name}")
        self._add_row(str(pair.pcid), {"R": pair.recto_dst, "V": pair.verso_dst})
        self._refresh_pending_count()

    def _on_prepare_done(self, count: int, error: str | None) -> None:
        self._set_busy(False)
        if error:
            self._set_status(self._("Preparation failed"))
            self._log(f"x {error}")
            messagebox.showerror(self._("Error"), error, parent=self)
            return
        self._set_status(self._("Ready"))
        self._log(self._("Prepared {count} postcard(s)").format(count=count))

    # ── Step 2: review ──────────────────────────────────────────────────────

    def _load_existing_pairs(self) -> None:
        """Populate the review list with pairs already prepared (previous run)."""
        for pcid, sides in complete_pairs(self.importdir).items():
            self._add_row(pcid, sides)

    def _add_row(self, pcid: str, paths: dict) -> None:
        if pcid in self._rows:
            for side, path in paths.items():
                self._rows[pcid].paths[side] = path
                self._rows[pcid]._refresh_thumb(side)
            return
        row = PairRow(self._scroll.body, self, pcid, paths)
        row.pack(fill=tk.X, padx=4, pady=4)
        self._rows[pcid] = row

    def _select_all(self, value: bool) -> None:
        for row in self._rows.values():
            if not row.added:
                row.included_var.set(value)

    # ── Step 3: add ─────────────────────────────────────────────────────────

    def _start_add(self) -> None:
        if self._busy:
            return
        self._save_settings_from_ui()
        ids = [pcid for pcid, row in self._rows.items()
               if row.included_var.get() and not row.added]
        if not ids:
            messagebox.showinfo(self._("Info"),
                                 self._("No validated postcard to add."), parent=self)
            return

        self._set_busy(True)
        self._set_status(self._("Adding postcards..."))
        threading.Thread(target=self._add_worker, args=(ids,), daemon=True).start()

    def _add_worker(self, ids: list) -> None:
        def _progress(i, total, added):
            self.after(0, self._on_add_progress, i, total, added)

        try:
            add_pairs(str(self.datadir), self.importdir, ids, on_progress=_progress)
            self.after(0, self._on_add_done, None)
        except Exception as exc:
            self.after(0, self._on_add_done, str(exc))

    def _on_add_progress(self, i: int, total: int, added) -> None:
        if added is None:
            return
        self._log(f"+ {self._('Added')} #{added.pcid}")
        row = self._rows.get(str(added.pcid))
        if row is not None:
            row.mark_added()
        self._set_status(f"{self._('Adding postcards...')} {i + 1}/{total}")

    def _clear_importdir(self) -> None:
        """Delete every file (not sub-directory) found in ``importdir``."""
        importdir = Path(self.importdir)
        if not importdir.is_dir():
            return
        for entry in importdir.iterdir():
            if entry.is_file():
                try:
                    entry.unlink(missing_ok=True)
                except Exception as exc:
                    logging.warning("could not remove %s: %s", entry, exc)

    def _on_add_done(self, error: str | None) -> None:
        self._set_busy(False)
        if error:
            self._set_status(self._("Add failed"))
            self._log(f"x {error}")
            messagebox.showerror(self._("Error"), error, parent=self)
            return
        if self._remove_after_var.get():
            self._clear_importdir()
            self._log(self._("Import folder emptied"))
            for pcid, row in list(self._rows.items()):
                if not row.added:
                    row.destroy()
                    del self._rows[pcid]
        self._set_status(self._("Ready"))
        self._refresh_pending_count()

    # ── Utilities ────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self._prepare_btn.config(state=state)
        self._add_btn.config(state=state)

    def _set_status(self, msg: str) -> None:
        self._status_var.set(f"{self._('Status:')} {msg}")

    def _log(self, msg: str) -> None:
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, msg + "\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _on_quit(self) -> None:
        self._save_settings_from_ui()
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point (click)
# ──────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--prefix", default=None, help="Override file prefix.")
@click.option("--white-threshold", default=None, type=int,
              help="Override white threshold for background transparency.")
@click.version_option("1.0.0", prog_name="tkimport")
@click.pass_obj
def main(common, prefix, white_threshold):
    """tkimport - review and import prepared scans into the collection."""
    global CONFIG_FILE
    if common and getattr(common, "conffile", None):
        CONFIG_FILE = Path(common.conffile)

    cfg = load_config()
    translation = setup_i18n(cfg["tkimport"].get("language") or None)
    gettext_func = translation.gettext

    if prefix:
        cfg["tkimport"]["prefix"] = prefix
    if white_threshold is not None:
        cfg["tkimport"]["white_threshold"] = str(white_threshold)

    app = PostcardImportApp(cfg, gettext_func)
    app.mainloop()


def run():
    """Standalone entry point (tkimport script): runs `cli main`."""
    sys.argv.insert(1, "main")
    cli()


if __name__ == "__main__":
    run()
