# -*- encoding: utf-8 -*-
"""
libpostcards/langs.py - Langues : disponibilité, erreurs, traduction.

Regroupe ce que l'OCR (``libpostcards.ocr``), la détection
(``libpostcards.detection``), le serveur simpostcards
(``simpostcards.blueprints.api``) et la commande ``kttools
detect-translate`` ont en commun à propos des langues :

- **Erreurs et signalements** : :class:`LanguageUnavailable` (la langue
  demandée n'existe pas là où le calcul est fait), :class:`TranslationError`,
  :class:`TranslatorUnavailable`, et :class:`Issue`, un signalement non
  bloquant (la détection a réussi mais n'a pas pu être traduite) que les
  appelants affichent à l'utilisateur.
- **Moteurs OCR** : tesseract (langues installées / manquantes : une langue
  absente provoquait avant une erreur 500 peu parlante côté serveur) et
  PaddleOCR (langues prises en charge, correspondance avec les codes
  tesseract, SANS importer paddle : le paquet charge tout ``paddlex``).
- **Traduction** : ``deep_translator`` (GoogleTranslator), sa liste de
  langues (statique, consultable HORS LIGNE : c'est ce qui permet de dire
  "langue indisponible" sans appel réseau) et un cache. Utilisée par le
  serveur (``POST /api/translate`` et ``/api/compute_detection``) et, en
  l'absence d'``external_helper``, directement par ``PostcardDetection``.
- :class:`HelperTranslator` : client de ``POST /api/translate``.
- :func:`looks_english` : repère les textes de détection restés en anglais
  (cartes enregistrées avant l'existence du marqueur de langue).

pytesseract, deep_translator et requests sont importés localement ou sous
``try`` : ce module s'importe sans eux, comme le reste de libpostcards.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

try:
    from deep_translator import GoogleTranslator
    from deep_translator.exceptions import TooManyRequests
    TRANSLATOR_AVAILABLE = True
except ImportError:  # pragma: no cover - dépend de l'installation
    GoogleTranslator = None
    TooManyRequests = None
    TRANSLATOR_AVAILABLE = False

LOG = logging.getLogger(__name__)

# Codes de ``Issue.code`` / de la clé "code" des réponses d'erreur du serveur.
LANGUAGE_UNAVAILABLE = "language_unavailable"
ENGINE_UNAVAILABLE = "engine_unavailable"
TRANSLATION_FAILED = "translation_failed"
TRANSLATOR_UNAVAILABLE = "translator_unavailable"


# ── Erreurs et signalements ─────────────────────────────────────────────────

class LanguageUnavailable(RuntimeError):
    """La langue demandée n'est pas disponible là où le calcul est fait.

    ``service`` : "ocr", "detection" ou "translate" ; ``lang`` : la langue
    demandée ; ``missing`` : pour tesseract, les codes absents d'une
    demande composée comme "fra+eng" ; ``available`` : (une partie de) ce
    qui est disponible, pour aider à corriger la configuration.
    """

    def __init__(self, service, lang, message, missing=None, available=None):
        super().__init__(message)
        self.service = service
        self.lang = lang
        self.missing = list(missing or [])
        self.available = list(available or [])


class EngineUnavailable(RuntimeError):
    """Le moteur OCR demandé ne peut pas tourner là où le calcul est fait
    (paquet non installé, désactivé sur le serveur, modèles introuvables...).

    ``engine`` : "tesseract" ou "paddleocr".
    """

    def __init__(self, engine, message):
        super().__init__(message)
        self.engine = engine


class TranslationError(RuntimeError):
    """La traduction a échoué (réseau, quota du service de traduction...)."""


class RateLimited(TranslationError):
    """Le service de traduction refuse de répondre trop vite (HTTP 429) et
    ne s'est pas calmé après plusieurs attentes. ``retry_after`` : secondes
    à attendre avant de réessayer (indicatif)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class TranslatorUnavailable(RuntimeError):
    """Aucun moteur de traduction n'est installé (``deep_translator``)."""


