"""Niveaux de clés API.

Un niveau est un ensemble nommé de *capacités* (``publish``, ``image``,
``ocr``...) ; chaque route de la passerelle exige une capacité (voir
routes.py). Les niveaux sont ordonnés (``rank``) et peuvent hériter d'un
autre (``extends``) : « basic = guest + traitement d'images ».

Les niveaux par défaut ci-dessous sont remplacés EN ENTIER par le contenu de
``KV_LEVELS_FILE`` s'il existe : ajouter un niveau, déplacer une capacité de
« premium » à « gold »... ne demande donc qu'une édition de ce fichier.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_LEVELS: dict[str, dict] = {
    "guest": {
        "rank": 10,
        "description": "Publication web (cartes + thème)",
        "capabilities": ["publish"],
    },
    "basic": {
        "rank": 20,
        "extends": "guest",
        "description": "Guest + traitement des images",
        "capabilities": ["image"],
    },
    "premium": {
        "rank": 30,
        "extends": "basic",
        "description": "Basic + OCR et détection",
        "capabilities": ["ocr", "detection", "translate"],
    },
    "gold": {
        "rank": 40,
        "extends": "premium",
        "description": "Accès total : premium + parcours (travels) + sauvegarde",
        "capabilities": ["travel", "backup", "publish:restart"],
    },
}


def scope_matches(granted: str, required: str) -> bool:
    """``granted`` couvre-t-il ``required`` ?

    ``*`` couvre tout ; ``read:*`` couvre ``read:secrets`` (et ``read``).
    """
    if granted == required or granted == "*":
        return True
    if granted.endswith(":*"):
        base = granted[:-2]
        return required == base or required.startswith(base + ":")
    return False


class LevelsError(ValueError):
    pass


@dataclass(frozen=True)
class Level:
    name: str
    rank: int
    description: str = ""
    # Capacités propres + héritées (déjà aplaties).
    capabilities: frozenset[str] = field(default_factory=frozenset)


class Levels:
    def __init__(self, raw: dict[str, dict]):
        if not isinstance(raw, dict) or not raw:
            raise LevelsError("la définition des niveaux doit être un objet non vide")
        self._levels: dict[str, Level] = {}
        for name in raw:
            self._levels[name] = self._resolve(name, raw, ())

    @classmethod
    def _resolve(cls, name: str, raw: dict, chain: tuple[str, ...]) -> Level:
        if name in chain:
            raise LevelsError("héritage circulaire : " + " -> ".join(chain + (name,)))
        meta = raw[name]
        if not isinstance(meta, dict):
            raise LevelsError(f"niveau '{name}' : objet attendu")
        caps = set(meta.get("capabilities", []))
        parent = meta.get("extends")
        if parent is not None:
            if parent not in raw:
                raise LevelsError(f"niveau '{name}' : 'extends' inconnu '{parent}'")
            caps |= cls._resolve(parent, raw, chain + (name,)).capabilities
        try:
            rank = int(meta["rank"])
        except (KeyError, TypeError, ValueError):
            raise LevelsError(f"niveau '{name}' : 'rank' (entier) obligatoire")
        return Level(name, rank, str(meta.get("description", "")), frozenset(caps))

    def get(self, name: str) -> Level | None:
        return self._levels.get(name)

    def names(self) -> list[str]:
        return [lv.name for lv in sorted(self._levels.values(), key=lambda lv: lv.rank)]

    def min_level_for(self, capability: str) -> str | None:
        """Plus petit niveau (par rang) qui accorde ``capability``."""
        for lv in sorted(self._levels.values(), key=lambda lv: lv.rank):
            if any(scope_matches(g, capability) for g in lv.capabilities):
                return lv.name
        return None


def get_levels() -> Levels:
    # Pas de lru_cache : read_json_cached ne relit le fichier que si son mtime
    # change, et la construction (quelques niveaux) est négligeable.
    from .config import get_settings

    raw = get_settings().levels_raw
    return Levels(raw if raw is not None else DEFAULT_LEVELS)
