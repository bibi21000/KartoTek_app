# -*- encoding: utf-8 -*-
"""Langues : OCR, détection et traduction (client <-> serveur simpostcards).

Le serveur est un vrai serveur Flask local ; BLIP/DETR/tesseract et le
service de traduction sont remplacés par des faux. On vérifie surtout que,
quand une langue n'est pas disponible, le serveur le consigne dans ses logs
ET que l'information remonte jusqu'à l'utilisateur.
"""
import io
import json
import logging
import threading

import pytest
from PIL import Image

pytest.importorskip("flask")
pytest.importorskip("imagehash")
pytest.importorskip("deep_translator")

import simpostcards.blueprints.api as api  # noqa: E402
from libpostcards import langs  # noqa: E402
from libpostcards.model import Model  # noqa: E402
from simpostcards import create_app  # noqa: E402

CAPTION = "a man standing in front of a building"
FR = {CAPTION: "un homme devant un bâtiment", "person": "personne",
      "tie": "cravate", "bus": "bus"}


@pytest.fixture
def server(monkeypatch, tmp_path):
    from werkzeug.serving import make_server

    monkeypatch.setattr(api, "CAPTION_AVAILABLE", True)
    monkeypatch.setattr(api, "OBJECTS_AVAILABLE", True)
    monkeypatch.setattr(api, "compute_caption", lambda image, **kw: CAPTION)
    monkeypatch.setattr(api, "compute_objects", lambda image, **kw: [
        ("person", 0.99), ("tie", 0.95), ("bus", 0.93)])
    monkeypatch.setattr(api, "OCR_AVAILABLE", True)
    monkeypatch.setattr(api, "installed_languages", lambda: ["eng", "fra"])
    monkeypatch.setattr(api, "missing_languages", lambda lang: [
        w for w in lang.split("+") if w not in ("eng", "fra")])
    monkeypatch.setattr(api, "compute_ocr", lambda image, lang="fra": "texte ocr")

    calls = []

    def fake_translate_one(src, target, text):
        # Comme le vrai service : plusieurs textes peuvent arriver dans une
        # même requête, un par ligne, et ressortent ligne par ligne.
        calls.append((target, text))
        return "\n".join(FR.get(line, "[%s] %s" % (target, line))
                         for line in text.split("\n"))

    monkeypatch.setattr(langs, "_translate_one", fake_translate_one)
    langs.reset_translation_state()
    langs.reset_logged_once()

    conf = tmp_path / "server.conf"
    conf.write_text("[simpostcards]\n")
    app = create_app(conf)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    info = type("S", (), {})()
    info.url = "http://127.0.0.1:%d" % httpd.server_port
    info.app = app
    info.client = app.test_client()
    info.translate_calls = calls
    yield info
    httpd.shutdown()


def png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (120, 80, 40)).save(buf, format="PNG")
    return buf.getvalue()


# ── bibliothèque ────────────────────────────────────────────────────────────

def test_resolve_language_and_aliases():
    assert langs.resolve_language("fr") == "fr"
    assert langs.resolve_language("fr-FR") == "fr"
    assert langs.resolve_language("pt_BR") == "pt"
    assert langs.resolve_language("zh") == "zh-CN"
    assert langs.resolve_language("xx") is None
    assert langs.resolve_language("") is None


def test_translate_texts_cache_and_errors(monkeypatch):
    seen = []
    monkeypatch.setattr(langs, "_translate_one",
                        lambda s, t, text: seen.append(text) or "T:" + text)
    langs.clear_translation_cache()
    assert langs.translate_texts(["a", "", "a"], "fr") == ["T:a", "", "T:a"]
    assert seen == ["a"]                                   # mis en cache
    with pytest.raises(langs.LanguageUnavailable) as err:
        langs.translate_texts(["a"], "xx")
    assert err.value.available and "xx" in str(err.value)

    def boom(*a):
        raise ConnectionError("no network")
    monkeypatch.setattr(langs, "_translate_one", boom)
    with pytest.raises(langs.TranslationError, match="no network"):
        langs.translate_texts(["new text"], "fr")
    monkeypatch.setattr(langs, "TRANSLATOR_AVAILABLE", False)
    with pytest.raises(langs.TranslatorUnavailable):
        langs.translate_texts(["x"], "fr")


