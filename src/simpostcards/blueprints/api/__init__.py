"""
Blueprint API de simpostcards : cinq points d'entrée.

  POST /api/compute_hashes
  POST /api/compute_hashes_only
  POST /api/compute_travel
  POST /api/compute_ocr
  POST /api/compute_detection

--------------------------------------------------------------------
POST /api/compute_hashes
--------------------------------------------------------------------

Reçoit l'image d'une carte postale (recto ou verso), la redresse et la
détoure (``libpostcards.scan_corrector.ScanCorrector``), puis calcule
et renvoie ses hashs perceptuels (``simpostcards.libs.hasher``) et, si
disponible (voir ci-dessous), son embedding CLIP
(``simpostcards.libs.embedder``), sous forme de JSON.

L'embedding CLIP n'est calculé que si TOUTES ces conditions sont
réunies :
  - torch et open-clip-torch sont installés (``embedder.CLIP_AVAILABLE``) ;
  - [simpostcards] clip_enabled n'est pas explicitement à "false" dans
    postcards.conf (actif par défaut).
Sinon, le champ "embedding" de chaque rotation vaut ``None`` --
comportement historique de cette API (hashs perceptuels seuls),
inchangé pour un déploiement qui n'installe pas ces paquets. Un
client qui s'appuie sur cette API pour un index de similarité complet
(voir ``libpostcards.similar.PostcardSearcher.external_helper``) doit
donc être prêt à recevoir ``None`` et retomber sur une comparaison
hashs seuls dans ce cas (voir ``PostcardSearcher._combined_score``).

Envoi de l'image (deux formats acceptés) :

  1. multipart/form-data, champ ``image`` :
       curl -F "image=@396_R.tiff" http://host/api/compute_hashes

  2. corps brut avec un Content-Type image/* :
       curl --data-binary @396_R.tiff -H "Content-Type: image/tiff" \\
            http://host/api/compute_hashes

Champ de formulaire (ou paramètre de requête) optionnel :

  - "single_rotation" ("true"/"false", "false" par défaut) : ne
    calculer que l'angle 0 (pas les 3 autres rotations) -- jusqu'à 4x
    plus rapide, utile pour un client qui sait déjà que son image est
    correctement orientée (voir plus haut). La réponse ne contient
    alors qu'un seul élément dans "hashes".

Réponse (200) :
  {
    "status": "ok",
    "hashes": [
      {
        "angle": 0,
        "hashes": {"ahash": "...", "dhash": "...", "phash": "...", "whash": "..."},
        "embedding": [0.01, -0.03, ...] ou null
      },
      ... (idem pour 90, 180, 270°)
    ],
    "correction": { ...rapport ScanCorrector (tailles, angle, ...)... }
  }

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible
  413                       -- image trop volumineuse (MAX_CONTENT_LENGTH)

--------------------------------------------------------------------
POST /api/compute_hashes_only
--------------------------------------------------------------------

Comme /api/compute_hashes, mais pour une image déjà correctement
orientée et cadrée (un scan déjà importé dans la collection) : SANS le
redressement/détourage (``libpostcards.scan_corrector.ScanCorrector``)
ni les rotations multiples -- juste les hashs perceptuels (+ embedding
CLIP dans les mêmes conditions que /api/compute_hashes) de l'image
telle quelle. Utilisée par
``libpostcards.similar.PostcardSearcher.compute_hashes_only()`` pour
(re)construire l'index de similarité et pour y ajouter une carte qui
vient d'être ajoutée à la collection -- appliquer un redressement à
une image déjà cadrée serait au mieux inutile, au pire nuisible (un
nouveau recadrage décalerait légèrement l'image, faussant les hashs
par rapport à ceux déjà en index pour cette même carte).

Envoi de l'image : mêmes deux formats que /api/compute_hashes ci-dessus.

Réponse (200), plus légère que /api/compute_hashes (un seul jeu de
hashs/embedding, pas de liste par rotation, pas de rapport de
correction puisqu'il n'y en a pas) :
  {
    "status": "ok",
    "hashes": {"ahash": "...", "dhash": "...", "phash": "...", "whash": "..."},
    "embedding": [0.01, -0.03, ...] ou null
  }

Erreurs : mêmes codes que /api/compute_hashes ci-dessus.

--------------------------------------------------------------------
POST /api/compute_travel
--------------------------------------------------------------------

Calcule un parcours optimisé (départ -> cartes -> arrivée libre, sans
retour au départ) à travers un ensemble de cartes géolocalisées, via
OR-Tools (``libpostcards.travel.ParcoursCartes``), au lieu d'un calcul
local -- ortools n'est alors pas requis sur la machine appelante (voir
``ParcoursCartes.external_helper``). Attend un corps JSON, pas une
image :

  curl -X POST http://host/api/compute_travel \\
       -H "Content-Type: application/json" \\
       -d '{"start": [48.85, 2.35], "time_limit": 10,
            "cards": [{"id": "12", "title": "Paris", "coord": [48.86, 2.34]}]}'

Champs :
  - "start" (requis) : [latitude, longitude] du point de départ.
  - "cards" (requis) : liste de {"id", "title", "coord": [lat, lon]} --
    déjà filtrée par l'appelant (collection, présence de coordonnées,
    ...) : ce point d'entrée ne fait AUCUN filtrage, juste
    l'optimisation elle-même.
  - "time_limit" (optionnel, défaut 10) : limite en secondes laissée à
    OR-Tools pour la recherche de solution.

Réponse (200), même format que
``ParcoursCartes.calculer()`` :
  {
    "distance_m": 1234, "distance_km": 1.23,
    "start": [48.85, 2.35], "end": [48.86, 2.34],
    "count": 1, "cards": [{"id": "12", "title": "Paris"}]
  }

Erreurs :
  400 { "error": "..." }   -- corps JSON absent/invalide
  422 { "error": "..." }   -- OR-Tools n'a trouvé aucune solution
  501 { "error": "..." }   -- ortools non installé sur ce serveur, ou
                              [simpostcards] travel_enabled = false

--------------------------------------------------------------------
POST /api/compute_ocr
--------------------------------------------------------------------

Extrait le texte d'une image (recto ou verso, déjà redressée/détourée
-- pas de correction ici, contrairement à /api/compute_hashes) via
Tesseract OCR (``simpostcards.libs.ocr_backend``), au lieu d'un calcul
local -- pytesseract (et le binaire tesseract-ocr) ne sont alors pas
requis sur la machine appelante (voir
``libpostcards.ocr.PostcardOCR.external_helper``).

Envoi de l'image : mêmes deux formats que /api/compute_hashes
(multipart/form-data champ "image", ou corps brut Content-Type
image/*). Langue tesseract (optionnelle, "fra" par défaut) : champ de
formulaire ou paramètre de requête ``lang``, ex :

  curl -F "image=@396_R.tiff" -F "lang=fra" http://host/api/compute_ocr

Réponse (200) :
  { "text": "...texte extrait...", "lang": "fra" }

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible
  501 { "error": "..." }   -- pytesseract non installé sur ce serveur,
                              ou [simpostcards] ocr_enabled = false

--------------------------------------------------------------------
POST /api/compute_detection
--------------------------------------------------------------------

Décrit une image (recto ou verso, déjà redressée/détourée -- pas de
correction ici, comme /api/compute_ocr) via :
  - une légende globale, modèle BLIP de "image captioning" ;
  - et/ou la liste des objets détectés, modèle DETR de détection
    d'objets ;
(``simpostcards.libs.detection_backend``), au lieu d'un calcul local --
transformers + torch ne sont alors pas requis sur la machine appelante
(voir ``libpostcards.detection.PostcardDetection.external_helper``).

Ni la légende ni les libellés d'objets ne sont traduits ici (toujours
en anglais, langue native des deux modèles) : la traduction reste
TOUJOURS effectuée côté client (``PostcardDetection.translate()``),
qui n'en a pas besoin de torch. De même, le filtrage par
``excluded_objects`` et la troncature à un ``max_objects`` "utile"
restent côté client -- voir ``libpostcards.detection.detect_objects``.

Envoi de l'image : mêmes deux formats que /api/compute_hashes
(multipart/form-data champ "image", ou corps brut Content-Type
image/*). Champs de formulaire (ou paramètres de requête) optionnels :

  - "caption" ("true"/"false", "true" par défaut) : calculer la légende.
  - "objects" ("true"/"false", "true" par défaut) : détecter les objets.
    (au moins l'un des deux doit rester actif)
  - "threshold" (0-1, "0.92" par défaut) : score de confiance minimal
    DETR pour qu'une détection soit retenue -- ignoré si objects=false.

  curl -F "image=@396_R.tiff" -F "objects=false" \\
       http://host/api/compute_detection

Réponse (200) :
  { "caption": "...", "objects": [{"label": "cat", "score": 0.97}, ...] }
(le champ correspondant est absent si "caption"/"objects" valait
"false" dans la requête.)

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible /
                              requête invalide (les deux à "false",
                              "threshold" non numérique, ...)
  501 { "error": "..." }   -- transformers/torch non installés sur ce
                              serveur (pour la capacité demandée), ou
                              [simpostcards] detection_enabled = false
"""

