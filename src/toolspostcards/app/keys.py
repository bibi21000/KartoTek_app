"""Gestion du fichier des clés (api_clients.json), partagée par le manager
(manage.py) et les tests.

Le fichier est la source de vérité lue par la passerelle (rechargé quand son
mtime change) : toute écriture est atomique (fichier temporaire + rename) et
conserve les permissions existantes, pour que la passerelle ne lise jamais un
fichier à moitié écrit.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class KeysError(Exception):
    """Erreur d'usage du manager (message destiné à l'opérateur)."""


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def new_raw_key() -> str:
    return "kt_" + secrets.token_urlsafe(32)


def key_id(hashed: str) -> str:
    return hashed[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save(path: str, clients: dict) -> None:
    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clients, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def resolve(clients: dict, ref: str) -> tuple[str, dict]:
    """Retrouve une clé par key_id (préfixe d'au moins 6 caractères) ou par
    nom exact. Ambigu ou inconnu => KeysError."""
    ref = ref.strip()
    matches = [h for h in clients if len(ref) >= 6 and h.startswith(ref.lower())]
    if not matches:
        matches = [h for h, m in clients.items() if m.get("name") == ref]
    if not matches:
        raise KeysError(f"clé introuvable : {ref}")
    if len(matches) > 1:
        ids = ", ".join(key_id(h) for h in matches)
        raise KeysError(f"référence ambiguë '{ref}' : {ids} (utilisez l'identifiant)")
    return matches[0], clients[matches[0]]


def add(path: str, meta: dict) -> tuple[str, str]:
    """Crée une clé ; renvoie (clé brute, hash). La clé brute n'est jamais
    écrite sur le disque."""
    clients = load(path)
    raw = new_raw_key()
    hashed = hash_key(raw)
    clients[hashed] = {**meta, "created_at": meta.get("created_at") or now_iso()}
    save(path, clients)
    return raw, hashed


def status(meta: dict, is_expired) -> str:
    if meta.get("disabled"):
        return "revoked" if meta.get("revoked_at") else "disabled"
    if meta.get("expires_at") and is_expired(meta["expires_at"]):
        return "expired"
    return "active"