# ── serveur : OCR ───────────────────────────────────────────────────────────

def test_ocr_language_missing_is_logged_and_returned(server, caplog):
    with caplog.at_level(logging.WARNING):
        r = server.client.post("/api/compute_ocr", data=png(),
                               content_type="image/png",
                               query_string={"lang": "fra+deu"})
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "language_unavailable" and body["service"] == "ocr"
    assert body["missing"] == ["deu"] and body["available"] == ["eng", "fra"]
    assert any("compute_ocr : langue indisponible" in m for m in caplog.messages)
    ok = server.client.post("/api/compute_ocr", data=png(),
                            content_type="image/png", query_string={"lang": "fra"})
    assert ok.status_code == 200 and ok.get_json()["text"] == "texte ocr"


def test_ocr_client_checks_language_once_and_raises(server):
    from libpostcards.ocr import PostcardOCR
    good = PostcardOCR(lang="fra+eng", external_helper=server.url)
    good.check_language()
    bad = PostcardOCR(lang="deu", external_helper=server.url)
    with pytest.raises(langs.LanguageUnavailable) as err:
        bad.check_language()
    assert err.value.service == "ocr" and err.value.missing == ["deu"]
    assert "tesseract-ocr-deu" in str(err.value)


def test_ocr_languages_route(server):
    assert server.client.get("/api/ocr_languages").get_json() == {
        "engine": "tesseract", "languages": ["eng", "fra"]}
    assert server.client.get("/api/ocr_languages?lang=deu").status_code == 422


# ── serveur : traduction ────────────────────────────────────────────────────

def test_translate_route_ok_preflight_and_errors(server, caplog):
    c = server.client
    r = c.post("/api/translate", json={"lang": "fr", "texts": ["person", "tie"]})
    assert r.get_json() == {"lang": "fr", "source": "en", "engine": "google",
                            "translations": ["personne", "cravate"]}
    assert c.post("/api/translate", json={"lang": "fr-FR", "texts": []}
                  ).get_json()["lang"] == "fr"             # préparation : rien à traduire
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/translate", json={"lang": "xx", "texts": []})
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "language_unavailable" and "fr" in body["supported"]
    assert any("translate : langue indisponible" in m for m in caplog.messages)
    assert c.post("/api/translate", json={"texts": []}).status_code == 400
    assert c.post("/api/translate", json={"lang": "fr", "texts": [1]}).status_code == 400


def test_translate_route_service_failure_and_disabled(server, monkeypatch):
    def boom(*a):
        raise ConnectionError("offline")
    monkeypatch.setattr(langs, "_translate_one", boom)
    langs.clear_translation_cache()
    r = server.client.post("/api/translate", json={"lang": "fr", "texts": ["zzz"]})
    assert r.status_code == 502 and r.get_json()["code"] == "translation_failed"
    server.app.config["TRANSLATE_ENABLED"] = False
    r = server.client.post("/api/translate", json={"lang": "fr", "texts": []})
    assert r.status_code == 501 and r.get_json()["code"] == "translator_unavailable"


# ── serveur : détection avec langue ─────────────────────────────────────────

def test_detection_translated_by_server(server):
    r = server.client.post("/api/compute_detection", data=png(),
                           content_type="image/png", query_string={"lang": "fr"})
    body = r.get_json()
    assert body["caption"] == CAPTION                      # l'anglais reste
    assert body["translation"] == {
        "status": "ok", "lang": "fr", "caption": FR[CAPTION], "engine": "google",
        "labels": {"bus": "bus", "person": "personne", "tie": "cravate"}}
    plain = server.client.post("/api/compute_detection", data=png(),
                               content_type="image/png").get_json()
    assert "translation" not in plain                      # sans lang : inchangé


