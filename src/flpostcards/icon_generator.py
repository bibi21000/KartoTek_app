"""
Génération de l'icône du site (favicon) :

- si un fichier static/icon.png, .jpg ou .jpeg existe, il est utilisé
  comme source (redimensionné dans les tailles nécessaires) ;
- sinon, si le paramètre de configuration [flask] icon est défini,
  un logo stylisé est généré à partir de ce texte (dégradé, ombre,
  légère rotation) ;
- sinon, aucune icône n'est générée (404 sur la route favicon).

Le résultat est mis en cache sur disque (datadir/cache), avec un nom de
fichier dépendant d'un hash de la source (mtime du fichier image, ou
texte de configuration), pour invalider automatiquement le cache si la
source change.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ICON_FILENAMES = ("icon.png", "icon.jpg", "icon.jpeg")

# Tailles d'icône générées (la plus grande sert de source, les autres
# sont dérivées par redimensionnement)
ICON_SIZE = 512

_FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
_FONT_BLACK = _FONTS_DIR / "Poppins-Black.ttf"
_FONT_BOLD = _FONTS_DIR / "Poppins-Bold.ttf"

# Palette de dégradés (haut-gauche -> bas-droite), choisie en fonction
# du texte pour une variation déterministe (même texte = mêmes couleurs)
_GRADIENTS = [
    ((255, 94, 98), (255, 195, 113)),    # corail -> pêche
    ((67, 97, 238), (114, 9, 183)),       # bleu -> violet
    ((17, 153, 142), (56, 239, 125)),     # émeraude -> vert clair
    ((247, 37, 133), (114, 9, 183)),      # rose -> violet
    ((0, 114, 255), (0, 198, 251)),       # bleu profond -> cyan
    ((255, 154, 0), (255, 61, 0)),        # orange -> rouge
]


def find_uploaded_icon(static_dir: Path) -> Path | None:
    """Retourne le chemin du premier static/icon.(png|jpg|jpeg) trouvé, ou None."""
    static_dir = Path(static_dir)
    for name in ICON_FILENAMES:
        candidate = static_dir / name
        if candidate.exists():
            return candidate
    return None


def _pick_gradient(text: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Choisit un dégradé de façon déterministe en fonction du texte."""
    index = int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16) % len(_GRADIENTS)
    return _GRADIENTS[index]


def _diagonal_gradient(
    size: int,
    color_start: tuple[int, int, int],
    color_end: tuple[int, int, int],
) -> Image.Image:
    """Génère un dégradé diagonal haut-gauche -> bas-droite."""
    base = Image.new("RGB", (size, size))
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            r = int(color_start[0] + (color_end[0] - color_start[0]) * t)
            g = int(color_start[1] + (color_end[1] - color_start[1]) * t)
            b = int(color_start[2] + (color_end[2] - color_start[2]) * t)
            base.putpixel((x, y), (r, g, b))
    return base


def _initials_or_short_text(text: str) -> str:
    """
    Réduit le texte à un fragment court et impactant pour l'icône.

    Si le premier mot est déjà un acronyme (entièrement en majuscules,
    2 à 5 lettres, ex: "CPA"), il est conservé tel quel. Sinon, on
    prend les initiales des mots (jusqu'à 3) si plusieurs mots, ou le
    texte tronqué à 4 caractères s'il n'y a qu'un seul mot.
    """
    words = [w for w in text.strip().split() if w]
    if not words:
        return text.strip()[:4].upper()

    first = words[0]
    if first.isupper() and 2 <= len(first) <= 5:
        return first

    if len(words) >= 2:
        return "".join(w[0].upper() for w in words[:3])
    return words[0][:4].upper()


