# -*- encoding: utf-8 -*-
"""
tkpostcards.libs.mask_editor
----------------------------

Fenêtre de retouche des masques COLMAP, ouverte depuis ktmanager
(menu « Plus » > « Masques »).

Elle parcourt les cartes de la collection actuellement sélectionnée dans
ktmanager. Les zones **ignorées** par COLMAP sont teintées en rouge ;
on les modifie au pinceau ou au rectangle, et on peut demander à SAM de
(re)proposer un masque pour la carte courante ou pour toute la collection
(travail en arrière-plan, l'interface reste utilisable).

Un masque enregistré depuis cette fenêtre est marqué « manuel » : la
génération automatique (ici comme dans ``kttools masks generate``) ne
l'écrasera plus.

Seul PIL est requis pour retoucher ; numpy / opencv / torch /
transformers ne sont nécessaires que pour la génération automatique.
"""
from __future__ import annotations

import importlib.util
import queue
import threading
import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageTk

from libpostcards.masks import (
    MaskParams, MaskStore, StampDetector, generate_mask, generate_masks,
    load_working_pil, sam_model_from_config,
)

# Même palette que tkmanager (dupliquée pour éviter un import circulaire).
BG_MAIN, BG_CARD, BG_FIELD = "#1a1a2e", "#16213e", "#0f3460"
BG_CANVAS = "#0d1117"
FG_TEXT, FG_LABEL, FG_ACCENT, FG_ACCENT2 = "#e0e0e0", "#a0b4c8", "#e94560", "#f5a623"
OVERLAY_RGB = (233, 69, 96)
OVERLAY_MAX_ALPHA = 110          # sur 255 : on voit encore la carte dessous
UNDO_LIMIT = 30


def sam_libs_available(remote=False):
    """Avec un external_helper, SAM tourne sur le serveur : il ne reste à
    avoir ici que de quoi trier les segments et les recevoir."""
    needed = (("numpy", "cv2", "requests") if remote
              else ("numpy", "cv2", "torch", "transformers"))
    return all(importlib.util.find_spec(m) is not None for m in needed)


