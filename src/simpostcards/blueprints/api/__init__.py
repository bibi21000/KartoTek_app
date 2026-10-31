"""
Blueprint API de simpostcards : six points d'entrée.

  POST /api/compute_hashes
  POST /api/compute_hashes_only
  POST /api/correct_scan
  POST /api/compute_travel
  POST /api/compute_ocr
  POST /api/compute_detection
  POST /api/compute_segments
  POST /api/translate
  GET  /api/ocr_languages
  POST /api/unload_models

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
POST /api/correct_scan
--------------------------------------------------------------------

Redresse/rogne (``libpostcards.scan_corrector.ScanCorrector``) un scan
brut et, sauf ``skip_transparency``, détoure son fond
(``libpostcards.transparency.TiffBackgroundRemover``) --
``simpostcards.libs.corrector.correct_scan``. Pipeline complet, piloté
par les mêmes paramètres qu'un ``ScanProfile`` (voir
``tkpostcards.libs.scan_profiles``), utilisé pour déléguer l'étape
"Analyze and correct scans" de tkimport/tktools (voir
``tkpostcards.libs.scan_prepare.make_corrector``) à ce serveur -- au
lieu d'un calcul local, qui nécessiterait cv2/numpy/
libpostcards.contour_utils installés sur la machine qui importe.

Contrairement aux autres points d'entrée, la réponse (200) n'est PAS
du JSON : c'est l'image corrigée elle-même, encodée dans le format
demandé (voir "output_format" ci-dessous), avec le
``Content-Type`` correspondant. Le rapport de correction
(``CorrectionReport``, mêmes clés que /api/compute_hashes) est renvoyé
à part, dans l'en-tête ``X-Scan-Correction`` (JSON).

Envoi de l'image : mêmes deux formats que /api/compute_hashes.
Champs de formulaire (ou paramètres de requête), tous optionnels et
correspondant terme à terme aux attributs d'un ``ScanProfile`` (voir
son docstring pour le détail de chacun) :

  - white_threshold (entier, 240 par défaut)
  - white_ratio_threshold (0-1, 0.98 par défaut)
  - crop_margin (entier, 20 par défaut)
  - final_crop_margin (entier, 5 par défaut)
  - angle_range (degrés, 20.0 par défaut)
  - use_contour_geometry ("true"/"false", "false" par défaut)
  - use_quad_geometry ("true"/"false", "false" par défaut)
  - contour_denoise ("true"/"false", "false" par défaut)
  - contour_clahe ("true"/"false", "false" par défaut)
  - contour_auto_canny ("true"/"false", "false" par défaut)
  - skip_transparency ("true"/"false", "false" par défaut)
  - transparency_white_threshold (entier, optionnel -- voir
    ``ScanProfile.effective_transparency_threshold`` : à résoudre côté
    appelant, absent ici = retombe sur white_threshold)
  - transparency_band (entier, optionnel)
  - output_format ("tiff"/"jpg"/"jpeg"/"png", "tiff" par défaut) :
    format d'encodage de l'image renvoyée -- à faire correspondre à
    l'extension du fichier de destination côté appelant.

  curl -F "image=@scan.jpg" -F "angle_range=15" -F "output_format=jpg" \\
       http://host/api/correct_scan -o carte_corrigee.jpg

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible /
                              paramètre invalide
  501 { "error": "..." }   -- [simpostcards] correct_scan_enabled = false

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

--------------------------------------------------------------------
POST /api/compute_segments
--------------------------------------------------------------------

Segmente une image (le recto d'une carte, déjà redressé/détouré -- pas de
correction ici) avec SAM (``simpostcards.libs.segment_backend``) et
renvoie TOUS les segments trouvés, pour que le client choisisse lui-même
ceux qui sont des timbres (voir ``libpostcards.masks.StampDetector`` et
``external_helper``). transformers + torch ne sont alors pas requis sur
la machine appelante.

Seule la segmentation est faite ici : le tri des segments (forme, taille,
position) reste côté client, avec les seuils de sa propre section
[masks], de sorte que les réglages s'appliquent de la même façon que SAM
tourne en local ou ici.

Envoi de l'image : mêmes deux formats que /api/compute_hashes
(multipart/form-data champ "image", ou corps brut Content-Type image/*).
Champs de formulaire (ou paramètres de requête) optionnels :

  - "points_per_side" (entier 4-64, "20" par défaut) : grille de points
    de prompts de SAM par côté (32 = défaut de SAM ; plus petit = plus
    rapide, suffisant pour des objets de la taille d'un timbre).
  - "pred_iou_thresh" (0-1, "0.86") et "stability_thresh" (0-1, "0.90") :
    seuils de qualité des segments retenus par SAM.
  - "max_masks" (entier, "100", plafonné à 200) : nombre maximal de
    segments renvoyés, les mieux notés d'abord.

  curl -F "image=@396_R.png" http://host/api/compute_segments

Réponse (200) :
  {
    "width": 1600, "height": 1100,
    "segments": [ {"score": 0.97, "mask": "<base64>"}, ... ]
  }
``mask`` : tableau booléen height x width, bits empaquetés puis zlib puis
base64 (``libpostcards.masks.unpack_mask``). Les segments ont la taille
de l'image envoyée : aucun redimensionnement à faire côté client.

Erreurs :
  400 { "error": "..." }   -- pas d'image envoyée / image illisible /
                              paramètre invalide
  501 { "error": "..." }   -- transformers/torch non installés sur ce
                              serveur, ou [simpostcards] segment_enabled =
                              false

--------------------------------------------------------------------
POST /api/unload_models
--------------------------------------------------------------------

Décharge IMMÉDIATEMENT tous les modèles que ce serveur a en mémoire (CLIP,
BLIP, DETR, SAM) et rend leur mémoire : GPU (cache CUDA) comme RAM. Ils
sont rechargés tout seuls à la prochaine requête qui en a besoin (la
première est donc plus lente, le temps du chargement).

Complète [simpostcards] gpu_idle_timeout (déchargement automatique après
inactivité) quand on veut libérer la mémoire sans attendre -- par ex. avant
de lancer un gros modèle sur le même GPU :

  curl -X POST http://host/api/unload_models

Pas de corps de requête. Un modèle utilisé par une requête en cours n'est
pas touché (état "busy") : réessayer après.

Réponse (200) :
  { "models": {"CLIP": "unloaded", "BLIP": "not_loaded",
               "DETR": "not_loaded", "SAM": "busy"} }
États : "unloaded" (était chargé, libéré), "not_loaded" (rien à
décharger), "busy" (requête en cours). Seuls les modèles dont le module a
été importé par ce processus figurent dans la liste. Avec plusieurs
workers gunicorn, la requête n'atteint que celui qui la traite : voir
[simpostcards] gpu_idle_timeout pour un déchargement qui les couvre tous.

--------------------------------------------------------------------
Langues : OCR, détection et traduction
--------------------------------------------------------------------

La langue choisie par le client est envoyée au serveur, qui la vérifie ; si
elle n'est pas disponible, il le consigne dans ses logs (niveau WARNING) ET
le dit au client, qui le montre à l'utilisateur.

POST /api/compute_ocr  (champs "lang", "engine", "min_score")
  Le CLIENT choisit le moteur : ``engine`` = "tesseract" (défaut) ou
  "paddleocr". ``lang`` : codes tesseract ("fra", "fra+eng") ou PaddleOCR
  ("fr") -- chaque moteur comprend les deux. ``min_score`` (0-1, "0.5") :
  confiance minimale d'une ligne, PaddleOCR seulement. Réponse :
    { "text": "...", "lang": "fra", "engine": "paddleocr" }
  (``engine`` y est TOUJOURS : un client qui exige PaddleOCR vérifie que la
  réponse vient bien de lui, un serveur ancien l'ignorerait et lirait avec
  tesseract.) Erreurs :
    400  moteur inconnu, image absente ou illisible
    422  langue absente du moteur sur ce serveur :
         { "error": "...", "code": "language_unavailable", "service": "ocr",
           "engine": "tesseract", "lang": "fra+deu", "missing": ["deu"],
           "available": ["eng", "fra"] }
    501  moteur absent ou désactivé sur ce serveur ([simpostcards]
         ocr_enabled / paddleocr_enabled, paquet non installé, modèles
         PaddleOCR introuvables) :
         { "error": "...", "code": "engine_unavailable", "engine": "paddleocr" }
  Chaque cas est consigné dans les logs du serveur (WARNING).

GET /api/ocr_languages[?engine=paddleocr][&lang=fra+eng]
  { "engine": "tesseract", "languages": ["eng", "fra", ...] } ; avec
  ``lang``, mêmes 422 ``language_unavailable`` / 501 ``engine_unavailable``
  que ci-dessus. Sert aux clients à vérifier le moteur et la langue UNE
  fois avant un lot.

POST /api/compute_detection  (champ "lang" optionnel : "fr", "de"...)
  Sans "lang" (ou "en") : réponse inchangée, légende et libellés anglais.
  Avec "lang", le serveur traduit lui-même et ajoute un bloc :
    "translation": {"status": "ok", "lang": "fr", "caption": "une rue",
                    "labels": {"tie": "cravate", "person": "personne"}}
  "caption" / "labels" ne sont présents que pour ce qui a été demandé. Les
  libellés restent clés par leur nom ANGLAIS : le filtrage des exclusions et
  la troncature se font côté client, sur l'anglais. Sinon, HTTP 200 quand
  même -- la détection, coûteuse, n'est pas perdue -- avec :
    "translation": {"status": "language_unavailable" |
                    "translator_unavailable" | "failed",
                    "lang": "xx", "message": "...", "supported": [...]}
  et la légende/les libellés en anglais ; le client le signale à
  l'utilisateur et enregistre le texte comme "en" (voir ``kttools
  detect-translate`` pour le traduire plus tard).

POST /api/translate  (JSON : {"lang": "fr", "source": "en", "texts": [...]})
  Traduit jusqu'à 200 textes de 4000 caractères (``source`` : "en" par
  défaut, ou "auto"). Réponse :
    { "lang": "fr", "source": "en", "translations": [...] }
  ``texts`` vide : ne traduit rien mais vérifie la langue (préparation d'un
  lot). Avec ``"prepare": true``, charge en plus (et télécharge au besoin) le
  modèle local si c'est lui qui va traduire. La réponse indique le moteur
  utilisé (``"engine": "google"`` ou ``"marian"``, voir [simpostcards]
  translate_engine : ``google`` en ligne, ``marian`` modèle local sans limite
  de débit, ``auto`` Google puis le modèle local s'il refuse ce serveur).
  Erreurs :
    400  corps invalide
    422  { "error", "code": "language_unavailable", "service": "translate",
           "lang", "supported": [...] }   langue inconnue du moteur
    429  { "error", "code": "rate_limited", "retry_after": 600 }  (+ en-tête
         Retry-After) Google refuse ce serveur malgré les attentes (gratuit :
         ~5 requêtes par seconde ; au-delà d'une rafale, il peut bloquer
         l'adresse IP des minutes ou des heures). Le serveur ne le sollicite
         plus pendant ``retry_after`` secondes (disjoncteur) : réessayer
         ensuite, ou passer à translate_engine = auto / marian (modèle local)
    502  { "error", "code": "translation_failed" }   échec du service
    501  { "error", "code": "translator_unavailable" }  deep_translator absent
         ou [simpostcards] translate_enabled = false
"""