def render_text_icon(text: str, size: int = ICON_SIZE) -> Image.Image:
    """
    Génère une icône stylisée à partir d'un texte court : fond en
    dégradé diagonal, texte en grand (initiales ou texte court) avec
    ombre portée et légère rotation, coins arrondis.
    """
    color_start, color_end = _pick_gradient(text)
    background = _diagonal_gradient(size, color_start, color_end)

    # Vignette douce pour donner du relief au fond
    vignette = Image.new("L", (size, size), 0)
    vdraw = ImageDraw.Draw(vignette)
    vdraw.ellipse(
        (-size * 0.2, -size * 0.2, size * 1.2, size * 1.2), fill=80
    )
    vignette = vignette.filter(ImageFilter.GaussianBlur(size * 0.15))
    background = Image.composite(
        background, Image.new("RGB", (size, size), (0, 0, 0)), vignette
    )
    background = background.convert("RGBA")

    # Accents géométriques semi-transparents (cercles décalés), pour
    # rompre l'uniformité du dégradé et donner un look plus graphique
    seed = int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)
    accents = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    adraw = ImageDraw.Draw(accents)
    accent_positions = [
        (size * 0.78, size * 0.12, size * 0.42, 35),
        (size * 0.08, size * 0.85, size * 0.30, 25),
    ]
    for cx, cy, radius, alpha in accent_positions:
        adraw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill=(255, 255, 255, alpha),
        )
    accents = accents.filter(ImageFilter.GaussianBlur(size * 0.01))
    background = Image.alpha_composite(background, accents)

    label = _initials_or_short_text(text)

    # Choix de taille de police selon la longueur du label, pour qu'il
    # reste bien proportionné sans dépasser le cadre
    font_path = _FONT_BLACK if _FONT_BLACK.exists() else _FONT_BOLD
    font_size = int(size * (0.58 if len(label) <= 2 else 0.4 if len(label) <= 4 else 0.28))
    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except Exception:
        font = ImageFont.load_default()

    # Calque texte (avec marge pour permettre la rotation sans rognage)
    margin = size // 2
    text_layer_size = size + margin * 2
    text_layer = Image.new("RGBA", (text_layer_size, text_layer_size), (0, 0, 0, 0))
    tdraw = ImageDraw.Draw(text_layer)

    bbox = tdraw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (text_layer_size - text_w) / 2 - bbox[0]
    text_y = (text_layer_size - text_h) / 2 - bbox[1]

    # Ombre portée (plus marquée pour un effet de relief net)
    shadow_layer = Image.new("RGBA", (text_layer_size, text_layer_size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_layer)
    shadow_offset = max(3, size // 60)
    sdraw.text(
        (text_x + shadow_offset * 1.4, text_y + shadow_offset * 2.0),
        label,
        font=font,
        fill=(0, 0, 0, 160),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(size * 0.014))

    # Rotation déterministe plus marquée (-14° à +14°) selon le texte,
    # pour un rendu nettement plus dynamique qu'un texte horizontal
    angle = (seed % 29) - 14

    tdraw.text(
        (text_x, text_y),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )

    # Contour pour détacher nettement le texte du fond
    outline_layer = Image.new("RGBA", (text_layer_size, text_layer_size), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(outline_layer)
    outline_width = max(3, size // 110)
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx == 0 and dy == 0:
                continue
            odraw.text(
                (text_x + dx, text_y + dy), label, font=font, fill=(0, 0, 0, 90)
            )

    combined = Image.alpha_composite(outline_layer, shadow_layer)
    combined = Image.alpha_composite(combined, text_layer)
    combined = combined.rotate(angle, resample=Image.BICUBIC, expand=False)

    # Recadre le calque texte à la taille finale
    crop_box = (margin, margin, margin + size, margin + size)
    combined = combined.crop(crop_box)

    result = background.convert("RGBA")
    result = Image.alpha_composite(result, combined)

    # Coins arrondis
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    radius = int(size * 0.16)
    mdraw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(result, (0, 0), mask)

    return rounded


def _source_signature(static_dir: Path, icon_config: str | None) -> str:
    """
    Construit une signature identifiant la source courante de l'icône
    (fichier uploadé + sa date de modification, ou texte de config),
    utilisée pour nommer le fichier en cache.
    """
    uploaded = find_uploaded_icon(static_dir)
    if uploaded is not None:
        return f"file:{uploaded.name}:{uploaded.stat().st_mtime_ns}"
    if icon_config:
        return f"text:{icon_config}"
    return ""


def cache_filename(signature: str) -> str:
    digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12]
    return f"icon-{digest}.png"


def get_or_generate_icon(
    datadir: Path, static_dir: Path, icon_config: str | None
) -> Path | None:
    """
    Retourne le chemin de l'icône (PNG carré, ICON_SIZE x ICON_SIZE) à
    utiliser comme favicon, en la générant et la mettant en cache sur
    disque si nécessaire.

    Ordre de priorité : static/icon.(png|jpg|jpeg) > texte de config
    [flask] icon > None (pas d'icône).
    """
    signature = _source_signature(static_dir, icon_config)
    if not signature:
        return None

    cache_dir = Path(datadir) / "cache"
    cache_path = cache_dir / cache_filename(signature)

    if cache_path.exists():
        return cache_path

    uploaded = find_uploaded_icon(static_dir)
    try:
        if uploaded is not None:
            with Image.open(uploaded) as src:
                src = src.convert("RGBA")
                # Recadre en carré (centré) puis redimensionne
                w, h = src.size
                side = min(w, h)
                left = (w - side) // 2
                top = (h - side) // 2
                src = src.crop((left, top, left + side, top + side))
                image = src.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        else:
            image = render_text_icon(icon_config)
    except Exception:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        image.save(cache_path, "PNG")
    except Exception:
        return None

    return cache_path
