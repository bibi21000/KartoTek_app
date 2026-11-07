"""Table des routes relayées par la passerelle : (méthodes, chemin) ->
(upstream, capacité requise).

Les capacités sont volontairement fines : c'est levels.json (voir
levels.py) qui décide quel niveau les accorde. Déplacer « translate » de
premium à gold ne demande donc aucune modification de code.

Tout chemin absent de cette table est refusé (404) : une nouvelle route de
simpostcards n'est jamais exposée tant qu'on ne l'a pas déclarée ici.

La première règle qui correspond l'emporte : les règles précises doivent
précéder les règles ``/*``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings

ALL_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})


@dataclass(frozen=True)
class Route:
    methods: frozenset[str]
    pattern: str  # chemin exact, ou préfixe terminé par "/*"
    upstream: str  # "simpostcards" | "flpostcards" | "backup"
    capability: str

    def matches_path(self, path: str) -> bool:
        if self.pattern.endswith("/*"):
            base = self.pattern[:-2]
            return path == base or path.startswith(base + "/")
        return path == self.pattern


def build_routes(settings: Settings) -> list[Route]:
    post = frozenset({"POST"})
    get = frozenset({"GET"})
    fl = "/" + settings.flpostcards_sync_prefix.strip("/")
    sim = "/" + settings.simpostcards_sync_prefix.strip("/")
    return [
        # --- Publication web (cartes + thème) : flpostcards et simpostcards
        # reçoivent les fichiers par le même protocole (remotesync, https).
        # Le redémarrage de l'appli web a sa propre capacité : la doc des
        # blueprints sync le veut plus strict que le reste.
        Route(post, f"{fl}/restart", "flpostcards", "publish:restart"),
        Route(ALL_METHODS, f"{fl}/*", "flpostcards", "publish"),
        Route(post, f"{sim}/restart", "simpostcards", "publish:restart"),
        Route(ALL_METHODS, f"{sim}/*", "simpostcards", "publish"),
        # --- Traitement des images
        Route(post, "/api/correct_scan", "simpostcards", "image"),
        Route(post, "/api/compute_hashes", "simpostcards", "image"),
        Route(post, "/api/compute_hashes_only", "simpostcards", "image"),
        # --- OCR, détection de contenu, traduction
        Route(get, "/api/ocr_languages", "simpostcards", "ocr"),
        Route(post, "/api/compute_ocr", "simpostcards", "ocr"),
        Route(post, "/api/compute_detection", "simpostcards", "detection"),
        Route(post, "/api/compute_segments", "simpostcards", "detection"),
        Route(post, "/api/translate", "simpostcards", "translate"),
        # --- Parcours optimisés
        Route(post, "/api/compute_travel", "simpostcards", "travel"),
        # --- Sauvegarde (réservé : voir KV_BACKUP_URL)
        Route(ALL_METHODS, "/backup/*", "backup", "backup"),
        # --- Opérationnel : jamais accordé par un niveau commercial (seul un
        # niveau interne avec "*", ou un scope explicite "admin", le permet).
        Route(post, "/api/unload_models", "simpostcards", "admin"),
    ]


def upstream_url(settings: Settings, upstream: str) -> str:
    return {
        "simpostcards": settings.simpostcards_url,
        "flpostcards": settings.flpostcards_url,
        "backup": settings.backup_url,
    }[upstream].strip().rstrip("/")


def resolve(routes: list[Route], method: str, path: str) -> tuple[Route | None, bool]:
    """(route, chemin_connu). ``chemin_connu`` vaut True si le chemin existe
    mais pas pour cette méthode (=> 405 plutôt que 404)."""
    path_known = False
    for route in routes:
        if route.matches_path(path):
            if method in route.methods:
                return route, True
            path_known = True
    return None, path_known