from __future__ import annotations

import io
import json
import time

from PIL import Image
from flask import Blueprint, current_app, jsonify, request

from libpostcards.travel import ORTOOLS_AVAILABLE, ParcoursCartes
from simpostcards.libs.corrector import ImageDecodeError, correct_image, correct_scan, encode_scan
from simpostcards.libs.detection_backend import (
    CAPTION_AVAILABLE, OBJECTS_AVAILABLE, compute_caption, compute_objects
)
from simpostcards.libs.embedder import CLIP_AVAILABLE, compute_embedding
from simpostcards.libs.hasher import compute_hashes
from simpostcards.libs.ocr_backend import (
    OCR_AVAILABLE, compute_ocr, installed_languages, missing_languages,
)
from simpostcards.libs.paddle_backend import PADDLE_AVAILABLE, compute_paddle_ocr
from libpostcards import langs as langs_lib
from simpostcards.libs.segment_backend import SEGMENT_AVAILABLE, compute_segments
from libpostcards.masks import pack_mask

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


def _form_int(name: str, default: int) -> int:
    return int(request.values.get(name, default))


def _form_float(name: str, default: float) -> float:
    return float(request.values.get(name, default))


def _form_bool(name: str, default: bool = False) -> bool:
    value = request.values.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("true", "1")


