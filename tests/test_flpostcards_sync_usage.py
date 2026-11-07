"""GET <sync_url_prefix>/usage : taille des données, cache 4 h, invalidation par les écritures sync."""
import json
import os
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("flask_caching")
pytest.importorskip("flask_limiter")

from flpostcards import create_app  # noqa: E402
from flpostcards.blueprints import sync  # noqa: E402
from flpostcards.extensions import cache  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE = "/api/v1/sync"


@pytest.fixture
def fl(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    conf = tmp_path / "postcards.conf"
    conf.write_text(
        f"[DEFAULT]\ndatadir = {data}\n[flask]\nsecret_key = {'k' * 40}\n"
        f"sync_enabled = true\nsync_token = {TOKEN}\n"
    )
    app = create_app(str(conf))
    app.config["TESTING"] = True
    with app.app_context():
        cache.clear()
    c = app.test_client()
    c.data = data
    c.app = app
    # L'appli crée elle-même ses fichiers (postcards.sqlite...) dans le datadir à
    # la première requête : on la provoque ici, et les tests raisonnent en écarts.
    c.get(f"{BASE}/info", headers=AUTH)
    with app.app_context():
        cache.clear()
    return c


def usage(c, **q):
    r = c.get(f"{BASE}/usage", headers=AUTH, query_string=q)
    assert r.status_code == 200, r.data
    return r.get_json()


def put_frame(c, name, payload: bytes):
    hdr = json.dumps({"p": name, "m": 1.0, "o": 0, "t": len(payload), "u": len(payload),
                      "s": len(payload), "z": 0, "c": zlib.crc32(payload)}).encode()
    body = struct.pack(">I", len(hdr)) + hdr + payload + struct.pack(">I", 0)
    return c.post(f"{BASE}/put?root=&session=" + "a" * 16, data=body, headers=AUTH)


def test_requires_token(fl):
    assert fl.get(f"{BASE}/usage").status_code == 401
    assert fl.get(f"{BASE}/usage", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_measures_the_datadir(fl):
    base = usage(fl, refresh=1)
    (fl.data / "size_div1").mkdir()
    (fl.data / "fake.db").write_bytes(b"s" * 1000)
    (fl.data / "size_div1" / "a.jpg").write_bytes(b"j" * 500)
    os.link(fl.data / "size_div1" / "a.jpg", fl.data / "size_div1" / "b.jpg")  # compté une fois
    os.symlink(fl.data / "fake.db", fl.data / "ln")                            # ignoré
    (fl.data / "refresh_tokens.sqlite").write_bytes(b"t" * 100)               # protégé mais sur le disque
    staging = fl.data / ".sync-staging"
    staging.mkdir()
    (staging / "x.part").write_bytes(b"z" * 9999)                              # transitoire : exclu
    u = usage(fl, refresh=1)
    assert (u["bytes"] - base["bytes"], u["files"] - base["files"]) == (1600, 3)
    assert u["disk_bytes"] - base["disk_bytes"] >= 1600
    assert u["cached"] is False and u["ttl_s"] == 4 * 3600
    assert u["measured_at"] > 0


def test_cached_for_four_hours_then_served_from_cache(fl, monkeypatch):
    calls = []
    real = sync._measure_disk
    monkeypatch.setattr(sync, "_measure_disk", lambda: calls.append(1) or real())
    base = usage(fl)["bytes"]
    (fl.data / "a").write_bytes(b"x" * 10)
    first = usage(fl, refresh=1)
    (fl.data / "b").write_bytes(b"x" * 90)  # changement hors synchro : le cache ne le voit pas
    second = usage(fl)
    assert second["cached"] is True and second["bytes"] == first["bytes"] == base + 10
    assert second["measured_at"] == first["measured_at"] and len(calls) == 2
    assert usage(fl, refresh=1)["bytes"] == base + 100  # refresh force le parcours
    assert len(calls) == 3


def test_default_ttl_is_four_hours_and_configurable(fl):
    assert fl.app.config["DISK_USAGE_CACHE_TTL_S"] == 14400
    fl.app.config["DISK_USAGE_CACHE_TTL_S"] = 0
    base = usage(fl)
    assert base["cached"] is False and base["ttl_s"] == 0
    (fl.data / "b").write_bytes(b"x")
    u = usage(fl)
    assert u["cached"] is False and u["files"] == base["files"] + 1  # ttl 0 : jamais de cache


def test_ttl_is_applied_to_the_cache_entry(fl, monkeypatch):
    seen = {}
    real_set = cache.set

    def spy(key, value, timeout=None):
        seen[key] = timeout
        return real_set(key, value, timeout=timeout)

    monkeypatch.setattr(cache, "set", spy)
    usage(fl)
    assert seen[sync._USAGE_KEY] == 14400


def test_put_invalidates(fl):
    base = usage(fl)
    assert usage(fl)["cached"] is True
    r = put_frame(fl, "cards/new.jpg", b"x" * 300)
    assert r.get_json()["ok"] == ["cards/new.jpg"]
    u = usage(fl)
    assert u["cached"] is False
    assert (u["files"] - base["files"], u["bytes"] - base["bytes"]) == (1, 300)


def test_delete_and_mkdir_invalidate(fl):
    base = usage(fl, refresh=1)["bytes"]
    (fl.data / "a.txt").write_bytes(b"x" * 50)
    assert usage(fl, refresh=1)["bytes"] == base + 50
    assert fl.delete(f"{BASE}/file?path=a.txt", headers=AUTH).status_code == 200
    u = usage(fl)
    assert u["cached"] is False and u["bytes"] == base
    assert usage(fl)["cached"] is True
    assert fl.post(f"{BASE}/mkdir", json={"path": "d"}, headers=AUTH).status_code == 200
    assert usage(fl)["cached"] is False


def test_commit_invalidates(fl):
    usage(fl)
    r = fl.post(f"{BASE}/commit", json={"root": "", "session": "b" * 16, "files": [], "done": True},
                headers=AUTH)
    assert r.status_code == 200
    assert usage(fl)["cached"] is False


def test_failed_write_still_invalidates(fl):
    # un lot peut avoir écrit des fichiers avant d'échouer
    (fl.data / "f").write_bytes(b"x")
    usage(fl)
    r = fl.post(f"{BASE}/mkdir", json={"path": "f/sub"}, headers=AUTH)  # un fichier gêne : 409
    assert r.status_code >= 400
    assert usage(fl)["cached"] is False


def test_unauthenticated_write_cannot_flush_the_cache(fl):
    usage(fl)
    assert fl.post(f"{BASE}/mkdir", json={"path": "d"}).status_code == 401
    assert fl.post(f"{BASE}/mkdir", json={"path": "d"}, headers={"Authorization": "Bearer x"}).status_code == 401
    assert usage(fl)["cached"] is True


def test_reads_do_not_invalidate(fl):
    usage(fl)
    for path in ("info", "list", "ls", "stat"):
        assert fl.get(f"{BASE}/{path}", headers=AUTH).status_code == 200
    assert usage(fl)["cached"] is True


def test_measure_started_before_a_write_cannot_cache_a_stale_value(fl, monkeypatch):
    real = sync._measure_disk

    def measure_then_write_lands():
        value = real()
        with fl.app.app_context():
            sync._invalidate_usage()  # une écriture sync survient pendant le parcours
        return value

    monkeypatch.setattr(sync, "_measure_disk", measure_then_write_lands)
    assert usage(fl)["cached"] is False
    monkeypatch.setattr(sync, "_measure_disk", real)
    u = usage(fl)
    assert u["cached"] is False  # la valeur périmée n'a pas été mise en cache
    assert usage(fl)["cached"] is True


def test_cache_outage_never_breaks_sync_or_usage(fl, monkeypatch):
    base = usage(fl, refresh=1)["bytes"]

    class Down:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionError("redis down")
            return boom

    monkeypatch.setattr(sync, "cache", Down())  # seulement pour ce blueprint
    assert fl.post(f"{BASE}/mkdir", json={"path": "d"}, headers=AUTH).status_code == 200
    (fl.data / "a").write_bytes(b"x" * 7)
    u = usage(fl)
    assert u["bytes"] == base + 7 and u["cached"] is False
