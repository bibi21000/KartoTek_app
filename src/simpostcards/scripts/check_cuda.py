# -*- encoding: utf-8 -*-
"""
simpostcards/scripts/check_cuda.py - Diagnostic torch/CUDA pour un
déploiement simpostcards.

Ne se contente pas de vérifier que CUDA est "disponible" (torch peut
répondre ``True`` alors que l'usage réel échoue -- pilotes NVIDIA trop
anciens sur l'hôte, conteneur lancé sans accès GPU, etc.) : effectue
aussi un petit calcul réel sur le GPU pour confirmer que CUDA
fonctionne effectivement, pas seulement qu'il se déclare présent.

Vérifie également les autres bibliothèques optionnelles utilisées par
simpostcards (open_clip, imagehash, pytesseract, ortools -- voir
simpostcards.libs.embedder/hasher/ocr_backend et libpostcards.travel),
pour un état des lieux complet en une seule commande.

Usage (sur la machine hôte, si le paquet est installé) :

    python3 -m simpostcards.scripts.check_cuda

Usage (dans un conteneur Docker déjà lancé) :

    docker exec -it <conteneur> python3 -m simpostcards.scripts.check_cuda

Code de sortie : 0 si tout va bien (y compris "pas de GPU, cpu
seulement" -- ce n'est pas une erreur), 1 si torch n'est pas installé,
2 si CUDA se déclare disponible mais que le calcul de test échoue.
"""

from __future__ import annotations

import sys


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _check_optional_lib(label: str, import_path: str, extra_hint: str = "") -> None:
    module_name, _, attr = import_path.partition(":")
    try:
        module = __import__(module_name, fromlist=[attr] if attr else [])
        if attr:
            getattr(module, attr)
        print(f"  {label:<14}: installé")
    except ImportError:
        suffix = f" ({extra_hint})" if extra_hint else ""
        print(f"  {label:<14}: absent{suffix}")


def main() -> int:
    print("=" * 70)
    print("simpostcards -- diagnostic torch / CUDA")
    print("=" * 70)

    try:
        import torch
    except ImportError:
        print()
        print("torch n'est PAS installé.")
        print("  -> le calcul d'embedding CLIP restera indisponible")
        print("     (simpostcards.libs.embedder.CLIP_AVAILABLE = False) ;")
        print("     les hashs perceptuels (imagehash) ne nécessitent pas")
        print("     torch et restent disponibles indépendamment.")
        return 1

    _section("torch")
    print(f"  version            : {torch.__version__}")
    print(f"  build CUDA         : {torch.version.cuda or '(build CPU uniquement)'}")

    cuda_available = torch.cuda.is_available()
    print(f"  torch.cuda.is_available() : {cuda_available}")

    if not cuda_available:
        _section("CUDA")
        print("  Indisponible pour torch. Causes possibles :")
        print("    - pas de GPU NVIDIA sur cette machine ;")
        print("    - pilotes NVIDIA absents/trop anciens sur l'HÔTE ;")
        print("    - conteneur lancé sans accès GPU (voir la commande docker")
        print("      run --gpus=all plus bas) ;")
        print("    - roue torch installée sans support CUDA (build 'cpu-only').")
        print()
        print("  cpu reste disponible (CLIP fonctionnera, juste plus lentement ;")
        print("  voir [simpostcards]/[DEFAULT] clip_device dans postcards.conf).")
        _report_optional_libs()
        return 0

    _section("GPU(s) détecté(s)")
    device_count = torch.cuda.device_count()
    print(f"  nombre de GPU visibles : {device_count}")

    for i in range(device_count):
        name = torch.cuda.get_device_name(i)
        props = torch.cuda.get_device_properties(i)
        total_mem_gb = props.total_memory / (1024 ** 3)
        print()
        print(f"  cuda:{i}")
        print(f"    nom                : {name}")
        print(f"    compute capability : {props.major}.{props.minor}")
        print(f"    mémoire totale     : {total_mem_gb:.1f} Go")

    _section("cuDNN")
    cudnn_available = torch.backends.cudnn.is_available()
    print(f"  disponible : {cudnn_available}")
    if cudnn_available:
        print(f"  version    : {torch.backends.cudnn.version()}")

    _section("Test fonctionnel (allocation + calcul sur cuda:0)")
    try:
        x = torch.rand(1024, 1024, device="cuda:0")
        y = torch.rand(1024, 1024, device="cuda:0")
        z = x @ y
        torch.cuda.synchronize()
        print(f"  OK -- calcul exécuté sur le GPU (somme de contrôle : {z.sum().item():.4f})")
    except Exception as exc:
        print(f"  ÉCHEC : {exc!r}")
        print("  -> CUDA se déclare disponible mais échoue à l'usage réel :")
        print("     pilotes NVIDIA/toolkit CUDA incompatibles entre hôte et")
        print("     conteneur, ou GPU non réellement accessible malgré --gpus.")
        _report_optional_libs()
        return 2

    _report_optional_libs()

    print()
    print("=" * 70)
    print("Résumé : CUDA fonctionne correctement sur ce conteneur/machine.")
    print("=" * 70)
    return 0


def _report_optional_libs() -> None:
    _section("Bibliothèques optionnelles de simpostcards")
    _check_optional_lib("open_clip", "open_clip",
                         "embedding CLIP indisponible -- voir clip_enabled")
    _check_optional_lib("imagehash", "imagehash",
                         "hashs perceptuels indisponibles -- requis en permanence")
    _check_optional_lib("pytesseract", "pytesseract",
                         "OCR indisponible -- voir ocr_enabled")
    _check_optional_lib("ortools", "ortools.constraint_solver.pywrapcp",
                         "calcul de parcours indisponible -- voir travel_enabled")


if __name__ == "__main__":
    sys.exit(main())
