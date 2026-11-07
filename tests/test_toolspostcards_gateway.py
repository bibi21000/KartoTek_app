"""Niveaux de clés API de toolspostcards (passerelle devant simpostcards/flpostcards)."""
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

TOOLS = Path(__file__).resolve().parents[1] / "src" / "toolspostcards"
sys.path.insert(0, str(TOOLS))

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, gateway, keys, levels, manage, sites  # noqa: E402


def h(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


KEYS = {
    "guest": "k-guest", "basic": "k-basic", "premium": "k-premium", "gold": "k-gold",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    clients = {
        h("k-guest"): {"name": "g", "level": "guest"},
        h("k-basic"): {"name": "b", "level": "basic"},
        h("k-premium"): {"name": "p", "level": "premium", "expires_at": future},
        h("k-gold"): {"name": "au", "level": "gold"},
        h("k-off"): {"name": "off", "level": "gold", "disabled": True},
        h("k-expired"): {"name": "exp", "level": "gold", "expires_at": past},
        h("k-badlevel"): {"name": "bl", "level": "platinum"},
        h("k-vault"): {"name": "vault", "scopes": ["read:secrets"]},
        h("k-extra"): {"name": "extra", "level": "guest", "scopes": ["travel"]},
        h("k-admin"): {"name": "adm", "scopes": ["admin"]},
    }
    cfile = tmp_path / "clients.json"
    cfile.write_text(json.dumps(clients))
    monkeypatch.setenv("KV_CLIENTS_FILE", str(cfile))
    monkeypatch.setenv("KV_LEVELS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setenv("KV_SIMPOSTCARDS_URL", "http://sim.test:8004")
    monkeypatch.setenv("KV_FLPOSTCARDS_URL", "http://fl.test:5000")
    monkeypatch.setenv("KV_BACKUP_URL", "")
    monkeypatch.setenv("KV_MAX_BODY_MB", "1")
    monkeypatch.setenv("KV_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    monkeypatch.setenv("KV_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("KV_STATS_DB_PATH", str(tmp_path / "gateway.db"))
    monkeypatch.setenv("KV_SITES_FILE", str(tmp_path / "sites.json"))
    config.get_settings.cache_clear()
    db.get_stats.cache_clear()
    config._json_cache.clear()

    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "down.test":
            raise httpx.ConnectError("boom")
        return httpx.Response(
            201, stream=httpx.ByteStream(b"upstream-body"),
            headers={"X-Up": "1", "Set-Cookie": "sid=secret"},
        )

    from app.main import app
    with TestClient(app) as tc:
        # Remplace le client HTTP créé au démarrage par un faux upstream.
        app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        tc.seen = seen
        tc.tmp = tmp_path
        tc.clients_file = cfile
        yield tc
    config.get_settings.cache_clear()
    db.get_stats.cache_clear()


def call(tc, key, method, path, **kw):
    headers = kw.pop("headers", {})
    if key:
        headers["X-API-Key"] = key
    return tc.request(method, path, headers=headers, **kw)


# (méthode, chemin, niveau minimal)
MATRIX = [
    ("PUT", "/api/v1/sync/put", "guest"),
    ("GET", "/api/v1/sync/list", "guest"),
    ("POST", "/sync/v1/commit", "guest"),
    ("POST", "/api/correct_scan", "basic"),
    ("POST", "/api/compute_hashes", "basic"),
    ("POST", "/api/compute_hashes_only", "basic"),
    ("GET", "/api/ocr_languages", "premium"),
    ("POST", "/api/compute_ocr", "premium"),
    ("POST", "/api/compute_detection", "premium"),
    ("POST", "/api/compute_segments", "premium"),
    ("POST", "/api/translate", "premium"),
    ("POST", "/api/compute_travel", "gold"),
    ("POST", "/api/v1/sync/restart", "gold"),
]
ORDER = ["guest", "basic", "premium", "gold"]


@pytest.mark.parametrize("method,path,minimum", MATRIX)
def test_level_matrix(env, method, path, minimum):
    for level in ORDER:
        r = call(env, KEYS[level], method, path)
        if ORDER.index(level) >= ORDER.index(minimum):
            assert r.status_code == 201, (level, r.text)
            assert r.content == b"upstream-body"
        else:
            assert r.status_code == 403, (level, r.status_code)
            body = r.json()
            assert body["required_level"] == minimum
            assert body["your_level"] == level
            assert body["error"] == body["detail"]


def test_routing_targets(env):
    call(env, "k-gold", "POST", "/api/compute_travel")
    call(env, "k-gold", "PUT", "/api/v1/sync/put")
    call(env, "k-gold", "POST", "/sync/v1/commit")
    hosts = [r.url.host for r in env.seen]
    assert hosts == ["sim.test", "fl.test", "sim.test"]


@pytest.mark.parametrize("key,status,fragment", [
    (None, 401, "manquante"),
    ("nope", 401, "invalide"),
    ("k-off", 401, "désactivée"),
    ("k-expired", 401, "expirée"),
    ("k-badlevel", 403, "inconnu"),
])
def test_rejected_keys(env, key, status, fragment):
    r = call(env, key, "POST", "/api/compute_travel")
    assert r.status_code == status
    assert fragment in r.json()["error"]
    assert not env.seen  # rien n'est relayé


def test_unknown_route_and_method(env):
    assert call(env, "k-gold", "POST", "/api/nouvelle_route").status_code == 404
    assert call(env, "k-gold", "GET", "/api/compute_travel").status_code == 405
    # Sans clé : toujours 401, jamais d'indice sur les routes existantes.
    assert call(env, None, "POST", "/api/nouvelle_route").status_code == 401


def test_admin_never_granted_by_a_level(env):
    assert call(env, "k-gold", "POST", "/api/unload_models").status_code == 403
    assert call(env, "k-admin", "POST", "/api/unload_models").status_code == 201


def test_per_key_extra_scope_and_scope_only_key(env):
    assert call(env, "k-extra", "POST", "/api/compute_travel").status_code == 201
    assert call(env, "k-extra", "POST", "/api/compute_ocr").status_code == 403
    # Clé « technique » du coffre : aucune route de la passerelle.
    assert call(env, "k-vault", "POST", "/api/correct_scan").status_code == 403
    assert call(env, "k-vault", "POST", "/api/correct_scan").json()["your_level"] is None


@pytest.mark.parametrize("path", [
    "/api/v1/sync/%2e%2e/%2e%2e/api/compute_travel",
    "/api/v1/sync/x/%2E%2E/%2E%2E/api/compute_travel",
    "/api/v1/sync/%2e/x",
    "/api/v1/sync/..%5C..%5Capi",
    "/api/v1/sync/a%00b",
])
def test_path_traversal_cannot_escape_the_granted_capability(env, path):
    r = call(env, "k-guest", "GET", path)
    assert r.status_code == 400
    assert not env.seen


def test_request_is_forwarded_faithfully(env):
    body = b"\x00\x01binary" * 1000
    r = call(env, "k-guest", "PUT", "/api/v1/sync/put?root=a%20b&session=ff",
             content=body, headers={"Authorization": "Bearer sync-secret",
                                    "Content-Type": "application/octet-stream"})
    up = env.seen[0]
    assert up.method == "PUT"
    assert up.url.path == "/api/v1/sync/put"
    assert up.url.query == b"root=a%20b&session=ff"
    assert up.content == body
    assert up.headers["authorization"] == "Bearer sync-secret"
    assert up.headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in up.headers
    assert "x-api-key" not in up.headers  # la clé ne sort pas de la passerelle
    assert up.headers["x-forwarded-for"]
    # réponse : statut/en-têtes relayés, cookie de session retiré
    assert r.status_code == 201 and r.headers["x-up"] == "1"
    assert "set-cookie" not in r.headers


def test_oversized_body_rejected(env):
    r = call(env, "k-guest", "PUT", "/api/v1/sync/put", content=b"x" * (2 * 1024 * 1024))
    assert r.status_code == 413
    assert not env.seen


def test_upstream_errors(env, monkeypatch):
    monkeypatch.setenv("KV_FLPOSTCARDS_URL", "http://down.test")
    config.get_settings.cache_clear()
    r = call(env, "k-guest", "GET", "/api/v1/sync/list")
    assert r.status_code == 502


def test_backup_is_501_until_an_upstream_is_configured(env, monkeypatch):
    assert call(env, "k-gold", "GET", "/backup/list").status_code == 501
    assert call(env, "k-premium", "GET", "/backup/list").status_code == 403
    monkeypatch.setenv("KV_BACKUP_URL", "http://backup.test")
    config.get_settings.cache_clear()
    assert call(env, "k-gold", "GET", "/backup/list").status_code == 201
    assert env.seen[-1].url.host == "backup.test"


def test_me(env):
    r = call(env, "k-basic", "GET", "/v1/me")
    assert r.status_code == 200
    j = r.json()
    assert j["level"] == "basic"
    assert set(j["capabilities"]) == {"publish", "image"}


def test_keys_file_is_hot_reloaded(env):
    assert call(env, "k-new", "GET", "/v1/me").status_code == 401
    data = json.loads(env.clients_file.read_text())
    data[h("k-new")] = {"name": "n", "level": "guest"}
    env.clients_file.write_text(json.dumps(data))
    os.utime(env.clients_file, ns=(1, env.clients_file.stat().st_mtime_ns + 10**9))
    assert call(env, "k-new", "GET", "/v1/me").status_code == 200


def test_health_and_docs_access(env):
    assert env.get("/health").status_code == 200
    assert env.get("/docs").status_code == 401
    assert call(env, "k-guest", "GET", "/openapi.json").status_code == 200


# --- niveaux --------------------------------------------------------------

def test_levels_inheritance_and_min_level():
    lv = levels.Levels(levels.DEFAULT_LEVELS)
    assert lv.get("basic").capabilities == {"publish", "image"}
    assert lv.get("gold").capabilities >= lv.get("premium").capabilities
    assert lv.min_level_for("ocr") == "premium"
    assert lv.min_level_for("publish") == "guest"
    assert lv.min_level_for("admin") is None
    assert lv.names() == ORDER


def test_levels_validation():
    with pytest.raises(levels.LevelsError, match="circulaire"):
        levels.Levels({"a": {"rank": 1, "extends": "b"}, "b": {"rank": 2, "extends": "a"}})
    with pytest.raises(levels.LevelsError, match="inconnu"):
        levels.Levels({"a": {"rank": 1, "extends": "zzz"}})
    with pytest.raises(levels.LevelsError, match="rank"):
        levels.Levels({"a": {}})


def test_scope_matches():
    assert levels.scope_matches("read:*", "read:secrets")
    assert levels.scope_matches("publish:*", "publish")
    assert levels.scope_matches("publish:*", "publish:restart")
    assert not levels.scope_matches("publish", "publish:restart")
    assert not levels.scope_matches("read:*", "write:secrets")
    assert levels.scope_matches("*", "anything")


def test_custom_levels_file_adds_a_level(env, monkeypatch):
    f = env.tmp / "levels.json"
    f.write_text(json.dumps({
        "guest": {"rank": 1, "capabilities": ["publish"]},
        "trial": {"rank": 2, "extends": "guest", "capabilities": ["ocr"]},
    }))
    monkeypatch.setenv("KV_LEVELS_FILE", str(f))
    config.get_settings.cache_clear()
    data = json.loads(env.clients_file.read_text())
    data[h("k-trial")] = {"name": "t", "level": "trial"}
    env.clients_file.write_text(json.dumps(data))
    assert call(env, "k-trial", "POST", "/api/compute_ocr").status_code == 201
    assert call(env, "k-trial", "POST", "/api/correct_scan").status_code == 403
    # les niveaux par défaut sont remplacés : "gold" n'existe plus
    assert call(env, "k-gold", "POST", "/api/compute_travel").status_code == 403


# --- script de génération -------------------------------------------------

def test_generate_api_key_script(tmp_path):
    script = TOOLS / "scripts" / "generate_api_key.py"
    cf = tmp_path / "clients.json"
    out = subprocess.run(
        [sys.executable, str(script), "--name", "acme", "--level", "gold",
         "--expires", "2027-01-31", "--clients-file", str(cf)],
        capture_output=True, text=True, check=True).stdout
    raw = next(l.strip() for l in out.splitlines() if l.strip().startswith("kt_"))
    entry = json.loads(cf.read_text())[h(raw)]
    created = entry.pop("created_at")
    assert created.startswith("20")
    assert entry == {"name": "acme", "level": "gold", "expires_at": "2027-01-31"}
    bad = subprocess.run([sys.executable, str(script), "--name", "x", "--level", "diamond"],
                         capture_output=True, text=True)
    assert bad.returncode != 0 and "niveau inconnu" in bad.stderr


# --- compteurs ------------------------------------------------------------

def _usage(key_raw):
    return db.get_stats().totals_by_capability(h(key_raw)[:12], db.days_ago(1))


def test_calls_are_counted_per_key_and_capability(env):
    call(env, "k-basic", "POST", "/api/correct_scan", content=b"x" * 100)
    call(env, "k-basic", "POST", "/api/correct_scan", content=b"x" * 50)
    call(env, "k-basic", "PUT", "/api/v1/sync/put")
    u = _usage("k-basic")
    assert u["image"]["calls"] == 2 and u["image"]["bytes_in"] == 150
    assert u["publish"]["calls"] == 1 and u["publish"]["errors"] == 0
    assert _usage("k-guest") == {}


def test_denied_and_errors_are_counted(env, monkeypatch):
    call(env, "k-guest", "POST", "/api/compute_ocr")
    call(env, "k-guest", "POST", "/api/compute_ocr")
    u = _usage("k-guest")
    assert u["ocr"]["denied"] == 2 and u["ocr"]["calls"] == 0
    monkeypatch.setenv("KV_FLPOSTCARDS_URL", "http://down.test")
    config.get_settings.cache_clear()
    call(env, "k-guest", "GET", "/api/v1/sync/list")
    assert _usage("k-guest")["publish"]["errors"] == 1


def test_rejected_keys_and_unknown_routes_are_not_counted(env):
    call(env, "nope", "POST", "/api/compute_travel")
    call(env, "k-gold", "POST", "/api/nouvelle_route")
    assert db.get_stats().totals_by_key(db.days_ago(1)) == {}


def test_counter_failure_never_breaks_the_proxy(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db.StatsStore, "record_call", boom)
    assert call(env, "k-basic", "POST", "/api/correct_scan").status_code == 201


def test_me_reports_usage(env):
    call(env, "k-basic", "POST", "/api/correct_scan")
    j = call(env, "k-basic", "GET", "/v1/me").json()
    assert j["usage"]["calls"] == 1 and j["usage"]["by_capability"] == {"image": 1}


# --- manager --------------------------------------------------------------

def run_manage(env, *argv):
    return manage.main(["--clients-file", str(env.clients_file), *argv])


def test_manager_list_revoke_enable(env, capsys):
    assert run_manage(env, "keys", "list") == 0
    out = capsys.readouterr().out
    assert "au" in out and "off" not in out.split()  # révoquée/désactivée masquée
    assert run_manage(env, "keys", "list", "--all", "--json") == 0
    listing = json.loads(capsys.readouterr().out)
    statuses = {k["name"]: k["status"] for k in listing}
    assert statuses["g"] == "active" and statuses["off"] == "disabled" and statuses["exp"] == "expired"

    kid = h("k-gold")[:12]
    assert run_manage(env, "keys", "revoke", kid, "--reason", "impayé") == 0
    assert call(env, "k-gold", "GET", "/v1/me").status_code == 401  # effet immédiat
    meta = json.loads(env.clients_file.read_text())[h("k-gold")]
    assert meta["disabled"] and meta["revoked_at"] and meta["revoke_reason"] == "impayé"
    assert run_manage(env, "keys", "enable", "au") == 0  # par nom
    assert call(env, "k-gold", "GET", "/v1/me").status_code == 200


def test_manager_create_set_rotate_delete(env, capsys):
    assert run_manage(env, "keys", "create", "--name", "nouveau", "--level", "basic",
                      "--email", "n@x.fr") == 0
    raw = next(l.strip() for l in capsys.readouterr().out.splitlines() if l.strip().startswith("kt_"))
    assert call(env, raw, "POST", "/api/correct_scan").status_code == 201
    assert call(env, raw, "POST", "/api/compute_ocr").status_code == 403

    kid = h(raw)[:12]
    assert run_manage(env, "keys", "set", kid, "--level", "premium",
                      "--add-scope", "travel", "--expires", "2099-01-01") == 0
    assert call(env, raw, "POST", "/api/compute_ocr").status_code == 201
    assert call(env, raw, "POST", "/api/compute_travel").status_code == 201

    assert run_manage(env, "keys", "rotate", kid) == 0
    new_raw = next(l.strip() for l in capsys.readouterr().out.splitlines() if l.strip().startswith("kt_"))
    assert call(env, raw, "GET", "/v1/me").status_code == 401  # ancienne révoquée
    j = call(env, new_raw, "GET", "/v1/me").json()
    assert j["level"] == "premium" and "travel" in j["capabilities"]

    assert run_manage(env, "keys", "delete", kid) == 1  # sans --yes : refusé
    assert run_manage(env, "keys", "delete", kid, "--yes") == 0
    assert h(raw) not in json.loads(env.clients_file.read_text())


def test_manager_errors(env, capsys):
    assert run_manage(env, "keys", "revoke", "inexistant") == 1
    assert "introuvable" in capsys.readouterr().err
    assert run_manage(env, "keys", "create", "--name", "x", "--level", "diamond") == 1
    assert "niveau inconnu" in capsys.readouterr().err
    assert run_manage(env, "keys", "create", "--name", "x", "--level", "basic",
                      "--expires", "demain") == 1
    assert "date ISO" in capsys.readouterr().err
    assert h("x") not in json.loads(env.clients_file.read_text())
    # préfixe trop court : jamais pris pour un identifiant
    with pytest.raises(keys.KeysError):
        keys.resolve(json.loads(env.clients_file.read_text()), "a")


def test_manager_usage_and_show(env, capsys):
    call(env, "k-basic", "POST", "/api/correct_scan")
    assert run_manage(env, "usage", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "b" and rows[0]["calls"] == 1
    assert run_manage(env, "usage", "--key", "b") == 0
    assert db.today() in capsys.readouterr().out
    assert run_manage(env, "keys", "show", "b") == 0
    assert "image" in capsys.readouterr().out


# --- page de demande ------------------------------------------------------

GOOD = {"name": "Alice", "email": "alice@example.org", "org": "Asso", "level": "basic",
        "use_case": "Numériser la collection de l'association."}


def post_form(env, data, **kw):
    return env.post("/request-key", data=data, **kw)


def test_request_page_is_public_and_lists_levels(env):
    r = env.get("/request-key", headers={"Accept-Language": "fr-FR"})
    assert r.status_code == 200 and "Demander une clé API" in r.text
    assert "premium" in r.text and "OCR" in r.text
    assert "Request an API key" in env.get("/request-key?lang=en").text


def test_request_is_recorded_and_approved_through_the_manager(env, capsys):
    r = post_form(env, GOOD)
    assert r.status_code == 200 and "#1" in r.text
    [req] = db.get_stats().list_requests("pending")
    assert req["email"] == "alice@example.org" and req["level_wanted"] == "basic"
    assert req["ip_hash"] and "testclient" not in req["ip_hash"]  # IP hachée

    assert run_manage(env, "requests", "list") == 0
    assert "alice@example.org" in capsys.readouterr().out
    assert run_manage(env, "requests", "approve", "1", "--level", "guest") == 0
    out = capsys.readouterr().out
    raw = next(l.strip() for l in out.splitlines() if l.strip().startswith("kt_"))
    assert "alice@example.org" in out
    j = call(env, raw, "GET", "/v1/me").json()
    assert j["level"] == "guest" and j["name"] == "Alice (Asso)"
    assert db.get_stats().get_request(1)["status"] == "approved"
    assert run_manage(env, "requests", "approve", "1", "--level", "guest") == 1  # déjà traitée


def test_request_reject(env, capsys):
    post_form(env, GOOD)
    assert run_manage(env, "requests", "reject", "1", "--reason", "spam") == 0
    assert db.get_stats().get_request(1)["status"] == "rejected"


@pytest.mark.parametrize("patch,status", [
    ({"email": "pas-un-email"}, 422),
    ({"name": ""}, 422),
    ({"use_case": ""}, 422),
    ({"level": "diamond"}, 422),
])
def test_request_validation(env, patch, status):
    r = post_form(env, {**GOOD, **patch})
    assert r.status_code == status
    assert db.get_stats().list_requests() == []
    assert 'role="alert"' in r.text


def test_request_html_is_escaped(env):
    r = post_form(env, {**GOOD, "email": "x", "name": "<script>alert(1)</script>"})
    assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_request_honeypot_and_duplicates(env):
    r = post_form(env, {**GOOD, "website": "http://spam"})
    assert r.status_code == 200 and db.get_stats().list_requests() == []
    post_form(env, GOOD)
    post_form(env, {**GOOD, "name": "Autre"})  # même email en attente
    assert len(db.get_stats().list_requests()) == 1


def test_request_rate_limit_per_ip_and_pending_cap(env, monkeypatch):
    for i in range(3):
        assert post_form(env, {**GOOD, "email": f"u{i}@example.org"}).status_code == 200
    assert post_form(env, {**GOOD, "email": "u9@example.org"}).status_code == 429
    assert len(db.get_stats().list_requests()) == 3

    monkeypatch.setenv("KV_REQUEST_RATE_PER_HOUR", "100")
    monkeypatch.setenv("KV_REQUESTS_MAX_PENDING", "3")
    config.get_settings.cache_clear()
    assert post_form(env, {**GOOD, "email": "u10@example.org"}).status_code == 503


def test_request_body_limit_and_disabled(env, monkeypatch):
    assert post_form(env, {**GOOD, "use_case": "x" * 40000}).status_code == 413
    monkeypatch.setenv("KV_REQUESTS_ENABLED", "false")
    config.get_settings.cache_clear()
    assert env.get("/request-key").status_code == 404


# --- sites rattachés, espace disque, compteurs dans le manager ------------

def _write_json(path, data):
    path.write_text(json.dumps(data))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # force le rechargement


@pytest.fixture
def acme(env):
    datadir = env.tmp / "acme-data"
    (datadir / "size_div1").mkdir(parents=True)
    (datadir / "postcards.sqlite").write_bytes(b"s" * 1000)
    (datadir / "size_div1" / "a.jpg").write_bytes(b"j" * 500)
    os.link(datadir / "size_div1" / "a.jpg", datadir / "size_div1" / "a-link.jpg")  # compté une fois
    os.symlink(datadir / "postcards.sqlite", datadir / "ln")  # ignoré
    _write_json(env.tmp / "sites.json", {
        "acme": {"url": "http://fl-acme:5000", "datadir": str(datadir)},
        "remote": {"url": "http://fl-remote:5000", "sync_token": "tok"},
        "blind": {"url": "http://fl-blind:5000"},
    })
    data = json.loads(env.clients_file.read_text())
    data[h("k-acme")] = {"name": "acme", "level": "guest", "site": "acme"}
    data[h("k-ghost")] = {"name": "ghost", "level": "guest", "site": "ghost"}
    _write_json(env.clients_file, data)
    return datadir


def test_publication_goes_to_the_site_attached_to_the_key(acme, env):
    call(env, "k-acme", "PUT", "/api/v1/sync/put")
    call(env, "k-guest", "PUT", "/api/v1/sync/put")  # sans site : site par défaut
    assert [r.url.host for r in env.seen] == ["fl-acme", "fl.test"]


def test_unknown_site_never_falls_back_to_the_default_site(acme, env):
    r = call(env, "k-ghost", "PUT", "/api/v1/sync/put")
    assert r.status_code == 501 and not env.seen
    # le site n'affecte pas simpostcards
    assert call(env, "k-acme", "POST", "/sync/v1/commit").status_code == 201
    assert env.seen[-1].url.host == "sim.test"
    assert call(env, "k-acme", "GET", "/v1/me").json()["site"] == "acme"


def test_local_disk_usage(acme):
    usage = sites.disk_usage(sites.load_sites()["acme"])
    assert (usage.bytes, usage.files, usage.source) == (1500, 2, "datadir")
    assert usage.disk_bytes >= 1500  # espace alloué (blocs)
    assert sites.disk_usage(sites.load_sites()["blind"]) is None
    with pytest.raises(OSError):
        sites.disk_usage(sites.Site("x", datadir="/nonexistent"))


def test_remote_disk_usage_uses_the_usage_endpoint():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"bytes": 350, "disk_bytes": 8192, "files": 2,
                                         "measured_at": 1.7e9, "cached": True, "ttl_s": 14400})

    usage = sites.remote_usage(sites.Site("r", url="http://fl-remote:5000", sync_token="tok"),
                               transport=httpx.MockTransport(handler))
    assert (usage.bytes, usage.files, usage.disk_bytes, usage.source) == (350, 2, 8192, "remote")
    assert usage.cached is True and usage.measured_at == 1.7e9
    assert [r.url.path for r in seen] == ["/api/v1/sync/usage"]  # pas d'inventaire complet
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_remote_disk_usage_falls_back_to_list_on_older_flpostcards():
    def handler(request):
        if request.url.path.endswith("/usage"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"files": {"a": [100, 1.0], "d/b": [250, 2.0]}})

    usage = sites.remote_usage(sites.Site("r", url="http://fl-remote:5000", sync_token="tok"),
                               transport=httpx.MockTransport(handler))
    assert (usage.bytes, usage.files, usage.disk_bytes, usage.source) == (350, 2, None, "remote-list")

    def unauthorized(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(httpx.HTTPStatusError):  # une vraie erreur n'est pas masquée par le repli
        sites.remote_usage(sites.Site("r", url="http://x", sync_token="bad"),
                           transport=httpx.MockTransport(unauthorized))


def test_human_sizes():
    assert sites.human(0) == "0 o" and sites.human(1500) == "1.5 Ko"
    assert sites.human(5 * 1024**3) == "5.0 Go" and sites.human(None) == "-"


def test_manager_list_shows_counters_and_disk(acme, env, capsys):
    call(env, "k-acme", "PUT", "/api/v1/sync/put", content=b"x" * 200)
    call(env, "k-acme", "PUT", "/api/v1/sync/put", content=b"x" * 100)
    call(env, "k-acme", "POST", "/api/compute_ocr")  # refusé
    assert run_manage(env, "keys", "list", "--disk", "--json") == 0
    rows = {k["name"]: k for k in json.loads(capsys.readouterr().out)}
    a = rows["acme"]
    assert (a["calls_today"], a["calls_30d"], a["calls_total"], a["denied_30d"]) == (2, 2, 2, 1)
    assert a["bytes_30d"] >= 300 and a["disk"]["bytes"] == 1500
    assert rows["g"]["calls_total"] == 0 and rows["g"]["disk"] is None  # pas de site

    assert run_manage(env, "keys", "list", "--disk") == 0
    out = capsys.readouterr().out
    for col in ("AUJ.", "30J", "TOTAL", "REFUSÉS 30J", "DISQUE SITE"):
        assert col in out
    row = next(l for l in out.splitlines() if " acme " in l)
    assert "acme" in row and "Ko" in row


def test_manager_show_displays_disk_and_counters(acme, env, capsys):
    call(env, "k-acme", "PUT", "/api/v1/sync/put")
    assert run_manage(env, "keys", "show", "acme") == 0
    out = capsys.readouterr().out
    assert "1.5 Ko en 2 fichiers" in out and "alloués" in out
    assert "Compteurs (aujourd'hui)" in out and "Compteurs (total)" in out
    assert "publish" in out and "TOTAL" in out


def test_manager_show_handles_unmeasurable_or_missing_sites(acme, env, capsys):
    assert run_manage(env, "keys", "show", "ghost") == 0
    assert "absent de KV_SITES_FILE" in capsys.readouterr().out
    assert run_manage(env, "keys", "set", "g", "--site", "blind") == 0
    capsys.readouterr()
    assert run_manage(env, "keys", "show", "g") == 0
    assert "n/a" in capsys.readouterr().out


def test_manager_sites_command(acme, env, capsys, monkeypatch):
    call(env, "k-acme", "PUT", "/api/v1/sync/put")
    monkeypatch.setattr(sites, "remote_usage", lambda site, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert run_manage(env, "sites", "--json") == 0
    rows = {r["site"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["acme"]["bytes"] == 1500 and rows["acme"]["keys"] == 1 and rows["acme"]["calls_30d"] == 1
    assert rows["remote"]["bytes"] is None and "erreur" in rows["remote"]["error"]
    assert run_manage(env, "sites") == 0
    assert "TAILLE" in capsys.readouterr().out


def test_manager_site_assignment_is_validated(acme, env, capsys):
    assert run_manage(env, "keys", "create", "--name", "n", "--level", "guest", "--site", "nope") == 1
    assert "site inconnu" in capsys.readouterr().err
    assert run_manage(env, "keys", "create", "--name", "n", "--level", "guest", "--site", "acme") == 0
    assert run_manage(env, "keys", "set", "n", "--no-site") == 0
    meta = next(m for m in json.loads(env.clients_file.read_text()).values() if m["name"] == "n")
    assert "site" not in meta
