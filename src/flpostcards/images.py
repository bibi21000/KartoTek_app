"""
Helpers partagés pour construire les chemins d'images recto/verso
servies via la route home.images (size_div3, size_div10, size_div20).
"""

from __future__ import annotations

from pathlib import Path

# Répertoires d'images utilisés par les différentes pages
SIZE_MAIN = "size_div3"     # Image principale (plein écran / fiche)
SIZE_SMALL = "size_div10"   # Petits affichages (PiP, vignette de bascule)
SIZE_THUMB = "size_div10"   # Vignettes de galerie

ALLOWED_SIZE_DIRS = {"size_div3", "size_div10", "size_div20"}


def card_images(card_id: str, size_dir: str = SIZE_MAIN) -> dict:
    """Construit les chemins recto/verso pour le répertoire de taille donné."""
    return {
        "recto": f"{size_dir}/{card_id}_R.png",
        "verso": f"{size_dir}/{card_id}_V.png",
    }


def image_dimensions(datadir: Path, relative_path: str) -> tuple[int, int] | None:
    """
    Retourne (largeur, hauteur) du PNG situé à datadir/relative_path,
    ou None si le fichier est absent ou illisible.

    Utilisé pour fournir og:image:width / og:image:height : ces balises
    évitent à WhatsApp (et aux autres clients de prévisualisation) de
    devoir télécharger l'image avant de connaître ses dimensions, ce qui
    accélère et fiabilise l'affichage de l'aperçu.
    """
    path = Path(datadir) / relative_path
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return None
