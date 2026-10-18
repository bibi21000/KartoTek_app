# -*- encoding: utf-8 -*-
"""Disjoncteur de Google et moteur de traduction local (Marian).

Le blocage constaté en production : Google refuse l'adresse IP du serveur
(HTTP 429), même à une requête toutes les quelques secondes. Insister ne sert
à rien ; le serveur cesse de solliciter Google (disjoncteur) et, selon
``translate_engine``, traduit avec un modèle local.
"""
import sys
import threading
import types
from contextlib import contextmanager

import pytest

pytest.importorskip("flask")
pytest.importorskip("imagehash")
pytest.importorskip("deep_translator")

from deep_translator.exceptions import TooManyRequests  # noqa: E402

import simpostcards.libs.translate_backend as tb  # noqa: E402
from libpostcards import langs, translate_marian  # noqa: E402
from simpostcards import create_app  # noqa: E402
from simpostcards.libs import _idle_unload as iu  # noqa: E402


class Clock:
    t = 5000.0

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t


class Google:
    """Faux Google : refuse (429) tant que ``blocked`` est vrai."""

    def __init__(self):
        self.calls = 0
        self.blocked = False

    def __call__(self, source, target, text):
        self.calls += 1
        if self.blocked:
            raise TooManyRequests()
        return "\n".join("G(%s)" % l for l in text.split("\n"))


class FakeMarian:
    def __init__(self):
        self.translated, self.prepared = [], []

    def translate(self, texts, code):
        self.translated.append(list(texts))
        return ["M(%s)" % t for t in texts]

    def prepare(self, code):
        self.prepared.append(code)


@pytest.fixture
def env(monkeypatch):
    clock, google = Clock(), Google()
    monkeypatch.setattr(langs, "_sleep", clock.sleep)
    monkeypatch.setattr(langs, "_clock", clock.now)
    monkeypatch.setattr(langs, "_translate_one", google)
    langs.reset_translation_state()
    yield types.SimpleNamespace(clock=clock, google=google, marian=FakeMarian())
    langs.reset_translation_state()


# ── disjoncteur ─────────────────────────────────────────────────────────────

def test_breaker_stops_asking_google_and_reopens_later(env):
    env.google.blocked = True
    with pytest.raises(langs.RateLimited):
        langs.translate_texts(["hello"], "fr")
    asked = env.google.calls
    assert asked == len(langs.BACKOFF) + 1
    assert 0 < langs.google_blocked_for() <= langs.BLOCK_SECONDS[0]

    for _ in range(5):                         # tant qu'il est ouvert : pas un seul appel
        with pytest.raises(langs.RateLimited) as err:
            langs.translate_texts(["other"], "fr")
    assert env.google.calls == asked
    assert 0 < err.value.retry_after <= langs.BLOCK_SECONDS[0]

    env.clock.t += langs.BLOCK_SECONDS[0] + 1  # le délai passe, Google est réessayé
    env.google.blocked = False
    assert langs.translate_texts(["hello"], "fr") == ["G(hello)"]
    assert langs.google_blocked_for() == 0


def test_breaker_escalates_when_the_refusal_persists_and_resets_on_success(env):
    env.google.blocked = True
    waits = []
    for level in range(len(langs.BLOCK_SECONDS) + 1):
        with pytest.raises(langs.RateLimited) as err:
            langs.translate_texts(["x%d" % level], "fr")
        waits.append(err.value.retry_after)
        env.clock.t += err.value.retry_after + 1
    assert waits == [600.0, 1800.0, 7200.0, 7200.0]          # plafonné
    env.google.blocked = False
    langs.translate_texts(["fine"], "fr")
    env.google.blocked = True
    with pytest.raises(langs.RateLimited) as err:
        langs.translate_texts(["again"], "fr")
    assert err.value.retry_after == 600.0                    # niveau remis à zéro


# ── moteurs ─────────────────────────────────────────────────────────────────

def test_google_engine_is_the_default_and_never_uses_marian(env):
    langs.configure_translation("google", env.marian)
    out, engine = langs.translate_texts_ex(["a", "b"], "fr")
    assert (out, engine) == (["G(a)", "G(b)"], "google") and env.marian.translated == []
    env.google.blocked = True
    with pytest.raises(langs.RateLimited):
        langs.translate_texts(["zzz"], "fr")                 # pas de repli en mode google
    assert env.marian.translated == []


def test_marian_engine_never_calls_google(env):
    langs.configure_translation("marian", env.marian)
    out, engine = langs.translate_texts_ex(["a", "b"], "fr")
    assert (out, engine) == (["M(a)", "M(b)"], "marian") and env.google.calls == 0
    with pytest.raises(langs.LanguageUnavailable):           # opus-mt-en-xx : source anglaise
        langs.translate_texts(["x"], "fr", source="de")