from __future__ import annotations

import io
import time

from PIL import Image
from flask import Blueprint, current_app, jsonify, request

from libpostcards.travel import ORTOOLS_AVAILABLE, ParcoursCartes
from simpostcards.libs.corrector import ImageDecodeError, correct_image
from simpostcards.libs.detection_backend import (
    CAPTION_AVAILABLE, OBJECTS_AVAILABLE, compute_caption, compute_objects
)
from simpostcards.libs.embedder import CLIP_AVAILABLE, compute_embedding
from simpostcards.libs.hasher import compute_hashes
from simpostcards.libs.ocr_backend import OCR_AVAILABLE, compute_ocr

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


def _clip_settings():
    """
    Réglages CLIP partagés entre /api/compute_hashes et
    /api/compute_hashes_only : (compute_clip, model_name, pretrained,
    device, offline). ``compute_clip`` combine la disponibilité réelle
    des libs et [simpostcards] clip_enabled -- voir docstring du module.
    """
    return (
        CLIP_AVAILABLE and current_app.config.get("CLIP_ENABLED", True),
        current_app.config.get("CLIP_MODEL_NAME"),
        current_app.config.get("CLIP_PRETRAINED"),
        current_app.config.get("CLIP_DEVICE"),
        current_app.config.get("CLIP_OFFLINE", False),
    )


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

    compute_clip, clip_model_name, clip_pretrained, clip_device, clip_offline = _clip_settings()

    # Par défaut, calcule pour les 4 rotations de l'image corrigée (0°,
    # 90°, 180°, 270°), afin de pouvoir retrouver une correspondance
    # quelle que soit l'orientation dans laquelle la carte postale a
    # été numérisée -- utile pour flpostcards (photo prise à main
    # levée par l'appli mobile, orientation inconnue). Un client qui
    # sait déjà que ses images sont correctement orientées (scans
    # importés, requêtes de PostcardSearcher/PostcardOCR/
    # PostcardDetection) peut passer single_rotation=true pour ne
    # calculer QUE l'angle 0 : jusqu'à 4x plus rapide (l'embedding CLIP
    # est de loin l'étape la plus coûteuse), ce qui réduit d'autant le
    # risque de dépasser le délai d'attente du client (voir
    # external_helper_timeout dans postcards.conf) et la charge GPU/CPU
    # inutile pour des rotations dont le résultat serait de toute façon
    # ignoré. Voir aussi /api/compute_hashes_only, pour une image déjà
    # redressée/cadrée (ex : PostcardSearcher.build_index()) : pas de
    # redressement du tout, plutôt qu'un simple choix de rotation.
    single_rotation = request.values.get("single_rotation", "false").strip().lower() in (
        "true", "1")
    angles = (0,) if single_rotation else (0, 90, 180, 270)

    hashes_per_rotation = []
    for angle in angles:
        rotated = image.rotate(-angle, expand=True) if angle else image

        embedding = None
        if compute_clip:
            try:
                embedding = compute_embedding(
                    rotated, clip_model_name, clip_pretrained, clip_device,
                    offline=clip_offline)
            except RuntimeError as exc:
                # Chargement du modèle CLIP impossible (ex : pas d'accès
                # réseau à Hugging Face Hub et poids absents du cache
                # local -- voir libs.embedder). Mémorisé par
                # l'embedder : cet avertissement ne se reproduira donc
                # PAS à chaque rotation ni à chaque requête suivante,
                # seulement à la première rencontre du problème.
                current_app.logger.warning(
                    "compute_hashes : embedding CLIP indisponible, "
                    "repli sur les hashs perceptuels seuls : %s", exc,
                )
                compute_clip = False

        hashes_per_rotation.append({
            "angle": angle,
            "hashes": compute_hashes(rotated),
            "embedding": embedding,
        })

    elapsed = time.perf_counter() - start
    current_app.logger.info(
        "compute_hashes : OK en %.2fs (image=%d octets, corrigée=%s, embedding=%s, from=%s)",
        elapsed, len(image_bytes), report.get("final_size"), compute_clip, remote,
    )

    return jsonify({
        "status": "ok",
        "hashes": hashes_per_rotation,
        "correction": report,
    })


