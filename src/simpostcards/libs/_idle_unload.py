# -*- encoding: utf-8 -*-
"""
simpostcards/libs/_idle_unload.py - Libération automatique (ou à la
demande) des modèles chargés en mémoire.

Utilitaire interne (préfixe "_" : pas d'API publique stable), partagé par
``simpostcards.libs.embedder`` (CLIP), ``simpostcards.libs.detection_backend``
(BLIP + DETR) et ``simpostcards.libs.segment_backend`` (SAM) : quatre
modèles indépendants qui, sans ceci, restent en mémoire GPU/RAM
indéfiniment une fois utilisés.

Utile sur une machine partagée avec d'autres usages (ex : Open WebUI /
Ollama qui ont besoin de la VRAM) : voir [simpostcards] gpu_idle_timeout
dans postcards.conf (configuré une fois au démarrage de l'appli, voir
simpostcards.__init__.create_app), et POST /api/unload_models pour tout
libérer immédiatement.

Fonctionnement
--------------
Chaque modèle possède un ``IdleUnloadTimer`` :

- ``with timer.busy():`` entoure toute requête qui utilise le modèle
  (chargement compris). Tant qu'une requête est en cours, le modèle n'est
  JAMAIS déchargé -- une inférence longue (SAM sur CPU, premier
  chargement d'un gros modèle) ne déclenche donc pas un déchargement au
  milieu du calcul, suivi d'un rechargement inutile. Le décompte
  d'inactivité repart à la FIN de la requête.
- un thread "watchdog" par timer vérifie périodiquement si le délai
  configuré est dépassé et, si oui, appelle ``unload_callback`` (fourni
  par le modèle : vider ses références ; il doit renvoyer True s'il y
  avait bien quelque chose à décharger) puis :func:`release_memory`.

:func:`release_memory` fait ce que ``torch.cuda.empty_cache()`` seul ne
fait pas : un ``gc.collect()`` d'abord (les modèles transformers
contiennent des cycles de références : sans lui, la VRAM reste occupée
tant que le ramasse-miettes n'est pas passé), puis rend le cache CUDA, puis
(Linux/glibc) demande à malloc de restituer à l'OS la mémoire libre -- pour
que la RAM d'un modèle chargé sur cpu soit réellement rendue aussi, pas
seulement la VRAM.

Gunicorn / fork
---------------
Les threads ne survivent pas à un ``fork()`` : sous ``gunicorn --preload``,
create_app() tourne dans le processus maître, et un watchdog démarré là
n'existerait pas dans les workers -- le déchargement ne se déclencherait
jamais. Chaque timer se réarme donc lui-même dans un processus fils
(``os.register_at_fork``) : un watchdog par worker, chacun surveillant les
modèles chargés dans SON processus. (Avec plusieurs workers, chacun peut
avoir son propre exemplaire de chaque modèle en mémoire : voir la note sur
GUNICORN_WORKERS dans le Dockerfile.)

Limites : le délai n'est vérifié qu'à intervalles réguliers (4 fois par
période d'inactivité), le déchargement a donc lieu entre ``idle_timeout``
et ``1.25 x idle_timeout`` après la dernière utilisation ; et un
déchargement ne peut pas rendre la mémoire d'un modèle encore référencé
ailleurs dans le processus.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager

_logger = logging.getLogger(__name__)

_TIMERS: list["IdleUnloadTimer"] = []
_TIMERS_LOCK = threading.Lock()


def _malloc_trim() -> None:
    """Rend à l'OS la mémoire libre du tas glibc (sans effet ailleurs)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # musl (alpine) n'a pas malloc_trim : tant pis
        pass


def release_memory() -> None:
    """À appeler APRÈS avoir lâché les références d'un modèle : ramasse
    les cycles, vide le cache CUDA et restitue la RAM libre à l'OS. Ne
    force pas l'import de torch s'il n'est pas déjà chargé."""
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
    # PaddlePaddle a son propre allocateur GPU : le cache de torch n'y change
    # rien. Même principe : seulement s'il est déjà chargé (PaddleOCR).
    paddle = sys.modules.get("paddle")
    if paddle is not None:
        try:
            if paddle.device.is_compiled_with_cuda():
                paddle.device.cuda.empty_cache()
        except Exception:
            pass
    _malloc_trim()