def test_detection_language_unavailable_keeps_result_and_logs(server, caplog):
    with caplog.at_level(logging.WARNING):
        r = server.client.post("/api/compute_detection", data=png(),
                               content_type="image/png", query_string={"lang": "xx"})
    assert r.status_code == 200                            # détection non perdue
    body = r.get_json()
    assert body["caption"] == CAPTION
    assert body["translation"]["status"] == "language_unavailable"
    assert any("compute_detection : langue indisponible" in m for m in caplog.messages)


# ── client : détection ──────────────────────────────────────────────────────

def detector(server, lang):
    from libpostcards.detection import PostcardDetection
    return PostcardDetection(lang=lang, external_helper=server.url)


def test_client_gets_server_translation_and_marker(server):
    d = detector(server, "fr")
    assert d.check_language() is None
    img = Image.new("RGB", (40, 30))
    cap = d.describe(img)
    assert (cap.text, cap.lang, cap.issues) == (FR[CAPTION], "fr", [])
    obj = d.describe_objects(img, threshold=0.5, excluded_objects=["tie"])
    assert obj.text == "personne, bus" and obj.lang == "fr"   # exclusion sur l'anglais
    assert d.to_string(img) == FR[CAPTION]                    # API historique


def test_client_language_unavailable_keeps_english_and_reports(server, caplog):
    d = detector(server, "xx")
    img = Image.new("RGB", (40, 30))
    with caplog.at_level(logging.WARNING):
        issue = d.check_language()
        first = d.describe(img)
        second = d.describe_objects(img, threshold=0.5)
    assert issue is not None and issue.code == "language_unavailable"
    assert "xx" in issue.message and "detect-translate" in issue.message
    assert first.text == CAPTION and first.lang == "en"       # marqué non traduit
    assert second.text == "person, tie, bus" and second.lang == "en"
    assert len(d.issues) == 1                                  # dédoublonné
    # Journalisé côté client une seule fois, et la langue n'est plus demandée
    # au serveur (pas de bruit dans ses logs à chaque carte).
    assert sum("Detection language" in m for m in caplog.messages) == 1


def test_client_old_server_without_translation_block_translates_locally(
        server, monkeypatch):
    # Un serveur qui ignore "lang" ne renvoie pas de bloc "translation"
    # (ici : null) : le client traduit lui-même la légende anglaise, comme
    # avant l'existence de la traduction côté serveur.
    monkeypatch.setattr(api, "_translate_detection", lambda *a, **k: None)
    d = detector(server, "fr")
    img = Image.new("RGB", (40, 30))
    cap = d.describe(img)
    assert (cap.text, cap.lang) == (FR[CAPTION], "fr")
    obj = d.describe_objects(img, threshold=0.5)
    # Chemin local historique : la liste est traduite d'un seul bloc.
    assert (obj.text, obj.lang) == ("[fr] person, tie, bus", "fr")


# ── détection de textes restés en anglais, traduction par lots ──────────────

@pytest.fixture
def datadir(tmp_path):
    (tmp_path / "cards").mkdir()
    cards = {
        1: {"detected_content": CAPTION, "detected_content_lang": "en",
            "detected_objects": "person, bus", "detected_objects_lang": "en"},
        2: {"detected_content": "une rue", "detected_content_lang": "fr",
            "detected_objects": "personne", "detected_objects_lang": "fr"},
        3: {"detected_content": "a street with cars", "detected_objects": "car, person"},
        4: {"detected_content": "Ma propre légende", "detected_content_lang": "manual",
            "detected_objects": "bus", "detected_objects_lang": "manual"},
        5: {"detected_content": "eine Strasse", "detected_content_lang": "de"},
        6: {"detected_content": "une rue ancienne", "detected_objects": "voiture"},
        7: {"title": "rien de détecté"},
    }
    for cid, extra in cards.items():
        (tmp_path / "cards" / f"{cid}.json").write_text(
            json.dumps({"id": cid, **extra}))
    model = Model(tmp_path)
    model.generate()
    model.close()
    return tmp_path


