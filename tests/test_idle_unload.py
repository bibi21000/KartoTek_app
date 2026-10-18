# -*- encoding: utf-8 -*-
"""Déchargement des modèles : minuteur d'inactivité, busy(), unload_all,
re-armement après fork, POST /api/unload_models."""
import os
import threading
import time

import pytest

from simpostcards.libs import _idle_unload as iu


class Fake:
    """Modèle factice : ``loaded`` + un callback qui dit s'il a déchargé."""
    def __init__(self):
        self.loaded = True
        self.unloads = 0

    def unload(self):
        if not self.loaded:
            return False
        self.loaded = False
        self.unloads += 1
        return True


@pytest.fixture
def timer():
    fake = Fake()
    t = iu.IdleUnloadTimer("TEST-%d" % id(fake), fake.unload)
    t.fake = fake
    yield t
    with iu._TIMERS_LOCK:
        iu._TIMERS.remove(t)


def test_unloads_after_idle_and_only_once(timer):
    timer.touch()
    timer._idle_timeout = 0.05              # sans lancer le thread : on pilote _check()
    timer._check()
    assert timer.fake.loaded                # délai pas encore écoulé
    time.sleep(0.08)
    timer._check()
    assert not timer.fake.loaded and timer.fake.unloads == 1
    timer._check()                          # pas de redéclenchement en boucle
    assert timer.fake.unloads == 1


def test_never_unloads_while_busy(timer):
    timer._idle_timeout = 0.05
    with timer.busy():
        time.sleep(0.1)                     # calcul "long" : délai dépassé pendant la requête
        timer._check()
        assert timer.fake.loaded
        assert timer.unload_now() == "busy"
    # à la fin de la requête, le décompte repart de zéro
    timer._check()
    assert timer.fake.loaded
    time.sleep(0.08)
    timer._check()
    assert not timer.fake.loaded


def test_disabled_by_default(timer):
    timer.touch()
    time.sleep(0.02)
    timer._check()                          # idle_timeout None : jamais
    assert timer.fake.loaded


def test_unload_all_reports_states(timer):
    other = Fake(); other.loaded = False
    t2 = iu.IdleUnloadTimer("OTHER-%d" % id(other), other.unload)
    try:
        states = iu.unload_all()
        assert states[timer.name] == "unloaded"
        assert states[t2.name] == "not_loaded"
        with timer.busy():
            timer.fake.loaded = True
            assert iu.unload_all()[timer.name] == "busy" and timer.fake.loaded
    finally:
        with iu._TIMERS_LOCK:
            iu._TIMERS.remove(t2)


def test_callback_error_does_not_break_watchdog(timer):
    def boom():
        raise RuntimeError("x")
    timer._unload_callback = boom
    assert timer.unload_now() == "not_loaded"      # exception avalée et journalisée


def test_release_memory_runs_without_torch():
    iu.release_memory()                            # ne doit ni lever ni importer torch


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX only")
def test_watchdog_rearmed_in_forked_child(timer):
    """gunicorn --preload : le thread créé dans le maître n'existe pas dans
    le worker ; le minuteur doit le recréer lui-même après fork."""
    timer.configure(60)
    assert timer._thread is not None and timer._thread.is_alive()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                                    # fils
        try:
            ok = (timer._thread is not None and timer._thread.is_alive()
                  and timer._thread is not threading.main_thread())
            os.write(w, b"1" if ok else b"0")
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    assert os.read(r, 1) == b"1"


def test_unload_models_endpoint(tmp_path):
    flask = pytest.importorskip("flask")  # noqa: F841
    pytest.importorskip("imagehash")
    from simpostcards import create_app

    fake = Fake()
    t = iu.IdleUnloadTimer("ENDPOINT-TEST", fake.unload)
    try:
        conf = tmp_path / "postcards.conf"
        conf.write_text("[simpostcards]\n")
        client = create_app(conf).test_client()
        r = client.post("/api/unload_models")
        assert r.status_code == 200
        assert r.get_json()["models"]["ENDPOINT-TEST"] == "unloaded"
        assert not fake.loaded
        assert client.get("/api/unload_models").status_code == 405
    finally:
        with iu._TIMERS_LOCK:
            iu._TIMERS.remove(t)
