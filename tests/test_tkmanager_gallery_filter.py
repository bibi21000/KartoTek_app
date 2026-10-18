# -*- encoding: utf-8 -*-
"""Filtre « Collection » de la vue galerie de ktmanager (App complète).
Ignoré sans affichage (lancer sous xvfb-run en intégration continue)."""
import json
import time

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def env(tmp_path, monkeypatch):
    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("pas d'affichage (xvfb-run ?)")

    from libpostcards.model import Model
    from tkpostcards import tkmanager as tm

    (tmp_path / "cards").mkdir()
    cards = {1: ["A"], 2: ["A"], 3: ["A", "B"], 4: ["A"], 5: ["A"],
             6: ["B"], 7: ["B"], 8: ["Z"], 9: []}      # Z : collection absente de la conf
    for i, colls in cards.items():
        (tmp_path / "cards" / f"{i}.json").write_text(
            json.dumps({"id": i, "title": f"Carte {i}", "collections": colls}))
    model = Model(tmp_path)
    model.generate()
    model.close()
    (tmp_path / "postcards.conf").write_text("[DEFAULT]\n[tkmanager]\n")
    monkeypatch.setattr(tm.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(tm.messagebox, "showwarning", lambda *a, **k: None)

    app = tm.App(tmp_path, conf_file=tmp_path / "postcards.conf")
    app.collections = ["A", "B"]               # comme [tkmanager] collections = A, B
    app.update()
    app._open_gallery()
    g = app._gallery
    pump(app, 0.4)
    yield app, g, tm
    app.destroy()


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.update_idletasks()
        app.update()
        time.sleep(0.02)


def select(app, g, label):
    g._coll_var.set(label)
    g._on_coll_changed()
    pump(app)


def test_choices_have_counts_configured_first_then_extra_then_none(env):
    app, g, tm = env
    g._refresh_coll_choices()
    assert list(g._coll_menu.cget("values")) == [
        tm._("tsearch_all"), "A (5)", "B (3)", "Z (1)",
        f"{tm._('gall_no_collection')} (1)"]


def test_filter_shows_only_matching_cards(env):
    app, g, tm = env
    assert g._visible_ids() == list(range(1, 10))
    select(app, g, "B (3)")
    assert g._visible_ids() == [3, 6, 7]
    assert [z[4] for z in g._hit_zones] == [3, 6, 7]        # et rien d'autre n'est dessiné
    select(app, g, f"{tm._('gall_no_collection')} (1)")
    assert g._visible_ids() == [9]
    select(app, g, tm._("tsearch_all"))
    assert len(g._visible_ids()) == 9


def test_main_window_list_is_untouched_and_open_card_works(env):
    app, g, tm = env
    before = app._ids[:]
    select(app, g, "B (3)")
    assert app._ids == before
    g._open_card(7)
    assert app._ids[app._current_idx] == 7


def test_gallery_follows_main_window_filter(env):
    app, g, tm = env
    select(app, g, "A (5)")
    assert g._coll_filter == "A"
    # La carte 3 est à la fois dans A et B : limiter la fenêtre principale à B
    # laisse donc « A » valable (1 carte).
    app._nav_collection = "B"
    app._scan_ids()
    pump(app)
    assert g._coll_filter == "A" and g._visible_ids() == [3]
    # En se limitant à Z, plus aucune carte de A : le filtre retombe sur « Toutes ».
    app._nav_collection = "Z"
    app._scan_ids()
    pump(app)
    assert g._coll_filter is None and g._coll_var.get() == tm._("tsearch_all")
    assert g._visible_ids() == [8]
    g._refresh_coll_choices()
    assert list(g._coll_menu.cget("values")) == [tm._("tsearch_all"), "Z (1)"]


def test_editing_a_card_collections_updates_the_filter(env):
    app, g, tm = env
    select(app, g, "B (3)")
    data = app.model.load_json(6)
    data["collections"] = ["A"]
    app.model.write_json(data)
    g.notify_card_changed(6)
    pump(app)
    assert g._visible_ids() == [3, 7]


def test_filter_change_during_loading_is_not_lost(env):
    app, g, tm = env
    g._loading = True
    g._start_loading()
    assert g._reload_requested
    g._loading = False
    g._queue.put(None)                       # fin du chargement en cours
    pump(app)
    assert not g._reload_requested