@dataclass(frozen=True)
class Issue:
    """Signalement non bloquant : le résultat existe (légende en anglais),
    mais n'a pas pu être traduit comme demandé."""
    code: str       # LANGUAGE_UNAVAILABLE / TRANSLATION_FAILED / TRANSLATOR_UNAVAILABLE
    service: str    # "ocr" / "detection" / "translate"
    lang: str
    message: str

    def __str__(self):
        return self.message


_logged_once: set = set()


def log_once(logger, key, message, *args):
    """``logger.warning`` une seule fois par ``key`` et par processus : un
    lot de cent cartes dans une langue indisponible ne doit pas produire
    cent lignes identiques."""
    if key in _logged_once:
        return False
    _logged_once.add(key)
    logger.warning(message, *args)
    return True


def reset_logged_once():
    """Pour les tests."""
    _logged_once.clear()


def raise_if_language_unavailable(response, service):
    """Lève :class:`LanguageUnavailable` si *response* (requests) est la
    réponse 422 ``language_unavailable`` d'un serveur simpostcards ;
    sinon ne fait rien (l'appelant continue avec ``raise_for_status``)."""
    if response.status_code != 422:
        return
    try:
        body = response.json()
    except ValueError:
        return
    if isinstance(body, dict) and body.get("code") == LANGUAGE_UNAVAILABLE:
        raise LanguageUnavailable(
            body.get("service") or service, body.get("lang") or "",
            body.get("error") or "language unavailable",
            missing=body.get("missing"),
            available=body.get("available") or body.get("supported"))


def raise_if_engine_unavailable(response):
    """Lève :class:`EngineUnavailable` si *response* est la réponse 501
    ``engine_unavailable`` d'un serveur simpostcards (le moteur OCR demandé
    n'y est pas installé ou y est désactivé)."""
    if response.status_code != 501:
        return
    try:
        body = response.json()
    except ValueError:
        return
    if isinstance(body, dict) and body.get("code") == ENGINE_UNAVAILABLE:
        raise EngineUnavailable(body.get("engine") or "", body.get("error")
                                or "OCR engine unavailable on the server")


# ── Moteurs OCR : choix et langues ──────────────────────────────────────────

OCR_ENGINES = ("tesseract", "paddleocr")
DEFAULT_OCR_ENGINE = "tesseract"
_ENGINE_ALIASES = {"paddle": "paddleocr", "paddle-ocr": "paddleocr",
                   "paddle_ocr": "paddleocr"}


def normalize_ocr_engine(engine):
    """Nom canonique d'un moteur OCR ("Paddle" -> "paddleocr") ; vide ->
    tesseract ; ValueError si inconnu."""
    name = (engine or "").strip().lower() or DEFAULT_OCR_ENGINE
    name = _ENGINE_ALIASES.get(name, name)
    if name not in OCR_ENGINES:
        raise ValueError("unknown OCR engine %r (expected one of: %s)"
                         % (engine, ", ".join(OCR_ENGINES)))
    return name


# ── Tesseract ───────────────────────────────────────────────────────────────

def tesseract_installed_languages():
    """Langues (données ``.traineddata``) installées pour tesseract ici."""
    import pytesseract
    return sorted(pytesseract.get_languages(config=""))


def tesseract_missing_languages(lang, installed=None):
    """Codes de *lang* ("fra" ou "fra+eng") absents de tesseract."""
    wanted = [p.strip() for p in (lang or "").split("+") if p.strip()]
    have = set(installed if installed is not None
               else tesseract_installed_languages())
    return [w for w in wanted if w not in have]


def ocr_language_message(lang, missing, available, engine="tesseract"):
    if engine == "paddleocr":
        return (
            "OCR language %s is not available for PaddleOCR: no model for %s. "
            "Use a PaddleOCR language code (fr, en, de, es, it...) or the usual "
            "tesseract codes (fra, eng, deu, spa, ita...) in ocr_langs "
            "([tkimport], or --ocr-langs), or choose the tesseract engine "
            "(ocr_engine / --ocr-engine)." % (lang, ", ".join(missing) or lang))
    return (
        "OCR language %s is not available: tesseract has no data for %s "
        "(installed: %s). Install the tesseract language pack on the machine "
        "that runs the OCR (e.g. the tesseract-ocr-%s package), or change the "
        "OCR language (ocr_langs in [tkimport], or --ocr-langs)."
        % (lang, ", ".join(missing), ", ".join(available) or "none",
           missing[0] if missing else lang))


