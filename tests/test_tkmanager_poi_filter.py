# -*- encoding: utf-8 -*-
"""Filtre « POI » de la barre de navigation de ktmanager (App complète).
Ignoré sans affichage (lancer sous xvfb-run en intégration continue)."""
import json
import types

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path, monkeypatch):
    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("pas d'affichage (xvfb-run ?)")

    from libpostcards.model import Model
    from tkpostcards import tkmanager as tm

    (tmp_path / "cards").mkdir()
    cards = {1: ("A", ["Eglise"]), 2: ("A", ["Eglise", "Mairie"]), 3: ("A", ["Mairie"]),
             4: ("A", []), 5: ("A", []), 6: ("B", ["Eglise"]), 7: ("B", ["Pont"]), 8: ("B", [])}
    for i, (coll, pois) in cards.items():
        (tmp_path / "cards" / f"{i}.json").write_text(json.dumps(
            {"id": i, "title": f"Carte {i}", "collections": [coll], "poi": pois}))
    model = Model(tmp_path)
    model.generate()
    model.close()
    (tmp_path / "postcards.conf").write_text("[DEFAULT]\n")

    infos = []
    monkeypatch.setattr(tm.messagebox, "showinfo", lambda *a, **k: infos.append(a[1]))
    monkeypatch.setattr(tm.messagebox, "showwarning", lambda *a, **k: None)
    a = tm.App(tmp_path, conf_file=tmp_path / "postcards.conf")
    a.update()
    a.tm, a.infos = tm, infos
    yield a
    a.destroy()


def choose(app, widget, **vars_):
    for name, value in vars_.items():
        getattr(app, name).set(value)
    app._on_nav_filter_changed(types.SimpleNamespace(widget=widget))
    app.update()


def test_choices_list_pois_with_counts_for_current_collection(app):
    app._refresh_poi_choices()
    all_label = app.tm._("tsearch_all")
    assert list(app._nav_poi_menu.cget("values")) == [
        all_label, "Eglise (3)", "Mairie (2)", "Pont (1)"]
    choose(app, app._nav_filter_menu, _nav_filter_var="A")
    app._refresh_poi_choices()
    assert list(app._nav_poi_menu.cget("values"))[1:] == ["Eglise (2)", "Mairie (2)"]


def test_filter_by_poi_and_combine_with_collection(app):
    choose(app, app._nav_poi_menu, _nav_poi_var="Eglise (3)")
    assert app._ids == [1, 2, 6]
    choose(app, app._nav_filter_menu, _nav_filter_var="A")
    assert app._ids == [1, 2]


def test_no_poi_and_a_poi_exclude_each_other(app):
    no_poi = app.tm._("nav_no_poi")
    choose(app, app._nav_poi_menu, _nav_poi_var="Eglise (3)")
    choose(app, app._nav_missing_menu, _nav_missing_var=no_poi)     # dernier choix : « Sans POI »
    assert app._nav_poi is None and app._ids == [4, 5, 8]
    choose(app, app._nav_poi_menu, _nav_poi_var="Mairie (2)")       # dernier choix : un POI
    assert not app._nav_no_poi and app._ids == [2, 3]


def test_empty_result_resets_everything(app):
    choose(app, app._nav_poi_menu, _nav_poi_var="Pont (1)")
    choose(app, app._nav_filter_menu, _nav_filter_var="A")           # Pont n'est pas dans A
    assert app.infos == ["nav_filter_empty"] or app.infos
    assert app._nav_poi is None and app._nav_collection is None
    assert len(app._ids) == 8


def test_cancelled_save_reverts_the_poi_list(app):
    choose(app, app._nav_poi_menu, _nav_poi_var="Eglise (3)")
    before = app._ids[:]
    app._ask_save_if_dirty = lambda: False
    choose(app, app._nav_poi_menu, _nav_poi_var="Pont (1)")
    assert app._nav_poi_var.get() == "Eglise (3)" and app._ids == before
