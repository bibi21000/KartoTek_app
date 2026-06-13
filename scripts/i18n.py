#!/usr/bin/env python3
"""
Génération et compilation des fichiers de traduction (.po / .mo)
pour les sous-projets : libpostcards, tkpostcards, flpostcards.

Usage:
    ./scripts/i18n.py {extract|init|update|compile|all}
"""

import subprocess
import sys
from pathlib import Path

SRC_DIR = Path("src")
LANGUAGES = ["en", "fr"]
PYBABEL = "./venv/bin/pybabel"

# Pour chaque projet : répertoire source et fichier de mapping optionnel
# (mapping_file=None => mapping par défaut de pybabel, Python uniquement)
PROJECTS = {
    "libpostcards": {
        "dir": SRC_DIR / "libpostcards",
        "mapping_file": None,
    },
    "tkpostcards": {
        "dir": SRC_DIR / "tkpostcards",
        "mapping_file": None,
    },
    "flpostcards": {
        "dir": SRC_DIR / "flpostcards",
        # Ce fichier doit exister pour scanner aussi les templates Jinja2
        "mapping_file": SRC_DIR / "flpostcards" / "babel.cfg",
    },
}


def get_version() -> str:
    """Lit la version depuis pyproject.toml."""
    pyproject = Path("pyproject.toml")
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


def run(cmd: list[str]) -> None:
    print("==> " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def extract_project(name: str, info: dict, version: str) -> None:
    src = info["dir"]
    mapping_file = info["mapping_file"]
    translations_dir = src / "translations"
    translations_dir.mkdir(parents=True, exist_ok=True)
    pot_file = translations_dir / f"{name}.pot"

    print(f"==> Extraction des chaînes pour {name}...")

    cmd = [
        PYBABEL, "extract",
        "--output-file", str(pot_file),
        "--project", name,
        "--version", version,
        "--copyright-holder", "bibi21000",
        "--msgid-bugs-address", "bibi21000@gmail.com",
        "-k", "_", "-k", "_l", "-k", "gettext", "-k", "ngettext:1,2", "-k", "lazy_gettext",
    ]

    if mapping_file is not None:
        if not mapping_file.exists():
            print(f"ATTENTION: fichier de mapping {mapping_file} introuvable, "
                  f"extraction Python uniquement (par défaut).")
            cmd += ["--input-dirs", str(src)]
        else:
            cmd += ["--mapping-file", str(mapping_file), str(src)]
    else:
        cmd += ["--input-dirs", str(src)]

    run(cmd)


def init_project(name: str, info: dict) -> None:
    src = info["dir"]
    translations_dir = src / "translations"
    pot_file = translations_dir / f"{name}.pot"

    for lang in LANGUAGES:
        po_file = translations_dir / lang / "LC_MESSAGES" / f"{name}.po"

        if po_file.exists():
            print(f"==> {po_file} existe déjà, skip init (utiliser update)")
            continue

        po_file.parent.mkdir(parents=True, exist_ok=True)
        print(f"==> Initialisation {lang} pour {name}...")
        run([
            PYBABEL, "init",
            "-i", str(pot_file),
            "-d", str(translations_dir),
            "-D", name,
            "-l", lang,
        ])


def update_project(name: str, info: dict) -> None:
    src = info["dir"]
    translations_dir = src / "translations"
    pot_file = translations_dir / f"{name}.pot"

    for lang in LANGUAGES:
        print(f"==> Mise à jour {lang} pour {name}...")
        run([
            PYBABEL, "update",
            "-i", str(pot_file),
            "-d", str(translations_dir),
            "-D", name,
            "-l", lang,
        ])


def compile_project(name: str, info: dict) -> None:
    src = info["dir"]
    translations_dir = src / "translations"

    print(f"==> Compilation des .mo pour {name}...")
    run([
        PYBABEL, "compile",
        "-d", str(translations_dir),
        "-D", name,
        "--statistics",
    ])


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "help"
    version = get_version()

    if action == "extract":
        for name, info in PROJECTS.items():
            extract_project(name, info, version)

    elif action == "init":
        for name, info in PROJECTS.items():
            extract_project(name, info, version)
            init_project(name, info)

    elif action == "update":
        for name, info in PROJECTS.items():
            extract_project(name, info, version)
            update_project(name, info)

    elif action == "compile":
        for name, info in PROJECTS.items():
            compile_project(name, info)

    elif action == "all":
        main_with_action("update")
        main_with_action("compile")

    else:
        print(__doc__)
        print("Actions disponibles :")
        print("  extract  : génère les fichiers .pot (gabarits)")
        print("  init     : crée les .po initiaux (1ère fois par langue)")
        print("  update   : met à jour les .po existants depuis les .pot")
        print("  compile  : génère les .mo à partir des .po")
        print("  all      : update + compile")
        return 1

    print(f"==> Terminé ({action}).")
    return 0


def main_with_action(action: str) -> None:
    """Permet de chaîner les actions (utilisé par 'all')."""
    sys.argv = [sys.argv[0], action]
    main()


if __name__ == "__main__":
    sys.exit(main())