def _form_optional_int(name: str) -> int | None:
    value = request.values.get(name)
    if value is None or not value.strip():
        return None
    return int(value)


@bp.route("/api/correct_scan", methods=["POST"])
def correct_scan_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    if not current_app.config.get("CORRECT_SCAN_ENABLED", True):
        current_app.logger.warning(
            "correct_scan : désactivé sur ce serveur (correct_scan_enabled=false, from=%s)",
            remote,
        )
        return jsonify({
            "error": "Scan correction is disabled on this server "
                     "([simpostcards] correct_scan_enabled = false)."
        }), 501

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning("correct_scan : aucune image reçue (from=%s)", remote)
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    try:
        white_threshold = _form_int("white_threshold", 240)
        white_ratio_threshold = _form_float("white_ratio_threshold", 0.98)
        crop_margin = _form_int("crop_margin", 20)
        final_crop_margin = _form_int("final_crop_margin", 5)
        angle_range = _form_float("angle_range", 20.0)
        transparency_white_threshold = _form_optional_int("transparency_white_threshold")
        transparency_band = _form_optional_int("transparency_band")
        use_contour_geometry = _form_bool("use_contour_geometry")
        use_quad_geometry = _form_bool("use_quad_geometry")
        contour_denoise = _form_bool("contour_denoise")
        contour_clahe = _form_bool("contour_clahe")
        contour_auto_canny = _form_bool("contour_auto_canny")
        skip_transparency = _form_bool("skip_transparency")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": "Paramètre invalide : %s" % exc}), 400

    output_format = request.values.get("output_format", "tiff").strip().lower()
    if not output_format.startswith("."):
        output_format = "." + output_format

    current_app.logger.info(
        "correct_scan : requête reçue, image=%d octets, sortie=%s (from=%s)",
        len(image_bytes), output_format, remote,
    )

    start = time.perf_counter()

    try:
        corrected, report = correct_scan(
            image_bytes,
            white_threshold=white_threshold,
            white_ratio_threshold=white_ratio_threshold,
            crop_margin=crop_margin,
            final_crop_margin=final_crop_margin,
            angle_range=angle_range,
            use_contour_geometry=use_contour_geometry,
            use_quad_geometry=use_quad_geometry,
            contour_denoise=contour_denoise,
            contour_clahe=contour_clahe,
            contour_auto_canny=contour_auto_canny,
            skip_transparency=skip_transparency,
            transparency_white_threshold=transparency_white_threshold,
            transparency_band=transparency_band,
        )
    except ImageDecodeError as exc:
        current_app.logger.warning(
            "correct_scan : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": str(exc)}), 400

    try:
        encoded = encode_scan(corrected, output_format)
    except ValueError as exc:
        current_app.logger.warning(
            "correct_scan : échec de l'encodage (%s, from=%s) : %s",
            output_format, remote, exc,
        )
        return jsonify({"error": str(exc)}), 400

    elapsed = time.perf_counter() - start
    current_app.logger.info(
        "correct_scan : OK en %.2fs (image=%d octets, sortie=%d octets, from=%s)",
        elapsed, len(image_bytes), len(encoded), remote,
    )

    content_type = {
        ".tif": "image/tiff", ".tiff": "image/tiff",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(output_format, "application/octet-stream")

    response = current_app.response_class(encoded, mimetype=content_type)
    response.headers["X-Scan-Correction"] = json.dumps(report)
    return response


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


def _requested_engine():
    """``(moteur, None)`` ou ``(None, réponse 400)`` : le moteur OCR demandé
    par le client (``engine``, tesseract par défaut)."""
    try:
        return langs_lib.normalize_ocr_engine(request.values.get("engine")), None
    except ValueError as exc:
        return None, (jsonify({"error": str(exc)}), 400)


def _ocr_engine_error(engine, remote, route):
    """None si *engine* peut tourner ici ; sinon la réponse 501
    ``engine_unavailable`` (consignée dans les logs) que le client montre à
    l'utilisateur. ``ocr_enabled = false`` coupe tous les moteurs."""
    config = current_app.config
    if not config.get("OCR_ENABLED", True):
        reason = "OCR is disabled on this server (ocr_enabled = false)"
    elif engine == "paddleocr":
        if not PADDLE_AVAILABLE:
            reason = ("PaddleOCR is not installed on this server (it needs "
                      "PaddlePaddle and paddleocr: pip install paddlepaddle, "
                      "then pip install pypostcards[paddleocr])")
        elif not config.get("PADDLEOCR_ENABLED", True):
            reason = "PaddleOCR is disabled on this server (paddleocr_enabled = false)"
        else:
            return None
    elif OCR_AVAILABLE:
        return None
    else:
        reason = "tesseract is not available on this server (pytesseract not installed)"
    current_app.logger.warning(
        "%s : moteur %s indisponible (from=%s) : %s", route, engine, remote, reason)
    return jsonify({"error": "OCR engine %s is not available on this server: %s"
                             % (engine, reason),
                    "code": langs_lib.ENGINE_UNAVAILABLE, "engine": engine}), 501


def _ocr_language_error(lang, remote, engine="tesseract"):
    """None si *engine* a toutes les langues de *lang* ; sinon la réponse
    422 ``language_unavailable`` (déjà consignée dans les logs du serveur),
    que le client transforme en message pour l'utilisateur. Si tesseract
    lui-même est inutilisable, on ne dit rien ici : l'OCR échouera avec son
    propre message, comme avant."""
    if engine == "paddleocr":
        missing = langs_lib.paddle_missing_languages(lang)
        installed = sorted(langs_lib.PADDLE_LANGUAGES)
    else:
        try:
            installed = installed_languages()
            missing = missing_languages(lang)
        except Exception:
            return None
    if not missing:
        return None
    message = langs_lib.ocr_language_message(lang, missing, installed, engine)
    current_app.logger.warning(
        "compute_ocr : langue indisponible pour %s (from=%s) : %s",
        engine, remote, message)
    return jsonify({
        "error": message, "code": langs_lib.LANGUAGE_UNAVAILABLE,
        "service": "ocr", "engine": engine, "lang": lang, "missing": missing,
        "available": installed,
    }), 422


@bp.route("/api/ocr_languages", methods=["GET"])
def ocr_languages_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    engine, bad = _requested_engine()
    if bad is not None:
        return bad
    unavailable = _ocr_engine_error(engine, remote, "ocr_languages")
    if unavailable is not None:
        return unavailable
    lang = request.values.get("lang")
    if lang:
        unavailable = _ocr_language_error(lang, remote, engine)
        if unavailable is not None:
            return unavailable
    if engine == "paddleocr":
        return jsonify({"engine": engine,
                        "languages": sorted(langs_lib.PADDLE_LANGUAGES)})
    try:
        return jsonify({"engine": engine, "languages": installed_languages()})
    except Exception as exc:
        current_app.logger.warning(
            "ocr_languages : tesseract inutilisable (from=%s) : %s", remote, exc)
        return jsonify({"error": "tesseract is not usable: %s" % exc}), 500


@bp.route("/api/compute_ocr", methods=["POST"])
def compute_ocr_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    engine, bad = _requested_engine()
    if bad is not None:
        return bad
    unavailable = _ocr_engine_error(engine, remote, "compute_ocr")
    if unavailable is not None:
        return unavailable

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_ocr : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    lang = request.values.get("lang") or current_app.config.get("OCR_DEFAULT_LANG", "fra")

    unavailable = _ocr_language_error(lang, remote, engine)
    if unavailable is not None:
        return unavailable

    try:
        min_score = _bounded("min_score", 0.5, float, 0.0, 1.0)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

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
        "compute_ocr : requête reçue, image=%d octets, engine=%s, lang=%s (from=%s)",
        len(image_bytes), engine, lang, remote,
    )

    start_ts = time.perf_counter()
    if engine == "paddleocr":
        try:
            text = compute_paddle_ocr(
                image, lang=lang,
                device=current_app.config.get("PADDLEOCR_DEVICE"),
                ocr_version=current_app.config.get("PADDLEOCR_VERSION"),
                min_score=min_score)
        except langs_lib.LanguageUnavailable as exc:
            # La liste de langues du serveur est en retard sur le paquet
            # installé : le constructeur de PaddleOCR a le dernier mot.
            current_app.logger.warning(
                "compute_ocr : langue indisponible pour paddleocr (from=%s) : %s",
                remote, exc)
            return jsonify({
                "error": str(exc), "code": langs_lib.LANGUAGE_UNAVAILABLE,
                "service": "ocr", "engine": engine, "lang": lang,
                "missing": exc.missing,
                "available": sorted(langs_lib.PADDLE_LANGUAGES)}), 422
        except langs_lib.EngineUnavailable as exc:
            current_app.logger.warning(
                "compute_ocr : paddleocr inutilisable (from=%s) : %s", remote, exc)
            return jsonify({"error": str(exc), "code": langs_lib.ENGINE_UNAVAILABLE,
                            "engine": engine}), 501
    else:
        text = compute_ocr(image, lang=lang)
    elapsed = time.perf_counter() - start_ts

    current_app.logger.info(
        "compute_ocr : OK en %.2fs (engine=%s, lang=%s, from=%s)",
        elapsed, engine, lang, remote,
    )

    return jsonify({"text": text, "lang": lang, "engine": engine})


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
    lang = (request.values.get("lang") or "").strip()

    if want_caption:
        # Début de légende optionnel (ex : "a postcard of"), voir
        # PostcardDetection.caption_prompt côté client. Borné pour éviter
        # qu'une requête n'injecte un prompt démesuré.
        prompt = (request.values.get("prompt") or "").strip()[:80] or None
        result["caption"] = compute_caption(image, model_name=model_name, device=device,
                                            prompt=prompt)

    if want_objects:
        detections = compute_objects(image, model_name=objects_model_name, device=device,
                                      threshold=threshold)
        result["objects"] = [
            {"label": label, "score": round(score, 4)} for label, score in detections
        ]

    # La détection (coûteuse) est faite : une langue indisponible ou une
    # traduction en échec ne doit pas la perdre. Le résultat anglais est
    # renvoyé tel quel, avec dans "translation" ce qui s'est passé ; le
    # client le montre à l'utilisateur.
    if lang and not langs_lib.is_english(lang):
        result["translation"] = _translate_detection(result, lang, remote)

    elapsed = time.perf_counter() - start_ts
    current_app.logger.info(
        "compute_detection : OK en %.2fs (caption=%s, objects=%s, lang=%s, from=%s)",
        elapsed, want_caption, want_objects, lang or "-", remote,
    )

    return jsonify(result)