class FakeTranslator:
    def __init__(self, fail_after=None):
        self.calls, self.fail_after = [], fail_after

    def translate(self, texts, lang):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise langs.TranslationError("quota exceeded")
        self.calls.append(list(texts))
        return [FR.get(t, "[fr] " + t) for t in texts]


def test_classify_markers_and_guess(datadir):
    from tkpostcards.libs.detect_translate import classify
    card = lambda **kw: kw  # noqa: E731
    f = lambda c, g=True: classify(c, "detected_content", "detected_content_lang", "fr", g)  # noqa: E731
    assert f(card(detected_content="a street", detected_content_lang="en")) == (True, "marker")
    assert f(card(detected_content="une rue", detected_content_lang="fr")) == (False, "done")
    assert f(card(detected_content="x", detected_content_lang="manual")) == (False, "manual")
    assert f(card(detected_content="eine Strasse", detected_content_lang="de")) == (False, "other")
    assert f(card(detected_content="a street with cars")) == (True, "guessed")
    assert f(card(detected_content="a street with cars"), False) == (False, "unknown")
    assert f(card(detected_content="une rue ancienne")) == (False, "unknown")
    assert f(card()) == (False, "empty")


def test_translate_cards_dry_run_then_real(datadir):
    from tkpostcards.libs.detect_translate import translate_cards
    model = Model(datadir)
    tr = FakeTranslator()
    dry = translate_cards(model, [1, 2, 3, 4, 5, 6, 7, 99], tr, "fr", "fr", dry_run=True)
    assert tr.calls == []                                      # rien traduit
    assert dry.cards_changed == 2 and dry.fields_translated == 4
    assert dry.missing == ["99"]
    assert {(c, f) for c, f, _r in dry.planned} == {
        (1, "detected_content"), (1, "detected_objects"),
        (3, "detected_content"), (3, "detected_objects")}

    done = translate_cards(model, [1, 2, 3, 4, 5, 6, 7], tr, "fr", "fr")
    assert done.error is None and done.cards_changed == 2
    c1, c3 = model.load_json(1), model.load_json(3)
    assert c1["detected_content"] == FR[CAPTION] and c1["detected_content_lang"] == "fr"
    assert c1["detected_objects"] == "personne, bus" and c1["detected_objects_lang"] == "fr"
    assert c3["detected_content_lang"] == "fr" and c3["detected_objects_lang"] == "fr"
    assert model.load_json(4)["detected_content"] == "Ma propre légende"   # manuel
    assert model.load_json(5)["detected_content"] == "eine Strasse"        # autre langue
    assert model.load_json(6)["detected_content"] == "une rue ancienne"    # inconnu
    assert model.get_card(1)["detected_content_lang"] == "fr"              # SQLite aussi
    assert translate_cards(model, [1, 2, 3], tr, "fr", "fr").cards_changed == 0  # idempotent
    model.close()


def test_translate_cards_interruption_keeps_what_was_done(datadir, monkeypatch):
    from tkpostcards.libs import detect_translate as dt
    monkeypatch.setattr(dt, "CARDS_PER_BATCH", 1)              # un lot par carte
    model = Model(datadir)
    out = dt.translate_cards(model, [1, 3], FakeTranslator(fail_after=2), "fr", "fr")
    assert out.error and "quota" in out.error and out.cards_changed == 1
    assert model.load_json(1)["detected_content_lang"] == "fr"   # lot 1 enregistré
    assert model.load_json(3)["detected_content"] == "a street with cars"
    again = dt.translate_cards(model, [1, 3], FakeTranslator(), "fr", "fr")
    assert again.error is None and again.cards_changed == 1     # reprend où ça s'est arrêté
    model.close()