def test_marian_engine_without_backend_says_what_to_install(env):
    langs.configure_translation("marian", None)
    with pytest.raises(langs.TranslatorUnavailable, match="translate-local"):
        langs.translate_texts(["x"], "fr")


def test_auto_engine_falls_back_to_marian_when_google_refuses(env):
    langs.configure_translation("auto", env.marian)
    assert langs.translate_texts_ex(["one"], "fr") == (["G(one)"], "google")   # Google d'abord
    env.google.blocked = True
    out, engine = langs.translate_texts_ex(["two", "three"], "fr")
    assert (out, engine) == (["M(two)", "M(three)"], "marian")                 # repli, sans erreur
    calls = env.google.calls
    out, engine = langs.translate_texts_ex(["four"], "fr")
    assert engine == "marian" and env.google.calls == calls    # disjoncteur : Google ignoré
    env.clock.t += langs.BLOCK_SECONDS[0] + 1                  # Google revient
    env.google.blocked = False
    assert langs.translate_texts_ex(["five"], "fr") == (["G(five)"], "google")


def test_auto_engine_without_marian_just_reports_the_refusal(env):
    langs.configure_translation("auto", None)
    env.google.blocked = True
    with pytest.raises(langs.RateLimited):
        langs.translate_texts(["x"], "fr")


def test_cached_translations_survive_an_engine_change(env):
    langs.configure_translation("auto", env.marian)
    langs.translate_texts(["hello"], "fr")
    env.google.blocked = True
    out, engine = langs.translate_texts_ex(["hello"], "fr")
    assert out == ["G(hello)"] and engine is None               # cache : aucun moteur sollicité


def test_prepare_only_loads_the_local_model_when_it_will_be_used(env):
    langs.configure_translation("google", env.marian)
    assert langs.prepare_translation("fr") is None             # Google : rien à préparer
    langs.configure_translation("auto", env.marian)
    assert langs.prepare_translation("fr") is None             # Google disponible : idem
    assert env.marian.prepared == []
    env.google.blocked = True
    langs.translate_texts(["x"], "fr")                         # Google refuse -> repli local
    assert langs.google_blocked_for() > 0
    assert langs.prepare_translation("fr") == "marian"         # le local va servir : on le prépare
    assert env.marian.prepared == ["fr"]
    langs.configure_translation("marian", env.marian)
    env.marian.prepared.clear()
    assert langs.prepare_translation("fr-FR") == "marian" and env.marian.prepared == ["fr"]
    with pytest.raises(langs.LanguageUnavailable):
        langs.prepare_translation("xx")


def test_unknown_engine_is_refused():
    with pytest.raises(ValueError, match="unknown translation engine"):
        langs.configure_translation("deepl", None)


# ── MarianEngine (faux transformers / torch) ────────────────────────────────

class FakeTokenizer:
    @classmethod
    def from_pretrained(cls, name):
        if name.endswith("-xx"):
            raise OSError("%s is not a local folder and is not a valid model "
                          "identifier listed on 'https://huggingface.co/models'" % name)
        if name.endswith("-offline"):
            raise OSError("We couldn't connect to 'https://huggingface.co'")
        return cls()

    def __call__(self, texts, **kw):
        assert kw["return_tensors"] == "pt" and kw["padding"] and kw["truncation"]
        return {"input_ids": FakeTensor(texts)}

    def batch_decode(self, generated, skip_special_tokens):
        assert skip_special_tokens
        return ["  FR:%s " % t for t in generated.texts]


class FakeTensor:
    def __init__(self, texts):
        self.texts = texts

    def to(self, device):
        return self


class FakeModel:
    loaded = []

    @classmethod
    def from_pretrained(cls, name):
        cls.loaded.append(name)
        return cls()

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        return self

    def generate(self, input_ids, num_beams, max_new_tokens):
        return input_ids


@pytest.fixture
def marian_libs(monkeypatch):
    import transformers
    monkeypatch.setattr(transformers, "MarianTokenizer", FakeTokenizer, raising=False)
    monkeypatch.setattr(transformers, "MarianMTModel", FakeModel, raising=False)

    @contextmanager
    def inference_mode():
        yield
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(inference_mode=inference_mode))
    monkeypatch.setattr(translate_marian, "missing_dependencies", lambda: [])
    FakeModel.loaded.clear()


def test_marian_engine_translates_in_batches_and_caches_the_model(marian_libs):
    engine = translate_marian.MarianEngine(batch_size=2)
    out = engine.translate(["a", "b", "c"], "fr")
    assert out == ["FR:a", "FR:b", "FR:c"]                      # décodé et nettoyé
    engine.translate(["d"], "fr")
    assert FakeModel.loaded == ["Helsinki-NLP/opus-mt-en-fr"]   # chargé une seule fois
    engine.translate(["x"], "pt-BR")
    assert FakeModel.loaded[-1] == "Helsinki-NLP/opus-mt-en-pt"
    assert engine.unload() is True and engine.unload() is False


