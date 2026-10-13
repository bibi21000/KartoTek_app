import json
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KV_")

    # Fichier contenant la clé de chiffrement maîtresse (Fernet, 32 octets
    # urlsafe-base64). Protégé uniquement par les permissions du système
    # de fichiers (chmod 600, root:root) — voir scripts/generate_master_key.py
    master_key_file: str = "/run/secrets/master.key"

    # Base SQLite contenant les secrets chiffrés
    db_path: str = "/data/secrets.db"

    # Fichier JSON listant les clients autorisés à interroger l'API :
    # { "hash_sha256_de_la_cle": {"name": "service-a", "scopes": ["read:secrets"]} }
    clients_file: str = "/run/secrets/api_clients.json"

    @property
    def clients(self) -> dict:
        return json.loads(Path(self.clients_file).read_text())


@lru_cache
def get_settings() -> Settings:
    return Settings()
