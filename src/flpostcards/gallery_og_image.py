"""
Génération de l'image og:image de /gallery/ : un collage de plusieurs
cartes postales tirées au hasard dans la collection, disposées de façon
désordonnée (position et rotation aléatoires) mais couvrant tout le
cadre -- les cartes sont réparties sur une grille (avec un jitter
aléatoire et un chevauchement volontaire) pour éviter les grands
aplats de fond vides, plutôt qu'une simple photo de carte isolée comme
sur home/ ou travel/.

Contrairement à osm_static_map.py (cache disque permanent, invalidé
seulement par un changement de configuration), cette image doit changer
régulièrement puisqu'elle est tirée au hasard à chaque (re)génération :
elle est donc mise en cache via flpostcards.extensions.cache (le cache
Flask-Caching partagé, mémoire ou Redis selon la config -- voir
data_cache.py) avec un TTL de 60 minutes, plutôt qu'écrite sur disque.
"""

from __future__ import annotations

import random
from io import BytesIO
from pathlib import Path

from PIL import Image

from flpostcards.extensions import cache
from flpostcards.images import SIZE_SMALL, card_images

# Dimensions visées pour l'image og:image (format recommandé ~1200x630)
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630

# Grille utilisée pour répartir les cartes sur tout le cadre (voir
# render_collage) : plus de cellules que nécessaire pour être sûr de
# couvrir les bords même avec le jitter et la rotation.
_GRID_COLS = 5
_GRID_ROWS = 3
_CARD_COUNT = _GRID_COLS * _GRID_ROWS

# Facteur d'agrandissement de chaque vignette par rapport à sa cellule :
# > 1 garantit un chevauchement volontaire qui masque le fond entre les
# cellules (c'est ce chevauchement qui évite les zones blanches).
_THUMB_SCALE = 1.65

# Amplitude du jitter aléatoire autour du centre de chaque cellule
# (en fraction de la taille de la cellule)
_JITTER_RATIO = 0.35

# Couleur de fond (visible seulement dans d'éventuels interstices résiduels)
_BACKGROUND_COLOR = "#e9e2d6"

_CACHE_KEY = "gallery_og_image"
_CACHE_TTL = 60 * 60  # 60 minutes


def _load_thumb(datadir: Path, card_id: str) -> Image.Image | None:
    """Charge le recto (taille réduite) d'une carte en RGBA, ou None si absent."""
    relative_path = card_images(card_id, SIZE_SMALL)["recto"]
    path = Path(datadir) / relative_path
    try:
        with Image.open(path) as img:
            return img.convert("RGBA")
    except Exception:
        return None


def _pick_random_card_ids(model, count: int) -> list[str]:
    """Tire jusqu'à ``count`` identifiants de cartes au hasard.

    Même principe que home.index() (offset aléatoire dans
    count_unique_cards/list_unique_cards), répété plusieurs fois pour
    obtenir un échantillon varié -- acceptable ici puisque le résultat
    est mis en cache 60 minutes, donc peu fréquent.

    Si la collection contient moins de cartes que ``count`` (nécessaire
    pour couvrir toute la grille), certaines cartes sont réutilisées
    (avec une rotation/position différente à chaque tirage) plutôt que
    de laisser des cellules de la grille vides.
    """
    total = model.count_unique_cards(exclude_status="exchanged")
    if not total:
        return []

    unique_needed = min(count, total)
    seen_offsets: set[int] = set()
    card_ids: list[str] = []
    attempts = 0
    max_attempts = unique_needed * 4
    while len(card_ids) < unique_needed and attempts < max_attempts:
        attempts += 1
        offset = random.randint(0, total - 1)
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        featured = model.list_unique_cards(
            limit=1, offset=offset, exclude_status="exchanged"
        )
        if featured:
            card_ids.append(featured[0]["id"])

    # Complète en réutilisant des cartes déjà tirées si la collection
    # est plus petite que la grille (rare, mais évite des trous).
    if card_ids:
        while len(card_ids) < count:
            card_ids.append(random.choice(card_ids))

    return card_ids


def render_collage(datadir: Path, model) -> Image.Image | None:
    """
    Compose un collage désordonné (position et rotation aléatoires) à
    partir de plusieurs cartes tirées au hasard, réparties sur une
    grille agrandie et chevauchante pour couvrir tout le cadre.

    Retourne None si aucune carte n'est disponible.
    """
    card_ids = _pick_random_card_ids(model, _CARD_COUNT)
    if not card_ids:
        return None

    canvas = Image.new("RGBA", (OG_IMAGE_WIDTH, OG_IMAGE_HEIGHT), _BACKGROUND_COLOR)

    cell_width = OG_IMAGE_WIDTH / _GRID_COLS
    cell_height = OG_IMAGE_HEIGHT / _GRID_ROWS

    # Une cellule par carte, mais l'ordre de pose (calques) est mélangé
    # indépendamment de la position, pour que ce ne soit pas toujours la
    # carte en bas à droite de la grille qui recouvre ses voisines.
    cells = [(col, row) for row in range(_GRID_ROWS) for col in range(_GRID_COLS)]
    random.shuffle(cells)

    for card_id, (col, row) in zip(card_ids, cells):
        thumb = _load_thumb(datadir, card_id)
        if thumb is None:
            continue

        # Redimensionne pour dépasser largement la cellule (chevauchement
        # volontaire avec les cellules voisines, cf. _THUMB_SCALE).
        target_width = cell_width * _THUMB_SCALE
        ratio = target_width / thumb.width
        thumb = thumb.resize(
            (max(1, int(thumb.width * ratio)), max(1, int(thumb.height * ratio))),
            Image.LANCZOS,
        )

        # Rotation aléatoire "posée en vrac"
        angle = random.uniform(-25, 25)
        thumb = thumb.rotate(angle, expand=True, resample=Image.BICUBIC)

        # Centre de la cellule + jitter aléatoire, pour un placement
        # désordonné qui reste réparti sur tout le cadre.
        center_x = (col + 0.5) * cell_width
        center_y = (row + 0.5) * cell_height
        jitter_x = random.uniform(-_JITTER_RATIO, _JITTER_RATIO) * cell_width
        jitter_y = random.uniform(-_JITTER_RATIO, _JITTER_RATIO) * cell_height

        x = int(center_x + jitter_x - thumb.width / 2)
        y = int(center_y + jitter_y - thumb.height / 2)

        canvas.alpha_composite(thumb, (x, y))

    return canvas.convert("RGB")


def get_or_render_collage(datadir: Path, model) -> dict | None:
    """
    Retourne ``{"bytes": <PNG>, "width": w, "height": h}`` pour l'image
    og:image de /gallery/, en la générant et en la mettant en cache 60
    minutes si nécessaire (cf. docstring du module).

    Retourne None si aucune carte n'est disponible pour composer l'image.
    """
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    image = render_collage(datadir, model)
    if image is None:
        return None

    buffer = BytesIO()
    image.save(buffer, "PNG")
    value = {
        "bytes": buffer.getvalue(),
        "width": image.width,
        "height": image.height,
    }
    cache.set(_CACHE_KEY, value, timeout=_CACHE_TTL)
    return value


def invalidate() -> None:
    """Purge immédiatement l'image en cache (cf. data_cache.invalidate)."""
    cache.delete(_CACHE_KEY)