def _translation_enabled():
    return (langs_lib.TRANSLATOR_AVAILABLE
            and current_app.config.get("TRANSLATE_ENABLED", True))


def _translate_detection(result, lang, remote):
    """Bloc ``translation`` de la réponse de compute_detection :
    ``{"status": "ok", "lang", "caption"?, "labels"?}`` ou, sinon,
    ``{"status": "language_unavailable"|"translator_unavailable"|"failed",
    "lang", "message"}``. Les libellés d'objets sont renvoyés dans un
    dictionnaire (libellé anglais -> traduction) : c'est le client qui
    écarte les exclusions et tronque, sur les libellés anglais."""
    log = current_app.logger
    if not _translation_enabled():
        message = ("the server has no translation engine (deep_translator "
                   "not installed, or translate_enabled = false)")
        log.warning("compute_detection : traduction vers %r impossible "
                    "(from=%s) : %s", lang, remote, message)
        return {"status": langs_lib.TRANSLATOR_UNAVAILABLE, "lang": lang,
                "message": message}

    resolved = langs_lib.resolve_language(lang)
    if resolved is None:
        message = ("language %r is not available on the server (the "
                   "translation engine does not support it)" % lang)
        log.warning("compute_detection : langue indisponible (from=%s) : %s",
                    remote, message)
        return {"status": langs_lib.LANGUAGE_UNAVAILABLE, "lang": lang,
                "message": message,
                "supported": sorted(set(langs_lib.translation_languages().values()))}

    translation = {"status": "ok", "lang": resolved}
    try:
        engines = set()
        if "caption" in result:
            out, engine = langs_lib.translate_texts_ex([result["caption"]], lang)
            translation["caption"] = out[0]
            engines.add(engine)
        if "objects" in result:
            labels = sorted({o["label"] for o in result["objects"]})
            out, engine = langs_lib.translate_texts_ex(labels, lang)
            translation["labels"] = dict(zip(labels, out))
            engines.add(engine)
        engines.discard(None)                    # None : tout venait du cache
        if engines:
            translation["engine"] = ",".join(sorted(engines))
    except (langs_lib.TranslatorUnavailable, langs_lib.LanguageUnavailable) as exc:
        log.warning("compute_detection : traduction vers %r impossible "
                    "(from=%s) : %s", lang, remote, exc)
        return {"status": (langs_lib.TRANSLATOR_UNAVAILABLE
                           if isinstance(exc, langs_lib.TranslatorUnavailable)
                           else langs_lib.LANGUAGE_UNAVAILABLE),
                "lang": lang, "message": str(exc)}
    except langs_lib.TranslationError as exc:
        log.warning("compute_detection : traduction vers %r en échec "
                    "(from=%s) : %s", lang, remote, exc)
        return {"status": "failed", "lang": lang, "message": str(exc)}
    return translation


