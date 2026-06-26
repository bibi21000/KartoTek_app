"""
Blueprint API v1 : endpoints JSON pour une application mobile de
localisation de cartes postales.

Routes :
  GET  /api/v1/bounds          → zone GPS couverte par les cartes (rectangle)
  GET  /api/v1/nearby          → cartes dans un rayon autour d'une position
  GET  /api/v1/next-update     → délai recommandé avant le prochain poll
  POST /api/v1/update          → enregistre un repérage de carte sur le terrain

Authentification (endpoint POST) :
  Un fichier JSON datadir/auth.json liste les tokens autorisés :
      {"tokens": ["secret1", "secret2"]}
  Le token est passé dans le corps JSON, champ "auth".
"""

from __future__ import annotations

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
# Authentification
# ---------------------------------------------------------------------------

def _check_auth(token: str | None) -> bool:
    """
    Vérifie le token contre datadir/auth.json.
    Format du fichier : {"tokens": ["token1", "token2", ...]}
    Retourne False si le fichier est absent, malformé ou si le token
    ne correspond pas.
    """
    if not token:
        return False
    datadir = Path(current_app.config["DATADIR"])
    auth_path = datadir / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = data.get("tokens", [])
        return token in tokens
    except (OSError, json.JSONDecodeError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@bp.route("/api/v1/bounds")
def bounds():
    """
    Zone GPS couverte par l'ensemble des cartes postales géolocalisées.

    Retourne le rectangle englobant (bounding box) sous la forme :
      { "min_lat", "max_lat", "min_lon", "max_lon", "count" }

    Utile pour initialiser la vue d'une carte mobile ou vérifier si la
    position courante de l'utilisateur est dans la zone couverte.
    """
    model = current_app.model
    cards = _cards_with_coord(model)

    if not cards:
        return jsonify({"count": 0, "bounds": None})

    lats = [c["coord"][0] for c in cards]
    lons = [c["coord"][1] for c in cards]

    return jsonify({
        "count": len(cards),
        "bounds": {
            "min_lat": min(lats),
            "max_lat": max(lats),
            "min_lon": min(lons),
            "max_lon": max(lons),
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

    Le délai dépend de :
      - la vitesse de déplacement (m/s) : plus on va vite, plus on doit
        rafraîchir souvent, car la zone visible change rapidement ;
      - le rayon de recherche (m) : un grand rayon est couvert plus
        longtemps avant que de nouvelles cartes y entrent ;
      - la proximité de la carte la plus proche : si une carte est déjà
        très proche du bord du rayon, on rafraîchit bientôt.

    Paramètres de requête :
      lat    : latitude (float)
      lon    : longitude (float)
      radius : rayon de recherche en mètres (float)
      speed  : vitesse de déplacement en m/s (float, 0 = immobile)

    Formule :
      Si vitesse > 0 :
        délai_base = (rayon - distance_min) / vitesse
          où distance_min est la distance à la carte la plus proche
          dans le rayon (ou rayon entier si aucune carte n'est dedans)
        délai = clamp(délai_base, POLL_MIN, POLL_MAX)
      Si vitesse = 0 (immobile) :
        délai = POLL_MAX (inutile de rafraîchir si on ne bouge pas)
    """
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        radius = min(float(request.args["radius"]), 50_000)
        speed = max(float(request.args.get("speed", 0)), 0.0)
    except (KeyError, ValueError):
        return jsonify({"error": "lat, lon, radius (et optionnellement speed) sont obligatoires"}), 400

    if speed <= 0:
        return jsonify({
            "next_update_s": _POLL_MAX_S,
            "reason": "immobile",
        })

    model = current_app.model
    cards = _cards_with_coord(model)

    # Distance à la carte la plus proche dans le rayon
    min_dist_in_radius: float | None = None
    for card in cards:
        dist = _haversine(lat, lon, card["coord"][0], card["coord"][1])
        if dist <= radius:
            if min_dist_in_radius is None or dist < min_dist_in_radius:
                min_dist_in_radius = dist

    # Si aucune carte dans le rayon, on utilise le rayon entier comme
    # référence (on vérifiera quand on aura parcouru l'équivalent du rayon)
    effective_distance = min_dist_in_radius if min_dist_in_radius is not None else radius

    # Délai = temps pour parcourir (rayon - distance_min) à la vitesse actuelle
    # Une carte au centre du rayon donne un délai long ; une carte au bord
    # du rayon donne un délai court (elle risque de sortir bientôt)
    remaining = max(radius - effective_distance, 0)
    delay = remaining / speed

    delay = max(_POLL_MIN_S, min(delay, _POLL_MAX_S))

    return jsonify({
        "next_update_s": round(delay, 1),
        "reason": "moving",
        "speed_ms": speed,
        "radius_m": radius,
        "nearest_card_m": round(min_dist_in_radius, 1) if min_dist_in_radius is not None else None,
    })


@bp.route("/api/v1/update", methods=["POST"])
def update():
    """
    Enregistre le repérage d'une carte postale sur le terrain.

    Corps JSON (Content-Type: application/json) :
      {
        "auth"    : "token secret",
        "card_id" : "123",
        "lat"     : 46.749,
        "lon"     : 5.620
      }

    Si l'authentification réussit, enregistre le repérage dans
    datadir/updates.json sous la forme d'une liste d'entrées :
      { "card_id", "lat", "lon", "ts" (timestamp UNIX) }

    Retourne :
      200 { "status": "ok", "card_id": "...", "ts": ... }
      401 { "error": "unauthorized" }
      400 { "error": "..." }       — champ manquant ou invalide
    """
    data: dict[str, Any] = request.get_json(silent=True) or {}

    token = data.get("auth")
    if not _check_auth(token):
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
    entry = {"card_id": card_id, "lat": lat, "lon": lon, "ts": ts}

    datadir = Path(current_app.config["DATADIR"])
    updates_path = datadir / "updates.json"

    # Lecture de la liste existante (crée si absente)
    try:
        updates: list[dict] = json.loads(updates_path.read_text(encoding="utf-8"))
        if not isinstance(updates, list):
            updates = []
    except (OSError, json.JSONDecodeError):
        updates = []

    updates.append(entry)

    # Écriture atomique via fichier temporaire (évite la corruption)
    tmp_path = updates_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(updates, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(updates_path)

    return jsonify({"status": "ok", "card_id": card_id, "ts": ts})
