import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# Cache des fichiers JSON de configuration, rechargés automatiquement
# quand leur mtime change : on peut ajouter/révoquer une clé API ou
# modifier les niveaux sans redémarrer le service (et sans relire le
# fichier à chaque requête).
_json_cache: dict[str, tuple[int, Any]] = {}


def read_json_cached(path: str) -> Any:
    st = os.stat(path)
    cached = _json_cache.get(path)
    if cached is not None and cached[0] == st.st_mtime_ns:
        return cached[1]
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    _json_cache[path] = (st.st_mtime_ns, data)
    return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KV_")

    # Fichier contenant la clé de chiffrement maîtresse (Fernet, 32 octets
    # urlsafe-base64). Protégé uniquement par les permissions du système
    # de fichiers (chmod 600, root:root) — voir scripts/generate_master_key.py
    master_key_file: str = "/run/secrets/master.key"

    # Base SQLite contenant les secrets chiffrés
    db_path: str = "/data/secrets.db"

    # Base SQLite des compteurs d'appels et des demandes de clé.
    stats_db_path: str = "/data/gateway.db"

    # Fichier JSON listant les clés API (par hash SHA256) :
    # { "hash_sha256_de_la_cle": {
    #       "name": "client-a",
    #       "level": "basic",                      # niveau (voir levels_file)
    #       "scopes": ["read:secrets"],            # droits supplémentaires
    #       "expires_at": "2027-01-31",            # optionnel
    #       "disabled": false                      # optionnel
    # } }
    clients_file: str = "/run/secrets/api_clients.json"

    # Fichier JSON des sites flpostcards rattachés aux clés (voir app/sites.py).
    sites_file: str = "/run/secrets/api_sites.json"

    # Fichier JSON décrivant les niveaux de clés (guest/basic/premium/gold,
    # extensible). Absent => niveaux par défaut (voir app/levels.py).
    levels_file: str = "/run/secrets/api_levels.json"

    # --- Passerelle ------------------------------------------------------
    # Serveurs situés derrière la passerelle. Un upstream vide => 501 sur
    # les routes qui en dépendent.
    simpostcards_url: str = "http://simpostcards:8004"
    flpostcards_url: str = "http://flpostcards:5000"
    # Réservé : stockage de sauvegardes (niveau gold). Aucun serveur de
    # sauvegarde n'existe encore côté KartoTek : vide => 501.
    backup_url: str = ""

    # Préfixes des blueprints de synchronisation (publication), tels que
    # configurés côté serveurs ([flask]/[simpostcards] sync_url_prefix).
    flpostcards_sync_prefix: str = "/api/v1/sync"
    simpostcards_sync_prefix: str = "/sync/v1"

    # Délai max (secondes) d'attente d'une réponse d'un upstream
    # (la détection/l'OCR sur GPU peuvent être lents).
    upstream_timeout: float = 120.0
    # Taille max d'un corps de requête relayé, en Mo (>= sync_max_body_mb
    # de flpostcards, 32 par défaut).
    max_body_mb: int = 32

    # --- Page de demande de clé (/request-key) -----------------------------
    requests_enabled: bool = True
    # Nombre max de demandes par adresse IP et par heure.
    request_rate_per_hour: int = 3
    # Au-delà, la page se ferme (anti-inondation) jusqu'au traitement.
    requests_max_pending: int = 200
    # Derrière nginx, true => l'IP du client est la dernière entrée de
    # X-Forwarded-For (posée par nginx). Ne l'activer que derrière un proxy
    # de confiance : sinon l'en-tête est falsifiable.
    trust_forwarded_for: bool = False
    # Nom du service affiché sur la page.
    service_name: str = "KartoTek"

    @property
    def clients(self) -> dict:
        return read_json_cached(self.clients_file)

    @property
    def levels_raw(self) -> dict | None:
        """Contenu de levels_file, ou None s'il n'existe pas."""
        if not Path(self.levels_file).is_file():
            return None
        return read_json_cached(self.levels_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
