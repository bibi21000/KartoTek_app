# -*- encoding: utf-8 -*-
"""Moteurs OCR : tesseract et PaddleOCR, choisis par le client, exécutés en
local ou sur le serveur simpostcards.

PaddleOCR est remplacé par un faux (pas de téléchargement de modèles) ; un
test vérifie en plus, si le vrai paquet est installé, que notre liste de
langues colle à ce qu'il sait faire.
"""
import io
import logging
import sys
import threading
import types

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("flask")
pytest.importorskip("imagehash")

import simpostcards.blueprints.api as api  # noqa: E402
import simpostcards.libs.paddle_backend as pb  # noqa: E402
from libpostcards import langs, ocr_paddle  # noqa: E402
from libpostcards.ocr import PostcardOCR  # noqa: E402
from simpostcards import create_app  # noqa: E402
from simpostcards.libs import _idle_unload as iu  # noqa: E402


class FakeResult(dict):
    """Résultat de PaddleOCR.predict() : un dict (clés vérifiées sur paddlex)."""


class FakePaddle:
    """Remplace paddleocr.PaddleOCR : lit toujours la même carte."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.seen = []
        FakePaddle.instances.append(self)

    def predict(self, array):
        self.seen.append(array.shape)
        return [FakeResult(
            rec_texts=["Vue générale", "de la Rue", "bruit"],
            rec_scores=[0.98, 0.95, 0.30],
            rec_boxes=np.array([[10, 10, 120, 40], [130, 12, 220, 42], [10, 80, 60, 100]]))]


@pytest.fixture(autouse=True)
def fake_paddle(monkeypatch):
    """paddleocr "installé", et ``from paddleocr import PaddleOCR`` -> faux."""
    FakePaddle.instances.clear()
    module = types.ModuleType("paddleocr")
    module.PaddleOCR = FakePaddle
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", True)
    monkeypatch.setattr(pb, "PADDLE_AVAILABLE", True)
    monkeypatch.setattr(api, "PADDLE_AVAILABLE", True)
    pb._models._engines.clear()
    yield
    pb._models._engines.clear()


def png(size=(60, 40)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


# ── langues et noms de moteurs ──────────────────────────────────────────────

def test_engine_names():
    assert langs.normalize_ocr_engine(None) == "tesseract"
    assert langs.normalize_ocr_engine("Paddle") == "paddleocr"
    assert langs.normalize_ocr_engine("PaddleOCR") == "paddleocr"
    with pytest.raises(ValueError, match="unknown OCR engine"):
        langs.normalize_ocr_engine("abbyy")


def test_paddle_language_mapping():
    r = langs.resolve_paddle_language
    assert [r(x) for x in ("fra", "fr", "fra+eng", "eng+fra", "deu", "german")] == [
        "fr", "fr", "fr", "fr", "de", "german"]
    assert r("eng") == "en"
    assert r("zzz") is None and r("fra+zzz") is None and r("") is None
    assert langs.paddle_missing_languages("fra+zzz") == ["zzz"]


def test_language_list_matches_the_installed_paddleocr(monkeypatch):
    """Garde-fou contre la dérive : chaque code de notre liste doit avoir un
    modèle dans le paquet RÉELLEMENT installé (ignoré s'il ne l'est pas)."""
    monkeypatch.delitem(sys.modules, "paddleocr", raising=False)   # le faux
    real = pytest.importorskip("paddleocr")
    bad = [c for c in sorted(langs.PADDLE_LANGUAGES)
           if real.PaddleOCR._get_ocr_model_names(None, c, None) == (None, None)]
    assert bad == []


# ── ocr_paddle : lecture ────────────────────────────────────────────────────

def test_assemble_lines_groups_blocks_on_the_same_line():
    items = [(300, 10, 380, 40, "Rue"), (10, 12, 120, 42, "Vue générale"),
             (130, 11, 290, 41, "de la"), (20, 80, 400, 112, "Carte postale")]
    assert ocr_paddle.assemble_lines(items) == "Vue générale de la Rue\nCarte postale"
    assert ocr_paddle.assemble_lines([]) == ""
    assert ocr_paddle.assemble_lines([(0, 0, 1, 1, "  ")]) == ""


def test_to_bgr_array_flattens_transparency_on_white():
    im = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    im.putpixel((0, 0), (255, 0, 0, 255))
    a = ocr_paddle.to_bgr_array(im)
    assert a[0, 0].tolist() == [0, 0, 255]            # rouge, en BGR
    assert a[0, 1].tolist() == [255, 255, 255]        # fond transparent -> blanc


def test_read_text_filters_by_min_score():
    engine = FakePaddle()
    assert ocr_paddle.read_text(engine, Image.new("RGB", (50, 50)), 0.5) == \
        "Vue générale de la Rue"
    assert ocr_paddle.read_text(engine, Image.new("RGB", (50, 50)), 0.0).endswith("bruit")


def test_create_engine_passes_the_configuration():
    ocr_paddle.create_engine("fra+eng", device="cpu", ocr_version="PP-OCRv5")
    kw = FakePaddle.instances[-1].kwargs
    assert kw["lang"] == "fr" and kw["device"] == "cpu"
    assert kw["ocr_version"] == "PP-OCRv5" and kw["text_rec_score_thresh"] == 0.0
    assert kw["use_doc_unwarping"] is False
    ocr_paddle.create_engine("fra")
    assert "device" not in FakePaddle.instances[-1].kwargs      # choix de PaddlePaddle


def test_create_engine_errors(monkeypatch):
    with pytest.raises(langs.LanguageUnavailable) as err:
        ocr_paddle.create_engine("zzz")
    assert err.value.missing == ["zzz"] and "PaddleOCR" in str(err.value)

    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", False)
    with pytest.raises(langs.EngineUnavailable, match="pip install pypostcards"):
        ocr_paddle.create_engine("fra")
    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", True)

    def no_models(**kw):
        raise ValueError("No models are available for lang='fr' and ocr_version=None.")
    monkeypatch.setattr(sys.modules["paddleocr"], "PaddleOCR", no_models)
    with pytest.raises(langs.LanguageUnavailable):
        ocr_paddle.create_engine("fra")

    def offline(**kw):
        raise Exception("No model source is available! Please check network")
    monkeypatch.setattr(sys.modules["paddleocr"], "PaddleOCR", offline)
    with pytest.raises(langs.EngineUnavailable, match="downloaded on first use"):
        ocr_paddle.create_engine("fra")


# ── client, calcul local ────────────────────────────────────────────────────

@pytest.fixture
def card_images(tmp_path):
    path = tmp_path / "card.png"
    Image.new("RGB", (80, 50), (210, 210, 210)).save(path)
    return path


def test_local_paddle_reads_and_reuses_its_engine(card_images):
    ocr = PostcardOCR(lang="fra", engine="paddleocr", min_score=0.5)
    assert ocr.to_string(card_images) == "Vue générale de la Rue"
    ocr.to_string(card_images)
    assert len(FakePaddle.instances) == 1               # modèles chargés une fois


def test_local_paddle_unavailable_and_bad_language(monkeypatch):
    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", False)
    with pytest.raises(langs.EngineUnavailable, match="not installed"):
        PostcardOCR(engine="paddleocr").check_language()
    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", True)
    with pytest.raises(langs.LanguageUnavailable) as err:
        PostcardOCR(lang="fra+klingon", engine="paddleocr").check_language()
    assert err.value.missing == ["klingon"]


def test_unknown_engine_is_refused_at_construction():
    with pytest.raises(ValueError):
        PostcardOCR(engine="abbyy")


# ── serveur ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server(monkeypatch, tmp_path):
    from werkzeug.serving import make_server

    monkeypatch.setattr(api, "OCR_AVAILABLE", True)
    monkeypatch.setattr(api, "installed_languages", lambda: ["eng", "fra"])
    monkeypatch.setattr(api, "missing_languages", lambda lang: [
        w for w in lang.split("+") if w not in ("eng", "fra")])
    calls = []
    monkeypatch.setattr(api, "compute_ocr", lambda image, lang="fra": "texte tesseract")
    real = api.compute_paddle_ocr
    monkeypatch.setattr(api, "compute_paddle_ocr",
                        lambda *a, **kw: calls.append(kw) or real(*a, **kw))

    conf = tmp_path / "server.conf"
    conf.write_text("[simpostcards]\npaddleocr_device = cpu\npaddleocr_version = PP-OCRv5\n")
    app = create_app(conf)
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    info = types.SimpleNamespace(
        url="http://127.0.0.1:%d" % httpd.server_port, app=app,
        client=app.test_client(), paddle_calls=calls)
    yield info
    httpd.shutdown()


def post(server, **fields):
    return server.client.post("/api/compute_ocr", data=png(), content_type="image/png",
                              query_string=fields)


def test_server_default_engine_is_tesseract_and_says_so(server):
    body = post(server, lang="fra").get_json()
    assert body == {"text": "texte tesseract", "lang": "fra", "engine": "tesseract"}


def test_server_paddleocr_with_deployment_settings(server):
    r = post(server, engine="paddleocr", lang="fra+eng", min_score="0.9")
    assert r.status_code == 200
    assert r.get_json() == {"text": "Vue générale de la Rue",
                            "lang": "fra+eng", "engine": "paddleocr"}
    call = server.paddle_calls[-1]
    assert call["device"] == "cpu" and call["ocr_version"] == "PP-OCRv5"   # [simpostcards]
    assert call["min_score"] == 0.9                                         # client
    assert FakePaddle.instances[-1].kwargs["device"] == "cpu"


def test_server_paddleocr_language_unavailable_is_logged_and_returned(server, caplog):
    with caplog.at_level(logging.WARNING):
        r = post(server, engine="paddleocr", lang="fra+klingon")
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "language_unavailable" and body["engine"] == "paddleocr"
    assert body["missing"] == ["klingon"] and "fr" in body["available"]
    assert any("langue indisponible pour paddleocr" in m for m in caplog.messages)
    assert FakePaddle.instances == []                  # aucun modèle chargé pour rien


def test_server_engine_unavailable_is_logged_and_returned(server, monkeypatch, caplog):
    monkeypatch.setattr(api, "PADDLE_AVAILABLE", False)
    with caplog.at_level(logging.WARNING):
        r = post(server, engine="paddleocr", lang="fra")
    assert r.status_code == 501
    body = r.get_json()
    assert body["code"] == "engine_unavailable" and body["engine"] == "paddleocr"
    assert "not installed" in body["error"]
    assert any("moteur paddleocr indisponible" in m for m in caplog.messages)
    assert post(server, lang="fra").status_code == 200       # tesseract : intact


def test_server_can_disable_paddleocr_or_all_ocr(server):
    server.app.config["PADDLEOCR_ENABLED"] = False
    r = post(server, engine="paddleocr")
    assert r.status_code == 501 and "paddleocr_enabled" in r.get_json()["error"]
    assert post(server, lang="fra").status_code == 200
    server.app.config["PADDLEOCR_ENABLED"] = True
    server.app.config["OCR_ENABLED"] = False
    assert post(server, engine="paddleocr").status_code == 501
    assert post(server, lang="fra").status_code == 501


def test_server_rejects_unknown_engine_and_bad_score(server):
    assert post(server, engine="abbyy").status_code == 400
    assert post(server, engine="paddleocr", min_score="x").status_code == 400
    assert post(server, engine="paddleocr", min_score="2").status_code == 400


def test_server_translates_late_errors_from_the_paddle_library(server, monkeypatch):
    def boom(*a, **k):
        raise langs.EngineUnavailable("paddleocr", "models could not be downloaded")
    monkeypatch.setattr(api, "compute_paddle_ocr", boom)
    r = post(server, engine="paddleocr", lang="fra")
    assert r.status_code == 501 and r.get_json()["code"] == "engine_unavailable"

    def nomodel(*a, **k):
        raise langs.LanguageUnavailable("ocr", "fr", "no model", missing=["fr"])
    monkeypatch.setattr(api, "compute_paddle_ocr", nomodel)
    r = post(server, engine="paddleocr", lang="fra")
    assert r.status_code == 422 and r.get_json()["code"] == "language_unavailable"


def test_server_ocr_languages_per_engine(server):
    assert server.client.get("/api/ocr_languages?engine=paddleocr").get_json()[
        "languages"][:2] == sorted(langs.PADDLE_LANGUAGES)[:2]
    assert server.client.get("/api/ocr_languages?engine=paddleocr&lang=fra").status_code == 200
    assert server.client.get("/api/ocr_languages?engine=paddleocr&lang=xx").status_code == 422
    assert server.client.get("/api/ocr_languages?engine=nope").status_code == 400
    assert server.client.get("/api/ocr_languages").get_json()["engine"] == "tesseract"


# ── backend : mémoire des moteurs, déchargement ────────────────────────────

def test_backend_shares_engines_per_resolved_language_and_caps_them():
    img = Image.new("RGB", (30, 30))
    for lang in ("fra", "fr", "fra+eng", "eng+fra"):
        pb.compute_paddle_ocr(img, lang=lang)
    assert len(FakePaddle.instances) == 1                # tous = "fr"
    for lang in ("deu", "spa", "ita"):
        pb.compute_paddle_ocr(img, lang=lang)
    assert len(pb._models._engines) == pb.MAX_ENGINES    # le plus ancien est oublié


def test_backend_is_unloaded_with_the_others_and_never_while_busy():
    img = Image.new("RGB", (30, 30))
    pb.compute_paddle_ocr(img, lang="fra")
    timer = pb._models._idle_timer
    with timer.busy():
        assert timer.unload_now() == "busy" and pb._models._engines
    assert timer.unload_now() == "unloaded" and not pb._models._engines
    assert timer.unload_now() == "not_loaded"
    assert "PADDLEOCR" in iu.unload_all()


# ── client <-> serveur ──────────────────────────────────────────────────────

def test_client_runs_paddleocr_on_the_server(server, card_images):
    ocr = PostcardOCR(lang="fra", engine="paddleocr", external_helper=server.url)
    ocr.check_language()
    assert ocr.to_string(card_images) == "Vue générale de la Rue"
    assert FakePaddle.instances                          # a tourné côté serveur
    tess = PostcardOCR(lang="fra", external_helper=server.url)
    assert tess.to_string(card_images) == "texte tesseract"


def test_client_gets_the_servers_explanations(server, monkeypatch):
    bad = PostcardOCR(lang="fra+klingon", engine="paddleocr", external_helper=server.url)
    with pytest.raises(langs.LanguageUnavailable) as err:
        bad.check_language()
    assert err.value.missing == ["klingon"]
    monkeypatch.setattr(api, "PADDLE_AVAILABLE", False)
    with pytest.raises(langs.EngineUnavailable, match="not installed on this server"):
        PostcardOCR(engine="paddleocr", external_helper=server.url).check_language()


def test_client_refuses_a_server_too_old_to_choose_the_engine(card_images, monkeypatch):
    """Un serveur ancien ignore ``engine`` et lirait avec tesseract : on ne
    doit pas prendre ce texte pour du PaddleOCR."""
    import requests

    class Resp:
        status_code = 200
        def __init__(self, body): self._b = body
        def raise_for_status(self): pass
        def json(self): return self._b

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp({"text": "t", "lang": "fra"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp({"languages": ["fra"]}))
    ocr = PostcardOCR(engine="paddleocr", external_helper="http://old")
    with pytest.raises(langs.EngineUnavailable, match="too old"):
        ocr.check_language()
    assert PostcardOCR(external_helper="http://old").to_string(card_images) == "t"   # tesseract : ok

    class NotFound(Resp):
        status_code = 404
    monkeypatch.setattr(requests, "get", lambda *a, **k: NotFound({}))
    with pytest.raises(langs.EngineUnavailable, match="no /api/ocr_languages"):
        PostcardOCR(engine="paddleocr", external_helper="http://old").check_language()
    PostcardOCR(external_helper="http://old").check_language()    # tesseract : toléré


# ── CLI ─────────────────────────────────────────────────────────────────────

def _runner():
    from click.testing import CliRunner
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


@pytest.fixture
def cli(server, tmp_path):
    from libpostcards.model import Model
    (tmp_path / "cards").mkdir()
    (tmp_path / "cards" / "1.json").write_text('{"id": 1}')
    for side in "RV":
        Image.new("RGB", (80, 50), (200, 200, 200)).save(tmp_path / "cards" / f"1_{side}.tiff")
    model = Model(tmp_path)
    model.generate()
    model.close()

    def run(*args, conf=""):
        (tmp_path / "postcards.conf").write_text(
            "[DEFAULT]\ndatadir = %s\n[tkimport]\nocr_langs = fra\n%s" % (tmp_path, conf))
        from tkpostcards.scripts.tktools import cli as kttools
        return _runner().invoke(kttools, [
            "--conffile", str(tmp_path / "postcards.conf"),
            "--external-helper", server.url, *args])
    run.datadir = tmp_path
    return run


def test_cli_engine_from_option_and_from_config(cli):
    from libpostcards.model import Model
    r = cli("ocr", "1", "--ocr-engine", "paddleocr")
    assert r.exit_code == 0, r.output + r.stderr
    assert Model(cli.datadir).load_json(1)["recto_ocr"] == "Vue générale de la Rue"

    Model(cli.datadir).write_json({"id": 1, "recto_ocr": "", "verso_ocr": ""})
    r = cli("ocr", "1", conf="ocr_engine = paddleocr\n")           # depuis la configuration
    assert r.exit_code == 0, r.output + r.stderr
    assert Model(cli.datadir).load_json(1)["verso_ocr"] == "Vue générale de la Rue"

    r = cli("ocr", "1", "--ocr-engine", "tesseract", conf="ocr_engine = paddleocr\n")
    assert r.exit_code == 0
    assert Model(cli.datadir).load_json(1)["recto_ocr"] == "texte tesseract"   # l'option l'emporte


def test_cli_tells_the_user_when_the_server_lacks_the_engine(cli, monkeypatch):
    monkeypatch.setattr(api, "PADDLE_AVAILABLE", False)
    r = cli("ocr", "1", "--ocr-engine", "paddleocr")
    assert r.exit_code != 0
    assert "paddleocr is not available on this server" in r.stderr
    assert "pip install pypostcards[paddleocr]" in r.stderr


def test_cli_language_unavailable_for_the_chosen_engine(cli):
    r = cli("ocr", "1", "--ocr-engine", "paddleocr", "--ocr-langs", "fra+klingon")
    assert r.exit_code != 0
    assert "klingon" in r.stderr and "PaddleOCR" in r.stderr


def test_cli_invalid_configuration(cli):
    r = cli("ocr", "1", conf="ocr_engine = abbyy\n")
    assert r.exit_code != 0 and "unknown OCR engine" in r.stderr
    r = cli("ocr", "1", conf="ocr_min_score = lots\n")
    assert r.exit_code != 0 and "ocr_min_score" in r.stderr


def test_add_pairs_never_silently_skips_a_missing_paddleocr(tmp_path, monkeypatch):
    """Tesseract absent = OCR ignoré (historique) ; PaddleOCR choisi mais absent
    = erreur claire AVANT de copier le moindre fichier."""
    from tkpostcards.libs.scan_add import add_pairs
    monkeypatch.setattr(ocr_paddle, "PADDLE_AVAILABLE", False)
    with pytest.raises(langs.EngineUnavailable, match="pip install pypostcards"):
        add_pairs(tmp_path, tmp_path, [], ocr_engine="paddleocr", use_searcher=False)


# ── périphérique : pas de GPU sur une build CPU de PaddlePaddle ─────────────

def _fake_paddle(monkeypatch, cuda=False, rocm=False):
    module = types.ModuleType("paddle")
    module.is_compiled_with_cuda = lambda: cuda
    module.is_compiled_with_rocm = lambda: rocm
    monkeypatch.setitem(sys.modules, "paddle", module)


def test_gpu_device_on_a_cpu_only_paddle_says_so(monkeypatch):
    """Les roues pip de PaddlePaddle n'ont pas de build AMD : sans ce contrôle
    l'erreur ressemblait à un problème de réseau ("could not load its models")."""
    _fake_paddle(monkeypatch)
    for device in ("gpu:0", "gpu", "cuda", "rocm"):
        with pytest.raises(langs.EngineUnavailable) as err:
            ocr_paddle.create_engine("fra", device=device)
        assert "built without GPU support" in str(err.value)
        assert "AMD ROCm" in str(err.value) and "paddleocr_device = cpu" in str(err.value)
        assert "network" not in str(err.value)
    assert FakePaddle.instances == []                       # le moteur n'a pas été construit


def test_cpu_and_gpu_builds_are_accepted(monkeypatch):
    _fake_paddle(monkeypatch)
    ocr_paddle.create_engine("fra", device="cpu")           # toujours possible
    ocr_paddle.create_engine("fra")                         # choix de PaddlePaddle
    _fake_paddle(monkeypatch, cuda=True)
    ocr_paddle.create_engine("fra", device="gpu:0")         # build NVIDIA
    _fake_paddle(monkeypatch, rocm=True)
    ocr_paddle.create_engine("fra", device="gpu:0")         # build AMD compilée à part
    assert len(FakePaddle.instances) == 4


def test_server_reports_a_gpu_request_on_a_cpu_build(server, monkeypatch):
    _fake_paddle(monkeypatch)
    server.app.config["PADDLEOCR_DEVICE"] = "gpu:0"
    pb._models._engines.clear()
    r = post(server, engine="paddleocr", lang="fra")
    assert r.status_code == 501 and r.get_json()["code"] == "engine_unavailable"
    assert "built without GPU support" in r.get_json()["error"]