# ── PaddleOCR : langues ─────────────────────────────────────────────────────
# Vérifié sur paddleocr 3.7 (``paddleocr._utils.langs`` +
# ``PaddleOCR._get_ocr_model_names``) : tous ces codes ont des modèles. Liste
# recopiée ici car ``import paddleocr`` charge tout ``paddlex`` : on doit
# pouvoir répondre "langue indisponible" sans ce poids (et sans paddle installé,
# côté client qui délègue au serveur). Si le paquet installé est plus ancien
# ou plus récent, son constructeur reste juge (``ValueError: No models are
# available``) : voir ``libpostcards.ocr_paddle.create_engine``.
PADDLE_LANGUAGES = frozenset("""
abq ady af ang ar ava az ba bal be bg bgc bh bho bs bua ca ch che
chinese_cht cs cv cy da dar de el en es et eu fa fi fr french ga german gl
gom hi hr hu id inh is it japan ka kaa kbd kk korean ku kv ky la lb lbe
lez lt lv mah mai mhr mi mk mn mo mr ms mt ne new nl no oc os pi pl ps pt
qu rm ro rs_cyrillic rs_latin ru sa sah sck sd sk sl sq sv sw ta tab te tg
th tl tr tt tyv udm ug uk ur uz vi xal
""".split())

# Codes tesseract (ceux de [tkimport] ocr_langs) -> codes PaddleOCR. Un code
# déjà PaddleOCR ("fr", "german"...) passe tel quel.
TESSERACT_TO_PADDLE = {
    "fra": "fr", "eng": "en", "deu": "de", "spa": "es", "ita": "it",
    "por": "pt", "nld": "nl", "dan": "da", "swe": "sv", "nor": "no",
    "fin": "fi", "pol": "pl", "ces": "cs", "hun": "hu", "ron": "ro",
    "tur": "tr", "cat": "ca", "lat": "la", "eus": "eu", "glg": "gl",
    "hrv": "hr", "slk": "sk", "slv": "sl", "sqi": "sq", "est": "et",
    "lav": "lv", "lit": "lt", "isl": "is", "gle": "ga", "cym": "cy",
    "afr": "af", "ind": "id", "msa": "ms", "vie": "vi", "swa": "sw",
    "ltz": "lb", "oci": "oc", "bos": "bs", "mlt": "mt",
    "rus": "ru", "ukr": "uk", "bel": "be", "bul": "bg", "mkd": "mk",
    "ell": "el", "ara": "ar", "fas": "fa", "urd": "ur", "hin": "hi",
    "mar": "mr", "nep": "ne", "jpn": "japan", "kor": "korean",
    "chi_sim": "ch", "chi_tra": "chinese_cht", "tha": "th", "tel": "te",
    "tam": "ta", "kat": "ka",
}


def paddle_language_parts(lang):
    """"fra+eng" / "fr,en" -> ["fra", "eng"] / ["fr", "en"]."""
    return [p.strip() for p in re.split(r"[+,]", lang or "") if p.strip()]


def resolve_paddle_language(lang):
    """Code PaddleOCR pour *lang* (codes tesseract ou PaddleOCR, éventuellement
    plusieurs : "fra+eng"), ou None si l'un d'eux n'a pas de modèle.

    PaddleOCR n'a qu'UN modèle de reconnaissance par appel (pas de "fra+eng"
    comme tesseract) : pour plusieurs langues on prend la première qui n'est
    pas l'anglais -- le modèle "latin" lit aussi l'anglais, alors que le
    modèle anglais supprime les accents. Des langues d'écritures différentes
    ("fra+rus") ne peuvent donc pas être lues ensemble : seule la première
    l'est.
    """
    codes = []
    for part in paddle_language_parts(lang):
        key = part.lower()
        code = TESSERACT_TO_PADDLE.get(key, key)
        if code not in PADDLE_LANGUAGES:
            return None
        codes.append(code)
    if not codes:
        return None
    return next((c for c in codes if c != "en"), "en")