# ── CLI de bout en bout ─────────────────────────────────────────────────────

def _runner():
    from click.testing import CliRunner
    try:
        return CliRunner(mix_stderr=False)      # click < 8.2
    except TypeError:
        return CliRunner()                      # click >= 8.2 : stderr séparé


@pytest.fixture
def cli_env(server, datadir):
    (datadir / "postcards.conf").write_text("[DEFAULT]\ndatadir = %s\n" % datadir)

    def run(*args):
        from click.testing import CliRunner
        from tkpostcards.scripts.tktools import cli
        res = _runner().invoke(
            cli, ["--conffile", str(datadir / "postcards.conf"),
                  "--external-helper", server.url, *args])
        return res
    return run


def test_cli_detect_translate_dry_run_and_real(cli_env, datadir):
    dry = cli_env("detect-translate", "--lang", "fr", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert "1: detected_content (marker)" in dry.output
    assert "3: detected_objects (guessed)" in dry.output
    assert Model(datadir).load_json(1)["detected_content"] == CAPTION   # rien écrit

    real = cli_env("detect-translate", "--lang", "fr")
    assert real.exit_code == 0, real.output + real.stderr
    assert Model(datadir).load_json(1)["detected_content"] == FR[CAPTION]
    assert "4 text(s) in 2 card(s) translated to fr" in real.output


def test_cli_detect_translate_language_unavailable_tells_the_user(
        cli_env, datadir, caplog):
    with caplog.at_level(logging.WARNING):
        res = cli_env("detect-translate", "--lang", "xx")
    assert res.exit_code != 0
    assert "xx" in res.stderr and "not available" in res.stderr
    assert "Supported language codes:" in res.stderr and " fr" in res.stderr
    # Consigné côté serveur ET côté client :
    assert any("translate : langue indisponible" in m for m in caplog.messages)
    assert any("detect-translate: language 'xx' unavailable" in m for m in caplog.messages)
    assert Model(datadir).load_json(1)["detected_content"] == CAPTION     # rien touché


def test_cli_detect_translate_needs_a_server(datadir):
    from click.testing import CliRunner
    from tkpostcards.scripts.tktools import cli
    (datadir / "postcards.conf").write_text("[DEFAULT]\ndatadir = %s\n" % datadir)
    res = _runner().invoke(
        cli, ["--conffile", str(datadir / "postcards.conf"), "detect-translate"])
    assert res.exit_code != 0 and "external_helper" in res.stderr


def test_cli_detect_with_unavailable_language_stores_english_and_warns(
        cli_env, datadir):
    Image.new("RGB", (40, 30)).save(datadir / "cards" / "7_R.tiff")
    res = cli_env("detect", "7", "--detect-lang", "xx", "--model-name", "m")
    assert res.exit_code == 0, res.output + res.stderr
    assert "WARNING" in res.stderr and "'xx'" in res.stderr
    assert "kttools detect-translate --lang xx" in res.stderr
    card = Model(datadir).load_json(7)
    assert card["detected_content"] == CAPTION and card["detected_content_lang"] == "en"
    assert card["detected_objects_lang"] == "en"

    ok = cli_env("detect", "7", "--detect-lang", "fr")
    assert ok.exit_code == 0 and "WARNING" not in ok.stderr
    card = Model(datadir).load_json(7)
    assert card["detected_content"] == FR[CAPTION] and card["detected_content_lang"] == "fr"


def test_cli_ocr_language_unavailable_stops_before_working(cli_env, datadir):
    res = cli_env("ocr", "1", "--ocr-langs", "deu")
    assert res.exit_code != 0
    assert "deu" in res.stderr and "tesseract" in res.stderr
    assert "OCR languages installed there: eng, fra" in res.stderr
