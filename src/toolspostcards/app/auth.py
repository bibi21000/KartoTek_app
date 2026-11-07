import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings
from .levels import Levels, LevelsError, get_levels, scope_matches

logger = logging.getLogger("kartotek_tools")


class ApiError(HTTPException):
    """Erreur JSON { "error", "detail", ...extra }.

    ``error`` est la clé lue par les clients remotesync ; ``detail`` celle
    que FastAPI utilise d'ordinaire.
    """

    def __init__(self, status_code: int, message: str, **extra):
        super().__init__(status_code=status_code, detail=message)
        self.extra = extra


@dataclass
class Client:
    name: str
    # Droits supplémentaires propres à la clé (ex: "read:secrets").
    scopes: list[str] = field(default_factory=list)
    # Niveau commercial (guest/basic/premium/gold...) ; None pour une clé
    # « technique » qui n'a que des scopes.
    level: str | None = None
    # Capacités du niveau (déjà aplaties, héritage compris).
    capabilities: frozenset[str] = frozenset()
    expires_at: str | None = None
    # 12 premiers caractères du hash : identifiant stable pour les compteurs
    # et le manager (la clé elle-même n'est jamais conservée).
    key_id: str = ""
    # Site flpostcards rattaché (voir sites.py) ; None = site par défaut.
    site: str | None = None

    def can(self, required: str) -> bool:
        return any(
            scope_matches(granted, required)
            for granted in (*self.capabilities, *self.scopes)
        )


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _is_expired(expires_at: str) -> bool:
    try:
        when = datetime.fromisoformat(expires_at)
    except ValueError:
        # Date illisible : on refuse la clé plutôt que de la croire illimitée.
        logger.error("expires_at illisible: %r (clé refusée)", expires_at)
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def get_client(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> Client:
    if not x_api_key:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Clé API manquante")

    # Le hash SHA256 est cherché dans un dict : un attaquant ne peut pas
    # progresser vers la clé en observant les temps de réponse, puisque
    # la clé n'est jamais comparée elle-même, seulement son empreinte.
    hashed = _hash_key(x_api_key)
    meta = settings.clients.get(hashed)
    if meta is None:
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Clé API invalide")

    name = meta["name"]
    if meta.get("disabled"):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Clé API désactivée")
    expires_at = meta.get("expires_at")
    if expires_at and _is_expired(expires_at):
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "Clé API expirée")

    capabilities: frozenset[str] = frozenset()
    level_name = meta.get("level")
    if level_name:
        try:
            level = get_levels().get(level_name)
        except LevelsError:
            logger.exception("Définition des niveaux invalide")
            raise ApiError(status.HTTP_500_INTERNAL_SERVER_ERROR, "Configuration des niveaux invalide")
        if level is None:
            # Niveau retiré de levels.json : on refuse (fail closed).
            logger.error("Clé '%s' : niveau inconnu '%s'", name, level_name)
            raise ApiError(status.HTTP_403_FORBIDDEN, f"Niveau de clé inconnu: {level_name}")
        capabilities = level.capabilities

    return Client(
        name=name,
        scopes=list(meta.get("scopes", [])),
        level=level_name,
        capabilities=capabilities,
        expires_at=expires_at,
        key_id=hashed[:12],
        site=meta.get("site") or None,
    )


def forbidden_for(client: Client, required: str, levels: Levels | None = None) -> ApiError:
    """Erreur 403 « capacité manquante », avec le niveau minimal qui l'accorde
    (utile à une appli cliente pour proposer une montée en gamme)."""
    min_level = (levels or get_levels()).min_level_for(required)
    msg = f"Capacité manquante: {required}"
    if min_level:
        msg += f" (niveau requis: {min_level})"
    return ApiError(
        status.HTTP_403_FORBIDDEN,
        msg,
        required_capability=required,
        required_level=min_level,
        your_level=client.level,
    )


def require_scope(required_scope: str):
    def _checker(client: Client = Depends(get_client)) -> Client:
        if not client.can(required_scope):
            raise forbidden_for(client, required_scope)
        return client

    return _checker