def paddle_missing_languages(lang):
    """Éléments de *lang* sans modèle PaddleOCR (liste vide : tout va bien)."""
    parts = paddle_language_parts(lang)
    if not parts:
        return [lang or ""]
    return [p for p in parts
            if TESSERACT_TO_PADDLE.get(p.lower(), p.lower()) not in PADDLE_LANGUAGES]


# ── Traduction (deep_translator) ────────────────────────────────────────────

# Codes que les gens écrivent et que le service de traduction nomme
# autrement.
_ALIASES = {"zh": "zh-CN", "he": "iw", "nb": "no", "nn": "no",
            "fil": "tl", "jv": "jw"}

_languages_cache = None


def translation_languages():
    """``{nom: code}`` des langues du moteur de traduction. Liste statique
    de deep_translator : aucun appel réseau."""
    global _languages_cache
    if _languages_cache is None:
        if not TRANSLATOR_AVAILABLE:
            raise TranslatorUnavailable(
                "deep_translator is not installed "
                "(pip install deep-translator)")
        _languages_cache = GoogleTranslator(
            source="en", target="fr").get_supported_languages(as_dict=True)
    return _languages_cache


def normalize_code(lang):
    return (lang or "").strip().replace("_", "-").lower()


def is_english(lang):
    return normalize_code(lang).split("-")[0] == "en"


def resolve_language(lang):
    """Code du moteur de traduction pour *lang* ("fr", "fr-FR", "zh"...),
    ou None si cette langue n'existe pas pour lui."""
    code = normalize_code(lang)
    if not code:
        return None
    by_lower = {c.lower(): c for c in translation_languages().values()}
    for candidate in (code, _ALIASES.get(code), code.split("-")[0],
                      _ALIASES.get(code.split("-")[0])):
        if candidate and candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    return None


def language_unavailable_error(lang, service="translate"):
    codes = sorted(set(translation_languages().values()))
    return LanguageUnavailable(
        service, lang,
        "translation language %r is not available (the translation engine "
        "does not support it)" % (lang,),
        available=codes)


_cache: dict = {}
_CACHE_MAX = 20000

# Le service gratuit de Google tolère environ 5 requêtes par seconde et
# 5000 caractères par requête (au-delà : HTTP 429). Trois mesures :
#  - cadence : au plus une requête toutes les ``MIN_INTERVAL`` secondes pour
#    TOUT le processus (serveur à plusieurs threads compris) ;
#  - regroupement : plusieurs textes courts partent dans UNE requête
#    (séparés par des retours à la ligne), jusqu'à ``PACK_CHARS`` caractères ;
#  - réessais : sur un 429, attente croissante (``BACKOFF``) puis nouvel
#    essai.
# Et un DISJONCTEUR : si le service refuse encore après ces attentes, ce n'est
# plus une rafale mais un refus durable de cette adresse IP (constaté en
# pratique : Google bloque l'adresse pendant des minutes, voire des heures,
# après une rafale). Insister ne sert à rien et peut prolonger le blocage :
# le serveur cesse de le solliciter pendant ``BLOCK_SECONDS[niveau]`` (10 min,
# puis 30 min, puis 2 h si le refus persiste) et répond tout de suite
# :class:`RateLimited` (``retry_after`` = temps restant) ; un succès remet le
# niveau à zéro. ``BACKOFF`` reste court : le client attend la réponse HTTP.
MIN_INTERVAL = 0.25
PACK_CHARS = 4000
BACKOFF = (1.0, 3.0, 8.0)
BLOCK_SECONDS = (600.0, 1800.0, 7200.0)
RETRY_AFTER = 30.0     # attente suggérée pour un 429 sans autre indication
MAX_AUTO_WAIT = 120.0  # au-delà, le client ne patiente pas : il remonte l'erreur

_sleep = time.sleep          # remplaçables dans les tests
_clock = time.monotonic
_upstream_lock = threading.Lock()
_last_call = 0.0
_blocked_until = 0.0         # pacing court (attente après un 429)
_google_down_until = 0.0     # disjoncteur : Google n'est plus sollicité avant
_google_level = 0


