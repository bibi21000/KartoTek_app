# -*- encoding: utf-8 -*-
"""Miniature du masque du recto dans la fenêtre principale de ktmanager
(sous le recto et le verso, seulement si un masque existe).
Ignoré sans affichage (lancer sous xvfb-run)."""
import json
import time

import pytest
from PIL import Image, ImageDraw

tk = pytest.importorskip("tkinter")


def pump(app, seconds=0.25):
    end = time.time() + seconds
    while time.time() < end:
        app.update_idletasks()
        app.update()
        time.sleep(0.02)


@pytest.fixture
def app(tmp_path, monkeypatch):
    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("pas d'affichage (xvfb-run ?)")
    from libpostcards.model import Model
    from tkpostcards import tkmanager as tm

    (tmp_path / "cards").mkdir()
    for i in (1, 2, 3):
        (tmp_path / "cards" / f"{i}.json").write_text(
            json.dumps({"id": i, "title": f"Vue {i}"}))
    # La carte 1 a une image du recto : l'éditeur de masques en a besoin.
    Image.new("RGBA", (400, 300), (150, 120, 90, 255)).save(
        tmp_path / "cards" / "1_R.tiff")
    model = Model(tmp_path)
    model.generate()
    model.close()
    (tmp_path / "postcards.conf").write_text("[DEFAULT]\n")
    monkeypatch.setattr(tm.messagebox, "showwarning", lambda *a, **k: None)
    monkeypatch.setattr(tm.messagebox, "showinfo", lambda *a, **k: None)
    a = tm.App(tmp_path, conf_file=tmp_path / "postcards.conf")
    a.update()
    a.tm = tm
    a.datadir_ = tmp_path
    yield a
    a.destroy()


def write_mask(app, cid, source="auto"):
    from libpostcards.masks import MaskStore
    m = Image.new("L", (400, 300), 255)
    ImageDraw.Draw(m).rectangle([300, 10, 390, 90], fill=0)
    MaskStore(app.datadir_).write(cid, m, source)


def shown(app):
    return app._mask_frame.winfo_manager() == "pack"


def test_hidden_without_mask(app):
    app._load_card(0)
    pump(app)
    assert not shown(app)


def test_shown_under_the_verso_when_a_mask_exists(app):
    write_mask(app, 1, "auto")
    app._load_card(0)
    pump(app)
    assert shown(app)
    column = app._lbl_r.master.pack_slaves()
    assert column.index(app._lbl_v) < column.index(app._mask_frame) < column.index(
        app._thumb_spacer)                                  # recto, verso, MASQUE, espaceur
    assert str(app._lbl_mask.cget("image"))                 # une vraie miniature
    title = app._mask_title.cget("text")
    assert app.tm._("side_mask") in title and app.tm._("mask_src_auto") in title


def test_title_tells_manual_masks(app):
    write_mask(app, 1, "manual")
    app._load_card(0)
    pump(app)
    assert app.tm._("mask_src_manual") in app._mask_title.cget("text")


def test_follows_the_current_card(app):
    write_mask(app, 1)
    app._load_card(0)
    pump(app)
    assert shown(app)
    app._load_card(1)                      # carte 2 : pas de masque
    pump(app)
    assert not shown(app)
    app._load_card(0)
    pump(app)
    assert shown(app)


def test_click_opens_the_mask_in_a_viewer(app):
    write_mask(app, 1)
    app._load_card(0)
    pump(app)
    app._open_mask_viewer()
    pump(app)
    assert "1_M" in app._viewers
    app._open_mask_viewer()                # un seul visualiseur par masque
    assert len(app._viewers) == 1


def test_saving_in_the_mask_editor_refreshes_the_thumbnail(app):
    app._load_card(0)
    pump(app)
    assert not shown(app)
    app._open_mask_editor()
    pump(app, 0.6)
    editor = app._mask_win
    assert editor._current_id() == "1" and editor._mask is not None
    editor._keep_all()
    editor._save()                         # enregistre un masque "manuel" pour la carte 1
    pump(app)
    assert shown(app)
    assert app.tm._("mask_src_manual") in app._mask_title.cget("text")
    editor._on_close()


def test_change_of_another_card_does_not_touch_this_one(app):
    app._load_card(0)
    pump(app)
    write_mask(app, 2)
    app._on_mask_changed(2)                # le masque de la carte 2 a changé, pas celle affichée
    assert not shown(app)
    app._on_mask_changed(None)             # lot (collection entière) : on relit la carte courante
    assert not shown(app)
    write_mask(app, 1)
    app._on_mask_changed(None)
    assert shown(app)
