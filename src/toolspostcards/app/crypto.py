from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status


class Crypto:
    def __init__(self, key_path: str):
        raw = Path(key_path).read_bytes().strip()
        self._fernet = Fernet(raw)

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            # Clé maîtresse incorrecte ou donnée corrompue : on ne donne
            # aucun détail au client, juste une erreur serveur générique.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Impossible de déchiffrer le secret",
            )


@lru_cache
def get_crypto() -> "Crypto":
    from .config import get_settings

    return Crypto(get_settings().master_key_file)
