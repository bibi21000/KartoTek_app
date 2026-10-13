import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name        TEXT PRIMARY KEY,
    ciphertext  BLOB NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    client      TEXT NOT NULL,
    action      TEXT NOT NULL,
    secret_name TEXT NOT NULL
);
"""


class SecretStore:
    """Stockage chiffré des secrets. Aucune notion de 'seal' :
    la clé de chiffrement est fournie une fois au démarrage (voir crypto.py)
    et le store est utilisable immédiatement, y compris après un redémarrage
    automatique du container."""

    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _audit(self, conn, client: str, action: str, secret_name: str):
        conn.execute(
            "INSERT INTO audit_log (ts, client, action, secret_name) VALUES (?, ?, ?, ?)",
            (time.time(), client, action, secret_name),
        )

    def read(self, name: str, client: str) -> bytes:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            self._audit(conn, client, "read", name)
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Secret introuvable"
                )
            return row[0]

    def write(self, name: str, ciphertext: bytes, client: str) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT version FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            version = (existing[0] + 1) if existing else 1
            conn.execute(
                """
                INSERT INTO secrets (name, ciphertext, created_at, updated_at, version)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    updated_at = excluded.updated_at,
                    version = excluded.version
                """,
                (name, ciphertext, now, now, version),
            )
            self._audit(conn, client, "write", name)
            return version

    def delete(self, name: str, client: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM secrets WHERE name = ?", (name,))
            self._audit(conn, client, "delete", name)

    def list_names(self) -> list[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT name FROM secrets ORDER BY name").fetchall()
            return [r[0] for r in rows]


@lru_cache
def get_store() -> SecretStore:
    from .config import get_settings

    return SecretStore(get_settings().db_path)