@bp.route("/api/compute_hashes_only", methods=["POST"])
def compute_hashes_only_route():
    """
    Comme /api/compute_hashes, mais SANS l'étape de redressement/
    détourage (``correct_image``) et sans les rotations multiples :
    pour une image déjà correctement orientée et cadrée -- un scan déjà
    importé dans la collection -- utilisée par
    ``libpostcards.similar.PostcardSearcher.compute_hashes_only()``
    pour (re)construire l'index de similarité et pour y ajouter une
    carte qui vient d'être ajoutée à la collection (voir
    ``tkpostcards.libs.scan_add``).

    /api/compute_hashes reste nécessaire pour une image quelconque
    (recherche par URL/presse-papiers, photo prise par l'appli mobile
    via flpostcards) où le redressement est réellement utile. L'appliquer
    ici serait au mieux inutile (temps perdu), au pire nuisible : un
    nouveau recadrage risquerait de légèrement décaler l'image, et donc
    de fausser les hashs perceptuels par rapport aux valeurs déjà
    stockées dans l'index pour cette même carte.
    """
    start = time.perf_counter()
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_hashes_only : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    current_app.logger.info(
        "compute_hashes_only : requête reçue, image=%d octets (from=%s)",
        len(image_bytes), remote,
    )

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        current_app.logger.warning(
            "compute_hashes_only : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": "Image illisible : %s" % exc}), 400

    compute_clip, clip_model_name, clip_pretrained, clip_device, clip_offline = _clip_settings()

    embedding = None
    if compute_clip:
        try:
            embedding = compute_embedding(
                image, clip_model_name, clip_pretrained, clip_device, offline=clip_offline)
        except RuntimeError as exc:
            # Voir le même commentaire dans compute_hashes_route :
            # mémorisé par l'embedder, ne se reproduira pas à chaque
            # requête suivante.
            current_app.logger.warning(
                "compute_hashes_only : embedding CLIP indisponible, "
                "repli sur les hashs perceptuels seuls : %s", exc,
            )

    hashes = compute_hashes(image)

    elapsed = time.perf_counter() - start
    current_app.logger.info(
        "compute_hashes_only : OK en %.2fs (image=%d octets, embedding=%s, from=%s)",
        elapsed, len(image_bytes), embedding is not None, remote,
    )

    return jsonify({
        "status": "ok",
        "hashes": hashes,
        "embedding": embedding,
    })


