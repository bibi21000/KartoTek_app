"""
flpostcards/metrics.py - Télémétrie légère en mémoire : compteurs de
requêtes par endpoint (nombre total, répartition par classe de statut
HTTP 2xx/3xx/4xx/5xx, latence moyenne/max), exposée aux managers via
GET /api/v1/metrics (voir blueprints/api/__init__.py).

Volontairement minimal : aucune dépendance externe (pas de Prometheus,
Sentry, StatsD...), aucune persistance -- juste de quoi répondre à
"est-ce que ça part en vrille en prod ?" sans rien installer de plus.
Ce n'est PAS un remplacement d'un vrai APM si le trafic grossit ou si
un historique dans le temps devient nécessaire (ici, tout est remis à
zéro à chaque redémarrage du process).

Limite connue et assumée : ces compteurs vivent en mémoire du process
Python, comme le stockage "memory://" par défaut de Flask-Limiter (voir
RATELIMIT_STORAGE_URI dans flpostcards/__init__.py) -- avec plusieurs
workers gunicorn, chaque worker a SES PROPRES compteurs :
GET /api/v1/metrics ne reflète que le worker qui a traité CETTE
requête, pas la somme de tous les workers. Suffisant pour un contrôle
ponctuel après une mise en prod (quelques appels successifs finissent
par toucher chaque worker), pas pour un graphe de suivi continu -- le
jour où ce besoin se présente, exportez ces mêmes chiffres vers un
backend partagé (Redis, Prometheus) plutôt que d'complexifier ce module.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_start_time = time.monotonic()


def _new_entry() -> dict[str, float]:
    return {
        "count": 0,
        "status_2xx": 0,
        "status_3xx": 0,
        "status_4xx": 0,
        "status_5xx": 0,
        "total_duration_s": 0.0,
        "max_duration_s": 0.0,
    }


_counters: dict[str, dict[str, float]] = defaultdict(_new_entry)


def record(endpoint: str, status_code: int, duration_s: float) -> None:
    """
    Enregistre une requête terminée. Appelé depuis un ``after_request``
    (voir flpostcards.create_app) pour chaque requête, y compris les
    pages HTML -- pas seulement l'API -- ``endpoint`` est le nom Flask
    de la vue (``request.endpoint``, ex. "api_v1.nearby"), plus stable
    dans le temps qu'un chemin d'URL brut (qui varie avec les
    paramètres de route).
    """
    bucket = f"status_{status_code // 100}xx"
    with _lock:
        entry = _counters[endpoint]
        entry["count"] += 1
        if bucket in entry:
            entry[bucket] += 1
        entry["total_duration_s"] += duration_s
        entry["max_duration_s"] = max(entry["max_duration_s"], duration_s)


def snapshot() -> dict:
    """
    Retourne un instantané sérialisable en JSON, trié par nombre de
    requêtes décroissant -- utilisé par GET /api/v1/metrics.
    """
    with _lock:
        raw = {endpoint: dict(entry) for endpoint, entry in _counters.items()}

    endpoints = {}
    for endpoint, entry in sorted(raw.items(), key=lambda kv: kv[1]["count"], reverse=True):
        count = entry["count"] or 1  # entry n'existe que si count >= 1, garde-fou seulement
        endpoints[endpoint] = {
            "count": entry["count"],
            "status_2xx": entry["status_2xx"],
            "status_3xx": entry["status_3xx"],
            "status_4xx": entry["status_4xx"],
            "status_5xx": entry["status_5xx"],
            "avg_duration_ms": round(entry["total_duration_s"] / count * 1000, 1),
            "max_duration_ms": round(entry["max_duration_s"] * 1000, 1),
        }

    return {
        "uptime_s": round(time.monotonic() - _start_time, 1),
        "endpoints": endpoints,
        "note": (
            "Compteurs en mémoire de ce worker uniquement (voir la "
            "docstring du module) -- non cumulés entre workers gunicorn."
        ),
    }
