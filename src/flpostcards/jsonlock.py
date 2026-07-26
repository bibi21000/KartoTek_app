"""
flpostcards/jsonlock.py - Lecture/écriture de fichiers JSON protégée par un
lockfile, factorisée à partir du mécanisme déjà utilisé par
``POST /api/v1/update`` pour ``updates.json`` (voir
``flpostcards.blueprints.api``).

Principe (identique à l'existant, juste réutilisable) : un fichier
``<path><suffix>`` (``.lck`` par défaut) sert de verrou exclusif via
``O_CREAT | O_EXCL`` (atomique sur POSIX). L'écriture du fichier cible se
fait sur un fichier temporaire puis ``rename`` atomique, pour ne jamais
laisser un lecteur voir un JSON tronqué.

Utilisé par ``flpostcards.push`` pour ``push_tokens.json`` (registre des
tokens FCM/APNs) et ``push_watch_state.json`` (curseur du job de
notification).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def acquire_lock(lock_path: Path, timeout: float = 60.0, poll_interval: float = 2.0) -> bool:
    """
    Tente d'acquérir un verrou exclusif via un fichier .lck (même logique
    que ``_acquire_lock`` dans ``flpostcards.blueprints.api``). Retourne
    True si acquis, False en cas de timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_interval)


def release_lock(lock_path: Path) -> None:
    """Relâche le verrou en supprimant le fichier .lck. Silencieux si déjà absent."""
    try:
        lock_path.unlink()
    except OSError:
        pass


def read_json(path: Path, default: Any) -> Any:
    """
    Lit et parse ``path``. Retourne ``default`` (une copie superficielle,
    via le type d'origine) si le fichier n'existe pas encore ou contient
    un JSON invalide/corrompu — ne lève jamais d'exception, pour rester
    utilisable sans bloc try/except à chaque appel.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    """Écrit ``data`` en JSON dans ``path`` via un fichier temporaire + rename atomique."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


class LockedJsonFile:
    """
    Context manager : acquiert le verrou, charge le JSON courant (ou
    ``default``), l'expose via ``.data`` (mutable en place), et
    réécrit automatiquement le fichier à la sortie du bloc `with` avant
    de relâcher le verrou — même en cas d'exception (le contenu n'est
    alors PAS réécrit, pour ne jamais persister un état intermédiaire
    incohérent).

    Usage :
        with LockedJsonFile(path, default={}) as f:
            f.data["some_key"] = "some_value"
        # f.data a été réécrit sur disque à la sortie du bloc (si pas d'exception)
    """

    def __init__(
        self,
        path: Path,
        default: Any,
        lock_suffix: str = ".lck",
        timeout: float = 60.0,
        poll_interval: float = 2.0,
    ) -> None:
        self.path = path
        self.default = default
        self.lock_path = path.with_name(path.name + lock_suffix)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.data: Any = None

    def __enter__(self) -> "LockedJsonFile":
        if not acquire_lock(self.lock_path, self.timeout, self.poll_interval):
            raise TimeoutError(
                f"verrou {self.lock_path.name} toujours présent après {self.timeout:.0f}s"
            )
        self.data = read_json(self.path, self.default)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                write_json_atomic(self.path, self.data)
        finally:
            release_lock(self.lock_path)
