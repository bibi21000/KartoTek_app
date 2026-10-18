# -*- encoding: utf-8 -*-
"""POST /api/compute_segments (SAM remplacé par un faux)."""
import io

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("flask")
pytest.importorskip("imagehash")

import simpostcards.blueprints.api as api  # noqa: E402
from libpostcards.masks import unpack_mask  # noqa: E402
from simpostcards import create_app  # noqa: E402


def _png(w=80, h=60):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    conf = tmp_path / "postcards.conf"
    conf.write_text("[simpostcards]\nsegment_model_name = my/sam\n")
    seen = {}

    def fake(image, **kw):
        seen.update(kw, size=image.size)
        m = np.zeros((image.height, image.width), bool)
        m[5:20, 10:30] = True
        return [(0.9, m), (0.5, np.zeros_like(m))]

    monkeypatch.setattr(api, "SEGMENT_AVAILABLE", True)
    monkeypatch.setattr(api, "compute_segments", fake)
    c = create_app(conf).test_client()
    c.seen = seen
    return c


def test_segments_ok(client):
    r = client.post("/api/compute_segments",
                    data={"image": (io.BytesIO(_png()), "i.png"),
                          "points_per_side": "12"},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert (body["width"], body["height"]) == (80, 60)
    mask = unpack_mask(body["segments"][0]["mask"], (60, 80))
    assert mask.sum() == 15 * 20
    assert client.seen["points_per_side"] == 12
    assert client.seen["model_name"] == "my/sam"       # réglage serveur, pas requête


def test_segments_raw_body_and_bad_params(client):
    r = client.post("/api/compute_segments", data=_png(), content_type="image/png")
    assert r.status_code == 200
    for bad in ({"points_per_side": "x"}, {"points_per_side": "999"},
                {"pred_iou_thresh": "2"}):
        r = client.post("/api/compute_segments", data=_png(),
                        content_type="image/png", query_string=bad)
        assert r.status_code == 400


def test_segments_errors(client, monkeypatch):
    assert client.post("/api/compute_segments").status_code == 400
    r = client.post("/api/compute_segments", data=b"junk", content_type="image/png")
    assert r.status_code == 400
    monkeypatch.setattr(api, "SEGMENT_AVAILABLE", False)
    assert client.post("/api/compute_segments", data=_png(),
                       content_type="image/png").status_code == 501
