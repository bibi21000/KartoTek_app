"""
Génération de l'image og:image de /gallery/ : un collage de plusieurs
cartes postales tirées au hasard dans la collection, disposées de façon
désordonnée (position et rotation aléatoires, se chevauchant légèrement),
plutôt qu'une simple photo de carte isolée comme sur home/ ou travel/.

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

# Nombre de cartes piochées pour composer le collage
_CARD_COUNT = 9

# Taille de base (avant rotation) de chaque vignette dans le collage
_THUMB_WIDTH = 320

# Couleur de fond du collage
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
    """Tire jusqu'à ``count`` identifiants de cartes uniques au hasard.

    Même principe que home.index() (offset aléatoire dans
    count_unique_cards/list_unique_cards), répété plusieurs fois pour
    obtenir un échantillon varié -- acceptable ici puisque le résultat
    est mis en cache 60 minutes, donc peu fréquent.
    """
    total = model.count_unique_cards(exclude_status="exchanged")
    if not total:
        return []

    seen_offsets: set[int] = set()
    card_ids: list[str] = []
    attempts = 0
    max_attempts = count * 4
    while len(card_ids) < min(count, total) and attempts < max_attempts:
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
    return card_ids


def render_collage(datadir: Path, model) -> Image.Image | None:
    """
    Compose un collage désordonné (position et rotation aléatoires,
    léger chevauchement) à partir de plusieurs cartes tirées au hasard.

    Retourne None si aucune carte n'est disponible.
    """
    card_ids = _pick_random_card_ids(model, _CARD_COUNT)
    if not card_ids:
        return None

    canvas = Image.new("RGBA", (OG_IMAGE_WIDTH, OG_IMAGE_HEIGHT), _BACKGROUND_COLOR)

    for card_id in card_ids:
        thumb = _load_thumb(datadir, card_id)
        if thumb is None:
            continue

        # Redimensionne à une largeur de base commune, en conservant le ratio
        ratio = _THUMB_WIDTH / thumb.width
        thumb = thumb.resize(
            (max(1, int(thumb.width * ratio)), max(1, int(thumb.height * ratio))),
            Image.LANCZOS,
        )

        # Rotation aléatoire "posée en vrac" (entre -30 et 30 degrés),
        # expand=True pour ne pas rogner les coins de la carte tournée.
        angle = random.uniform(-30, 30)
        thumb = thumb.rotate(angle, expand=True, resample=Image.BICUBIC)

        # Position aléatoire, en autorisant les cartes à déborder
        # partiellement du cadre pour un rendu désordonné/naturel.
        max_x = OG_IMAGE_WIDTH - thumb.width // 2
        max_y = OG_IMAGE_HEIGHT - thumb.height // 2
        x = random.randint(-thumb.width // 2, max_x)
        y = random.randint(-thumb.height // 2, max_y)

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