def _translate_one(source, target, text):
    """Un appel au moteur (isolé pour pouvoir être remplacé dans les tests)."""
    return GoogleTranslator(source=source, target=target).translate(text)


def google_blocked_for():
    """Secondes pendant lesquelles Google ne sera plus sollicité (0 : il peut
    l'être). Voir le disjoncteur ci-dessus."""
    return max(0.0, _google_down_until - _clock())


def _trip_breaker(exc):
    global _google_down_until, _google_level
    _google_level = min(_google_level + 1, len(BLOCK_SECONDS))
    duration = BLOCK_SECONDS[_google_level - 1]
    _google_down_until = _clock() + duration
    LOG.warning(
        "the translation service keeps refusing this server (HTTP 429) after "
        "%d attempts: not asking it again for %.0f min (level %d/%d). Set "
        "translate_engine = auto or marian in [simpostcards] to translate "
        "locally meanwhile.",
        len(BACKOFF) + 1, duration / 60, _google_level, len(BLOCK_SECONDS))
    raise RateLimited(_blocked_message(duration, exc), retry_after=duration) from exc


def _blocked_message(seconds, detail=None):
    text = ("the translation service is refusing this server (HTTP 429, too "
            "many requests) and is not asked again for %.0f min" % (seconds / 60))
    if detail:
        text += ": %s" % detail
    return (text + ". To translate without it, set translate_engine = auto "
            "(or marian) in [simpostcards]: a local model, with no rate limit.")


def _upstream(source, target, text):
    """Un appel au service, cadencé, réessayé sur HTTP 429, protégé par le
    disjoncteur."""
    global _last_call, _blocked_until, _google_level
    remaining = google_blocked_for()
    if remaining > 0:
        raise RateLimited(_blocked_message(remaining), retry_after=remaining)
    rate_errors = (TooManyRequests,) if TooManyRequests else ()
    for attempt in range(len(BACKOFF) + 1):
        with _upstream_lock:
            now = _clock()
            wait = max(_last_call + MIN_INTERVAL - now, _blocked_until - now, 0.0)
            if wait > 0:
                _sleep(wait)
            _last_call = _clock()
        try:
            result = _translate_one(source, target, text)
        except rate_errors as exc:
            if attempt == len(BACKOFF):
                _trip_breaker(exc)
            delay = BACKOFF[attempt]
            LOG.warning("translation service rate limit (429), waiting %.0fs "
                        "(attempt %d/%d)", delay, attempt + 1, len(BACKOFF))
            with _upstream_lock:
                _blocked_until = max(_blocked_until, _clock() + delay)
        else:
            _google_level = 0
            return result


def _pack(texts, limit=None):
    """Groupes de textes tenant dans une requête (``limit`` caractères, par
    défaut ``PACK_CHARS`` lu à l'appel). Un texte qui contient lui-même un
    retour à la ligne ne peut pas être regroupé (c'est notre séparateur) : il
    part seul."""
    limit = PACK_CHARS if limit is None else limit
    chunk, size = [], 0
    for text in texts:
        if "\n" in text:
            if chunk:
                yield chunk
                chunk, size = [], 0
            yield [text]
            continue
        if chunk and size + len(text) + 1 > limit:
            yield chunk
            chunk, size = [], 0
        chunk.append(text)
        size += len(text) + 1
    if chunk:
        yield chunk


def _translate_chunk(source, target, chunk):
    """Traductions de ``chunk`` (même longueur), en UNE requête si possible."""
    if len(chunk) == 1:
        return [_upstream(source, target, chunk[0])]
    reply = _upstream(source, target, "\n".join(chunk))
    parts = [p.strip() for p in (reply or "").split("\n")]
    if len(parts) == len(chunk) and all(parts):
        return parts
    # Le service a fusionné ou coupé des lignes : impossible de savoir quelle
    # traduction va avec quel texte, on traduit un par un.
    LOG.info("grouped translation returned %d line(s) for %d text(s): "
             "falling back to one request per text", len(parts), len(chunk))
    return [_upstream(source, target, t) for t in chunk]