class IdleUnloadTimer:
    """Déclenche ``unload_callback()`` après ``idle_timeout`` secondes
    sans utilisation. Voir le docstring du module pour le détail."""

    def __init__(self, name: str, unload_callback) -> None:
        self._name = name
        self._unload_callback = unload_callback
        self._state_lock = threading.Lock()
        self._last_used: float | None = None
        self._idle_timeout: float | None = None
        self._thread: threading.Thread | None = None
        self._busy = 0
        with _TIMERS_LOCK:
            _TIMERS.append(self)

    @property
    def name(self) -> str:
        return self._name

    @contextmanager
    def busy(self):
        """Marque une requête en cours : tant qu'elle dure, le modèle ne
        sera pas déchargé. Le décompte d'inactivité repart à sa fin."""
        with self._state_lock:
            self._busy += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._busy -= 1
            self._last_used = time.monotonic()

    def touch(self) -> None:
        """Redémarre le décompte d'inactivité (sans marquer de requête en
        cours : préférer :meth:`busy` autour d'une utilisation)."""
        self._last_used = time.monotonic()

    def configure(self, idle_timeout: float | None) -> None:
        """
        (Re)configure le délai d'inactivité, en secondes. ``None`` ou
        ``0`` désactive la libération automatique (le modèle reste chargé
        indéfiniment une fois utilisé). Démarre le thread de surveillance
        au tout premier appel avec une valeur réelle -- rappeler
        ``configure()`` avec une nouvelle valeur ensuite ne recrée pas de
        second thread.
        """
        with self._state_lock:
            self._idle_timeout = idle_timeout if idle_timeout else None
            self._start_thread_locked()

    def _start_thread_locked(self) -> None:
        if self._idle_timeout and self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name=f"idle-unload-{self._name}")
            self._thread.start()

    def _after_fork_in_child(self) -> None:
        # Le thread du parent n'existe pas dans le fils, et un verrou
        # détenu par un autre thread au moment du fork le resterait pour
        # toujours : on repart de zéro. ``busy`` aussi : les requêtes du
        # parent ne sont pas celles du fils.
        self._state_lock = threading.Lock()
        self._thread = None
        self._busy = 0
        with self._state_lock:
            self._start_thread_locked()

    def _loop(self) -> None:
        while True:
            # Vérifie 4 fois par période d'inactivité (au moins toutes
            # les 1s, jamais plus souvent) : assez réactif sans
            # réveiller le thread trop fréquemment pour rien.
            timeout = self._idle_timeout or 60.0
            time.sleep(max(1.0, timeout / 4))
            self._check()

    def _check(self) -> None:
        timeout = self._idle_timeout
        last_used = self._last_used
        if not timeout or last_used is None or self._busy > 0:
            return

        idle_for = time.monotonic() - last_used
        if idle_for < timeout:
            return

        # Évite de redéclencher en boucle à chaque passage du watchdog
        # tant que rien n'a retouché le modèle (le prochain usage réel
        # réarmera le décompte).
        self._last_used = None

        _logger.info(
            "%s : inactif depuis %.0fs (> %.0fs), libération de la mémoire",
            self._name, idle_for, timeout,
        )
        self._run_unload()

    def _run_unload(self) -> bool:
        try:
            unloaded = bool(self._unload_callback())
            if unloaded:
                release_memory()
            return unloaded
        except Exception:
            _logger.exception("%s : échec de la libération", self._name)
            return False

    def unload_now(self) -> str:
        """Décharge le modèle tout de suite. Renvoie ``"unloaded"``,
        ``"not_loaded"`` (rien à décharger) ou ``"busy"`` (une requête
        l'utilise : on n'y touche pas, réessayer plus tard)."""
        if self._busy > 0:
            return "busy"
        self._last_used = None
        return "unloaded" if self._run_unload() else "not_loaded"


def unload_all() -> dict[str, str]:
    """Décharge tous les modèles enregistrés (CLIP, BLIP, DETR, SAM... ceux
    dont le module a été importé). Renvoie ``{nom: état}`` -- voir
    :meth:`IdleUnloadTimer.unload_now`."""
    with _TIMERS_LOCK:
        timers = list(_TIMERS)
    return {t.name: t.unload_now() for t in timers}


def _reset_all_after_fork() -> None:
    global _TIMERS_LOCK
    _TIMERS_LOCK = threading.Lock()
    for timer in list(_TIMERS):
        timer._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_all_after_fork)
