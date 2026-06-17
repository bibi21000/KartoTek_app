"""
Génération d'une image statique OpenStreetMap (assemblage de tuiles)
pour servir de og:image à la page /map/.

L'image est générée une seule fois à partir du paramètre de configuration
``osm_map`` (format ``zoom/latitude/longitude``, identique à l'ancre d'URL
openstreetmap.org), puis mise en cache sur disque. Si ``osm_map`` change,
le nom de fichier change aussi (hash de la valeur), ce qui invalide
naturellement le cache sans qu'il faille de logique de purge explicite.
"""

from __future__ import annotations

import hashlib
import math
import urllib.request
from pathlib import Path

from PIL import Image

TILE_SIZE = 256

# Dimensions visées pour l'image og:image (format recommandé ~1200x630)
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630

# Respecte la politique d'usage des tuiles OpenStreetMap : User-Agent
# identifiable. cf. https://operations.osmfoundation.org/policies/tiles/
USER_AGENT = "pypostcards/1.0 (+https://github.com/bibi21000/pypostcards)"

TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def parse_osm_map(value: str) -> tuple[int, float, float] | None:
    """
    Parse une valeur ``osm_map`` au format ``zoom/lat/lon``.

    Retourne (zoom, lat, lon) ou None si le format est invalide.
    """
    if not value:
        return None
    parts = value.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        zoom = int(parts[0])
        lat = float(parts[1])
        lon = float(parts[2])
    except ValueError:
        return None
    return zoom, lat, lon


def _lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Convertit lon/lat en coordonnées pixel absolues à un niveau de zoom donné."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    y = (
        1.0
        - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi
    ) / 2.0 * n * TILE_SIZE
    return x, y


def _fetch_tile(z: int, x: int, y: int) -> Image.Image | None:
    """Télécharge une tuile OSM, ou None en cas d'échec (tuile hors limites, réseau...)."""
    n = 2 ** z
    if x < 0 or y < 0 or x >= n or y >= n:
        return None

    url = TILE_URL_TEMPLATE.format(z=z, x=x, y=y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            from io import BytesIO

            return Image.open(BytesIO(resp.read())).convert("RGB")
    except Exception:
        return None


def render_static_map(
    zoom: int,
    lat: float,
    lon: float,
    width: int = OG_IMAGE_WIDTH,
    height: int = OG_IMAGE_HEIGHT,
) -> Image.Image:
    """
    Assemble les tuiles OSM nécessaires pour produire une image centrée
    sur (lat, lon) au niveau de zoom donné, de taille (width, height).
    """
    center_x, center_y = _lonlat_to_pixel(lon, lat, zoom)

    # Tuile contenant le coin haut-gauche de l'image cible
    origin_x = center_x - width / 2.0
    origin_y = center_y - height / 2.0

    first_tile_x = int(math.floor(origin_x / TILE_SIZE))
    first_tile_y = int(math.floor(origin_y / TILE_SIZE))

    tiles_needed_x = int(math.ceil((width + (origin_x - first_tile_x * TILE_SIZE)) / TILE_SIZE)) + 1
    tiles_needed_y = int(math.ceil((height + (origin_y - first_tile_y * TILE_SIZE)) / TILE_SIZE)) + 1

    canvas = Image.new("RGB", (tiles_needed_x * TILE_SIZE, tiles_needed_y * TILE_SIZE), "#dddddd")

    for ty in range(tiles_needed_y):
        for tx in range(tiles_needed_x):
            tile = _fetch_tile(zoom, first_tile_x + tx, first_tile_y + ty)
            if tile is not None:
                canvas.paste(tile, (tx * TILE_SIZE, ty * TILE_SIZE))

    crop_left = int(round(origin_x - first_tile_x * TILE_SIZE))
    crop_top = int(round(origin_y - first_tile_y * TILE_SIZE))

    cropped = canvas.crop(
        (crop_left, crop_top, crop_left + width, crop_top + height)
    )
    return cropped


def cache_filename(osm_map: str) -> str:
    """Nom de fichier déterministe, dépendant uniquement de la config osm_map."""
    digest = hashlib.sha1(osm_map.encode("utf-8")).hexdigest()[:12]
    return f"og-map-{digest}.png"


def get_or_generate_map_image(datadir: Path, osm_map: str) -> Path | None:
    """
    Retourne le chemin de l'image og:image générée pour la valeur ``osm_map``
    donnée, en la générant et la mettant en cache sur disque si nécessaire.

    Retourne None si ``osm_map`` est invalide ou si la génération échoue
    (par exemple absence d'accès réseau aux tuiles OpenStreetMap).
    """
    parsed = parse_osm_map(osm_map)
    if parsed is None:
        return None

    cache_dir = Path(datadir) / "cache"
    cache_path = cache_dir / cache_filename(osm_map)

    if cache_path.exists():
        return cache_path

    zoom, lat, lon = parsed
    try:
        image = render_static_map(zoom, lat, lon)
    except Exception:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        image.save(cache_path, "PNG")
    except Exception:
        return None

    return cache_path
