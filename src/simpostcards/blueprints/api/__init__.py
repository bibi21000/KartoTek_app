"""
Blueprint API de simpostcards : un seul point d'entrée.

  POST /api/compute_hashes

Reçoit l'image d'une carte postale (recto ou verso), la redresse et la
détoure (``libpostcards.scan_corrector.ScanCorrector``), puis
calcule et renvoie ses hashs perceptuels (``simpostcards.libs.hasher``)
sous forme de JSON.

Envoi de l'image (deux formats acceptés) :

  1. multipart/form-data, champ ``image`` :
       curl -F "image=@396_R.tiff" http://host/api/compute_hashes

  2. corps brut avec un Content-Type image/* :
       curl --data-binary @396_R.tiff -H "Content-Type: image/tiff" \\
            http://host/api/compute_hashes

Réponse (200) :
  {
    "status": "ok",
    "hashes": {"ahash": "...", "dhash": "...", "phash": "...", "whash": "..."},
    "correction": { ...rapport ScanCorrector (tailles, angle, ...)... }
  }

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible
  413                       -- image trop volumineuse (MAX_CONTENT_LENGTH)
"""

from __future__ import annotations

import time

from flask import Blueprint, current_app, jsonify, request

from simpostcards.libs.corrector import ImageDecodeError, correct_image
from simpostcards.libs.hasher import compute_hashes

bp = Blueprint("simpostcards_api", __name__)


def _extract_image_bytes() -> bytes | None:
    """
    Récupère les octets de l'image envoyée, quel que soit le format
    d'envoi utilisé par le client (voir docstring du module).
    """
    if request.files:
        file_storage = request.files.get("image")
        if file_storage is None:
            # Un seul champ fichier envoyé sous un autre nom : on le
            # prend quand même, pour rester tolérant côté client.
            file_storage = next(iter(request.files.values()), None)
        if file_storage is not None and file_storage.filename:
            return file_storage.read()

    data = request.get_data()
    if data:
        return data

    return None


@bp.route("/api/compute_hashes", methods=["POST"])
def compute_hashes_route():
    start = time.perf_counter()
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_hashes : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    current_app.logger.info(
        "compute_hashes : requête reçue, image=%d octets (from=%s)",
        len(image_bytes), remote,
    )

    white_threshold = current_app.config.get("SCAN_WHITE_THRESHOLD", 240)

    try:
        image, report = correct_image(image_bytes, white_threshold=white_threshold)
    except ImageDecodeError as exc:
        current_app.logger.warning(
            "compute_hashes : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": str(exc)}), 400

    hashes = compute_hashes(image)

    elapsed = time.perf_counter() - start
    current_app.logger.info(
        "compute_hashes : OK en %.2fs (image=%d octets, corrigée=%s, from=%s)",
        elapsed, len(image_bytes), report.get("final_size"), remote,
    )

    return jsonify({
        "status": "ok",
        "hashes": hashes,
        "correction": report,
    })