def test_marian_engine_template_and_device(marian_libs):
    engine = translate_marian.MarianEngine(device="cpu", model_template="my/opus-{lang}")
    engine.prepare("de")
    assert FakeModel.loaded == ["my/opus-de"]


def test_marian_engine_errors_are_explicit(marian_libs, monkeypatch):
    engine = translate_marian.MarianEngine()
    with pytest.raises(langs.LanguageUnavailable) as err:
        engine.translate(["a"], "xx")                           # modèle introuvable
    assert "opus-mt-en-xx" in str(err.value)
    with pytest.raises(langs.TranslationError, match="check the network"):
        translate_marian.MarianEngine(model_template="Helsinki-NLP/opus-mt-en-{lang}"
                                      ).translate(["a"], "offline")
    monkeypatch.setattr(translate_marian, "missing_dependencies",
                        lambda: ["torch", "sentencepiece"])
    with pytest.raises(langs.TranslatorUnavailable, match="torch, sentencepiece"):
        translate_marian.MarianEngine().prepare("fr")


# ── serveur ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server(env, monkeypatch, tmp_path):
    from werkzeug.serving import make_server

    def build(conf_text, marian_ok=True):
        monkeypatch.setattr(tb, "missing_dependencies", lambda: [] if marian_ok else ["torch"])
        monkeypatch.setattr(tb._backend.engine, "translate",
                            lambda texts, code: env.marian.translate(texts, code))
        monkeypatch.setattr(tb._backend.engine, "prepare",
                            lambda code: env.marian.prepare(code))
        conf = tmp_path / "server.conf"
        conf.write_text("[simpostcards]\n" + conf_text)
        app = create_app(conf)
        httpd = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return types.SimpleNamespace(client=app.test_client(), httpd=httpd, app=app,
                                     url="http://127.0.0.1:%d" % httpd.server_port)

    built = []

    def make(*a, **k):
        s = build(*a, **k)
        built.append(s)
        return s
    yield make
    for s in built:
        s.httpd.shutdown()


def test_server_marian_engine_translates_locally_and_says_so(server, env):
    s = server("translate_engine = marian\n")
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": ["a", "b"]})
    assert r.get_json() == {"lang": "fr", "source": "en", "engine": "marian",
                            "translations": ["M(a)", "M(b)"]}
    assert env.google.calls == 0


def test_server_prepare_loads_the_local_model_before_the_texts(server, env):
    s = server("translate_engine = marian\n")
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": [], "prepare": True})
    assert r.status_code == 200 and r.get_json()["engine"] == "marian"
    assert env.marian.prepared == ["fr"]
    s2 = server("translate_engine = google\n")
    r = s2.client.post("/api/translate", json={"lang": "fr", "texts": [], "prepare": True})
    assert r.get_json()["engine"] == "google"                   # rien à préparer


def test_server_auto_engine_rides_out_a_google_block(server, env):
    s = server("translate_engine = auto\n")
    env.google.blocked = True
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": ["hello"]})
    assert r.status_code == 200
    assert r.get_json()["translations"] == ["M(hello)"] and r.get_json()["engine"] == "marian"


def test_server_google_engine_answers_429_with_the_way_out(server, env):
    s = server("")                                              # google par défaut
    env.google.blocked = True
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": ["hello"]})
    assert r.status_code == 429 and "translate_engine" in r.get_json()["error"]


def test_server_marian_without_dependencies_answers_501(server, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        s = server("translate_engine = marian\n", marian_ok=False)
    assert any("translate_engine = marian" in m and "torch" in m for m in caplog.messages)
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": ["a"]})
    assert r.status_code == 501 and "translate-local" in r.get_json()["error"]


def test_server_marian_model_missing_is_a_language_problem(server, env, monkeypatch):
    s = server("translate_engine = marian\n")

    def no_model(texts, code):
        raise langs.LanguageUnavailable("translate", code, "no local translation model 'x'")
    monkeypatch.setattr(tb._backend.engine, "translate", no_model)
    r = s.client.post("/api/translate", json={"lang": "fr", "texts": ["a"]})
    assert r.status_code == 422 and r.get_json()["code"] == "language_unavailable"


def test_server_invalid_engine_falls_back_to_google(server, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        server("translate_engine = deepl\n")
    assert any("unknown translate_engine" in m for m in caplog.messages)
    assert langs.translation_engine() == "google"


def test_marian_is_unloaded_with_the_other_models_and_never_while_busy():
    timer = tb._backend._idle_timer
    assert "MARIAN" in iu.unload_all()
    with timer.busy():
        assert timer.unload_now() == "busy"