# ── Moteurs de traduction : Google (en ligne) et Marian (local) ─────────────
# ``google`` (défaut, comportement historique), ``marian`` (modèle local, voir
# libpostcards.translate_marian : sans limite de débit, utilisable hors ligne
# une fois le modèle téléchargé) ou ``auto`` : Google d'abord, et le modèle
# local dès que Google refuse ce serveur (disjoncteur). Le serveur
# simpostcards règle cela ([simpostcards] translate_engine) via
# :func:`configure_translation` ; sans appel, tout reste sur Google.
TRANSLATE_ENGINES = ("google", "marian", "auto")
_engine = "google"
_marian = None


def configure_translation(engine="google", marian=None):
    """Choisit le moteur. ``marian`` : objet avec ``translate(textes, code)``
    et ``prepare(code)`` (voir simpostcards.libs.translate_backend)."""
    global _engine, _marian
    engine = (engine or "google").strip().lower()
    if engine not in TRANSLATE_ENGINES:
        raise ValueError("unknown translation engine %r (expected one of: %s)"
                         % (engine, ", ".join(TRANSLATE_ENGINES)))
    _engine, _marian = engine, marian


def translation_engine():
    return _engine


def _marian_pending(pending, src, code):
    if _marian is None:
        raise TranslatorUnavailable(
            "the local translation engine (marian) is not available on this "
            "server (it needs transformers, torch, sentencepiece, sacremoses: "
            "pip install pypostcards[translate-local])")
    if src not in ("en", "auto"):
        raise language_unavailable_error(src)     # opus-mt-en-xx : source anglaise
    results = _marian.translate(pending, code)
    return dict(zip(pending, results))


def _google_pending(pending, src, code):
    got = {}
    for chunk in _pack(pending):
        for text, result in zip(chunk, _translate_chunk(src, code, chunk)):
            if not result:
                raise TranslationError(
                    "translation to %s returned nothing for %r" % (code, text))
            got[text] = result
    return got


def _translate_pending(pending, src, code):
    """``(traductions, moteur)`` des textes pas encore en cache."""
    if _engine == "marian":
        return _marian_pending(pending, src, code), "marian"
    if _engine == "auto" and _marian is not None and google_blocked_for() > 0:
        return _marian_pending(pending, src, code), "marian"
    try:
        return _google_pending(pending, src, code), "google"
    except RateLimited:
        if _engine == "auto" and _marian is not None:
            LOG.warning("translation: Google refuses this server, translating "
                        "%d text(s) with the local model instead", len(pending))
            return _marian_pending(pending, src, code), "marian"
        raise


def translate_texts_ex(texts, target, source="en"):
    """Comme :func:`translate_texts`, mais renvoie ``(traductions, moteur)`` :
    ``"google"``, ``"marian"`` ou ``None`` si tout venait du cache (ou si la
    langue cible est la langue source)."""
    if not TRANSLATOR_AVAILABLE:
        raise TranslatorUnavailable(
            "deep_translator is not installed (pip install deep-translator)")
    code = resolve_language(target)
    if code is None:
        raise language_unavailable_error(target)
    if source == "auto":
        src = "auto"
    else:
        src = resolve_language(source)
        if src is None:
            raise language_unavailable_error(source)
    if src == code:
        return list(texts), None

    stripped = [(t or "").strip() for t in texts]
    known = {t: _cache[(src, code, t)] for t in set(stripped)
             if t and (src, code, t) in _cache}
    pending = list(dict.fromkeys(
        t for t in stripped if t and t not in known))
    engine = None
    try:
        if pending:
            got, engine = _translate_pending(pending, src, code)
            for text, result in got.items():
                known[text] = result
                if len(_cache) >= _CACHE_MAX:
                    _cache.clear()
                _cache[(src, code, text)] = result
    except (TranslationError, LanguageUnavailable, TranslatorUnavailable):
        raise
    except Exception as exc:
        raise TranslationError(
            "translation to %s failed: %s" % (code, exc)) from exc
    return [known[t] if t else "" for t in stripped], engine


