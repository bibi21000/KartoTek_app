"""
Test d'intégration du transport https : vrai serveur Flask (blueprint sync de
flpostcards) en TLS avec certificat auto-signé, vrai client RemoteSync.

    PYTHONPATH=src pytest tests/test_remotesync_https.py -v
"""
from __future__ import annotations

import http.client
import json
import os
import random
import socket
import ssl
import subprocess
import textwrap
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from tkpostcards.libs.remotesync import RemoteSync

TOKEN = "t" * 40


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    crt, key = d / "c.pem", d / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", str(key), "-out", str(crt), "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    return crt, key


def _start(tmp_path, cert, tls: bool, **cfg):
    """App Flask minimale + blueprint sync, servie en HTTPS (ou HTTP simple)."""
    from flask import Flask
    from flpostcards.blueprints import sync
    from flpostcards.extensions import limiter

    base = tmp_path / "srv"                 # = DATADIR de l'appli
    base.mkdir()
    (base / "refresh_tokens.sqlite").write_bytes(b"LIVE-AUTH-DATA")
    (base / "cache").mkdir()
    (base / "cache" / "icon.png").write_bytes(b"cache")
    app = Flask("t")
    app.config.update(
        DATADIR=base, CACHEDIR=base / "cache",
        SYNC_TOKEN=TOKEN, SYNC_URL_PREFIX="/api/v1/sync",
        SYNC_MAX_BODY_MB=0.6,               # petit : force le découpage en parties
        SYNC_ALLOW_RESTART=True,
    )
    app.config.update(cfg)
    sync.validate_config(app)
    limiter.init_app(app)
    app.register_blueprint(sync.bp, url_prefix="/api/v1/sync")
    seen = []                      # en-têtes X-API-Key vus (simule la passerelle)
    gateway = {"max_request": None}

    @app.before_request
    def gw():
        from flask import request, abort
        seen.append(request.headers.get("X-API-Key"))
        lim = gateway["max_request"]
        if lim and (request.content_length or 0) > lim:
            abort(413)             # comme nginx client_max_body_size

    port = _free_port()
    kw = {"ssl_context": (str(cert[0]), str(cert[1]))} if tls else {}
    srv = make_server("127.0.0.1", port, app, threaded=True, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, type("S", (), dict(port=port, base=base, app=app,
                                   seen=seen, gateway=gateway))


@pytest.fixture(autouse=True)
def _no_real_restart(monkeypatch):
    """Ne jamais envoyer de vrai signal (ni purger un vrai cache) pendant les tests."""
    from flpostcards import data_cache
    from flpostcards.blueprints import sync
    calls = {"kill": [], "invalidate": 0}
    monkeypatch.setattr(sync, "_kill", lambda pid, sig: calls["kill"].append((pid, sig)))
    monkeypatch.setattr(data_cache, "invalidate",
                        lambda: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    monkeypatch.setattr(sync, "_gunicorn_master_pid", lambda: 4242)
    sync._failures.clear()
    sync._restart_pending = False
    return calls


@pytest.fixture
def server(tmp_path, cert):
    srv, info = _start(tmp_path, cert, tls=True)
    yield info
    srv.shutdown()


@pytest.fixture
def plain_server(tmp_path, cert):
    srv, info = _start(tmp_path, cert, tls=False)
    yield info
    srv.shutdown()


def make_sync(tmp_path, server, cert, **extra) -> RemoteSync:
    opts = dict(
        protocol="https", host="localhost", port=server.port, token=TOKEN,
        remote_base_dir="/", ca_bundle=str(cert[0]), max_workers=4,
        part_mb="0.25", batch_mb="0.1", batch_files="50", timeout="10",
    )
    opts.update(extra)
    ini = tmp_path / "c.ini"
    ini.write_text("[remotesync]\n" + "\n".join(f"{k} = {v}" for k, v in opts.items()))
    return RemoteSync(ini)


def make_tree(root: Path, n=300) -> dict[str, bytes]:
    rnd = random.Random(1)
    files = {}
    for i in range(n):
        rel = f"d{i % 7}/sub{i % 3}/f{i}.jpg"
        files[rel] = rnd.randbytes(rnd.randint(0, 5000))
    files["big.sqlite"] = (b"SQLite format 3\x00" + rnd.randbytes(100) + b"A" * 700_000)
    files["incompressible.bin"] = rnd.randbytes(900_000)   # multi-parties, non compressible
    files["données/été.json"] = json.dumps({"é": list(range(5000))}).encode()
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def test_full_cycle(tmp_path, server, cert):
    local = tmp_path / "local"
    files = make_tree(local)
    sync = make_sync(tmp_path, server, cert)

    r = sync.sync_directory(local, "size_div3")
    assert r.success, r.errors[:3]
    assert len(r.uploaded) == len(files) and not r.skipped
    for rel, data in files.items():
        dest = server.base / "size_div3" / rel
        assert dest.read_bytes() == data, rel
        assert abs(dest.stat().st_mtime - (local / rel).stat().st_mtime) < 0.01
    assert not [p for p in (server.base / ".sync-staging").rglob("*.part")]

    # 2e passage : rien à envoyer
    r = sync.sync_directory(local, "size_div3")
    assert r.success and not r.uploaded and len(r.skipped) == len(files)

    # un fichier modifié (petit) + le gros : seuls eux repartent
    (local / "d1/sub1/f1.jpg").write_bytes(b"new content")
    os.utime(local / "d1/sub1/f1.jpg", (time.time() + 10,) * 2)
    big = local / "incompressible.bin"
    big.write_bytes(os.urandom(800_000))
    os.utime(big, (time.time() + 10,) * 2)
    r = sync.sync_directory(local, "size_div3")
    assert r.success and len(r.uploaded) == 2, r.uploaded
    assert (server.base / "size_div3/incompressible.bin").read_bytes() == big.read_bytes()

    # orphelins
    sync.config.delete_orphans = True
    (local / "d0/sub0/f0.jpg").unlink()
    r = sync.sync_directory(local, "size_div3")
    assert r.success and len(r.deleted) == 1
    assert not (server.base / "size_div3/d0/sub0/f0.jpg").exists()

    # garde-fou : répertoire local vide => on ne vide pas le serveur
    empty = tmp_path / "empty"
    empty.mkdir()
    r = sync.sync_directory(empty, "size_div3")
    assert not r.success and not r.deleted
    assert (server.base / "size_div3/d1/sub1/f1.jpg").exists()


def test_dry_run_touches_nothing(tmp_path, server, cert):
    local = tmp_path / "local"
    make_tree(local, 20)
    r = make_sync(tmp_path, server, cert, dry_run="true").sync_directory(local, "x")
    assert r.success and len(r.uploaded) == 23
    assert not (server.base / "x").exists()


def test_sync_file_and_locked_fetch(tmp_path, server, cert):
    sync = make_sync(tmp_path, server, cert)
    f = tmp_path / "collections.json"
    f.write_text('{"a": 1}')
    assert sync.sync_file(f).success
    assert (server.base / "collections.json").read_text() == '{"a": 1}'
    assert sync.sync_file(f).skipped            # déjà à jour

    big = tmp_path / "postcards.sqlite"
    big.write_bytes(os.urandom(700_000))        # > part_mb : multi-parties via upload_file
    assert sync.sync_file(big).success
    assert (server.base / "postcards.sqlite").read_bytes() == big.read_bytes()

    # fetch_locked : verrou exclusif, téléchargement, suppression du fichier ET du verrou
    (server.base / "updates.json").write_text("[1,2]")
    dest = tmp_path / "got.json"
    with sync.fetch_locked("updates.json", dest) as ctx:
        assert (server.base / "updates.json.lck").exists()
    assert ctx.result.success and dest.read_text() == "[1,2]"
    assert not (server.base / "updates.json").exists()
    assert not (server.base / "updates.json.lck").exists()

    # verrou déjà pris => LockTimeoutError, et le verrou d'autrui n'est pas supprimé
    (server.base / "updates.json").write_text("[]")
    (server.base / "updates.json.lck").write_text("")
    from tkpostcards.libs.remotesync import LockTimeoutError
    with pytest.raises(LockTimeoutError):
        with sync.fetch_locked("updates.json", dest, timeout=0.3, poll_interval=0.1):
            pass
    assert (server.base / "updates.json.lck").exists()


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_restart(tmp_path, server, cert, _no_real_restart, monkeypatch):
    import signal
    from flpostcards.blueprints import sync
    calls = _no_real_restart
    sync_ = make_sync(tmp_path, server, cert)
    assert sync_.trigger_restart() is False        # restart_remote absent : no-op
    assert calls["kill"] == []

    sync_ = make_sync(tmp_path, server, cert, restart_remote="true")
    assert sync_.trigger_restart() is True         # sous gunicorn : SIGHUP au maître
    assert _wait_for(lambda: calls["kill"])
    assert calls["kill"] == [(4242, signal.SIGHUP)] and calls["invalidate"] == 1

    # sans gunicorn : SIGTERM à soi-même (le superviseur relance)
    assert _wait_for(lambda: not sync._restart_pending)
    calls["kill"].clear()
    monkeypatch.setattr(sync, "_gunicorn_master_pid", lambda: None)
    assert sync_.trigger_restart() is True
    assert _wait_for(lambda: calls["kill"])
    assert calls["kill"] == [(os.getpid(), signal.SIGTERM)]

    # refusé par l'admin du serveur : avertissement, pas d'exception, rien de tué
    assert _wait_for(lambda: not sync._restart_pending)
    calls["kill"].clear()
    server.app.config["SYNC_ALLOW_RESTART"] = False
    assert sync_.trigger_restart() is False
    time.sleep(0.7)
    assert calls["kill"] == []


def test_auth_and_tls(tmp_path, server, cert):
    bad = make_sync(tmp_path, server, cert, token="x" * 40)
    r = bad.sync_directory(tmp_path, "x")
    assert not r.success                              # 401
    # certificat non reconnu (pas de ca_bundle) : refusé, sans réessais
    no_ca = make_sync(tmp_path, server, cert, ca_bundle="")
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert not no_ca.sync_file(f).success
    assert not (server.base / "a.txt").exists()
    # host en http:// : refusé d'emblée
    r = make_sync(tmp_path, server, cert, host="http://localhost").sync_file(f)
    assert not r.success and "https://" in r.errors[0]
    # verify_tls=false : accepté
    assert make_sync(tmp_path, server, cert, ca_bundle="", verify_tls="false").sync_file(f).success


def _raw(server, cert, method, path, body=None, token=TOKEN):
    ctx = ssl.create_default_context(cafile=str(cert[0]))
    c = http.client.HTTPSConnection("localhost", server.port, context=ctx)
    c.request(method, "/api/v1/sync" + path, body=body,
              headers={"Authorization": f"Bearer {token}"})
    r = c.getresponse()
    return r.status, r.read()


@pytest.mark.parametrize("path", [
    "/stat?path=../etc/passwd", "/file?path=../../etc/passwd",
    "/file?path=a/../../x", "/file?path=.sync-staging/tmp/x",
    "/list?path=.sync-staging", "/stat?path=%2e%2e%2fx", "/file?path=a%00b",
])
def test_path_traversal_rejected(tmp_path, server, cert, path):
    (tmp_path / "secret").write_text("s")
    status, _ = _raw(server, cert, "GET", path)
    assert status in (400, 403, 404)


def test_symlink_escape_rejected(tmp_path, server, cert):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/secret.txt").write_text("secret")
    os.symlink(tmp_path / "outside", server.base / "link")
    assert _raw(server, cert, "GET", "/file?path=link/secret.txt")[0] == 403
    status, body = _raw(server, cert, "GET", "/list?path=")
    assert "secret.txt" not in body.decode()           # le lien n'est pas suivi


def test_no_token_no_access(tmp_path, server, cert):
    assert _raw(server, cert, "GET", "/info", token="")[0] == 401


def test_corrupted_frame_rejected(tmp_path, server, cert):
    from tkpostcards.libs.remotesync import _make_frame, _FRAME_END
    frame = bytearray(_make_frame("a.bin", b"hello world" * 10, 1.0, 0, 110, False))
    frame[-1] ^= 0xFF                                  # corrompt le dernier octet de données
    st, body = _raw(server, cert, "POST", "/put?root=r&session=" + "a" * 32,
                    bytes(frame) + _FRAME_END)
    res = json.loads(body)
    assert st == 200 and res["ok"] == [] and "crc32" in res["errors"][0][1]
    assert not (server.base / "r/a.bin").exists()


def test_decompression_bomb_rejected(tmp_path, server, cert):
    import struct, zlib
    raw = b"\0" * 5_000_000
    stored = zlib.compress(raw)
    hdr = json.dumps({"p": "bomb", "m": 1.0, "o": 0, "t": 10, "u": 10,
                      "s": len(stored), "z": 1}).encode()
    body = struct.pack(">I", len(hdr)) + hdr + stored + struct.pack(">I", 0)
    st, out = _raw(server, cert, "POST", "/put?root=&session=" + "b" * 32, body)
    assert json.loads(out)["errors"] and not (server.base / "bomb").exists()


# --- accès LAN direct en HTTP simple ---------------------------------------

def test_plain_http_requires_opt_in(tmp_path, plain_server, cert):
    url = f"http://127.0.0.1:{plain_server.port}"
    f = tmp_path / "a.txt"
    f.write_text("x")
    r = make_sync(tmp_path, plain_server, cert, host=url).sync_file(f)
    assert not r.success and "allow_http" in r.errors[0]
    assert not (plain_server.base / "a.txt").exists()


def test_plain_http_full_cycle_and_restart(tmp_path, plain_server, cert, _no_real_restart):
    local = tmp_path / "local"
    files = make_tree(local, 60)
    sync = make_sync(tmp_path, plain_server, cert, allow_http="true",
                     host=f"http://127.0.0.1:{plain_server.port}", restart_remote="true")
    r = sync.sync_directory(local, "d")
    assert r.success, r.errors[:2]
    for rel, data in files.items():
        assert (plain_server.base / "d" / rel).read_bytes() == data
    assert sync.sync_directory(local, "d").uploaded == []
    assert sync.trigger_restart() is True
    assert _wait_for(lambda: _no_real_restart["kill"])


def test_plain_http_default_port_is_80(tmp_path):
    from tkpostcards.libs.remotesync import SyncConfig, _HTTPSBackend
    ini = tmp_path / "c.ini"
    ini.write_text("[remotesync]\nprotocol=https\nhost=http://srv.lan\nallow_http=true\n")
    cfg = SyncConfig.from_ini(ini)
    assert _HTTPSBackend._parse_endpoint(cfg)[:2] == ("srv.lan", 80)
    ini.write_text("[remotesync]\nprotocol=https\nhost=https://srv.lan/tools\n")
    assert _HTTPSBackend._parse_endpoint(SyncConfig.from_ini(ini)) == (
        "srv.lan", 443, "/tools/api/v1/sync", "https")


# --- passerelle : clé API, limite de taille de requête -----------------------

def test_api_key_header_sent_on_every_request(tmp_path, server, cert):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert make_sync(tmp_path, server, cert, api_key="K-hosting").sync_file(f).success
    assert server.seen and set(server.seen) == {"K-hosting"}
    server.seen.clear()
    make_sync(tmp_path, server, cert, api_key="K2", api_key_header="X-API-Key").sync_file(f)
    assert set(server.seen) == {"K2"}


def test_gateway_request_size_limit(tmp_path, server, cert):
    server.gateway["max_request"] = 200 * 1024     # « nginx » : 200 Ko par requête
    local = tmp_path / "local"
    files = make_tree(local, 80)
    # sans réglage : refus 413, message qui dit quoi faire
    r = make_sync(tmp_path, server, cert).sync_directory(local, "g")
    assert not r.success and "max_request_mb" in " ".join(r.errors)
    # avec max_request_mb adapté : tout passe, gros fichiers inclus
    r = make_sync(tmp_path, server, cert, max_request_mb="0.2").sync_directory(local, "g2")
    assert r.success, r.errors[:2]
    for rel, data in files.items():
        assert (server.base / "g2" / rel).read_bytes() == data


# --- données vivantes protégées ----------------------------------------------

def test_live_data_is_protected(tmp_path, server, cert):
    live = server.base / "refresh_tokens.sqlite"
    for method, path in [
        ("GET", "/file?path=refresh_tokens.sqlite"),
        ("GET", "/file?path=refresh_tokens.sqlite-wal"),
        ("DELETE", "/file?path=refresh_tokens.sqlite"),
        ("GET", "/stat?path=cache/icon.png"),
        ("PUT", "/lock?path=push_watch_state.json"),
    ]:
        assert _raw(server, cert, method, path)[0] == 403, (method, path)
    # ni listées (donc jamais prises pour des orphelins)
    body = _raw(server, cert, "GET", "/list?path=")[1].decode()
    assert "refresh_tokens" not in body and "icon.png" not in body
    # écriture par lot refusée
    from flpostcards.blueprints.sync import _HDR  # noqa: F401
    from tkpostcards.libs.remotesync import _make_frame, _FRAME_END
    st, out = _raw(server, cert, "POST", "/put?root=&session=" + "c" * 32,
                   _make_frame("refresh_tokens.sqlite", b"EVIL", 1.0, 0, 4, False) + _FRAME_END)
    assert json.loads(out)["errors"]
    # publication de la racine avec suppression des orphelins : données vivantes intactes
    local = tmp_path / "local"
    make_tree(local, 10)
    sync = make_sync(tmp_path, server, cert, delete_orphans="true")
    assert sync.sync_directory(local, "/").success
    (server.base / "stale.json").write_text("{}")
    r = sync.sync_directory(local, "/")
    assert r.success and any(d.endswith("stale.json") for d in r.deleted)
    assert live.read_bytes() == b"LIVE-AUTH-DATA"
    assert (server.base / "cache/icon.png").exists()


def test_custom_protected_and_cachedir_inside_datadir(tmp_path, cert):
    srv, info = _start(tmp_path, cert, tls=False, SYNC_PROTECTED="secret*.json",
                       CACHEDIR=tmp_path / "srv" / "moncache")
    try:
        from flpostcards.blueprints import sync
        pats = info.app.config["SYNC_PROTECTED_PATTERNS"]
        assert "secret*.json" in pats and "moncache" in pats
    finally:
        srv.shutdown()


def test_max_body_enforced_by_server(tmp_path, server, cert):
    big = b"x" * (700 * 1024)                      # > sync_max_body_mb (0.6)
    st, out = _raw(server, cert, "POST", "/put?root=&session=" + "d" * 32, big)
    assert st == 413
    # /put sans Content-Length (chunked) : refusé
    ctx = ssl.create_default_context(cafile=str(cert[0]))
    c = http.client.HTTPSConnection("localhost", server.port, context=ctx)
    c.request("POST", "/api/v1/sync/put", body=iter([b"abc"]),
              headers={"Authorization": f"Bearer {TOKEN}"}, encode_chunked=True)
    assert c.getresponse().status == 411


# --- câblage dans la vraie application flpostcards ----------------------------

def test_flpostcards_create_app_wiring(tmp_path):
    from flpostcards import create_app
    data = tmp_path / "data"
    data.mkdir()
    conf = tmp_path / "p.conf"

    def build(flask_section: str):
        conf.write_text(f"[DEFAULT]\ndatadir = {data}\n[flask]\n"
                        f"secret_key = {'k' * 40}\n{flask_section}")
        return create_app(conf)

    hdr = {"Authorization": f"Bearer {TOKEN}"}
    app = build(f"sync_enabled = true\nsync_token = {TOKEN}\nsync_url_prefix = /s/\n"
                "sync_max_body_mb = 8\nsync_protected = mes_secrets*\n")
    c = app.test_client()
    r = c.get("/s/info", headers=hdr)
    assert r.status_code == 200
    assert r.get_json()["max_body"] == 8 * 1024 * 1024 and r.get_json()["restart"] is True
    assert c.get("/s/info").status_code == 401
    assert "mes_secrets*" in app.config["SYNC_PROTECTED_PATTERNS"]
    # le reste du site n'est pas affecté (ni limité en taille)
    assert app.config.get("MAX_CONTENT_LENGTH") is None
    # désactivé par défaut : la route n'existe pas
    assert build("").test_client().get("/api/v1/sync/info").status_code == 404
    # sync_allow_restart = false
    app = build(f"sync_enabled = true\nsync_token = {TOKEN}\nsync_allow_restart = false\n")
    assert app.test_client().get("/api/v1/sync/info", headers=hdr).get_json()["restart"] is False
    # activé sans jeton, ou avec un jeton trop court : refus au démarrage
    with pytest.raises(RuntimeError):
        build("sync_enabled = true\n")
    with pytest.raises(RuntimeError):
        build("sync_enabled = true\nsync_token = court\n")