@bp.route("/api/compute_travel", methods=["POST"])
def compute_travel_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    if not (ORTOOLS_AVAILABLE and current_app.config.get("TRAVEL_ENABLED", True)):
        current_app.logger.warning(
            "compute_travel : indisponible sur ce serveur (ortools=%s, "
            "travel_enabled=%s, from=%s)",
            ORTOOLS_AVAILABLE, current_app.config.get("TRAVEL_ENABLED", True), remote,
        )
        return jsonify({
            "error": "Travel route computation is not available on this server "
                     "(ortools not installed, or explicitly disabled)."
        }), 501

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Corps JSON attendu"}), 400

    start = payload.get("start")
    cards = payload.get("cards")
    time_limit = payload.get("time_limit", 10)

    if (not isinstance(start, (list, tuple)) or len(start) != 2
            or not all(isinstance(v, (int, float)) for v in start)):
        return jsonify({"error": "'start' doit être [latitude, longitude]"}), 400
    if not isinstance(cards, list):
        return jsonify({"error": "'cards' doit être une liste"}), 400

    start_ts = time.perf_counter()
    current_app.logger.info(
        "compute_travel : requête reçue, %d carte(s) (from=%s)", len(cards), remote,
    )

    try:
        parcours = ParcoursCartes(cards)
        result = parcours.calculer(start[0], start[1], time_limit=time_limit)
    except RuntimeError as exc:
        # OR-Tools n'a trouvé aucune solution -- pas une erreur serveur,
        # juste un résultat vide (ex : time_limit trop court).
        current_app.logger.warning("compute_travel : %s (from=%s)", exc, remote)
        return jsonify({"error": str(exc)}), 422
    except (TypeError, KeyError, IndexError) as exc:
        return jsonify({"error": "'cards' malformée : %s" % exc}), 400

    elapsed = time.perf_counter() - start_ts
    current_app.logger.info(
        "compute_travel : OK en %.2fs (%d carte(s) -> %d retenue(s), from=%s)",
        elapsed, len(cards), result["count"], remote,
    )

    return jsonify(result)