@bp.route("/api/translate", methods=["POST"])
def translate_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)
    log = current_app.logger

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "JSON body expected: "
                                 "{\"lang\", \"source\"?, \"texts\"}"}), 400
    lang = body.get("lang")
    source = body.get("source") or "en"
    texts = body.get("texts", [])
    if not isinstance(lang, str) or not lang.strip():
        return jsonify({"error": "'lang' is required"}), 400
    if (not isinstance(texts, list) or len(texts) > 200
            or not all(isinstance(t, str) and len(t) <= 4000 for t in texts)):
        return jsonify({"error": "'texts' must be a list of at most 200 "
                                 "strings of at most 4000 characters"}), 400

    if not _translation_enabled():
        log.warning("translate : indisponible sur ce serveur "
                    "(deep_translator=%s, translate_enabled=%s, from=%s)",
                    langs_lib.TRANSLATOR_AVAILABLE,
                    current_app.config.get("TRANSLATE_ENABLED", True), remote)
        return jsonify({
            "error": "Translation is not available on this server "
                     "(deep_translator not installed, or explicitly disabled).",
            "code": langs_lib.TRANSLATOR_UNAVAILABLE,
        }), 501

    resolved = langs_lib.resolve_language(lang)
    if resolved is None:
        supported = sorted(set(langs_lib.translation_languages().values()))
        message = ("language %r is not available on the server (the "
                   "translation engine does not support it)" % lang)
        log.warning("translate : langue indisponible (from=%s) : %s",
                    remote, message)
        return jsonify({
            "error": message, "code": langs_lib.LANGUAGE_UNAVAILABLE,
            "service": "translate", "lang": lang, "supported": supported,
        }), 422

    engine_used = None
    try:
        if body.get("prepare"):
            # Préparation d'un lot : si c'est le modèle local qui va traduire,
            # on le charge (voire le télécharge) maintenant, avant les textes.
            engine_used = langs_lib.prepare_translation(lang)
        translations, used = langs_lib.translate_texts_ex(texts, lang, source)
        engine_used = used or engine_used
    except langs_lib.TranslatorUnavailable as exc:
        log.warning("translate : moteur de traduction indisponible "
                    "(from=%s) : %s", remote, exc)
        return jsonify({"error": str(exc),
                        "code": langs_lib.TRANSLATOR_UNAVAILABLE}), 501
    except langs_lib.LanguageUnavailable as exc:
        log.warning("translate : langue source indisponible (from=%s) : %s",
                    remote, exc)
        return jsonify({
            "error": "source %s" % exc, "code": langs_lib.LANGUAGE_UNAVAILABLE,
            "service": "translate", "lang": source,
            "supported": exc.available,
        }), 422
    except langs_lib.RateLimited as exc:
        # Le service de traduction limite le débit de CE serveur et ne se
        # calme pas après nos propres attentes : on dit au client d'attendre
        # et de réessayer (HelperTranslator le fait) plutôt que d'échouer.
        log.warning("translate : service de traduction saturé vers %r "
                    "(from=%s), le client est invité à réessayer dans %.0fs : %s",
                    lang, remote, exc.retry_after, exc)
        response = jsonify({"error": str(exc), "code": "rate_limited",
                            "retry_after": exc.retry_after})
        response.status_code = 429
        response.headers["Retry-After"] = str(int(exc.retry_after or 30))
        return response
    except langs_lib.TranslationError as exc:
        log.warning("translate : échec vers %r (from=%s) : %s",
                    lang, remote, exc)
        return jsonify({"error": str(exc),
                        "code": langs_lib.TRANSLATION_FAILED}), 502

    log.info("translate : OK, %d texte(s) en -> %s avec %s (from=%s)",
             len(texts), resolved, engine_used or "cache", remote)
    return jsonify({"lang": resolved, "source": source,
                    "engine": engine_used or langs_lib.translation_engine(),
                    "translations": translations})


