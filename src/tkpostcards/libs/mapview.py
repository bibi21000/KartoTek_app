# -*- encoding: utf-8 -*-
"""
tkpostcards.libs.mapview - Widget Tkinter de carte interactive ("slippy map").

Fournit :

- des "providers" de tuiles raster (OpenStreetMap, Google Maps) ;
- ``SlippyMapCanvas``, un ``tk.Frame`` autonome affichant ces tuiles avec
  zoom (molette / boutons) et déplacement (glisser-déposer sur le fond de
  carte), ainsi que des marqueurs colorés cliquables et, pour certains
  d'entre eux, déplaçables à la souris.

Ce module ne dépend que de la bibliothèque standard et de Pillow (déjà une
dépendance du projet) : aucune bibliothèque de cartographie tierce n'est
nécessaire. Les tuiles sont téléchargées à la demande dans un thread
d'arrière-plan (jamais d'appel Tk hors du thread principal, cf.
``_fetch_tile_worker`` / ``_poll_tiles``) et mises en cache sur disque pour
limiter les requêtes réseau lors des réouvertures ultérieures.

Fournisseur Google Maps
------------------------
Il n'existe pas d'API Google légère et gratuite fournissant des tuiles
raster brutes pour un widget de bureau comme celui-ci (l'API JavaScript
Maps est prévue pour un navigateur, l'API Static Maps facture l'image et
ne permet pas le zoom/déplacement interactif). Lorsqu'une clé
``google_maps_api_key`` est configurée, ce module utilise donc le point
d'accès de tuiles ``mt{0-3}.google.com/vt`` (le même que celui utilisé en
coulisse par de nombreux outils de bureau/mobiles non officiels) ; la clé
API n'est pas transmise à ce point d'accès (il n'en a pas besoin) mais sa
présence en configuration sert d'indicateur explicite du choix de
l'utilisateur d'utiliser Google Maps plutôt qu'OpenStreetMap. Sans clé
configurée, OpenStreetMap est utilisé.
"""

from __future__ import annotations

import math
import queue
import threading
import tkinter as tk
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageTk

TILE_SIZE = 256

# Latitude maximale représentable par la projection de Mercator utilisée
# par les tuiles slippy-map (au-delà, y diverge vers l'infini).
_MAX_LAT = 85.05112878

_CACHE_DIR = Path.home() / ".cache" / "tkpostcards" / "tiles"


# ─────────────────────────────────────────────────────────────────────────────
#  Fournisseurs de tuiles
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TileProvider:
    name: str
    max_zoom: int
    user_agent: str
    url_fn: Callable[[int, int, int], str]

    def tile_url(self, z: int, x: int, y: int) -> str:
        return self.url_fn(z, x, y)


def osm_provider() -> TileProvider:
    """OpenStreetMap (politique d'usage des tuiles : User-Agent identifiable,
    cf. https://operations.osmfoundation.org/policies/tiles/)."""
    return TileProvider(
        name="osm",
        max_zoom=19,
        user_agent="pypostcards-tkmanager/1.0 (+https://github.com/bibi21000/pypostcards)",
        url_fn=lambda z, x, y: f"https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    )


def google_provider(api_key: str = "") -> TileProvider:
    """Tuiles routières Google Maps (cf. note en tête de module)."""
    def url_fn(z: int, x: int, y: int) -> str:
        sub = (x + y) % 4
        return f"https://mt{sub}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}"
    return TileProvider(
        name="google",
        max_zoom=20,
        user_agent="Mozilla/5.0 (compatible; pypostcards-tkmanager/1.0)",
        url_fn=url_fn,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Chargement des tuiles (thread d'arrière-plan uniquement — pas d'appel Tk)
# ─────────────────────────────────────────────────────────────────────────────
def _disk_cache_path(provider: TileProvider, z: int, x: int, y: int) -> Path:
    return _CACHE_DIR / provider.name / str(z) / str(x) / f"{y}.png"


def _load_tile_image(provider: TileProvider, z: int, x: int, y: int) -> Optional["Image.Image"]:
    """Charge une tuile (cache disque, puis réseau). Retourne None en cas
    d'échec (tuile hors bornes, pas de réseau...). Aucun appel Tk ici."""
    n = 2 ** z
    if x < 0 or y < 0 or x >= n or y >= n:
        return None

    cache_path = _disk_cache_path(provider, z, x, y)
    try:
        if cache_path.exists():
            with Image.open(cache_path) as im:
                return im.convert("RGB")
    except Exception:
        pass

    url = provider.tile_url(z, x, y)
    req = urllib.request.Request(url, headers={"User-Agent": provider.user_agent})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        img = Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        return None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(cache_path, "PNG")
    except Exception:
        pass

    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Conversions géographiques <-> pixels "monde" (projection Web Mercator)
# ─────────────────────────────────────────────────────────────────────────────
def lonlat_to_world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, _MAX_LAT), -_MAX_LAT)
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def world_px_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    lon = x / (n * TILE_SIZE) * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / (n * TILE_SIZE))))
    lat = math.degrees(lat_rad)
    return lat, lon


