import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings


@dataclass
class Client:
    name: str
    scopes: list[str]


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _scope_matches(granted: str, required: str) -> bool:
    # supporte les wildcards simples: "read:*" couvre "read:anything"
    if granted == required:
        return True
    if granted.endswith(":*"):
        prefix = granted[:-1]
        return required.startswith(prefix)
    return False


def get_client(
    x_api_key: str = Header(..., alias="X-API-Key"),
    settings: Settings = Depends(get_settings),
) -> Client:
    hashed = _hash_key(x_api_key)
    clients = settings.clients

    for stored_hash, meta in clients.items():
        if hmac.compare_digest(stored_hash, hashed):
            return Client(name=meta["name"], scopes=meta.get("scopes", []))

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Clé API invalide",
    )


def require_scope(required_scope: str):
    def _checker(client: Client = Depends(get_client)) -> Client:
        if not any(_scope_matches(s, required_scope) for s in client.scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Scope manquant: {required_scope}",
            )
        return client

    return _checker