def _bounded(name, default, cast, low, high):
    """Paramètre de requête numérique borné ; ValueError si invalide."""
    raw = request.values.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError("'%s' doit être un nombre" % name)
    if not low <= value <= high:
        raise ValueError("'%s' doit être compris entre %s et %s" % (name, low, high))
    return value


@bp.route("/api/compute_segments", methods=["POST"])
def compute_segments_route():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr)

    segment_enabled = current_app.config.get("SEGMENT_ENABLED", True)
    if not (SEGMENT_AVAILABLE and segment_enabled):
        current_app.logger.warning(
            "compute_segments : indisponible sur ce serveur "
            "(transformers/torch=%s, segment_enabled=%s, from=%s)",
            SEGMENT_AVAILABLE, segment_enabled, remote,
        )
        return jsonify({
            "error": "Segmentation (SAM) is not available on this server "
                     "(transformers/torch not installed, or explicitly disabled)."
        }), 501

    try:
        points_per_side = _bounded("points_per_side", 20, int, 4, 64)
        pred_iou_thresh = _bounded("pred_iou_thresh", 0.86, float, 0.0, 1.0)
        stability_thresh = _bounded("stability_thresh", 0.90, float, 0.0, 1.0)
        max_masks = _bounded("max_masks", 100, int, 1, 200)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    image_bytes = _extract_image_bytes()
    if not image_bytes:
        current_app.logger.warning(
            "compute_segments : aucune image reçue (from=%s)", remote
        )
        return jsonify({"error": "Aucune image envoyée (champ 'image' ou corps brut)"}), 400

    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as exc:
        current_app.logger.warning(
            "compute_segments : image illisible (%d octets, from=%s) : %s",
            len(image_bytes), remote, exc,
        )
        return jsonify({"error": "Image illisible : %s" % exc}), 400

    current_app.logger.info(
        "compute_segments : requête reçue, image=%d octets (%dx%d), "
        "points_per_side=%d (from=%s)",
        len(image_bytes), image.width, image.height, points_per_side, remote,
    )

    start_ts = time.perf_counter()
    segments = compute_segments(
        image,
        model_name=current_app.config.get("SEGMENT_MODEL_NAME"),
        device=current_app.config.get("SEGMENT_DEVICE"),
        points_per_side=points_per_side,
        pred_iou_thresh=pred_iou_thresh,
        stability_thresh=stability_thresh,
        max_masks=max_masks,
    )
    payload = {
        "width": image.width,
        "height": image.height,
        "segments": [
            {"score": round(score, 4), "mask": pack_mask(mask)}
            for score, mask in segments
        ],
    }
    current_app.logger.info(
        "compute_segments : OK en %.2fs (%d segments, from=%s)",
        time.perf_counter() - start_ts, len(segments), remote,
    )
    return jsonify(payload)


@bp.route("/api/unload_models", methods=["POST"])
def unload_models_route():
    from simpostcards.libs._idle_unload import unload_all

    remote = request.headers.get("X-Forwarded-For", request.remote_addr)
    states = unload_all()
    current_app.logger.info("unload_models : %s (from=%s)", states, remote)
    return jsonify({"models": states})
