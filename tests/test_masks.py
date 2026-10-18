# -*- encoding: utf-8 -*-
"""Tests de libpostcards.masks (SAM remplacé par un détecteur factice)."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from libpostcards import masks as M

W, H = 3200, 2200


@pytest.fixture
def datadir(tmp_path):
    (tmp_path / "cards").mkdir()
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([100, 100, 3100, 2100], fill=(180, 160, 120, 255))
    d.rectangle([2650, 150, 2950, 520], fill=(200, 50, 50, 255))   # timbre
    d.rectangle([900, 900, 1300, 1300], fill=(30, 90, 160, 255))   # fenêtre
    for pid in ("1", "2"):
        im.save(tmp_path / "cards" / f"{pid}_R.tiff", compression="tiff_deflate")
    return tmp_path


class FakeDetector:
    def segment(self, rgb):
        h, w = rgb.shape[:2]
        s = w / W

        def box(x0, y0, x1, y1):
            m = np.zeros((h, w), bool)
            m[int(y0 * s):int(y1 * s), int(x0 * s):int(x1 * s)] = True
            return m
        return [box(2650, 150, 2950, 520),      # timbre
                box(900, 900, 1300, 1300),      # fenêtre au centre : à garder
                box(2700, 200, 2900, 400),      # dans le timbre : doublon
                box(100, 100, 3100, 2100)]      # toute la carte : trop grand


def test_stamp_masked_window_kept(datadir):
    mask, info = M.generate_mask(datadir / "cards" / "1_R.tiff", FakeDetector())
    s = mask.shape[1] / W
    at = lambda x, y: mask[int(y * s), int(x * s)]
    assert info["stamps"] == 1
    assert at(2800, 300) == 0          # timbre
    assert at(30, 30) == 0             # fond transparent
    assert at(1100, 1100) == 255       # fenêtre conservée
    assert at(1500, 1500) == 255


def test_manual_masks_are_protected(datadir):
    store = M.MaskStore(datadir)
    det = FakeDetector()
    assert M.generate_masks(datadir, [1, 2], detector=det)["done"] == 2
    assert store.source("1") == "auto"
    store.write("2", store.read("2"), "manual")
    counts = M.generate_masks(datadir, [1, 2], force=True, detector=det)
    assert counts["done"] == 1 and counts["skipped"] == 1
    assert store.source("2") == "manual"
    M.generate_masks(datadir, [2], force_manual=True, detector=det)
    assert store.source("2") == "auto"


def test_missing_image_does_not_stop_batch(datadir):
    counts = M.generate_masks(datadir, [1, 99, 2], use_sam=False)
    assert counts == {"done": 2, "skipped": 0, "missing": 1, "error": 0}


def test_export_colmap_layout(datadir, tmp_path):
    M.generate_masks(datadir, [1], detector=FakeDetector())
    out = tmp_path / "out"
    exported, missing = M.export_colmap(datadir, [1, 2, 7], out, max_side=800)
    assert exported == 2 and missing == ["7"]
    img = Image.open(out / "images" / "1_R.png")
    msk = Image.open(out / "masks" / "1_R.png.png")    # <image>.png : convention COLMAP
    assert img.size == msk.size and msk.mode == "L"
    assert Image.open(out / "masks" / "2_R.png.png").getextrema() == (255, 255)


def test_params_from_config():
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_string("[masks]\nstamp_dilate = 0.02\nmax_stamps = 5\nsam_model = x/y\n")
    p = M.MaskParams.from_config(cfg)
    assert p.stamp_dilate == 0.02 and p.max_stamps == 5
    assert p.border_margin == M.MaskParams().border_margin
    assert M.sam_model_from_config(cfg) == "x/y"


# ── Transport + external_helper ─────────────────────────────────────────────

def test_pack_unpack_roundtrip():
    rng = np.random.default_rng(0)
    m = rng.random((37, 53)) > 0.5
    assert np.array_equal(M.unpack_mask(M.pack_mask(m), m.shape), m)
    with pytest.raises(ValueError):
        M.unpack_mask(M.pack_mask(m), (100, 100))


def test_remote_detector_posts_and_decodes(monkeypatch, datadir):
    """StampDetector(external_helper=...) appelle /api/compute_segments et
    décode les segments, sans jamais charger torch."""
    import requests
    seen = {}

    class Resp:
        status_code = 200
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def fake_post(url, files, data, timeout):
        seen.update(url=url, data=data, timeout=timeout)
        h, w = np.asarray(Image.open(files["image"][1])).shape[:2]
        m = np.zeros((h, w), bool)
        m[int(h * .07):int(h * .24), int(w * .83):int(w * .92)] = True   # timbre
        return Resp({"width": w, "height": h,
                     "segments": [{"score": .9, "mask": M.pack_mask(m)}]})

    monkeypatch.setattr(requests, "post", fake_post)
    det = M.StampDetector(external_helper="http://helper:8002/", external_helper_timeout=7)
    mask, info = M.generate_mask(datadir / "cards" / "1_R.tiff", det)
    assert seen["url"] == "http://helper:8002/api/compute_segments"
    assert seen["timeout"] == 7 and seen["data"]["points_per_side"] == 20
    assert info["stamps"] == 1


def test_remote_detector_rejects_wrong_size(monkeypatch):
    import requests

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"width": 5, "height": 5, "segments": []}

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    det = M.StampDetector(external_helper="http://h")
    with pytest.raises(RuntimeError):
        det.segment(np.zeros((10, 10, 3), np.uint8))


def test_remote_detector_501_message(monkeypatch):
    import requests

    class Resp:
        status_code = 501
        def json(self): return {"error": "no sam here"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    with pytest.raises(RuntimeError, match="no sam here"):
        M.StampDetector(external_helper="http://h").segment(np.zeros((10, 10, 3), np.uint8))
