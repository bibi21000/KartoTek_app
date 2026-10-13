# -*- encoding: utf-8 -*-
"""
simpostcards/libs/_idle_unload.py - Libération automatique d'un modèle
GPU resté inactif trop longtemps.

Utilitaire interne (préfixe "_" : pas d'API publique stable), partagé
par ``simpostcards.libs.embedder`` (CLIP) et
``simpostcards.libs.detection_backend`` (BLIP + DETR) -- trois modèles
indépendants qui peuvent chacun rester chargés en mémoire GPU
indéfiniment une fois utilisés (voir leurs docstrings respectifs).

Utile sur un serveur GPU partagé avec d'autres usages (ex : un autre
service utilisant aussi le GPU) : sans ceci, simpostcards garde la
mémoire de chaque modèle qu'il a fini par charger pour toujours, même
des heures après sa dernière requête. Voir [simpostcards]
gpu_idle_timeout dans postcards.conf (configuré une fois au démarrage
de l'appli, voir simpostcards.__init__.load_config).

Fonctionnement : chaque appelant (ex : ``_ClipEmbedder``) possède un
``IdleUnloadTimer`` et appelle ``touch()`` à chaque utilisation
réussie du modèle. Un unique thread "watchdog" par timer vérifie
périodiquement si le délai configuré est dépassé depuis le dernier
``touch()`` et, si oui, appelle une fois ``unload_callback`` (fourni
par l'appelant : à lui de vider ses propres références et de rappeler
``torch.cuda.empty_cache()``) puis se réarme automatiquement pour la
prochaine période d'inactivité une fois le modèle rechargé (le
prochain ``touch()`` recommence le décompte).

Ce n'est PAS une garantie stricte : la vérification n'a lieu qu'à
intervalles réguliers (pas immédiatement au dépassement du délai), et
rien n'interrompt une requête déjà en cours d'utiliser le modèle au
moment où le déchargement se produit -- voir la note sur les
références Python dans le docstring de embedder.py. Sur un serveur GPU
partagé, un délai de quelques minutes est un compromis raisonnable
entre "rendre la mémoire rapidement" et "éviter de recharger le modèle
(donc de re-payer le coût réseau/initialisation) à chaque courte
pause entre deux requêtes".
"""

from __future__ import annotations

import logging
import threading
import time

_logger = logging.getLogger(__name__)


class IdleUnloadTimer:
    """Déclenche ``unload_callback()`` après ``idle_timeout`` secondes
    sans ``touch()``. Voir le docstring du module pour le détail."""

    def __init__(self, name: str, unload_callback) -> None:
        self._name = name
        self._unload_callback = unload_callback
        self._state_lock = threading.Lock()
        self._last_used: float | None = None
        self._idle_timeout: float | None = None
        self._thread: threading.Thread | None = None

    def touch(self) -> None:
        """À appeler à chaque utilisation réussie du modèle surveillé
        (ex : juste après une inférence) : redémarre le décompte
        d'inactivité."""
        self._last_used = time.monotonic()

    def configure(self, idle_timeout: float | None) -> None:
        """
        (Re)configure le délai d'inactivité, en secondes. ``None`` ou
        ``0`` désactive la libération automatique (comportement
        historique : le modèle reste chargé indéfiniment une fois
        utilisé). Démarre le thread de surveillance au tout premier
        appel avec une valeur réelle -- rappeler ``configure()`` avec
        une nouvelle valeur ensuite ne recrée pas de second thread.
        """
        with self._state_lock:
            self._idle_timeout = idle_timeout if idle_timeout else None
            if self._idle_timeout and self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name=f"idle-unload-{self._name}")
                self._thread.start()

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
        if not timeout or last_used is None:
            return

        idle_for = time.monotonic() - last_used
        if idle_for < timeout:
            return

        # Évite de redéclencher en boucle à chaque passage du
        # watchdog tant que rien n'a retouché le modèle (touch() le
        # réarmera naturellement au prochain rechargement réel).
        self._last_used = None

        _logger.info(
            "%s : inactif depuis %.0fs (> %.0fs), libération de la mémoire GPU",
            self._name, idle_for, timeout,
        )
        try:
            self._unload_callback()
        except Exception:
            _logger.exception("%s : échec de la libération automatique", self._name)
