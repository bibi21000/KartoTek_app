"""Base SQLite de la passerelle : compteurs d'appels et demandes de clé.

Distincte de secrets.db (coffre chiffré) : on peut la purger, la sauvegarder
ou l'interroger sans toucher aux secrets. Les clés y sont désignées par leur
``key_id`` (12 premiers caractères du hash SHA256), jamais par la clé.

Les compteurs sont agrégés par (jour UTC, clé, capacité) : la table reste
petite quelle que soit la volumétrie, et suffit à facturer ou plafonner.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    day         TEXT NOT NULL,           -- YYYY-MM-DD (UTC)
    key_id      TEXT NOT NULL,
    capability  TEXT NOT NULL,
    name        TEXT NOT NULL,           -- dernier nom/niveau vus (informatif)
    level       TEXT,
    calls       INTEGER NOT NULL DEFAULT 0,   -- appels relayés
    errors      INTEGER NOT NULL DEFAULT 0,   -- dont réponses >= 400 / upstream KO
    denied      INTEGER NOT NULL DEFAULT 0,   -- refusés (niveau insuffisant)
    bytes_in    INTEGER NOT NULL DEFAULT 0,
    bytes_out   INTEGER NOT NULL DEFAULT 0,
    last_ts     REAL NOT NULL,
    PRIMARY KEY (day, key_id, capability)
);
CREATE INDEX IF NOT EXISTS usage_by_key ON usage_daily (key_id, day);

CREATE TABLE IF NOT EXISTS key_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  REAL NOT NULL,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    org         TEXT NOT NULL DEFAULT '',
    use_case    TEXT NOT NULL,
    level_wanted TEXT NOT NULL,
    ip_hash     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    decided_at  REAL,
    key_id      TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS requests_status ON key_requests (status, created_at);
"""


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


class StatsStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with closing(self._connect()) as conn:
            # WAL : le manager (CLI) peut lire pendant que la passerelle écrit.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------ compteurs
    def record_call(
        self, key_id: str, name: str, level: str | None, capability: str, *,
        calls: int = 0, errors: int = 0, denied: int = 0,
        bytes_in: int = 0, bytes_out: int = 0,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO usage_daily
                    (day, key_id, capability, name, level, calls, errors, denied,
                     bytes_in, bytes_out, last_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, key_id, capability) DO UPDATE SET
                    name = excluded.name, level = excluded.level,
                    calls = calls + excluded.calls,
                    errors = errors + excluded.errors,
                    denied = denied + excluded.denied,
                    bytes_in = bytes_in + excluded.bytes_in,
                    bytes_out = bytes_out + excluded.bytes_out,
                    last_ts = excluded.last_ts
                """,
                (today(), key_id, capability, name, level, calls, errors, denied,
                 bytes_in, bytes_out, time.time()),
            )

    def totals_by_capability(self, key_id: str, since_day: str) -> dict[str, dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT capability, SUM(calls) calls, SUM(errors) errors,
                          SUM(denied) denied, SUM(bytes_in) bytes_in,
                          SUM(bytes_out) bytes_out
                   FROM usage_daily WHERE key_id = ? AND day >= ?
                   GROUP BY capability ORDER BY capability""",
                (key_id, since_day),
            ).fetchall()
        return {r["capability"]: dict(r) for r in rows}

    def totals_by_key(self, since_day: str) -> dict[str, dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT key_id, SUM(calls) calls, SUM(errors) errors,
                          SUM(denied) denied, SUM(bytes_in) bytes_in,
                          SUM(bytes_out) bytes_out, MAX(last_ts) last_ts
                   FROM usage_daily WHERE day >= ? GROUP BY key_id""",
                (since_day,),
            ).fetchall()
        return {r["key_id"]: dict(r) for r in rows}

    def totals_by_day(self, since_day: str, key_id: str | None = None) -> list[dict]:
        sql = ("SELECT day, SUM(calls) calls, SUM(errors) errors, SUM(denied) denied, "
               "SUM(bytes_in) bytes_in, SUM(bytes_out) bytes_out "
               "FROM usage_daily WHERE day >= ?")
        args: list = [since_day]
        if key_id:
            sql += " AND key_id = ?"
            args.append(key_id)
        sql += " GROUP BY day ORDER BY day"
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -------------------------------------------------------------- demandes
    def add_request(self, *, name: str, email: str, org: str, use_case: str,
                    level_wanted: str, ip_hash: str) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """INSERT INTO key_requests
                   (created_at, name, email, org, use_case, level_wanted, ip_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), name, email, org, use_case, level_wanted, ip_hash),
            )
            return cur.lastrowid

    def count_recent_requests(self, ip_hash: str, since_ts: float) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM key_requests WHERE ip_hash = ? AND created_at >= ?",
                (ip_hash, since_ts),
            ).fetchone()[0]

    def count_pending(self) -> int:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM key_requests WHERE status = 'pending'"
            ).fetchone()[0]

    def has_pending_email(self, email: str) -> bool:
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT 1 FROM key_requests WHERE status = 'pending' AND lower(email) = ?",
                (email.lower(),),
            ).fetchone() is not None

    def list_requests(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM key_requests", []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY id"
        with closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def get_request(self, request_id: int) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM key_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def decide_request(self, request_id: int, status: str, *,
                       key_id: str | None = None, note: str | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE key_requests SET status = ?, decided_at = ?, key_id = ?, note = ? "
                "WHERE id = ?",
                (status, time.time(), key_id, note, request_id),
            )


@lru_cache
def get_stats() -> StatsStore:
    from .config import get_settings

    return StatsStore(get_settings().stats_db_path)