@bp.route("/api/compute_ocr", methods=["POST"])
def compute_ocr_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    if not (OCR_AVAILABLE and current_app.config.get("OCR_ENABLED", True)):
        current_app.logger.warning(
            "compute_ocr : indisponible sur ce serveur (pytesseract=%s, "
            "ocr_enabled=%s, from=%s)",
            OCR_AVAILABLE, current_app.config.get("OCR_ENABLED", True), remote,
        )
        return jsonify({
            "error": "OCR is not available on this server "
                     "(pytesseract not installed, or explicitly disabled)."
        }), 501

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_ocr : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    lang = request.values.get("lang") or current_app.config.get("OCR_DEFAULT_LANG", "fra")

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:
        current_app.logger.warning(
            "compute_ocr : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": "Image illisible : %s" % exc}), 400

    current_app.logger.info(
        "compute_ocr : requête reçue, image=%d octets, lang=%s (from=%s)",
        len(image_bytes), lang, remote,
    )

    start_ts = time.perf_counter()
    text = compute_ocr(image, lang=lang)
    elapsed = time.perf_counter() - start_ts

    current_app.logger.info(
        "compute_ocr : OK en %.2fs (lang=%s, from=%s)", elapsed, lang, remote,
    )

    return jsonify({"text": text, "lang": lang})


@bp.route("/api/compute_detection", methods=["POST"])
def compute_detection_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    want_caption = request.values.get("caption", "true").strip().lower() not in ("false", "0", "")
    want_objects = request.values.get("objects", "true").strip().lower() not in ("false", "0", "")

    if not want_caption and not want_objects:
        return jsonify({
            "error": "At least one of 'caption'/'objects' must stay enabled"
        }), 400

    detection_enabled = current_app.config.get("DETECTION_ENABLED", True)
    if want_caption and not (CAPTION_AVAILABLE and detection_enabled):
        current_app.logger.warning(
            "compute_detection : légende indisponible sur ce serveur "
            "(transformers=%s, detection_enabled=%s, from=%s)",
            CAPTION_AVAILABLE, detection_enabled, remote,
        )
        return jsonify({
            "error": "Caption generation is not available on this server "
                     "(transformers/torch not installed, or explicitly disabled)."
        }), 501
    if want_objects and not (OBJECTS_AVAILABLE and detection_enabled):
        current_app.logger.warning(
            "compute_detection : détection d'objets indisponible sur ce serveur "
            "(transformers=%s, detection_enabled=%s, from=%s)",
            OBJECTS_AVAILABLE, detection_enabled, remote,
        )
        return jsonify({
            "error": "Object detection is not available on this server "
                     "(transformers/torch not installed, or explicitly disabled)."
        }), 501

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_detection : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        current_app.logger.warning(
            "compute_detection : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": "Image illisible : %s" % exc}), 400

    threshold = 0.92
    if want_objects:
        try:
            threshold = float(request.values.get("threshold", threshold))
        except (TypeError, ValueError):
            return jsonify({"error": "'threshold' doit être un nombre"}), 400

    model_name = current_app.config.get("DETECTION_MODEL_NAME")
    objects_model_name = current_app.config.get("DETECTION_OBJECTS_MODEL_NAME")
    device = current_app.config.get("DETECTION_DEVICE")

    current_app.logger.info(
        "compute_detection : requête reçue, image=%d octets, caption=%s, "
        "objects=%s (threshold=%s) (from=%s)",
        len(image_bytes), want_caption, want_objects, threshold, remote,
    )

    result = {}
    start_ts = time.perf_counter()

    if want_caption:
        result["caption"] = compute_caption(image, model_name=model_name, device=device)

    if want_objects:
        detections = compute_objects(image, model_name=objects_model_name, device=device,
                                      threshold=threshold)
        result["objects"] = [
            {"label": label, "score": round(score, 4)} for label, score in detections
        ]

    elapsed = time.perf_counter() - start_ts
    current_app.logger.info(
        "compute_detection : OK en %.2fs (caption=%s, objects=%s, from=%s)",
        elapsed, want_caption, want_objects, remote,
    )

    return jsonify(result)