def translate_texts(texts, target, source="en"):
    """Traduit *texts* de *source* vers *target* ; renvoie une liste de même
    longueur, dans le même ordre.

    Lève :class:`TranslatorUnavailable` (pas de moteur),
    :class:`LanguageUnavailable` (langue inconnue du moteur),
    :class:`RateLimited` (Google limite le débit et ne se calme pas : voir le
    disjoncteur plus haut) ou :class:`TranslationError` (autre échec). Les
    résultats sont mis en cache : les libellés d'objets (80 classes COCO au
    plus) ne sont traduits qu'une fois par processus. Voir ``MIN_INTERVAL`` /
    ``PACK_CHARS`` / ``BACKOFF`` pour la cadence, le regroupement et les
    réessais, et :func:`configure_translation` pour le moteur.
    """
    return translate_texts_ex(texts, target, source)[0]


def prepare_translation(lang):
    """Prépare le moteur local pour *lang* (charge, voire télécharge, son
    modèle) quand c'est lui qui va traduire : ``translate_engine = marian``,
    ou ``auto`` alors que Google est bloqué. Renvoie le moteur préparé, ou
    None s'il n'y a rien à préparer (Google). Évite qu'un premier lot de
    traductions ne paie un téléchargement de plusieurs centaines de Mo."""
    if _engine == "google" or (_engine == "auto" and google_blocked_for() <= 0):
        return None
    code = resolve_language(lang)
    if code is None:
        raise language_unavailable_error(lang)
    if _marian is None:
        raise TranslatorUnavailable(
            "the local translation engine (marian) is not available on this "
            "server (it needs transformers, torch, sentencepiece, sacremoses: "
            "pip install pypostcards[translate-local])")
    _marian.prepare(code)
    return "marian"


def clear_translation_cache():
    _cache.clear()


def reset_translation_state():
    """Cache, cadence, disjoncteur et moteur remis à zéro (pour les tests)."""
    global _last_call, _blocked_until, _google_down_until, _google_level
    _cache.clear()
    _last_call = _blocked_until = _google_down_until = 0.0
    _google_level = 0
    configure_translation("google", None)


# ── Client de POST /api/translate ───────────────────────────────────────────

