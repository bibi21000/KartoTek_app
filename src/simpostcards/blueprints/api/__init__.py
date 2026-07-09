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
    image_bytes = _extract_image_bytes()
    if not image_bytes:
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    white_threshold = current_app.config.get("SCAN_WHITE_THRESHOLD", 240)

    try:
        image, report = correct_image(image_bytes, white_threshold=white_threshold)
    except ImageDecodeError as exc:
        return jsonify({"error": str(exc)}), 400

    hashes = compute_hashes(image)

    return jsonify({
        "status": "ok",
        "hashes": hashes,
        "correction": report,
    })