class MaskEditorView(tk.Toplevel):

    TOOLS = (
        ("brush_off", "mask_tool_brush_off"),
        ("brush_on", "mask_tool_brush_on"),
        ("rect_off", "mask_tool_rect_off"),
        ("rect_on", "mask_tool_rect_on"),
    )

    def __init__(self, parent, tr, datadir, ids, start_id=None,
                 collection_label="", cfg=None, side="R",
                 external_helper=None, external_helper_timeout=None,
                 on_mask_changed=None):
        super().__init__(parent)
        # Called with the card id after a mask was saved (None: several
        # masks changed, e.g. a whole-collection generation) so that the main
        # window can refresh its mask thumbnail.
        self._on_mask_changed = on_mask_changed
        self._ = tr
        self._cfg = cfg
        self._ids = [str(i) for i in ids]
        self._collection_label = collection_label
        self._params = MaskParams.from_config(cfg)
        self._sam_model = sam_model_from_config(cfg)
        self._device = ((cfg.get("DEFAULT", "torch_device", fallback="")
                         if cfg is not None else "").strip() or None)
        file_format = (cfg.get("DEFAULT", "file_format", fallback="tiff")
                       if cfg is not None else "tiff")
        self._datadir = datadir
        self._file_format = file_format
        self._side = side
        self._store = MaskStore(datadir, file_format, side)
        self._detector = None
        self._external_helper = (external_helper or "").strip() or None
        self._external_helper_timeout = external_helper_timeout

        self._idx = 0
        if start_id is not None and str(start_id) in self._ids:
            self._idx = self._ids.index(str(start_id))

        self._rgb = None            # carte aplatie (PIL RGB, résolution de travail)
        self._mask = None           # PIL "L" : 255 = garder, 0 = ignorer
        self._source = None
        self._missing_path = None
        self._dirty = False
        self._undo = []
        self._redo = []
        self._base_disp = None
        self._base_key = None
        self._photo = None
        self._render_job = None
        self._stroke = None
        self._busy = False
        self._stop = threading.Event()
        self._queue = queue.Queue()

        self.title(self._("mask_editor_title"))
        self.configure(bg=BG_MAIN)
        self.geometry("1100x800")
        self.minsize(720, 480)
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, lambda: self._load(self._idx))

    # ── Interface ───────────────────────────────────────────────────────────
    def _btn(self, parent, text, cmd, **kw):
        return tk.Button(parent, text=text, command=cmd, bg=BG_FIELD,
                         fg=FG_TEXT, activebackground=FG_ACCENT,
                         activeforeground="#fff", relief=tk.FLAT, padx=8,
                         **kw)

    def _build_ui(self):
        bar1 = tk.Frame(self, bg=BG_CARD, pady=4)
        bar1.pack(fill=tk.X)
        self._btn(bar1, "◀", lambda: self._step(-1)).pack(side=tk.LEFT, padx=(6, 2))
        self._btn(bar1, "▶", lambda: self._step(+1)).pack(side=tk.LEFT, padx=2)
        self._btn(bar1, self._("mask_undo"), self._undo_last).pack(side=tk.LEFT, padx=(12, 2))
        self._btn(bar1, self._("mask_redo"), self._redo_last).pack(side=tk.LEFT, padx=2)
        self._btn(bar1, self._("mask_save"), self._save).pack(side=tk.LEFT, padx=(12, 2))
        self._btn(bar1, self._("mask_clear"), self._keep_all).pack(side=tk.LEFT, padx=(12, 2))
        self._btn(bar1, self._("mask_exclude_all"), self._exclude_all).pack(side=tk.LEFT, padx=2)

        self._auto_btns = [
            self._btn(bar1, self._("mask_auto_one"), self._auto_current),
            self._btn(bar1, self._("mask_auto_all"), self._auto_collection),
        ]
        for b in self._auto_btns:
            b.pack(side=tk.LEFT, padx=(12, 2))
        self._stop_btn = self._btn(bar1, self._("mask_stop"), self._stop.set)
        self._show_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar1, text=self._("mask_show"), variable=self._show_var,
                       command=self._request_render, bg=BG_CARD, fg=FG_TEXT,
                       selectcolor=BG_FIELD, activebackground=BG_CARD,
                       activeforeground=FG_TEXT).pack(side=tk.RIGHT, padx=8)

        bar2 = tk.Frame(self, bg=BG_CARD, pady=2)
        bar2.pack(fill=tk.X)
        self._tool = tk.StringVar(value="brush_off")
        self._tool.trace_add("write", lambda *_a: self._update_cursor())
        for value, key in self.TOOLS:
            tk.Radiobutton(bar2, text=self._(key), value=value,
                           variable=self._tool, indicatoron=False,
                           bg=BG_FIELD, fg=FG_TEXT, selectcolor=FG_ACCENT,
                           activebackground=FG_ACCENT, padx=8,
                           relief=tk.FLAT).pack(side=tk.LEFT, padx=2, pady=2)
        tk.Label(bar2, text=self._("mask_size"), bg=BG_CARD,
                 fg=FG_LABEL).pack(side=tk.LEFT, padx=(14, 4))
        self._size_var = tk.IntVar(value=18)
        tk.Scale(bar2, from_=3, to=120, orient=tk.HORIZONTAL, length=160,
                 variable=self._size_var, showvalue=True, bg=BG_CARD,
                 fg=FG_TEXT, highlightthickness=0,
                 troughcolor=BG_FIELD).pack(side=tk.LEFT)

        self._cv = tk.Canvas(self, bg=BG_CANVAS, highlightthickness=0,
                             cursor="none")
        self._cv.pack(fill=tk.BOTH, expand=True)
        self._update_cursor()
        self._cv.bind("<Configure>", lambda _e: self._request_render())
        self._cv.bind("<Motion>", self._on_motion)
        self._cv.bind("<Leave>", lambda _e: self._cv.delete("cursor"))
        self._cv.bind("<ButtonPress-1>", lambda e: self._press(e, False))
        self._cv.bind("<B1-Motion>", self._drag)
        self._cv.bind("<ButtonRelease-1>", self._release)
        for btn in (2, 3):   # clic droit : Button-3 (Linux/Windows), Button-2 (macOS)
            self._cv.bind("<ButtonPress-%d>" % btn, lambda e: self._press(e, True))
            self._cv.bind("<B%d-Motion>" % btn, self._drag)
            self._cv.bind("<ButtonRelease-%d>" % btn, self._release)

        self._status = tk.Label(self, text="", anchor="w", bg=BG_CARD,
                                fg=FG_ACCENT2, padx=8)
        self._status.pack(fill=tk.X)
        tk.Label(self, text=self._("mask_help"), anchor="w", bg=BG_MAIN,
                 fg=FG_LABEL, padx=8).pack(fill=tk.X)

    def _bind_keys(self):
        self.bind("<Left>", lambda _e: self._step(-1))
        self.bind("<Right>", lambda _e: self._step(+1))
        # Annuler : Ctrl+Z. Rétablir : Ctrl+Y, ou Ctrl+Maj+Z. Verrouillage des
        # majuscules actif, Ctrl+Z arrive comme le keysym "Z" : on regarde la
        # touche Maj elle-même pour ne pas confondre les deux.
        self.bind("<Control-z>", lambda _e: self._undo_last())
        self.bind("<Control-Z>", lambda e: self._redo_last() if e.state & 0x1
                  else self._undo_last())
        self.bind("<Control-y>", lambda _e: self._redo_last())
        self.bind("<Control-Y>", lambda _e: self._redo_last())
        self.bind("<Control-s>", lambda _e: self._save())
        self.bind("m", lambda _e: (self._show_var.set(not self._show_var.get()),
                                   self._request_render()))
        self.bind("[", lambda _e: self._size_var.set(max(3, self._size_var.get() - 3)))
        self.bind("]", lambda _e: self._size_var.set(min(120, self._size_var.get() + 3)))
        self.bind("<Escape>", lambda _e: self._on_close())

    # ── Chargement / navigation ─────────────────────────────────────────────
    def _current_id(self):
        return self._ids[self._idx] if self._ids else None

    def _load(self, idx):
        self._idx = idx
        self._undo.clear()
        self._redo.clear()
        self._dirty = False
        self._base_key = None
        self._rgb = self._mask = None
        pid = self._current_id()
        self._source = None
        self._missing_path = None
        if pid is not None:
            path = self._store.card_path(pid)
            if path is None:
                self._missing_path = str(
                    self._store.datadir / "cards" / f"{pid}_{self._side}.{self._file_format}")
            else:
                rgba, _orig = load_working_pil(path, self._params.max_side)
                alpha = rgba.getchannel("A")
                flat = Image.new("RGB", rgba.size, (40, 40, 48))
                flat.paste(rgba, mask=alpha)
                self._rgb = flat
                mask = self._store.read(pid)
                if mask is not None:
                    if mask.size != flat.size:
                        mask = mask.resize(flat.size, Image.BILINEAR).point(
                            lambda v: 255 if v > 127 else 0)
                    self._source = self._store.source(pid)
                else:
                    thr = self._params.alpha_threshold
                    mask = alpha.point(lambda v: 255 if v > thr else 0)
                self._mask = mask
        self._update_cursor()
        self._update_status()
        self._request_render()

    def _step(self, delta):
        if not self._ids:
            return
        new = self._idx + delta
        if not 0 <= new < len(self._ids):
            return
        if self._dirty:
            self._save()
        self._load(new)

    def _update_status(self):
        pid = self._current_id()
        if pid is None:
            self._status.config(text="")
            return
        src = {None: "mask_src_none", "auto": "mask_src_auto",
               "manual": "mask_src_manual"}[self._source]
        text = self._("mask_status").format(
            id=pid, n=self._idx + 1, total=len(self._ids),
            src=self._(src), coll=self._collection_label)
        if self._dirty:
            text += "  *"
        self._status.config(text=text)

    # ── Rendu ───────────────────────────────────────────────────────────────
    def _request_render(self):
        if self._render_job is None:
            self._render_job = self.after(15, self._render)

    def _geom(self):
        cw = max(1, self._cv.winfo_width())
        ch = max(1, self._cv.winfo_height())
        w, h = self._mask.size
        s = min(cw / w, ch / h)
        dw, dh = max(1, int(w * s)), max(1, int(h * s))
        return s, (cw - dw) // 2, (ch - dh) // 2, dw, dh

    def _render(self):
        self._render_job = None
        self._cv.delete("img")
        self._cv.delete("msg")
        if self._mask is None:
            text = (self._("mask_no_image").format(path=self._missing_path)
                    if self._missing_path else "")
            self._cv.create_text(self._cv.winfo_width() // 2,
                                 self._cv.winfo_height() // 2, text=text,
                                 fill=FG_ACCENT, tags="msg")
            return
        s, ox, oy, dw, dh = self._geom()
        key = (self._idx, dw, dh)
        if key != self._base_key:
            self._base_disp = self._rgb.resize((dw, dh), Image.BILINEAR)
            self._base_key = key
        img = self._base_disp
        if self._show_var.get():
            m = self._mask.resize((dw, dh), Image.BILINEAR)
            alpha = m.point(lambda v: (255 - v) * OVERLAY_MAX_ALPHA // 255)
            img = Image.composite(Image.new("RGB", (dw, dh), OVERLAY_RGB),
                                  img, alpha)
        self._photo = ImageTk.PhotoImage(img)
        self._cv.create_image(ox, oy, anchor="nw", image=self._photo, tags="img")
        self._cv.tag_lower("img")

    # ── Dessin ──────────────────────────────────────────────────────────────
    def _to_mask(self, ex, ey):
        s, ox, oy, _dw, _dh = self._geom()
        return (ex - ox) / s, (ey - oy) / s

    def _update_cursor(self):
        """Curseur de la zone de dessin. Le pinceau cache le curseur du système
        et dessine à la place un cercle de la taille du trait (voir
        _on_motion) ; le rectangle n'a pas d'aperçu de ce genre, il garde donc
        un curseur visible (une croix). Sans carte chargée : la flèche."""
        if self._mask is None:
            cursor = "arrow"
        elif self._tool.get().startswith("brush"):
            cursor = "none"
        else:
            cursor = "crosshair"
        self._cv.config(cursor=cursor)
        if cursor != "none":
            self._cv.delete("cursor")     # cercle du pinceau resté à l'écran

    def _on_motion(self, event):
        self._cv.delete("cursor")
        if self._tool.get().startswith("brush") and self._mask is not None:
            r = self._size_var.get()
            self._cv.create_oval(event.x - r, event.y - r, event.x + r,
                                 event.y + r, outline="#fff", tags="cursor")

    def _record_change(self, before):
        """Mémorise l'état d'AVANT une modification (pour l'annuler) et oublie
        ce qu'on pouvait rétablir : une nouvelle action coupe l'historique."""
        self._undo.append(before)
        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)
        self._redo.clear()

    def _push_undo(self):
        """Mémorise le masque tel qu'il est, avant de le remplacer d'un coup."""
        self._record_change(self._mask.copy())

    def _press(self, event, invert):
        if self._mask is None:
            return
        kind, state = self._tool.get().split("_")
        value = 0 if state == "off" else 255
        if invert:
            value = 255 - value
        p = self._to_mask(event.x, event.y)
        # ``before`` n'entre dans l'historique que si le geste change vraiment
        # le masque : un clic sans effet ne doit ni créer une étape à annuler
        # ni effacer ce qu'on peut rétablir.
        self._stroke = {"kind": kind, "value": value, "last": p,
                        "start": p, "start_cv": (event.x, event.y),
                        "changed": False, "before": self._mask.copy()}
        if kind == "brush":
            self._paint(p, p, value)

    def _drag(self, event):
        st = self._stroke
        if st is None:
            return
        p = self._to_mask(event.x, event.y)
        if st["kind"] == "brush":
            self._paint(st["last"], p, st["value"])
            st["last"] = p
            self._on_motion(event)
        else:
            self._cv.delete("rubber")
            x0, y0 = st["start_cv"]
            self._cv.create_rectangle(
                x0, y0, event.x, event.y, tags="rubber", dash=(4, 3),
                outline="#fff" if st["value"] == 255 else FG_ACCENT)

    def _release(self, event):
        st, self._stroke = self._stroke, None
        if st is None:
            return
        if st["kind"] == "rect":
            self._cv.delete("rubber")
            x0, y0 = st["start"]
            x1, y1 = self._to_mask(event.x, event.y)
            if abs(x1 - x0) >= 2 and abs(y1 - y0) >= 2:
                self._record_change(st["before"])
                ImageDraw.Draw(self._mask).rectangle(
                    [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)],
                    fill=st["value"])
                st["changed"] = True
        if st["changed"]:
            self._mark_dirty()
            self._request_render()

    def _paint(self, p0, p1, value):
        s = self._geom()[0]
        r = self._size_var.get() / s
        if not self._stroke["changed"]:
            self._record_change(self._stroke["before"])    # 1er coup de pinceau du geste
        d = ImageDraw.Draw(self._mask)
        d.line([p0, p1], fill=value, width=max(1, int(2 * r)))
        for x, y in (p0, p1):
            d.ellipse([x - r, y - r, x + r, y + r], fill=value)
        self._stroke["changed"] = True
        self._mark_dirty()
        self._request_render()

    def _mark_dirty(self):
        if not self._dirty:
            self._dirty = True
            self._update_status()

    def _undo_last(self):
        """Annule le dernier masquage (Ctrl+Z)."""
        if self._undo and not self._busy and self._stroke is None:
            self._redo.append(self._mask.copy())
            self._mask = self._undo.pop()
            self._dirty = True
            self._update_status()
            self._request_render()

    def _redo_last(self):
        """Rétablit ce qu'on vient d'annuler (Ctrl+Y)."""
        if self._redo and not self._busy and self._stroke is None:
            self._undo.append(self._mask.copy())
            self._mask = self._redo.pop()
            self._dirty = True
            self._update_status()
            self._request_render()

    def _fill_all(self, value):
        if self._mask is None:
            return
        self._push_undo()
        self._mask = Image.new("L", self._mask.size, value)
        self._mark_dirty()
        self._request_render()

    def _keep_all(self):
        """Toute la carte utilisable (aucune zone ignorée)."""
        self._fill_all(255)

    def _exclude_all(self):
        """Toute la carte ignorée : on repart d'une carte entièrement masquée,
        puis on dégage au pinceau ou au rectangle (mode « garder ») les
        zones à conserver."""
        self._fill_all(0)

    def _save(self):
        pid = self._current_id()
        if pid is None or self._mask is None:
            return
        self._store.write(pid, self._mask, "manual")
        self._source = "manual"
        self._dirty = False
        self._update_status()
        if self._on_mask_changed is not None:
            self._on_mask_changed(pid)

    # ── Génération automatique (SAM) en arrière-plan ───────────────────────
    def _ensure_libs(self):
        remote = bool(self._external_helper)
        if sam_libs_available(remote):
            return True
        key = "mask_need_libs_remote" if remote else "mask_need_libs"
        messagebox.showwarning(self._("mask_editor_title"), self._(key),
                               parent=self)
        return False

    def _get_detector(self):
        if self._detector is None:
            self._detector = StampDetector(
                self._sam_model, self._device, self._params,
                external_helper=self._external_helper,
                external_helper_timeout=self._external_helper_timeout)
        return self._detector

    def _wait_text(self):
        return self._("mask_remote_wait").format(helper=self._external_helper) \
            if self._external_helper else self._("mask_loading_model")

    def _set_busy(self, busy, text=None):
        self._busy = busy
        for b in self._auto_btns:
            b.config(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self._stop.clear()
            self._stop_btn.pack(side=tk.LEFT, padx=2)
        else:
            self._stop_btn.pack_forget()
        if text is not None:
            self._status.config(text=text)

    def _auto_current(self):
        pid = self._current_id()
        if self._busy or pid is None or self._mask is None or not self._ensure_libs():
            return
        path = self._store.card_path(pid)
        detector = self._get_detector()
        params = self._params
        self._set_busy(True, self._wait_text())

        def job():
            try:
                mask, _info = generate_mask(path, detector, params)
                self._queue.put(("one", pid, mask))
            except Exception as exc:
                self._queue.put(("error", str(exc)))

        threading.Thread(target=job, daemon=True).start()
        self.after(100, self._poll)

    def _auto_collection(self):
        if self._busy or not self._ids or not self._ensure_libs():
            return
        overwrite = messagebox.askyesnocancel(
            self._("mask_editor_title"), self._("mask_overwrite_q"), parent=self)
        if overwrite is None:
            return
        if self._dirty:
            self._save()
        ids, detector, params = list(self._ids), self._get_detector(), self._params
        self._set_busy(True, self._wait_text())

        def job():
            try:
                counts = generate_masks(
                    self._datadir, ids, file_format=self._file_format,
                    side=self._side, force=overwrite, use_sam=True,
                    detector=detector, params=params,
                    on_progress=lambda i, t, pid, st, info:
                        self._queue.put(("progress", i, t)),
                    should_stop=self._stop.is_set)
                self._queue.put(("batch_done", counts))
            except Exception as exc:
                self._queue.put(("error", str(exc)))

        threading.Thread(target=job, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        finished = False
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self._status.config(text=self._("mask_batch_progress").format(
                        i=msg[1], total=msg[2]))
                elif kind == "one":
                    if msg[1] == self._current_id() and self._mask is not None:
                        new = Image.fromarray(msg[2], "L")
                        if new.size != self._mask.size:
                            new = new.resize(self._mask.size, Image.BILINEAR)
                        self._push_undo()
                        self._mask = new
                        self._mark_dirty()
                        self._request_render()
                    finished = True
                elif kind == "batch_done":
                    messagebox.showinfo(
                        self._("mask_editor_title"),
                        self._("mask_batch_done").format(**msg[1]), parent=self)
                    self._load(self._idx)
                    if self._on_mask_changed is not None:
                        self._on_mask_changed(None)
                    finished = True
                elif kind == "error":
                    messagebox.showerror(
                        self._("mask_editor_title"),
                        self._("mask_error").format(error=msg[1]), parent=self)
                    finished = True
        except queue.Empty:
            pass
        if finished:
            self._set_busy(False)
            self._update_status()
        else:
            self.after(100, self._poll)

    # ── Fermeture ───────────────────────────────────────────────────────────
    def _on_close(self):
        if self._dirty:
            answer = messagebox.askyesnocancel(
                self._("mask_editor_title"), self._("mask_save_q"), parent=self)
            if answer is None:
                return
            if answer:
                self._save()
        self._stop.set()
        self.destroy()
