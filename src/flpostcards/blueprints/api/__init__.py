"""
Blueprint API v1 : endpoints JSON pour une application mobile de
localisation de cartes postales.

Routes :
  GET  /api/v1/dbid          → hash du fichier postcards.sqlite (détection de changement)
  GET  /api/v1/gps           → coordonnées GPS paginées (sans doublons, curseur after_id)
  GET  /api/v1/bounds        → zone GPS couverte par les cartes (rectangle)
  GET  /api/v1/nearby        → cartes dans un rayon autour d'une position
  GET  /api/v1/next-update   → délai recommandé avant le prochain poll
  POST /api/v1/update        → enregistre un repérage de carte sur le terrain

Authentification (endpoint POST) :
  Utilise la table ``auths`` de la base SQLite via ``model.check_auth()``.
  Les comptes sont créés avec ``model.write_auth(email, password)``.
  Le corps JSON doit contenir les champs ``email`` et ``password``.
"""

from __future__ import annotations

import os
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("api_v1", __name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Rayon de la Terre en mètres (WGS-84 approx.)
_EARTH_R = 6_371_000.0

# Délai minimum entre deux polls (secondes), même en mouvement rapide
_POLL_MIN_S = 10
# Délai maximum entre deux polls quand aucune carte n'est proche
_POLL_MAX_S = 300


# ---------------------------------------------------------------------------
# Helpers géographiques
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance en mètres entre deux points (formule de Haversine)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * _EARTH_R * math.asin(math.sqrt(a))


def _cards_with_coord(model) -> list[dict]:
    """Retourne toutes les cartes uniques ayant des coordonnées GPS."""
    return [
        c for c in model.list_unique_cards()
        if c.get("coord") and c["coord"][0] is not None and c["coord"][1] is not None
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/api/v1/dbid")
def dbid():
    """
    Retourne un hash SHA1 (12 premiers caractères) du fichier
    postcards.sqlite (et de son fichier WAL s'il existe), permettant à
    un client de détecter si la base a changé depuis son dernier appel
    (nouvelles cartes, coordonnées GPS mises à jour, etc.) sans
    télécharger ni interroger la base entière.

    Le fichier -wal est inclus dans le hash car SQLite en mode WAL
    n'écrit pas immédiatement dans le fichier principal.

    Réponse JSON : { "hash": "abc123def456", "mtime": 1234567890 }
    """
    db_path = Path(current_app.config["DATADIR"]) / "postcards.sqlite"
    if not db_path.exists():
        return jsonify({"error": "database not found"}), 404

    h = hashlib.sha1()
    candidates = [db_path, Path(str(db_path) + "-wal")]
    latest_mtime = 0

    for path in candidates:
        if not path.exists():
            continue
        stat = path.stat()
        latest_mtime = max(latest_mtime, int(stat.st_mtime))
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)

    return jsonify({"hash": h.hexdigest()[:12], "mtime": latest_mtime})


@bp.route("/api/v1/gps")
def gps():
    """
    Liste paginée des coordonnées GPS des cartes uniques (sans doublons)
    ayant une position renseignée, triées par id numérique croissant.

    Paramètres de requête (optionnels) :
      after_id : id numérique de la dernière carte reçue à la page
                 précédente (défaut 0 pour démarrer). Pagination par
                 curseur : stable même si la base change pendant le
                 parcours des pages (ajout/màj/suppression de cartes).
      offset   : nombre de résultats à retourner (défaut 500, max 2000)

      start    : (déprécié) index OFFSET dans la liste. Conservé pour
                 compatibilité ascendante uniquement si `after_id`
                 n'est pas fourni. À éviter : une pagination OFFSET
                 peut renvoyer des cartes en double ou en sauter si la
                 base est modifiée entre deux appels (tri non figé
                 entre requêtes distinctes). Migrer vers `after_id`.

    Réponse JSON :
      {
        "count": 42,             -- nombre de résultats dans cette page
        "total": 150,            -- nombre total de cartes GPS sans doublons
        "next_after_id": "187",  -- à repasser en after_id pour la page suivante (null si fin)
        "cards": [
          { "id": "1", "lat": 46.749, "lon": 5.620 },
          ...
        ]
      }

    Utilisation typique (recommandée) : appeler avec after_id=0, puis
    reprendre avec after_id=next_after_id jusqu'à ce que
    next_after_id soit null (ou count < offset).
    """
    try:
        limit = min(int(request.args.get("offset", 500)), 2000)
    except ValueError:
        return jsonify({"error": "offset doit être un entier"}), 400

    model = current_app.model

    if "start" in request.args and "after_id" not in request.args:
        # Ancien mode OFFSET/LIMIT — conservé pour compatibilité
        # ascendante, mais déconseillé : instable si la base change
        # pendant la pagination (doublons / cartes manquantes).
        try:
            start = max(int(request.args.get("start", 0)), 0)
        except ValueError:
            return jsonify({"error": "start doit être un entier"}), 400
        page_cards = [
            c for c in model.list_unique_cards(limit=limit, offset=start)
            if c.get("coord") and c["coord"][0] is not None and c["coord"][1] is not None
        ]
    else:
        try:
            after_id = max(int(request.args.get("after_id", 0)), 0)
        except ValueError:
            return jsonify({"error": "after_id doit être un entier"}), 400
        page_cards = model.list_unique_cards_with_coord(
            after_id=after_id, limit=limit
        )

    cards = [
        {"id": c["id"], "lat": c["coord"][0], "lon": c["coord"][1]}
        for c in page_cards
    ]

    # Total calculé avec exactement le même filtre (unique + GPS) que
    # la pagination, pour que "total" corresponde à ce qui peut
    # effectivement être renvoyé (auparavant ce total comptait aussi
    # les cartes marquées doublons, qui ne sortent jamais des pages).
    total = model.count_unique_cards_with_coord()

    next_after_id = None
    if len(cards) == limit:
        try:
            next_after_id = str(max(int(c["id"]) for c in cards))
        except ValueError:
            next_after_id = cards[-1]["id"]

    return jsonify({
        "count": len(cards),
        "total": total,
        "next_after_id": next_after_id,
        "cards": cards,
    })


@bp.route("/api/v1/bounds")
def bounds():
    """
    Zone GPS couverte par l'ensemble des cartes postales géolocalisées.

    Retourne le rectangle englobant (bounding box) sous la forme :
      { "min_lat", "max_lat", "min_lon", "max_lon", "count" }
    """
    model = current_app.model
    conn = model._get_conn()

    row = conn.execute(
        "SELECT COUNT(*), MIN(coord_lat), MAX(coord_lat), "
        "       MIN(coord_lon), MAX(coord_lon) "
        "FROM cards WHERE coord_lat IS NOT NULL AND coord_lon IS NOT NULL"
    ).fetchone()

    count = row[0] if row else 0
    if not count:
        return jsonify({"count": 0, "bounds": None})

    return jsonify({
        "count": count,
        "bounds": {
            "min_lat": row[1],
            "max_lat": row[2],
            "min_lon": row[3],
            "max_lon": row[4],
        },
    })


@bp.route("/api/v1/nearby")
def nearby():
    """
    Cartes postales dans un rayon autour d'une position GPS.

    Paramètres de requête (tous obligatoires) :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float, max 50 000)

    Retourne la liste des cartes triées par distance croissante, avec
    pour chaque carte : id, title, coord, distance_m, recto (size_div10).
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon et radius sont obligatoires (float)"}), 400

    model = current_app.model
    cards = _cards_with_coord(model)

    results = []
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            results.append({
                "id": card["id"],
                "title": card.get("title"),
                "coord": card["coord"],
                "distance_m": round(dist, 1),
                "recto": f"size_div10/{card['id']}_R.png",
            })

    results.sort(key=lambda x: x["distance_m"])

    return jsonify({"count": len(results), "cards": results})


@bp.route("/api/v1/next-update")
def next_update():
    """
    Délai recommandé (en secondes) avant le prochain appel à /api/v1/nearby.

    Paramètres de requête :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float)
      speed  : vitesse de déplacement en m/s (float, 0 = immobile)
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
        speed = max(float(request.args.get("speed", 0)), 0.0)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon, radius (et optionnellement speed) sont obligatoires"}), 400

    if speed <= 0:
        return jsonify({"next_update_s": _POLL_MAX_S, "reason": "immobile"})

    model = current_app.model
    cards = _cards_with_coord(model)

    min_dist_in_radius: float | None = None
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            if min_dist_in_radius is None or dist < min_dist_in_radius:
                min_dist_in_radius = dist

    effective_distance = min_dist_in_radius if min_dist_in_radius is not None else radius
    remaining = max(radius - effective_distance, 0)
    delay = max(_POLL_MIN_S, min(remaining / speed, _POLL_MAX_S))

    return jsonify({
        "next_update_s": round(delay, 1),
        "reason": "moving",
        "speed_ms": speed,
        "radius_m": radius,
        "nearest_card_m": round(min_dist_in_radius, 1) if min_dist_in_radius is not None else None,
    })