# ─────────────────────────────────────────────────────────────────────────────
#  Widget principal
# ─────────────────────────────────────────────────────────────────────────────
class SlippyMapCanvas(tk.Frame):
    """
    Carte interactive minimaliste (zoom + déplacement + marqueurs).

    Marqueurs : ``set_markers()`` prend une liste de dicts
    ``{"id", "lat", "lon", "kind", "color", "label"?, "radius"?}``.
    ``kind`` vaut ``"postcard"`` (déplaçable à la souris) ou tout autre
    valeur (par convention ``"poi"``, non déplaçable).

    Callbacks (tous optionnels) :
    - ``on_marker_click(marker_id)`` : clic simple sur un marqueur "postcard".
    - ``on_marker_drop(marker_id, lat, lon)`` : glisser-déposer terminé sur
      un marqueur "postcard" (nouvelle position, pas encore persistée).
    - ``on_poi_click(marker_id)`` : clic simple sur un marqueur d'un autre
      type (POI).
    - ``on_empty_click(lat, lon)`` : clic simple en dehors de tout marqueur.
    - ``on_marker_hover(marker_id, x_root, y_root)`` : le pointeur survole
      le marqueur ``marker_id`` (coordonnées écran, utiles pour positionner
      une infobulle) ; appelé avec ``marker_id=None`` quand le pointeur
      quitte un marqueur (et plus aucun autre n'est survolé).
    """

    MIN_ZOOM = 2
    MAX_ZOOM = 19
    TILE = TILE_SIZE
    _DRAG_THRESHOLD = 4     # px : au-delà, un clic est considéré comme un glisser
    _TILE_CACHE_LIMIT = 500

    def __init__(
        self,
        parent: tk.Widget,
        provider: TileProvider,
        *,
        bg: str = "#0f1626",
        on_marker_click: Optional[Callable[[str], None]] = None,
        on_marker_drop: Optional[Callable[[str, float, float], None]] = None,
        on_poi_click: Optional[Callable[[str], None]] = None,
        on_empty_click: Optional[Callable[[float, float], None]] = None,
        on_marker_hover: Optional[Callable[[Optional[str], int, int], None]] = None,
        **kw,
    ):
        super().__init__(parent, bg=bg, **kw)
        self.provider = provider

        self._on_marker_click = on_marker_click
        self._on_marker_drop = on_marker_drop
        self._on_poi_click = on_poi_click
        self._on_empty_click = on_empty_click
        self._on_marker_hover = on_marker_hover
        self._hover_marker_id: str | None = None

        self._zoom = 13
        self._center_lat = 0.0
        self._center_lon = 0.0
        self._markers: dict[str, dict] = {}

        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._tile_cache: dict[tuple, "ImageTk.PhotoImage"] = {}
        self._tile_cache_order: list[tuple] = []
        self._tile_pending: set[tuple] = set()
        self._tile_queue: "queue.Queue" = queue.Queue()
        self._gen = 0   # incrémenté à chaque changement de fournisseur/zoom

        self._drag_mode: str | None = None      # None | "pan" | "marker" | "marker_static"
        self._drag_marker_id: str | None = None
        self._drag_start_xy: tuple | None = None
        self._drag_last_xy: tuple | None = None
        self._drag_moved = False

        self._canvas.bind("<Configure>", lambda _e: self._layout(fetch=True))
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_motion)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Motion>", self._on_hover_motion)
        self._canvas.bind("<Leave>", self._on_hover_leave)
        self._canvas.bind("<MouseWheel>", self._on_wheel)     # Windows / macOS
        self._canvas.bind("<Button-4>", self._on_wheel)       # Linux (molette haut)
        self._canvas.bind("<Button-5>", self._on_wheel)       # Linux (molette bas)

        self.after(50, self._poll_tiles)

    # ── API publique ─────────────────────────────────────────────────────
    def set_provider(self, provider: TileProvider) -> None:
        self.provider = provider
        self._tile_cache.clear()
        self._tile_cache_order.clear()
        self._gen += 1
        self._layout(fetch=True)

    def set_view(self, lat: float, lon: float, zoom: int) -> None:
        self._center_lat, self._center_lon = lat, lon
        self._zoom = max(self.MIN_ZOOM, min(min(self.MAX_ZOOM, self.provider.max_zoom), zoom))
        self._layout(fetch=True)

    def fit_bounds(self, min_lat: float, min_lon: float,
                   max_lat: float, max_lon: float, padding: int = 40) -> None:
        self._canvas.update_idletasks()
        cw = max(self._canvas.winfo_width(), 100)
        ch = max(self._canvas.winfo_height(), 100)

        if min_lat == max_lat and min_lon == max_lon:
            self.set_view(min_lat, min_lon, 14)
            return

        max_zoom = min(self.MAX_ZOOM, self.provider.max_zoom)
        chosen = self.MIN_ZOOM
        for z in range(max_zoom, self.MIN_ZOOM - 1, -1):
            x0, y0 = lonlat_to_world_px(max_lat, min_lon, z)
            x1, y1 = lonlat_to_world_px(min_lat, max_lon, z)
            if (x1 - x0) <= (cw - 2 * padding) and (y1 - y0) <= (ch - 2 * padding):
                chosen = z
                break

        self.set_view((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0, chosen)

    def set_markers(self, markers: list[dict]) -> None:
        self._markers = {m["id"]: dict(m) for m in markers}
        self._redraw_markers()

    def update_marker(self, marker_id: str, lat: float | None = None,
                       lon: float | None = None) -> None:
        m = self._markers.get(marker_id)
        if not m:
            return
        if lat is not None:
            m["lat"] = lat
        if lon is not None:
            m["lon"] = lon
        self._redraw_markers()

    def zoom_in(self) -> None:
        cw = self._canvas.winfo_width() or 1
        ch = self._canvas.winfo_height() or 1
        self._zoom_at(cw / 2, ch / 2, 1)

    def zoom_out(self) -> None:
        cw = self._canvas.winfo_width() or 1
        ch = self._canvas.winfo_height() or 1
        self._zoom_at(cw / 2, ch / 2, -1)

    # ── Conversions relatives au canvas ─────────────────────────────────
    def _world_center(self) -> tuple[float, float]:
        return lonlat_to_world_px(self._center_lat, self._center_lon, self._zoom)

    def _canvas_to_lonlat(self, cx: float, cy: float) -> tuple[float, float]:
        cw = self._canvas.winfo_width() or 1
        ch = self._canvas.winfo_height() or 1
        wcx, wcy = self._world_center()
        wx = cx - cw / 2.0 + wcx
        wy = cy - ch / 2.0 + wcy
        return world_px_to_lonlat(wx, wy, self._zoom)

    # ── Rendu ────────────────────────────────────────────────────────────
    def _layout(self, fetch: bool = True) -> None:
        if not self.winfo_exists():
            return
        if fetch:
            self._clear_hover()
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return

        self._canvas.delete("tile")

        n = 2 ** self._zoom
        wcx, wcy = self._world_center()

        x_first = int((wcx - cw / 2.0) // self.TILE) - 1
        x_last = int((wcx + cw / 2.0) // self.TILE) + 1
        y_first = int((wcy - ch / 2.0) // self.TILE) - 1
        y_last = int((wcy + ch / 2.0) // self.TILE) + 1

        for ty in range(y_first, y_last + 1):
            if ty < 0 or ty >= n:
                continue
            for tx in range(x_first, x_last + 1):
                tx_wrapped = tx % n
                wx, wy = tx * self.TILE, ty * self.TILE
                cx, cy = wx - wcx + cw / 2.0, wy - wcy + ch / 2.0
                key = (self.provider.name, self._zoom, tx_wrapped, ty)
                photo = self._tile_cache.get(key)
                if photo is not None:
                    self._canvas.create_image(cx, cy, image=photo, anchor=tk.NW, tags=("tile",))
                else:
                    self._canvas.create_rectangle(
                        cx, cy, cx + self.TILE, cy + self.TILE,
                        fill="#13213a", outline="", tags=("tile",))
                    if fetch:
                        self._schedule_tile_fetch(self._zoom, tx_wrapped, ty)

        self._canvas.tag_lower("tile")
        self._redraw_markers()

    def _redraw_markers(self) -> None:
        if not self.winfo_exists():
            return
        self._canvas.delete("marker")
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        wcx, wcy = self._world_center()

        # Les POI sont dessinés en premier : les cartes postales (plus
        # importantes pour l'utilisateur) restent au-dessus visuellement.
        # Aucun texte n'est dessiné sur la carte : le titre (et, pour les
        # cartes postales, le recto) est affiché via une infobulle au
        # survol (cf. ``on_marker_hover``), pour ne pas surcharger la
        # carte visuellement.
        ordered = sorted(self._markers.values(),
                         key=lambda m: 0 if m.get("kind") != "postcard" else 1)
        for m in ordered:
            wx, wy = lonlat_to_world_px(m["lat"], m["lon"], self._zoom)
            cx, cy = wx - wcx + cw / 2.0, wy - wcy + ch / 2.0
            radius = m.get("radius", 7 if m.get("kind") == "postcard" else 5)
            color = m.get("color", "#e94560")
            tags = (f"mk:{m['id']}", "marker")
            self._canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                fill=color, outline="#ffffff", width=1.5, tags=tags)
        self._canvas.tag_raise("marker")

    # ── Tuiles asynchrones ───────────────────────────────────────────────
    def _schedule_tile_fetch(self, z: int, x: int, y: int) -> None:
        key = (self.provider.name, z, x, y)
        if key in self._tile_cache or key in self._tile_pending:
            return
        self._tile_pending.add(key)
        gen = self._gen
        provider = self.provider
        threading.Thread(
            target=self._fetch_tile_worker,
            args=(gen, provider, z, x, y, key),
            daemon=True,
        ).start()

    def _fetch_tile_worker(self, gen: int, provider: TileProvider,
                           z: int, x: int, y: int, key: tuple) -> None:
        """Thread d'arrière-plan : aucun appel Tk ici (PhotoImage doit être
        créée dans le thread principal, cf. ``_poll_tiles``)."""
        pil = _load_tile_image(provider, z, x, y)
        self._tile_queue.put((gen, key, pil))

    def _poll_tiles(self) -> None:
        if not self.winfo_exists():
            return
        placed_any = False
        try:
            while True:
                gen, key, pil = self._tile_queue.get_nowait()
                self._tile_pending.discard(key)
                if gen != self._gen or pil is None:
                    continue
                try:
                    photo = ImageTk.PhotoImage(pil)
                except Exception:
                    continue
                self._tile_cache[key] = photo
                self._tile_cache_order.append(key)
                if len(self._tile_cache_order) > self._TILE_CACHE_LIMIT:
                    old_key = self._tile_cache_order.pop(0)
                    self._tile_cache.pop(old_key, None)
                placed_any = True
        except queue.Empty:
            pass
        if placed_any:
            self._layout(fetch=False)
        self.after(60, self._poll_tiles)

    # ── Zoom ─────────────────────────────────────────────────────────────
    def _zoom_at(self, cx: float, cy: float, delta: int) -> None:
        max_zoom = min(self.MAX_ZOOM, self.provider.max_zoom)
        new_zoom = max(self.MIN_ZOOM, min(max_zoom, self._zoom + delta))
        if new_zoom == self._zoom:
            return
        lat, lon = self._canvas_to_lonlat(cx, cy)
        self._zoom = new_zoom
        cw = self._canvas.winfo_width() or 1
        ch = self._canvas.winfo_height() or 1
        target_wx, target_wy = lonlat_to_world_px(lat, lon, self._zoom)
        center_wx = target_wx - (cx - cw / 2.0)
        center_wy = target_wy - (cy - ch / 2.0)
        self._center_lat, self._center_lon = world_px_to_lonlat(center_wx, center_wy, self._zoom)
        self._layout(fetch=True)

    def _on_wheel(self, event) -> None:
        delta = 0
        if getattr(event, "delta", 0):
            delta = 1 if event.delta > 0 else -1
        elif getattr(event, "num", None) == 4:
            delta = 1
        elif getattr(event, "num", None) == 5:
            delta = -1
        if delta:
            self._zoom_at(event.x, event.y, delta)

    # ── Souris : sélection / glisser-déposer ────────────────────────────
    def _hit_marker(self, cx: float, cy: float) -> str | None:
        for item in reversed(self._canvas.find_overlapping(cx - 6, cy - 6, cx + 6, cy + 6)):
            for tag in self._canvas.gettags(item):
                if tag.startswith("mk:"):
                    return tag[3:]
        return None

    def _on_press(self, event) -> None:
        self._clear_hover()
        self._drag_start_xy = (event.x, event.y)
        self._drag_last_xy = (event.x, event.y)
        self._drag_moved = False

        marker_id = self._hit_marker(event.x, event.y)
        if marker_id is not None:
            kind = self._markers.get(marker_id, {}).get("kind")
            self._drag_marker_id = marker_id
            self._drag_mode = "marker" if kind == "postcard" else "marker_static"
        else:
            self._drag_marker_id = None
            self._drag_mode = "pan"

    def _on_motion(self, event) -> None:
        if self._drag_start_xy is None:
            return
        lx, ly = self._drag_last_xy
        dx, dy = event.x - lx, event.y - ly
        sx, sy = self._drag_start_xy
        if abs(event.x - sx) > self._DRAG_THRESHOLD or abs(event.y - sy) > self._DRAG_THRESHOLD:
            self._drag_moved = True

        if self._drag_mode == "pan":
            self._canvas.move("all", dx, dy)
        elif self._drag_mode == "marker":
            self._canvas.move(f"mk:{self._drag_marker_id}", dx, dy)

        self._drag_last_xy = (event.x, event.y)

    def _on_release(self, event) -> None:
        if self._drag_start_xy is None:
            return
        sx, sy = self._drag_start_xy
        mode, marker_id, moved = self._drag_mode, self._drag_marker_id, self._drag_moved
        total_dx, total_dy = event.x - sx, event.y - sy

        self._drag_mode = None
        self._drag_marker_id = None
        self._drag_start_xy = None
        self._drag_last_xy = None

        if mode == "pan":
            if moved:
                wcx, wcy = self._world_center()
                wcx -= total_dx
                wcy -= total_dy
                self._center_lat, self._center_lon = world_px_to_lonlat(wcx, wcy, self._zoom)
                self._layout(fetch=True)
            else:
                lat, lon = self._canvas_to_lonlat(event.x, event.y)
                if self._on_empty_click:
                    self._on_empty_click(lat, lon)

        elif mode == "marker":
            if moved:
                lat, lon = self._canvas_to_lonlat(event.x, event.y)
                m = self._markers.get(marker_id)
                if m is not None:
                    m["lat"], m["lon"] = lat, lon
                self._redraw_markers()
                if self._on_marker_drop:
                    self._on_marker_drop(marker_id, lat, lon)
            else:
                if self._on_marker_click:
                    self._on_marker_click(marker_id)

        elif mode == "marker_static":
            if not moved and self._on_poi_click:
                self._on_poi_click(marker_id)

    # ── Survol (infobulle) ───────────────────────────────────────────────
    def _clear_hover(self) -> None:
        if self._hover_marker_id is not None:
            self._hover_marker_id = None
            if self._on_marker_hover:
                self._on_marker_hover(None, 0, 0)

    def _on_hover_motion(self, event) -> None:
        # Un déplacement bouton enfoncé (pan / glisser un marqueur) déclenche
        # aussi <Motion> sur certaines plateformes : on l'ignore, il est déjà
        # géré par _on_motion, et l'infobulle a été masquée dans _on_press.
        if self._drag_start_xy is not None:
            return
        marker_id = self._hit_marker(event.x, event.y)
        if marker_id != self._hover_marker_id:
            self._hover_marker_id = marker_id
            if self._on_marker_hover:
                self._on_marker_hover(marker_id, event.x_root, event.y_root)

    def _on_hover_leave(self, _event) -> None:
        self._clear_hover()
