# -*- encoding: utf-8 -*-
"""Éditeur de masques : curseur selon l'outil, annuler / rétablir, tout masquer.
Ignoré sans affichage (lancer sous xvfb-run)."""
import time
import types

import pytest
from PIL import Image

tk = pytest.importorskip("tkinter")


def pump(root, seconds=0.2):
    end = time.time() + seconds
    while time.time() < end:
        root.update_idletasks()
        root.update()
        time.sleep(0.01)


@pytest.fixture
def ed(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("pas d'affichage (xvfb-run ?)")
    from tkpostcards.libs.mask_editor import MaskEditorView
    (tmp_path / "cards").mkdir()
    for cid in (1, 2):
        Image.new("RGBA", (400, 300), (150, 120, 90, 255)).save(
            tmp_path / "cards" / f"{cid}_R.tiff")
    changed = []
    editor = MaskEditorView(root, lambda key: key, tmp_path, [1, 2], start_id=1,
                            on_mask_changed=changed.append)
    editor.changed = changed
    editor.root = root
    editor.geometry("900x700+0+0")
    pump(root, 0.5)
    editor.focus_force()
    pump(root, 0.2)
    assert editor._mask is not None
    yield editor
    editor._dirty = False
    editor._on_close()
    root.destroy()


def ev(x, y):
    return types.SimpleNamespace(x=x, y=y)


def at(ed, mx, my):
    s, ox, oy, _w, _h = ed._geom()
    return ox + mx * s, oy + my * s


def rect(ed, x0, y0, x1, y1, tool="rect_off"):
    ed._tool.set(tool)
    ed._press(ev(*at(ed, x0, y0)), False)
    ed._drag(ev(*at(ed, x1, y1)))
    ed._release(ev(*at(ed, x1, y1)))


def px(ed, x, y):
    return ed._mask.getpixel((x, y))


def key(ed, sequence):
    ed.focus_force()
    ed.update()
    ed.event_generate(sequence, when="now")
    pump(ed.root, 0.05)


# ── curseur ─────────────────────────────────────────────────────────────────

def test_cursor_is_visible_with_the_rectangle_tools(ed):
    """Le cercle d'aperçu n'existe que pour le pinceau : avec le rectangle, un
    curseur « none » rendait la souris invisible."""
    seen = {}
    for tool in ("brush_off", "brush_on", "rect_off", "rect_on"):
        ed._tool.set(tool)
        seen[tool] = str(ed._cv.cget("cursor"))
    assert seen == {"brush_off": "none", "brush_on": "none",
                    "rect_off": "crosshair", "rect_on": "crosshair"}


def test_brush_circle_is_dropped_when_switching_to_the_rectangle(ed):
    ed._tool.set("brush_off")
    ed._on_motion(ev(300, 300))
    assert ed._cv.find_withtag("cursor")                       # cercle du pinceau
    ed._tool.set("rect_off")
    assert not ed._cv.find_withtag("cursor")                   # plus de cercle fantôme


def test_cursor_is_an_arrow_without_a_card(ed):
    ed._mask = None
    ed._update_cursor()
    assert str(ed._cv.cget("cursor")) == "arrow"


# ── annuler / rétablir ──────────────────────────────────────────────────────

def test_undo_and_redo_with_buttons_logic(ed):
    before = px(ed, 100, 100)
    rect(ed, 80, 80, 140, 140)
    assert px(ed, 100, 100) == 0 and ed._dirty
    ed._undo_last()
    assert px(ed, 100, 100) == before
    ed._redo_last()
    assert px(ed, 100, 100) == 0
    ed._undo_last()
    ed._undo_last()                                            # plus rien à annuler : sans effet
    assert px(ed, 100, 100) == before


def test_keyboard_shortcuts(ed):
    rect(ed, 80, 80, 140, 140)
    key(ed, "<Control-z>")
    assert px(ed, 100, 100) == 255                             # Ctrl+Z annule
    key(ed, "<Control-y>")
    assert px(ed, 100, 100) == 0                               # Ctrl+Y rétablit
    key(ed, "<Control-z>")
    key(ed, "<Control-Shift-Z>")
    assert px(ed, 100, 100) == 0                               # Ctrl+Maj+Z rétablit aussi
    key(ed, "<Control-z>")
    key(ed, "<Control-Y>")
    assert px(ed, 100, 100) == 0                               # Ctrl+Y avec majuscules : idem


def test_caps_lock_does_not_turn_undo_into_redo(ed):
    """Majuscules verrouillées, Ctrl+Z arrive comme le keysym "Z" avec le bit
    Lock (et non Maj) : c'est un annuler. Ctrl+Maj+Z, lui, rétablit."""
    CONTROL, SHIFT, LOCK = 0x4, 0x1, 0x2
    rect(ed, 80, 80, 140, 140)
    ed.focus_force()
    ed.update()
    ed.event_generate("<KeyPress>", keysym="Z", state=CONTROL | LOCK)
    assert px(ed, 100, 100) == 255                             # annulé
    ed.event_generate("<KeyPress>", keysym="Z", state=CONTROL | SHIFT)
    assert px(ed, 100, 100) == 0                               # rétabli


def test_a_new_action_cuts_the_redo_history(ed):
    rect(ed, 80, 80, 140, 140)
    ed._undo_last()
    rect(ed, 200, 200, 240, 240)                               # nouvelle action
    ed._redo_last()                                            # il n'y a plus rien à rétablir
    assert px(ed, 100, 100) == 255 and px(ed, 220, 220) == 0


def test_a_click_that_changes_nothing_keeps_the_redo_history(ed):
    rect(ed, 80, 80, 140, 140)
    ed._undo_last()
    ed._tool.set("rect_off")
    ed._press(ev(*at(ed, 300, 50)), False)
    ed._release(ev(*at(ed, 300, 50)))                          # clic sans glisser : aucun effet
    assert len(ed._undo) == 0 and len(ed._redo) == 1
    ed._redo_last()
    assert px(ed, 100, 100) == 0


def test_a_brush_stroke_is_a_single_undo_step(ed):
    ed._tool.set("brush_off")
    ed._size_var.set(10)
    ed._press(ev(*at(ed, 50, 50)), False)
    for i in range(1, 30):
        ed._drag(ev(*at(ed, 50 + i * 5, 50 + i * 3)))
    ed._release(ev(*at(ed, 195, 137)))
    assert px(ed, 50, 50) == 0 and px(ed, 120, 92) == 0
    ed._undo_last()
    assert px(ed, 50, 50) == 255 and px(ed, 120, 92) == 255    # tout le trait d'un coup


def test_right_click_paints_the_opposite_and_is_undoable(ed):
    rect(ed, 80, 80, 140, 140)
    ed._tool.set("brush_off")
    ed._press(ev(*at(ed, 100, 100)), True)                     # clic droit : "garder"
    ed._release(ev(*at(ed, 100, 100)))
    assert px(ed, 100, 100) == 255
    ed._undo_last()
    assert px(ed, 100, 100) == 0


def test_history_is_reset_on_another_card(ed):
    rect(ed, 80, 80, 140, 140)
    ed._undo_last()
    ed._save()
    ed._step(+1)
    pump(ed.root, 0.2)
    assert ed._undo == [] and ed._redo == []


# ── tout masquer / tout garder ──────────────────────────────────────────────

def test_mask_all_then_keep_a_region(ed):
    ed._exclude_all()
    assert ed._mask.getextrema() == (0, 0) and ed._dirty
    rect(ed, 100, 100, 200, 180, tool="rect_on")               # on ne garde que ce rectangle
    assert px(ed, 150, 140) == 255 and px(ed, 10, 10) == 0
    ed._save()
    saved = ed._store.read("1")
    w = saved.width / ed._mask.width
    assert saved.getpixel((int(150 * w), int(140 * w))) == 255
    assert saved.getpixel((int(10 * w), int(10 * w))) == 0
    assert ed._store.source("1") == "manual" and ed.changed[-1] == "1"


def test_mask_all_is_undoable_and_keep_all_still_works(ed):
    start = ed._mask.copy()
    ed._exclude_all()
    ed._undo_last()
    assert ed._mask.tobytes() == start.tobytes()
    ed._redo_last()
    assert ed._mask.getextrema() == (0, 0)
    ed._keep_all()
    assert ed._mask.getextrema() == (255, 255)
    ed._undo_last()
    assert ed._mask.getextrema() == (0, 0)


def test_the_new_buttons_exist(ed):
    texts = set()

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, tk.Button):
                texts.add(child.cget("text"))
            walk(child)
    walk(ed)
    assert {"mask_undo", "mask_redo", "mask_clear", "mask_exclude_all"} <= texts
