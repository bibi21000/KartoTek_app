"""
simpostcards - Micro-appli Flask de calcul de hashs (+ embedding CLIP
optionnel), de parcours optimisés et de détection de contenu pour les
cartes postales.

Six points d'entrée API (voir ``simpostcards.blueprints.api``) :

  POST /api/compute_hashes
    Étant donné une image de carte postale (recto ou verso) :
      1. la redresse et la détoure (``libpostcards.scan_corrector``) ;
      2. calcule ses hashs perceptuels (``simpostcards.libs.hasher``,
         mêmes hashs que ``libpostcards.similar``) ;
      3. si torch/open-clip-torch sont installés et [simpostcards]
         clip_enabled n'est pas à "false" (``libs.embedder``), calcule
         aussi son embedding CLIP, dans le MÊME espace vectoriel que
         ``libpostcards.similar.PostcardSearcher`` (mêmes modèle/poids
         par défaut) -- sinon, le champ "embedding" de la réponse vaut
         ``None`` (comportement historique, hashs seuls) ;
      4. renvoie le résultat en JSON.

  POST /api/compute_hashes_only
    Comme /api/compute_hashes, mais pour une image déjà correctement
    orientée et cadrée (un scan déjà importé dans la collection) :
    sans le redressement/détourage ni les rotations multiples --
    utilisé par ``libpostcards.similar.PostcardSearcher`` pour
    (re)construire l'index de similarité et pour y ajouter une
    nouvelle carte, plutôt que de recadrer inutilement (voire
    nuisiblement) une image déjà cadrée.

  POST /api/correct_scan
    Étant donné un scan brut (pas encore importé) et les paramètres
    d'un ``ScanProfile`` (voir ``tkpostcards.libs.scan_profiles``),
    redresse/rogne (``libpostcards.scan_corrector``) puis, sauf
    demande contraire, détoure le fond
    (``libpostcards.transparency``) -- utilisé par
    ``tkpostcards.libs.scan_prepare.make_corrector`` pour déléguer
    l'étape "Analyze and correct scans" de tkimport/tktools à ce
    serveur. Renvoie l'image corrigée elle-même (pas du JSON), sauf si
    [simpostcards] correct_scan_enabled = false (501).

  POST /api/compute_travel
    Étant donné un point de départ et une liste de cartes
    géolocalisées, calcule un parcours optimisé via OR-Tools
    (``libpostcards.travel.ParcoursCartes``) -- sauf si ortools n'est
    pas installé ici, ou si [simpostcards] travel_enabled = false (501
    dans ces deux cas).

  POST /api/compute_ocr
    Étant donné une image déjà redressée/détourée (recto ou verso),
    extrait son texte via Tesseract OCR
    (``simpostcards.libs.ocr_backend``) -- sauf si pytesseract (+ le
    binaire tesseract-ocr) n'est pas installé ici, ou si [simpostcards]
    ocr_enabled = false (501 dans ces deux cas).

  POST /api/compute_detection
    Étant donné une image déjà redressée/détourée (recto ou verso),
    génère une légende (BLIP) et/ou détecte les objets présents (DETR)
    via ``simpostcards.libs.detection_backend`` -- sauf si transformers/
    torch ne sont pas installés ici (pour la capacité demandée), ou si
    [simpostcards] detection_enabled = false (501 dans ces deux cas).
    Ni la légende ni les libellés ne sont traduits ici : la traduction
    reste toujours côté client (n'a pas besoin de torch).

  POST /api/compute_segments
    Étant donné une image (recto d'une carte), la segmente avec SAM
    (``simpostcards.libs.segment_backend``) et renvoie tous les segments
    -- utilisé par ``libpostcards.masks.StampDetector`` pour déléguer la
    génération des masques COLMAP (timbres...) à ce serveur ; le choix
    des segments qui sont des timbres reste côté client. 501 si
    transformers/torch ne sont pas installés ici, ou si [simpostcards]
    segment_enabled = false.

  POST /api/translate, GET /api/ocr_languages
    Langues (OCR tesseract, traduction) : le client envoie la langue qu'il
    a choisie, le serveur la vérifie et, si elle n'est pas disponible, le
    consigne dans ses logs et le renvoie au client (422 / bloc
    "translation"). La traduction de la légende et des libellés de
    détection ("lang" de /api/compute_detection) se fait ici ; voir
    [simpostcards] translate_enabled.

  POST /api/unload_models
    Décharge tout de suite tous les modèles en mémoire (CLIP, BLIP, DETR,
    SAM) et rend leur mémoire GPU et RAM ; ils se rechargent seuls à la
    prochaine requête.

Sur un serveur GPU partagé avec d'autres usages, voir [simpostcards]
gpu_idle_timeout : libère automatiquement la mémoire (GPU et RAM) de
chaque modèle (CLIP, BLIP, DETR, SAM) resté inactif trop longtemps -- voir
``simpostcards.libs._idle_unload`` et les docstrings de
``libs.embedder``/``libs.detection_backend``.

Aucune donnée n'est persistée : simpostcards ne lit ni n'écrit dans
datadir/, ne touche pas à postcards.sqlite et n'a pas besoin de
``libpostcards.model.Model``. Elle est indépendante de flpostcards et
peut être déployée séparément (par exemple pour être appelée par
tkpostcards ou par un script de publication).
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path

from flask import Flask

from simpostcards.libs.detection_backend import (
    DEFAULT_MODEL_NAME as DEFAULT_DETECTION_MODEL_NAME,
    DEFAULT_OBJECTS_MODEL_NAME,
)
from simpostcards.libs.embedder import DEFAULT_MODEL_NAME, DEFAULT_PRETRAINED
from simpostcards.libs.segment_backend import (
    DEFAULT_MODEL_NAME as DEFAULT_SEGMENT_MODEL_NAME,
)


def load_config(app: Flask, config_path: str | Path = "postcards.conf") -> None:
    """Charge postcards.conf (section [simpostcards], héritant de [DEFAULT])."""
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(config_path)

    if parser.has_section("simpostcards"):
        defaults = set(parser.defaults().keys())
        for key, value in parser.items("simpostcards"):
            if key in defaults:
                # Clé héritée de [DEFAULT], non pertinente ici
                continue
            if key == "debug":
                app.config["DEBUG"] = parser.getboolean("simpostcards", "debug")
            elif key == "port":
                app.config["PORT"] = parser.getint("simpostcards", "port")
            elif key == "host":
                app.config["HOST"] = value
            elif key == "secret_key":
                app.config["SECRET_KEY"] = value
            elif key == "white_threshold":
                app.config["SCAN_WHITE_THRESHOLD"] = parser.getint(
                    "simpostcards", "white_threshold"
                )
            elif key == "max_content_length_mb":
                app.config["MAX_CONTENT_LENGTH"] = (
                    parser.getint("simpostcards", "max_content_length_mb") * 1024 * 1024
                )
            elif key == "clip_enabled":
                # Permet de désactiver le calcul d'embedding même quand
                # torch/open-clip-torch sont installés (ex : déploiement
                # au CPU limité où seuls les hashs perceptuels sont
                # voulus). Voir libs.embedder.CLIP_AVAILABLE pour le cas
                # "pas installés du tout".
                app.config["CLIP_ENABLED"] = parser.getboolean(
                    "simpostcards", "clip_enabled"
                )
            elif key == "clip_offline":
                # Voir libs.embedder : évite toute tentative réseau vers
                # Hugging Face Hub au chargement du modèle CLIP (poids
                # déjà présents dans le cache local requis).
                app.config["CLIP_OFFLINE"] = parser.getboolean(
                    "simpostcards", "clip_offline"
                )
            elif key == "travel_enabled":
                # Permet de désactiver /api/compute_travel même quand
                # ortools est installé (ex : serveur dédié aux hashs/
                # embeddings, pas au calcul de parcours -- gourmand en
                # CPU). Voir libpostcards.travel.ORTOOLS_AVAILABLE pour
                # le cas "pas installé du tout" (auquel cas la route
                # répond déjà 501, quelle que soit cette valeur).
                app.config["TRAVEL_ENABLED"] = parser.getboolean(
                    "simpostcards", "travel_enabled"
                )
            elif key == "ocr_enabled":
                # Permet de désactiver /api/compute_ocr même quand
                # pytesseract est installé. Voir
                # simpostcards.libs.ocr_backend.OCR_AVAILABLE pour le
                # cas "pas installé du tout" (auquel cas la route
                # répond déjà 501, quelle que soit cette valeur).
                app.config["OCR_ENABLED"] = parser.getboolean(
                    "simpostcards", "ocr_enabled"
                )
            elif key == "detection_enabled":
                # Permet de désactiver /api/compute_detection même
                # quand transformers/torch sont installés (ex : serveur
                # dédié aux hashs/embeddings/OCR, pas à la détection de
                # contenu -- gourmande en mémoire, deux modèles). Voir
                # simpostcards.libs.detection_backend.CAPTION_AVAILABLE/
                # OBJECTS_AVAILABLE pour le cas "pas installé du tout"
                # (auquel cas la route répond déjà 501 pour la capacité
                # concernée, quelle que soit cette valeur).
                app.config["DETECTION_ENABLED"] = parser.getboolean(
                    "simpostcards", "detection_enabled"
                )
            elif key == "paddleocr_enabled":
                # Permet de désactiver le moteur OCR PaddleOCR même quand
                # paddleocr est installé (tesseract reste disponible).
                app.config["PADDLEOCR_ENABLED"] = parser.getboolean(
                    "simpostcards", "paddleocr_enabled"
                )
            elif key == "translate_enabled":
                # Permet de désactiver la traduction côté serveur
                # (/api/translate et le champ "lang" de /api/compute_detection)
                # même quand deep_translator est installé -- voir
                # libpostcards.langs.TRANSLATOR_AVAILABLE pour le cas "pas
                # installé" (501 aussi).
                app.config["TRANSLATE_ENABLED"] = parser.getboolean(
                    "simpostcards", "translate_enabled"
                )
            elif key == "segment_enabled":
                # Permet de désactiver /api/compute_segments (SAM) même
                # quand transformers/torch sont installés -- voir
                # simpostcards.libs.segment_backend.SEGMENT_AVAILABLE
                # pour le cas "pas installés du tout" (501 aussi).
                app.config["SEGMENT_ENABLED"] = parser.getboolean(
                    "simpostcards", "segment_enabled"
                )
            elif key == "gpu_idle_timeout":
                # Voir libs._idle_unload : délai (secondes) d'inactivité
                # au-delà duquel un modèle GPU (CLIP, BLIP, DETR) est
                # déchargé et sa mémoire rendue -- utile sur un serveur
                # GPU partagé avec d'autres usages. 0/vide désactive
                # (comportement historique, jamais déchargé).
                app.config["GPU_IDLE_TIMEOUT"] = parser.getfloat(
                    "simpostcards", "gpu_idle_timeout"
                )
            elif key == "correct_scan_enabled":
                # Permet de désactiver /api/correct_scan (ex : serveur
                # dédié aux hashs/embeddings/OCR/détection, pas au
                # redressement des scans bruts). Contrairement à
                # clip_enabled/ocr_enabled/etc., il n'y a pas de "libs
                # absentes" possible ici : cv2/numpy/
                # libpostcards.contour_utils sont des dépendances de
                # base de simpostcards (déjà requises par
                # /api/compute_hashes), jamais optionnelles.
                app.config["CORRECT_SCAN_ENABLED"] = parser.getboolean(
                    "simpostcards", "correct_scan_enabled"
                )
            else:
                # Inclut notamment clip_model_name / clip_pretrained /
                # clip_device (voir libs.embedder) et
                # detection_model_name / detection_objects_model_name /
                # detection_device (voir libs.detection_backend) :
                # simples chaînes, pas besoin d'un traitement
                # particulier ici.
                app.config[key.upper()] = value

    app.config.setdefault("HOST", "127.0.0.1")
    app.config.setdefault("PORT", 8002)
    app.config.setdefault("SCAN_WHITE_THRESHOLD", 240)
    # Limite par défaut : 25 Mo, largement suffisant pour un scan de
    # carte postale (tiff non compressé compris) tout en évitant
    # qu'une requête abusive ne consomme trop de mémoire/CPU.
    app.config.setdefault("MAX_CONTENT_LENGTH", 25 * 1024 * 1024)
    # Actif par défaut : si torch/open-clip-torch ne sont pas
    # installés (CLIP_AVAILABLE = False, voir libs.embedder), le calcul
    # est de toute façon sauté au niveau de la route, quelle que soit
    # cette valeur -- ce réglage ne sert qu'à désactiver explicitement
    # le calcul sur un déploiement où ces paquets SONT installés.
    app.config.setdefault("CLIP_ENABLED", True)
    app.config.setdefault("CLIP_MODEL_NAME", DEFAULT_MODEL_NAME)
    app.config.setdefault("CLIP_PRETRAINED", DEFAULT_PRETRAINED)
    # Chaîne vide (clé absente, ou présente mais vide) normalisée en
    # None : voir libs.embedder.compute_embedding(device=None), qui
    # sélectionne alors automatiquement cuda si disponible, sinon cpu.
    app.config["CLIP_DEVICE"] = app.config.get("CLIP_DEVICE", "").strip() or None
    # Mode hors-ligne (voir libs.embedder pour le contexte complet :
    # évite les tentatives réseau -- et leurs 5 tentatives avec délai
    # croissant -- vers Hugging Face Hub sur un déploiement sans accès
    # sortant, à condition que les poids soient déjà dans le cache
    # local). Off par défaut (comportement historique : accès réseau
    # autorisé, comme n'importe quel usage normal d'open_clip).
    app.config.setdefault("CLIP_OFFLINE", False)
    # Actif par défaut, comme CLIP_ENABLED : si ortools n'est pas
    # installé (ORTOOLS_AVAILABLE = False, voir libpostcards.travel),
    # /api/compute_travel répond 501 de toute façon, quelle que soit
    # cette valeur -- ce réglage ne sert qu'à désactiver explicitement
    # la route sur un déploiement où ortools EST installé.
    app.config.setdefault("TRAVEL_ENABLED", True)
    # Idem : actif par défaut, sans effet si pytesseract n'est pas
    # installé (voir simpostcards.libs.ocr_backend.OCR_AVAILABLE).
    app.config.setdefault("OCR_ENABLED", True)
    # Langue tesseract utilisée par défaut par /api/compute_ocr si le
    # client n'en précise pas (rare en pratique : PostcardOCR envoie
    # toujours sa propre langue -- voir libpostcards.ocr).
    app.config.setdefault("OCR_DEFAULT_LANG", "fra")
    # Idem : actif par défaut, sans effet si transformers/torch ne sont
    # pas installés (voir simpostcards.libs.detection_backend
    # .CAPTION_AVAILABLE/OBJECTS_AVAILABLE).
    app.config.setdefault("DETECTION_ENABLED", True)
    app.config.setdefault("DETECTION_MODEL_NAME", DEFAULT_DETECTION_MODEL_NAME)
    app.config.setdefault("DETECTION_OBJECTS_MODEL_NAME", DEFAULT_OBJECTS_MODEL_NAME)
    app.config["DETECTION_DEVICE"] = app.config.get("DETECTION_DEVICE", "").strip() or None
    # 0/absent (par défaut) : comportement historique, aucun modèle
    # n'est jamais déchargé automatiquement -- voir libs._idle_unload
    # et le docstring du module ci-dessus.
    app.config.setdefault("GPU_IDLE_TIMEOUT", 0)
    app.config.setdefault("CORRECT_SCAN_ENABLED", True)
    # SAM (/api/compute_segments, masques COLMAP) : actif par défaut, sans
    # effet si transformers/torch ne sont pas installés.
    app.config.setdefault("PADDLEOCR_ENABLED", True)
    # "cpu" laisse le GPU à d'autres usages ; vide = le choix de PaddlePaddle.
    app.config["PADDLEOCR_DEVICE"] = str(app.config.get("PADDLEOCR_DEVICE") or "").strip() or None
    app.config["PADDLEOCR_VERSION"] = str(app.config.get("PADDLEOCR_VERSION") or "").strip() or None
    app.config.setdefault("TRANSLATE_ENABLED", True)
    app.config.setdefault("TRANSLATE_ENGINE", "google")
    # "cpu" par défaut : le modèle de traduction est petit, et le GPU reste
    # libre pour d'autres usages.
    app.config["TRANSLATE_DEVICE"] = str(app.config.get("TRANSLATE_DEVICE") or "").strip() or None
    app.config["TRANSLATE_MARIAN_MODEL"] = str(app.config.get("TRANSLATE_MARIAN_MODEL") or "").strip() or None
    app.config.setdefault("SEGMENT_ENABLED", True)
    app.config.setdefault("SEGMENT_MODEL_NAME", DEFAULT_SEGMENT_MODEL_NAME)
    app.config["SEGMENT_DEVICE"] = app.config.get("SEGMENT_DEVICE", "").strip() or None


def create_app(config_path: str | Path = "postcards.conf") -> Flask:
    app = Flask(__name__)
    load_config(app, config_path)

    # Sans ça, seuls les WARNING+ remontent par défaut : les logs INFO
    # de compute_hashes (taille de l'image reçue, timing du
    # redressement + hashs) resteraient invisibles sous gunicorn.
    app.logger.setLevel(logging.DEBUG if app.debug else logging.INFO)

    # Voir [simpostcards] gpu_idle_timeout ci-dessus / libs._idle_unload :
    # démarre (une fois par processus -- important sous gunicorn
    # --preload avec plusieurs workers, voir la note dans le Dockerfile
    # sur GUNICORN_WORKERS) le déchargement automatique des modèles GPU
    # inactifs. Import différé : ne force pas torch à être importé sur
    # un déploiement qui n'en a pas besoin (ces fonctions ne touchent
    # torch que si un modèle a réellement été chargé au moins une fois).
    from simpostcards.libs.embedder import configure_idle_timeout as _clip_idle
    from simpostcards.libs.detection_backend import configure_idle_timeout as _detection_idle
    from simpostcards.libs.segment_backend import configure_idle_timeout as _segment_idle
    from simpostcards.libs.paddle_backend import configure_idle_timeout as _paddle_idle
    from simpostcards.libs.translate_backend import (
        configure as _configure_translation,
        configure_idle_timeout as _translate_idle,
    )
    idle_timeout = app.config.get("GPU_IDLE_TIMEOUT") or None
    _clip_idle(idle_timeout)
    _detection_idle(idle_timeout)
    _segment_idle(idle_timeout)
    _paddle_idle(idle_timeout)
    _translate_idle(idle_timeout)
    _configure_translation(
        app.config.get("TRANSLATE_ENGINE"),
        device=app.config.get("TRANSLATE_DEVICE"),
        model_template=app.config.get("TRANSLATE_MARIAN_MODEL"))

    from simpostcards.blueprints.api import bp as api_bp
    app.register_blueprint(api_bp)

    return app