# ---------------------------------------------------------------------------
# Lockfile
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: Path) -> bool:
    """
    Tente d'acquérir un verrou exclusif via un fichier .lck.

    Utilise ``O_CREAT | O_EXCL`` qui est atomique sur POSIX : seul le
    processus qui crée le fichier en premier obtient le verrou.

    Attend jusqu'à ``LOCK_TIMEOUT`` secondes (config) que le fichier
    disparaisse si quelqu'un d'autre le tient, par sondages espacés de
    ``LOCK_POLL_INTERVAL`` secondes (config).
    Retourne True si le verrou est acquis, False en cas de timeout.
    """
    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    poll = current_app.config.get("LOCK_POLL_INTERVAL", 2.0)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)


def _release_lock(lock_path: Path) -> None:
    """Relâche le verrou en supprimant le fichier .lck."""
    try:
        lock_path.unlink()
    except OSError:
        pass


@bp.route("/api/v1/update", methods=["POST"])
def update():
    """
    Enregistre le repérage d'une carte postale sur le terrain.

    Corps JSON (Content-Type: application/json) :
      {
        "email"   : "utilisateur@example.com",
        "password": "mot de passe",
        "card_id" : "123",
        "lat"     : 46.749,
        "lon"     : 5.620
      }

    L'authentification est vérifiée via ``model.check_auth(email, password)``
    (table ``auths`` de la base SQLite, mots de passe hashés PBKDF2-SHA256).

    L'écriture dans ``updates.json`` est protégée par un lockfile
    ``updates.json.lck`` : si ce fichier existe, on attend jusqu'à 10
    secondes qu'il disparaisse avant d'écrire (protection contre les
    écritures concurrentes depuis plusieurs workers gunicorn).

    En cas de succès, enregistre le repérage dans datadir/updates.json :
      { "card_id", "email", "lat", "lon", "ts" (timestamp UNIX) }

    Codes de retour :
      200 { "status": "ok", "card_id": "...", "ts": ... }
      401 { "error": "unauthorized" }
      400 { "error": "..." }   — champ manquant ou invalide
      503 { "error": "..." }   — timeout sur le lockfile (rare)
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))

    if not current_app.model.check_auth(email, password):
        return jsonify({"error": "unauthorized"}), 401

    card_id = str(data.get("card_id", "")).strip()
    if not card_id:
        return jsonify({"error": "card_id est obligatoire"}), 400

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lat et lon sont obligatoires (float)"}), 400

    ts = int(time.time())
    entry = {"card_id": card_id, "email": email, "lat": lat, "lon": lon, "ts": ts}

    datadir = Path(current_app.config["DATADIR"])
    updates_path = datadir / "updates.json"
    lock_suffix = current_app.config.get("LOCK_SUFFIX", ".lck")
    lock_path = Path(str(updates_path) + lock_suffix)

    timeout = current_app.config.get("LOCK_TIMEOUT", 60.0)
    if not _acquire_lock(lock_path):
        return jsonify({
            "error": f"verrou {lock_path.name} toujours présent après {timeout:.0f}s"
        }), 503

    try:
        try:
            updates: list[dict] = json.loads(updates_path.read_text(encoding="utf-8"))
            if not isinstance(updates, list):
                updates = []
        except (OSError, json.JSONDecodeError):
            updates = []

        updates.append(entry)

        tmp_path = updates_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(updates_path)
    finally:
        _release_lock(lock_path)

    return jsonify({"status": "ok", "card_id": card_id, "ts": ts})