class HelperTranslator:
    """Traduction déléguée à un serveur simpostcards (``external_helper``).

    Quand le service de traduction limite le débit du serveur (HTTP 429, le
    serveur ayant déjà essayé de patienter lui-même), on attend ce qu'il
    demande (``retry_after``) puis on réessaie, au plus ``max_waits`` fois
    par lot ; ``on_wait(secondes, n)`` permet de le dire à l'utilisateur.
    Au-delà, :class:`RateLimited` : ce qui est déjà traduit est conservé par
    l'appelant, relancer reprend.
    """

    def __init__(self, external_helper, timeout=None, on_wait=None, max_waits=3):
        self.base = external_helper.rstrip("/")
        # Une requête de traduction peut durer : le serveur cadence et
        # réessaie lui-même auprès du service (voir BACKOFF).
        self.timeout = max(60.0, timeout or 0.0)
        self.on_wait = on_wait
        self.max_waits = max_waits
        # Moteur de traduction annoncé par le serveur ("google", "marian" ou
        # "auto") après check_language() ; None tant qu'on ne l'a pas demandé,
        # ou si le serveur est trop ancien pour en parler (il date d'avant
        # translate_engine : ni modèle local, ni disjoncteur).
        self.engine = None

    def _post(self, payload, timeout=None):
        import requests
        response = requests.post(f"{self.base}/api/translate", json=payload,
                                 timeout=timeout or self.timeout)
        raise_if_language_unavailable(response, "translate")
        if response.status_code in (429, 501, 502):
            try:
                body = response.json()
            except ValueError:
                body = {}
            detail = body.get("error", "")
            if response.status_code == 429:
                try:
                    retry_after = float(body.get("retry_after")
                                        or response.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = RETRY_AFTER
                raise RateLimited(detail or "translation service rate limit",
                                  retry_after=retry_after)
            exc = (TranslatorUnavailable if response.status_code == 501
                   else TranslationError)
            raise exc(detail or "translation unavailable on the server")
        response.raise_for_status()
        return response.json()

    def check_language(self, lang):
        """Code de langue retenu par le serveur ; lève LanguageUnavailable
        (le serveur le consigne dans ses logs) si elle n'existe pas."""
        # ``prepare`` : si c'est le moteur local qui va traduire, le serveur
        # charge (voire télécharge) son modèle maintenant ; d'où le long délai.
        reply = self._post({"lang": lang, "texts": [], "prepare": True},
                           timeout=max(self.timeout, 900.0))
        self.engine = reply.get("engine")
        return reply["lang"]

    def _post_waiting(self, payload):
        for attempt in range(self.max_waits + 1):
            try:
                return self._post(payload)
            except RateLimited as exc:
                # Un refus long (le disjoncteur du serveur : 10 min ou plus)
                # ne se règle pas en attendant ici : on le remonte tout de
                # suite, avec le temps restant, plutôt que de faire patienter.
                if attempt == self.max_waits or (exc.retry_after or 0) > MAX_AUTO_WAIT:
                    raise
                delay = min(exc.retry_after or RETRY_AFTER, MAX_AUTO_WAIT)
                LOG.warning("translation rate-limited: waiting %.0fs "
                            "(wait %d/%d)", delay, attempt + 1, self.max_waits)
                if self.on_wait is not None:
                    self.on_wait(delay, attempt + 1)
                _sleep(delay)

    def translate(self, texts, lang, source="en", batch_size=40):
        """Traductions de ``texts`` (même ordre). Le serveur regroupe luimême
        les textes en requêtes au service de traduction : 40 textes par appel
        ici, c'est typiquement UNE requête là-bas."""
        texts = list(texts)
        out = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            reply = self._post_waiting(
                {"lang": lang, "source": source, "texts": chunk})
            out.extend(reply["translations"])
        return out


# ── Texte de détection resté en anglais ─────────────────────────────────────

COCO_LABELS = frozenset(label.strip() for label in """
person, bicycle, car, motorcycle, airplane, bus, train, truck, boat,
traffic light, fire hydrant, stop sign, parking meter, bench, bird, cat, dog,
horse, sheep, cow, elephant, bear, zebra, giraffe, backpack, umbrella,
handbag, tie, suitcase, frisbee, skis, snowboard, sports ball, kite,
baseball bat, baseball glove, skateboard, surfboard, tennis racket, bottle,
wine glass, cup, fork, knife, spoon, bowl, banana, apple, sandwich, orange,
broccoli, carrot, hot dog, pizza, donut, cake, chair, couch, potted plant,
bed, dining table, toilet, tv, laptop, mouse, remote, keyboard, cell phone,
microwave, oven, toaster, sink, refrigerator, book, clock, vase, scissors,
teddy bear, hair drier, toothbrush
""".replace("\n", " ").split(","))

_EN_WORDS = frozenset("""
a an the of with in on at and is are to next front standing sitting there
view photo picture black white street man woman people building buildings
old city road car cars between near behind from for by that this
""".split())
_OTHER_WORDS = frozenset("""
un une le la les des du de et avec dans sur devant rue homme femme il elle est
sont derrière près pour par der die das und mit ein eine el los las y con en
una lo gli e di da per
""".split())


def looks_english(text):
    """Heuristique : ce texte de détection est-il (encore) de l'anglais ?

    Pour les cartes enregistrées avant l'existence du marqueur de langue
    (``detected_content_lang``), dont rien d'autre ne dit si la légende a été
    traduite. Une liste d'objets dont tous les éléments sont des classes
    COCO est de l'anglais brut ; une légende l'est si elle contient plus de
    mots-outils anglais que d'autres langues (BLIP produit un anglais très
    régulier : "a man standing in front of a building"). Ce n'est qu'une
    heuristique : la commande permet de la désactiver (``--no-guess``) et de
    prévisualiser (``--dry-run``).
    """
    text = (text or "").strip().lower()
    if not text:
        return False
    items = [i.strip() for i in text.split(",") if i.strip()]
    if items and all(i in COCO_LABELS for i in items):
        return True
    words = re.findall(r"[a-zà-ÿ']+", text)
    english = sum(1 for w in words if w in _EN_WORDS)
    other = sum(1 for w in words if w in _OTHER_WORDS)
    return english >= 1 and english > other
