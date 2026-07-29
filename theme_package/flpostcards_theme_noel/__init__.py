"""
Thème "Noël" pour flpostcards, distribué comme paquet Python séparé.

Installation côté site flpostcards : `pip install flpostcards-theme-noel`
puis, dans postcards.conf :

    [flask]
    theme = noel

flpostcards le découvre automatiquement via le point d'entrée déclaré
dans pyproject.toml (groupe "flpostcards.themes"), sans rien copier
manuellement dans un dossier "themes/".
"""

from pathlib import Path


def theme_dir() -> Path:
    """
    Retourne le dossier contenant templates/, static/, translations/,
    views.py du thème. Une fonction (plutôt qu'un Path statique)
    permet de résoudre le chemin de façon fiable une fois le paquet
    installé, y compris si son emplacement exact dépend de
    l'environnement (venv, site-packages, etc.).
    """
    return Path(__file__).parent / "theme_data"
