# -*- encoding: utf-8 -*-
"""
libpostcards.masks
------------------

Masques de postcards pour la photogrammétrie (COLMAP / pycolmap).

Un masque est une image 8 bits à un canal : **255 = zone utilisable**,
**0 = zone à ignorer** (timbre, cachet, fond transparent, liseré du bord).
C'est la convention de COLMAP : aucun point d'intérêt n'est détecté dans
les zones à 0.

Stockage : ``<datadir>/masks/<id>_<R|V>.png``, plus un petit fichier
``<id>_<R|V>.json`` qui mémorise l'origine du masque :

- ``"auto"``   : produit par :func:`generate_masks` (SAM + heuristiques) ;
  peut être régénéré ;
- ``"manual"`` : sauvegardé après retouche dans ktmanager ; jamais écrasé
  par une régénération, sauf ``force_manual=True``.

Les masques sont stockés à une résolution de travail (``max_side``,
1600 px par défaut sur le plus grand côté) et non à celle des TIFF :
c'est largement suffisant pour des zones aussi grossières qu'un timbre,
et cela rend la retouche instantanée. :func:`export_colmap` les
redimensionne à la taille des images exportées.

Les dépendances lourdes (numpy, cv2, torch, transformers) sont importées
localement, comme dans libpostcards.detection : le module s'importe sans
elles, et seules les fonctions qui en ont besoin les exigent (extra
``masks`` de pyproject.toml). La retouche (PIL seul) n'en a pas besoin.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, fields
from pathlib import Path

MASK_DIRNAME = "masks"
DEFAULT_SAM_MODEL = "facebook/sam-vit-base"
_IMAGE_EXTS = ("tiff", "tif", "png", "jpg", "jpeg")


# ── Paramètres ──────────────────────────────────────────────────────────────

@dataclass
class MaskParams:
    """Réglages de la génération automatique.

    Les surfaces et distances sont des *fractions* (de la surface de la
    carte, ou de son plus grand côté) pour ne pas dépendre de la
    résolution du scan.
    """
    max_side: int = 1600
    # Fond transparent : pixels dont l'alpha est <= à ce seuil.
    alpha_threshold: int = 16
    # Bande ignorée le long du bord de la carte (liseré blanc, coins usés).
    border_margin: float = 0.004
    # Timbre : surface (fraction de la carte) entre ces deux bornes...
    stamp_min_area: float = 0.004
    stamp_max_area: float = 0.12
    # ...segment proche d'un rectangle (surface / surface du rectangle
    # englobant orienté) et pas trop allongé...
    stamp_min_fill: float = 0.80
    stamp_max_aspect: float = 2.0
    # ...et dont la boîte englobante frôle un bord de la carte (distance
    # <= cette fraction de la largeur/hauteur de la carte).
    stamp_edge_zone: float = 0.15
    # Le masque de timbre est agrandi de cette fraction du grand côté
    # pour couvrir la dentelure, l'ombre et le cachet qui déborde.
    stamp_dilate: float = 0.006
    max_stamps: int = 3
    # SAM : nombre de points par côté de la grille de prompts (32 =
    # défaut de SAM ; 20 suffit pour des objets de la taille d'un timbre
    # et va ~2,5x plus vite), seuils de qualité des segments.
    sam_points_per_side: int = 20
    sam_pred_iou_thresh: float = 0.86
    sam_stability_thresh: float = 0.90

    @classmethod
    def from_config(cls, cfg, section="masks"):
        """Lit les réglages dans ``[masks]`` d'un ConfigParser ; les
        clés absentes gardent leur valeur par défaut."""
        params = cls()
        if cfg is None or not cfg.has_section(section):
            return params
        for f in fields(cls):
            if not cfg.has_option(section, f.name):
                continue
            raw = cfg.get(section, f.name).strip()
            if raw:
                setattr(params, f.name, type(getattr(params, f.name))(raw))
        return params


def sam_model_from_config(cfg, section="masks"):
    if cfg is not None and cfg.has_option(section, "sam_model"):
        return cfg.get(section, "sam_model").strip() or DEFAULT_SAM_MODEL
    return DEFAULT_SAM_MODEL


# ── Chargement d'image (PIL seul, utilisé aussi par l'éditeur Tk) ──────────

def load_working_pil(path, max_side=1600):
    """Ouvre ``path`` et renvoie ``(rgba, taille_originale)`` ; ``rgba`` est
    une image PIL RGBA réduite pour que son grand côté vaille au plus
    ``max_side`` (jamais agrandie)."""
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        orig_size = im.size
        rgba = im.convert("RGBA")
    scale = min(1.0, max_side / max(rgba.size))
    if scale < 1.0:
        size = (max(1, round(rgba.width * scale)),
                max(1, round(rgba.height * scale)))
        rgba = rgba.resize(size, Image.LANCZOS)
    return rgba, orig_size


def load_working_image(path, max_side=1600):
    """Comme :func:`load_working_pil`, en tableaux numpy
    ``(rgb HxWx3, alpha HxW, taille_originale)``."""
    import numpy as np
    rgba, orig_size = load_working_pil(path, max_side)
    arr = np.asarray(rgba)
    return arr[..., :3].copy(), arr[..., 3].copy(), orig_size


# ── Transport des segments (client <-> POST /api/compute_segments) ─────────

def pack_mask(mask):
    """Masque booléen HxW -> texte ASCII compact (bits empaquetés, zlib,
    base64). Un segment SAM est une grande zone d'un seul tenant : il
    se réduit à quelques Ko."""
    import base64
    import zlib
    import numpy as np
    bits = np.packbits(np.asarray(mask, dtype=bool).ravel())
    return base64.b64encode(zlib.compress(bits.tobytes(), 6)).decode("ascii")


def unpack_mask(data, shape):
    """Inverse de :func:`pack_mask` ; ``shape`` = (hauteur, largeur)."""
    import base64
    import zlib
    import numpy as np
    raw = zlib.decompress(base64.b64decode(data))
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))
    n = shape[0] * shape[1]
    if bits.size < n:
        raise ValueError("segment tronqué")
    return bits[:n].astype(bool).reshape(shape)


# ── Détection des timbres ───────────────────────────────────────────────────

class StampDetector:
    """Segmente l'image avec SAM (pipeline ``mask-generation`` de
    transformers, donc sans le paquet ``segment-anything``). Le modèle
    est chargé à la première utilisation.

    Si ``external_helper`` (URL d'un serveur simpostcards, voir
    [DEFAULT] external_helper dans postcards.conf) est renseigné, la
    segmentation est déléguée à ``POST /api/compute_segments`` : ni
    torch ni transformers ne sont alors nécessaires ici (``model`` et
    ``device`` sont ceux du serveur, voir [simpostcards] segment_model_name
    / segment_device). Le choix du timbre parmi les segments reste
    toujours fait localement (:func:`stamp_candidates`), pour que les
    seuils de ``[masks]`` s'appliquent identiquement dans les deux cas.
    """

    def __init__(self, model=DEFAULT_SAM_MODEL, device=None, params=None,
                 external_helper=None, external_helper_timeout=None):
        self.model = model
        self.device = device
        self.params = params or MaskParams()
        self.external_helper = (external_helper or "").strip() or None
        # SAM sur le CPU d'un serveur modeste peut prendre un moment.
        self.external_helper_timeout = (
            120.0 if external_helper_timeout is None
            else external_helper_timeout)
        self._pipe = None

    def _ensure_pipe(self):
        if self._pipe is not None:
            return self._pipe
        import torch
        from transformers import pipeline
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._pipe = pipeline("mask-generation", model=self.model, device=device)
        return self._pipe

    def _segment_remote(self, rgb):
        """Segmentation déléguée à ``{external_helper}/api/compute_segments``."""
        import io
        import requests
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        buf.seek(0)
        p = self.params
        response = requests.post(
            f"{self.external_helper.rstrip('/')}/api/compute_segments",
            files={"image": ("image.png", buf, "image/png")},
            data={
                "points_per_side": p.sam_points_per_side,
                "pred_iou_thresh": p.sam_pred_iou_thresh,
                "stability_thresh": p.sam_stability_thresh,
            },
            timeout=self.external_helper_timeout,
        )
        if response.status_code == 501:
            try:
                detail = response.json().get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(
                "external_helper cannot compute segments: %s" % detail)
        response.raise_for_status()
        payload = response.json()
        shape = rgb.shape[:2]
        if (payload.get("height"), payload.get("width")) != shape:
            raise RuntimeError(
                "external_helper returned segments for a %sx%s image, "
                "expected %sx%s" % (payload.get("width"), payload.get("height"),
                                    shape[1], shape[0]))
        return [unpack_mask(seg["mask"], shape) for seg in payload["segments"]]

    def segment(self, rgb):
        """Renvoie la liste des segments SAM (tableaux booléens HxW)."""
        if self.external_helper:
            return self._segment_remote(rgb)
        import numpy as np
        from PIL import Image
        p = self.params
        out = self._ensure_pipe()(
            Image.fromarray(rgb),
            points_per_batch=64,
            points_per_crop=p.sam_points_per_side,
            pred_iou_thresh=p.sam_pred_iou_thresh,
            stability_score_thresh=p.sam_stability_thresh,
        )
        return [np.asarray(m, dtype=bool) for m in out["masks"]]

    def unload(self):
        self._pipe = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def card_region(alpha, params):
    """Pixels de la carte (alpha au-dessus du seuil)."""
    return alpha > params.alpha_threshold


def stamp_candidates(segments, card, params):
    """Choisit parmi les segments SAM ceux qui ressemblent à un timbre :
    rectangulaire, de taille raisonnable, collé à un bord de la carte.

    Quand plusieurs segments se recouvrent (le timbre entier, l'image à
    l'intérieur, le chiffre de la valeur faciale...), on garde le plus
    grand : celui qui couvre le timbre entier.
    """
    import cv2
    import numpy as np

    ys, xs = np.nonzero(card)
    if len(xs) == 0:
        return []
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    cw, ch = x1 - x0, y1 - y0
    card_area = float(card.sum())

    found = []
    for seg in segments:
        seg = np.asarray(seg, dtype=bool) & card
        area = float(seg.sum())
        if not (params.stamp_min_area
                <= area / card_area <= params.stamp_max_area):
            continue
        contours, _ = cv2.findContours(
            seg.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        (_cx, _cy), (rw, rh), _angle = cv2.minAreaRect(cnt)
        if min(rw, rh) < 1:
            continue
        fill = area / (rw * rh)
        aspect = max(rw, rh) / min(rw, rh)
        if fill < params.stamp_min_fill or aspect > params.stamp_max_aspect:
            continue
        bx, by, bw, bh = cv2.boundingRect(cnt)
        dx = min(bx - x0, x1 - (bx + bw)) / cw
        dy = min(by - y0, y1 - (by + bh)) / ch
        if min(dx, dy) > params.stamp_edge_zone:
            continue
        found.append((area, fill, seg))

    kept = []
    for area, fill, seg in sorted(found, key=lambda t: -t[0]):
        if any((seg & k).sum() / area >= 0.5 for _a, _f, k in kept):
            continue
        kept.append((area, fill, seg))
    kept.sort(key=lambda t: -t[1])
    return [seg for _a, _f, seg in kept[:params.max_stamps]]


def compose_mask(card, stamps, params):
    """Masque final uint8 (255 = garder) : carte moins le liseré moins les
    timbres agrandis."""
    import cv2
    import numpy as np

    longest = max(card.shape)
    margin = round(params.border_margin * longest)
    keep = card.astype(np.uint8)
    if margin > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (2 * margin + 1, 2 * margin + 1))
        keep = cv2.erode(keep, kernel, borderType=cv2.BORDER_CONSTANT,
                         borderValue=0)
    keep = keep.astype(bool)

    if stamps:
        excluded = np.zeros(card.shape, dtype=np.uint8)
        for s in stamps:
            excluded |= s.astype(np.uint8)
        grow = round(params.stamp_dilate * longest)
        if grow > 0:
            kernel = cv2.getStructuringElement(
                cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
            excluded = cv2.dilate(excluded, kernel)
        keep &= ~excluded.astype(bool)
    return keep.astype(np.uint8) * 255


def generate_mask(card_path, detector=None, params=None):
    """Masque automatique d'une image. Sans ``detector``, seuls le fond
    transparent et le liseré sont masqués (pas de détection de timbre).

    Renvoie ``(mask uint8 HxW, info)``.
    """
    params = params or MaskParams()
    rgb, alpha, orig_size = load_working_image(card_path, params.max_side)
    card = card_region(alpha, params)
    stamps = []
    if detector is not None:
        stamps = stamp_candidates(detector.segment(rgb), card, params)
    mask = compose_mask(card, stamps, params)
    return mask, {"stamps": len(stamps), "orig_size": list(orig_size)}


# ── Stockage ────────────────────────────────────────────────────────────────

class MaskStore:
    """Accès aux masques d'une collection de données (``datadir``)."""

    def __init__(self, datadir, file_format="tiff", side="R"):
        self.datadir = Path(datadir)
        self.file_format = file_format
        self.side = side
        self.dir = self.datadir / MASK_DIRNAME

    def card_path(self, pid):
        """Chemin de l'image de la carte, ou None si introuvable."""
        base = self.datadir / "cards" / f"{pid}_{self.side}"
        exts = (self.file_format,) + tuple(
            e for e in _IMAGE_EXTS if e != self.file_format)
        for ext in exts:
            p = base.with_suffix("." + ext)
            if p.is_file():
                return p
        return None

    def mask_path(self, pid):
        return self.dir / f"{pid}_{self.side}.png"

    def _meta_path(self, pid):
        return self.dir / f"{pid}_{self.side}.json"

    def has_mask(self, pid):
        return self.mask_path(pid).is_file()

    def source(self, pid):
        """``"auto"``, ``"manual"`` ou None (pas de masque)."""
        if not self.has_mask(pid):
            return None
        try:
            with open(self._meta_path(pid), encoding="utf-8") as f:
                return json.load(f).get("source", "manual")
        except (OSError, ValueError):
            return "manual"  # masque sans fiche : on le protège

    def read(self, pid):
        """Masque en image PIL ``L``, ou None."""
        from PIL import Image
        path = self.mask_path(pid)
        if not path.is_file():
            return None
        with Image.open(path) as im:
            return im.convert("L")

    def write(self, pid, mask, source, info=None):
        """Enregistre ``mask`` (PIL ``L`` ou tableau uint8) et sa fiche."""
        from PIL import Image
        if not isinstance(mask, Image.Image):
            mask = Image.fromarray(mask, "L")
        self.dir.mkdir(parents=True, exist_ok=True)
        mask.convert("L").save(self.mask_path(pid), optimize=True)
        meta = {"source": source, "time": int(time.time())}
        meta.update(info or {})
        with open(self._meta_path(pid), "w", encoding="utf-8") as f:
            json.dump(meta, f)

    def delete(self, pid):
        for p in (self.mask_path(pid), self._meta_path(pid)):
            if p.is_file():
                p.unlink()


# ── Génération par lot ──────────────────────────────────────────────────────

def generate_masks(
    datadir, ids, *, file_format="tiff", side="R", force=False,
    force_manual=False, use_sam=True, model=DEFAULT_SAM_MODEL, device=None,
    params=None, detector=None, on_progress=None, should_stop=None,
    external_helper=None, external_helper_timeout=None,
):
    """Génère les masques automatiques de ``ids``.

    ``on_progress(i, total, pid, status, info)`` est appelé pour chaque
    carte ; ``status`` vaut ``"done"``, ``"skipped"``, ``"missing"``
    (image introuvable) ou ``"error"``. ``should_stop()`` permet
    d'interrompre proprement (utilisé par l'interface graphique).

    ``external_helper`` / ``external_helper_timeout`` : délèguent SAM à un
    serveur simpostcards (voir :class:`StampDetector`) ; sans effet si un
    ``detector`` est fourni.

    Un masque ``manual`` n'est jamais écrasé sans ``force_manual``, et un
    masque ``auto`` n'est régénéré qu'avec ``force``.

    Renvoie un dictionnaire de compteurs par statut.
    """
    params = params or MaskParams()
    store = MaskStore(datadir, file_format, side)
    ids = [str(i) for i in ids]
    if use_sam and detector is None:
        detector = StampDetector(model, device, params, external_helper,
                                 external_helper_timeout)
    if not use_sam:
        detector = None

    counts = {"done": 0, "skipped": 0, "missing": 0, "error": 0}
    for i, pid in enumerate(ids, 1):
        if should_stop is not None and should_stop():
            break
        src = store.source(pid)
        if src == "manual" and not force_manual:
            status, info = "skipped", {"reason": "manual"}
        elif src == "auto" and not (force or force_manual):
            status, info = "skipped", {"reason": "exists"}
        else:
            path = store.card_path(pid)
            if path is None:
                status, info = "missing", {}
            else:
                try:
                    mask, info = generate_mask(path, detector, params)
                    store.write(pid, mask, "auto", {
                        "stamps": info["stamps"], "sam": bool(use_sam)})
                    status = "done"
                except Exception as exc:  # une carte en échec n'arrête pas le lot
                    status, info = "error", {"error": str(exc)}
        counts[status] += 1
        if on_progress is not None:
            on_progress(i, len(ids), pid, status, info)
    return counts


# ── Export pour COLMAP ──────────────────────────────────────────────────────

def export_colmap(
    datadir, ids, out_dir, *, file_format="tiff", side="R", max_side=2000,
    on_progress=None,
):
    """Prépare un dossier directement utilisable par pycolmap :

    - ``out_dir/images/<id>_R.png``      : image RGB (fond transparent
      aplati sur un gris neutre, de toute façon masqué) ;
    - ``out_dir/masks/<id>_R.png.png``   : masque à la taille de l'image
      (COLMAP cherche ``<image>.png`` dans ``mask_path``).

    Une carte sans masque reçoit un masque tout blanc. Renvoie le nombre
    de cartes exportées et la liste de celles dont l'image est introuvable.
    """
    from PIL import Image

    store = MaskStore(datadir, file_format, side)
    out_dir = Path(out_dir)
    img_dir, msk_dir = out_dir / "images", out_dir / "masks"
    img_dir.mkdir(parents=True, exist_ok=True)
    msk_dir.mkdir(parents=True, exist_ok=True)

    exported, missing = 0, []
    ids = [str(i) for i in ids]
    for i, pid in enumerate(ids, 1):
        path = store.card_path(pid)
        if path is None:
            missing.append(pid)
        else:
            rgba, _orig = load_working_pil(path, max_side)
            flat = Image.new("RGB", rgba.size, (128, 128, 128))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            name = f"{pid}_{side}.png"
            flat.save(img_dir / name)

            mask = store.read(pid)
            if mask is None:
                mask = Image.new("L", flat.size, 255)
            else:
                mask = mask.resize(flat.size, Image.BILINEAR).point(
                    lambda v: 255 if v > 127 else 0)
            mask.save(msk_dir / (name + ".png"))
            exported += 1
        if on_progress is not None:
            on_progress(i, len(ids), pid)
    return exported, missing
