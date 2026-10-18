# -*- encoding: utf-8 -*-
"""Limites du service de traduction (Google gratuit : ~5 requêtes/seconde,
5000 caractères/requête, HTTP 429 au-delà).

Le service est simulé avec une horloge simulée : les attentes de la cadence et
des réessais sont instantanées, mais on vérifie qu'elles ont bien lieu.
"""
import json
import threading
import types

import pytest

pytest.importorskip("flask")
pytest.importorskip("imagehash")
pytest.importorskip("deep_translator")

from deep_translator.exceptions import TooManyRequests  # noqa: E402

import simpostcards.blueprints.api as api  # noqa: E402
from libpostcards import langs  # noqa: E402
from libpostcards.model import Model  # noqa: E402
from simpostcards import create_app  # noqa: E402


class Service:
    """Faux service de traduction qui applique la règle de Google : plus de
    5 requêtes dans la même seconde -> TooManyRequests."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []          # (instant, nombre de lignes, texte)
        self.fail_next = 0       # n prochains appels : toujours 429
        self.merge_lines = False  # simule un service qui fusionne les lignes

    def __call__(self, source, target, text):
        now = self.clock.t
        recent = [t for t, _n, _x in self.calls if now - t < 1.0]
        self.calls.append((now, text.count("\n") + 1, text))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TooManyRequests()
        if len(recent) >= 5:
            raise TooManyRequests()
        lines = [("FR(%s)" % l) for l in text.split("\n")]
        return " ".join(lines) if self.merge_lines else "\n".join(lines)


class Clock:
    t = 1000.0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


@pytest.fixture
def service(monkeypatch):
    clock = Clock()
    svc = Service(clock)
    monkeypatch.setattr(langs, "_sleep", clock.sleep)
    monkeypatch.setattr(langs, "_clock", clock.now)
    monkeypatch.setattr(langs, "_translate_one", svc)
    langs.reset_translation_state()
    svc.clock = clock
    return svc


# ── moteur de traduction ────────────────────────────────────────────────────

def test_many_texts_make_few_requests_and_never_trip_the_limit(service):
    captions = ["a man standing in front of building number %d" % i for i in range(120)]
    out = langs.translate_texts(captions, "fr")
    assert out[0] == "FR(a man standing in front of building number 0)"
    assert out == ["FR(%s)" % c for c in captions]           # ordre conservé
    # 120 textes (~5600 caractères) -> 2 requêtes de 4000 caractères au plus,
    # et non 120 requêtes à 5 par seconde.
    assert len(service.calls) == 2
    assert all(len(text) <= langs.PACK_CHARS for _t, _n, text in service.calls)


def test_requests_are_spaced_even_when_packing_fails(service):
    """Service qui fusionne les lignes : on retombe sur une requête par texte,
    cadencée sous la limite."""
    service.merge_lines = True
    texts = ["text %d" % i for i in range(20)]
    out = langs.translate_texts(texts, "fr")
    assert out == ["FR(%s)" % t for t in texts]
    assert len(service.calls) == 21         # 1 groupée (refusée) + 20 une à une
    gaps = [b[0] - a[0] for a, b in zip(service.calls, service.calls[1:])]
    assert min(gaps) >= langs.MIN_INTERVAL - 1e-9


def test_rate_limit_is_waited_out(service):
    service.fail_next = 2
    out = langs.translate_texts(["hello"], "fr")
    assert out == ["FR(hello)"] and len(service.calls) == 3
    waited = service.calls[-1][0] - service.calls[0][0]
    assert waited >= langs.BACKOFF[0] + langs.BACKOFF[1]       # 1 s puis 3 s


def test_rate_limit_that_never_clears_raises_rate_limited(service):
    service.fail_next = 99
    with pytest.raises(langs.RateLimited) as err:
        langs.translate_texts(["hello"], "fr")
    assert err.value.retry_after == langs.BLOCK_SECONDS[0]     # disjoncteur : 10 min
    assert len(service.calls) == len(langs.BACKOFF) + 1
    assert isinstance(err.value, langs.TranslationError)       # reste une TranslationError
    assert "translate_engine" in str(err.value)                # et dit quoi faire


def test_cache_and_mixed_batches(service):
    langs.translate_texts(["person", "bus"], "fr")
    n = len(service.calls)
    assert langs.translate_texts(["bus", "", "person"], "fr") == ["FR(bus)", "", "FR(person)"]
    assert len(service.calls) == n                               # tout en cache
    langs.translate_texts(["tie", "bus"], "fr")
    assert service.calls[-1][2] == "tie"                         # seul l'inconnu est envoyé


def test_text_with_newline_is_not_packed(service):
    out = langs.translate_texts(["one", "two\nlines", "three"], "fr")
    assert out == ["FR(one)", "FR(two)\nFR(lines)", "FR(three)"]
    assert any(c[2] == "two\nlines" for c in service.calls)      # parti seul


# ── serveur : 429 ───────────────────────────────────────────────────────────

@pytest.fixture
def server(service, tmp_path):
    from werkzeug.serving import make_server
    conf = tmp_path / "server.conf"
    conf.write_text("[simpostcards]\n")
    app = create_app(conf)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    info = types.SimpleNamespace(url="http://127.0.0.1:%d" % httpd.server_port,
                                 client=app.test_client(), service=service)
    yield info
    httpd.shutdown()


def test_server_answers_429_with_retry_after_when_the_service_stays_saturated(server, caplog):
    import logging
    server.service.fail_next = 99
    with caplog.at_level(logging.WARNING):
        r = server.client.post("/api/translate", json={"lang": "fr", "texts": ["hello"]})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == str(int(langs.BLOCK_SECONDS[0]))
    body = r.get_json()
    assert body["code"] == "rate_limited" and body["retry_after"] == langs.BLOCK_SECONDS[0]
    assert "translate_engine" in body["error"]
    assert any("service de traduction saturé" in m for m in caplog.messages)


def test_detection_translation_reports_the_rate_limit_without_losing_the_result(
        server, monkeypatch):
    monkeypatch.setattr(api, "CAPTION_AVAILABLE", True)
    monkeypatch.setattr(api, "OBJECTS_AVAILABLE", False)
    monkeypatch.setattr(api, "compute_caption", lambda image, **kw: "a street")
    server.service.fail_next = 99
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, format="PNG")
    r = server.client.post("/api/compute_detection", data=buf.getvalue(),
                           content_type="image/png",
                           query_string={"lang": "fr", "caption": "true", "objects": "false"})
    body = r.get_json()
    assert r.status_code == 200 and body["caption"] == "a street"
    assert body["translation"]["status"] == "failed"
    assert "refusing this server" in body["translation"]["message"]


# ── client ──────────────────────────────────────────────────────────────────

def test_client_waits_and_retries_on_429(server, monkeypatch):
    # Refus COURT (30 s) : le client patiente et réessaie. (Un refus long, comme
    # le disjoncteur par défaut de 10 min, est remonté tout de suite : plus bas.)
    monkeypatch.setattr(langs, "BLOCK_SECONDS", (30.0,))
    waits = []
    server.service.fail_next = 99          # le serveur patiente lui-même, puis 429
    translator = langs.HelperTranslator(server.url,
                                        on_wait=lambda d, n: waits.append((d, n)))
    real_post = translator._post
    answers = {"n": 0}

    def post(payload):
        answers["n"] += 1
        if answers["n"] == 3:               # le service se calme au 3e essai
            server.service.fail_next = 0
        return real_post(payload)

    translator._post = post
    out = translator.translate(["hello", "world"], "fr")
    assert out == ["FR(hello)", "FR(world)"]
    assert waits == [(30.0, 1), (30.0, 2)]


def test_client_does_not_wait_for_a_long_refusal(server):
    """retry_after de 10 min (disjoncteur) : inutile de patienter 3 x 2 min."""
    waits = []
    server.service.fail_next = 10_000
    translator = langs.HelperTranslator(server.url, on_wait=lambda d, n: waits.append(d))
    with pytest.raises(langs.RateLimited) as err:
        translator.translate(["hello"], "fr")
    assert waits == [] and err.value.retry_after == langs.BLOCK_SECONDS[0]


def test_client_gives_up_after_max_waits(server, monkeypatch):
    monkeypatch.setattr(langs, "BLOCK_SECONDS", (30.0,))
    server.service.fail_next = 10_000
    translator = langs.HelperTranslator(server.url, max_waits=2)
    with pytest.raises(langs.RateLimited):
        translator.translate(["hello"], "fr")


# ── commande, le scénario du signalement ────────────────────────────────────

def _runner():
    from click.testing import CliRunner
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


@pytest.fixture
def cards(tmp_path):
    (tmp_path / "cards").mkdir()
    labels = ["person", "tie", "bus", "car"]
    for i in range(1, 61):
        extra = ({"detected_content": "a man standing in front of building %d" % i,
                  "detected_content_lang": "en",
                  "detected_objects": ", ".join(labels[: 1 + i % 4]),
                  "detected_objects_lang": "en"} if i <= 50 else
                 {"detected_content": "une rue", "detected_content_lang": "fr"} if i <= 55 else
                 {"detected_content": "Zebra crossing near the old church"} if i == 56 else
                 {"detected_content": "une vieille rue", "detected_objects": "voiture"})
        (tmp_path / "cards" / f"{i}.json").write_text(json.dumps({"id": i, **extra}))
    model = Model(tmp_path)
    model.generate()
    model.close()
    (tmp_path / "postcards.conf").write_text("[DEFAULT]\ndatadir = %s\n" % tmp_path)
    return tmp_path


def run(server, cards, *args):
    from tkpostcards.scripts.tktools import cli
    return _runner().invoke(cli, ["--conffile", str(cards / "postcards.conf"),
                                  "--external-helper", server.url, *args])


def test_cli_translates_hundreds_of_texts_without_hitting_the_limit(server, cards):
    res = run(server, cards, "detect-translate", "--lang", "fr")
    assert res.exit_code == 0, res.output + res.stderr
    assert "101 text(s) in 51 card(s) translated to fr" in res.output   # 50x2 + la carte 56
    assert len(server.service.calls) <= 6          # et non ~150 requêtes
    assert Model(cards).load_json(3)["detected_objects_lang"] == "fr"


def test_cli_explains_what_it_left_alone(server, cards):
    res = run(server, cards, "detect-translate", "--lang", "fr")
    out = res.output
    assert "Left as is:" in out
    assert "unknown: 8" in out and "assumed already translated" in out   # 4 cartes x 2 champs
    assert "done: 5" in out and "already in fr" in out
    assert "empty: 6" in out and "nothing to translate" in out


def test_cli_dry_run_shows_examples_of_unknown_texts(server, cards):
    res = run(server, cards, "detect-translate", "--lang", "fr", "--dry-run", "56-60")
    assert res.exit_code == 0
    assert "Examples of texts judged" in res.output
    assert "une vieille rue" in res.output          # de quoi juger l'heuristique
    assert server.service.calls == []               # rien n'a été traduit


def test_cli_reports_progress_when_interrupted_and_resumes(server, cards, monkeypatch):
    """Le bilan "0 texte(s) de 0 carte(s)" du signalement : désormais on dit
    combien étaient prévus, et relancer reprend."""
    server.service.fail_next = 10_000
    res = run(server, cards, "detect-translate", "--lang", "fr")
    assert res.exit_code != 0
    assert "0 of 101 text(s) translated before the interruption" in res.output
    assert "translate_engine" in res.stderr            # dit quoi faire
    assert "run the command again" in res.stderr
    assert Model(cards).load_json(3)["detected_content"].startswith("a man")   # rien de perdu


def test_cli_reports_the_servers_translation_engine(server, cards):
    res = run(server, cards, "detect-translate", "--lang", "fr", "--dry-run")
    assert res.exit_code == 0
    assert "Translation engine on the server: google" in res.output
    assert "older simpostcards" not in res.stderr


def test_cli_flags_a_server_older_than_the_client(server, cards, monkeypatch):
    """Un serveur d'avant ``translate_engine`` ne répond pas "engine" : sans cet
    avertissement, ses refus de Google ressemblent à ceux d'un serveur à jour et
    on corrige le mauvais côté."""
    real = langs.HelperTranslator._post

    def old_server(self, payload, timeout=None):
        reply = real(self, payload, timeout)
        reply.pop("engine", None)
        return reply

    monkeypatch.setattr(langs.HelperTranslator, "_post", old_server)
    res = run(server, cards, "detect-translate", "--lang", "fr", "--dry-run")
    assert res.exit_code == 0
    assert "older simpostcards" in res.stderr and "Redeploy" in res.stderr
    assert "Translation engine on the server" not in res.output
