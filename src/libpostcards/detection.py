# -*- encoding: utf-8 -*-
"""
libpostcards/detection.py - Détection automatique du contenu du recto
des cartes postales.

Déplacé depuis tkpostcards.libs.detection (même changement que
libpostcards.similar/travel/ocr en leur temps) : cette classe ne
dépend pas de Tk et est aussi utile côté serveur
(``simpostcards.blueprints.api``, qui expose POST /api/compute_detection
pour ce module) que côté desktop
(``tkpostcards.libs.scan_add``/tkmanager/tktools).

Deux détections complémentaires, toutes deux traduites (générées en
anglais par les modèles) dans la langue voulue :

- ``caption`` / ``to_string`` : description globale de l'image, via un
  modèle de "image captioning" (BLIP -
  Salesforce/blip-image-captioning-large, via `transformers`).
- ``detect_objects`` / ``to_objects_string`` : liste des objets
  identifiés dans l'image, via un modèle de détection d'objets (DETR -
  facebook/detr-resnet-101, via `transformers`). La variante
  "resnet-101" (plutôt que "resnet-50") et un seuil de confiance élevé
  par défaut sont choisis délibérément pour privilégier la qualité
  (peu de faux positifs) à la quantité de détections.

Suit le même principe que libpostcards.ocr.PostcardOCR : les
dépendances lourdes (torch, transformers) sont volontairement
importées localement, dans les méthodes qui en ont besoin, et non ici
en tête de module. Cela permet d'importer libpostcards.detection (ex :
pour introspection ou tests) sans les avoir installées ; elles ne sont
requises qu'au moment où la détection est réellement effectuée -- voir
l'extra "detection" de pyproject.toml. ``deep_translator`` (traduction)
fait exception : léger, sans lien avec torch, il reste utilisé de la
même façon que la détection soit locale ou distante -- voir
``translate()``.

Un déploiement peut aussi se passer ENTIÈREMENT de transformers/torch
en pointant PostcardDetection vers un serveur simpostcards distant via
``external_helper`` : le calcul (légende BLIP et/ou détection d'objets
DETR) se fait alors côté serveur (POST /api/compute_detection) -- même
principe que ``external_helper`` sur
``libpostcards.similar.PostcardSearcher``/
``libpostcards.travel.ParcoursCartes``/``libpostcards.ocr.PostcardOCR``.
La traduction suit la même logique que le calcul : avec ``external_helper``,
la langue demandée (``lang``) est envoyée au serveur, qui traduit lui-même la
légende et les libellés (voir ``translation`` dans la réponse de
``POST /api/compute_detection``) ; sans lui, la traduction est locale
(``deep_translator``). Un serveur plus ancien, qui ne renvoie pas de bloc
``translation``, est traité comme avant : traduction locale du texte anglais.

Si la langue n'est pas disponible (ou si la traduction échoue), la détection
n'est pas perdue : le texte reste en anglais, la langue du texte
(``DetectionText.lang``) vaut alors "en", et un :class:`libpostcards.langs.Issue`
est renvoyé à l'appelant (``DetectionText.issues`` / ``PostcardDetection
.issues``) en plus d'être consigné dans les logs ; la commande ``kttools
detect-translate`` permet de traduire ces textes plus tard.
"""
import io
import logging
import re
from dataclasses import dataclass, field

import requests

from PIL import Image

from .langs import (
    LANGUAGE_UNAVAILABLE, TRANSLATION_FAILED, TRANSLATOR_UNAVAILABLE, Issue,
    HelperTranslator, LanguageUnavailable, TranslationError,
    TranslatorUnavailable, is_english, log_once, normalize_code,
    resolve_language, translate_texts,
)

LOG = logging.getLogger(__name__)


@dataclass
class DetectionText:
    """Texte de détection (légende ou liste d'objets) prêt à être stocké.

    ``lang`` : langue du texte, à enregistrer dans
    ``detected_content_lang`` / ``detected_objects_lang`` ("en" quand il
    n'a pas pu être traduit). ``issues`` : signalements à montrer à
    l'utilisateur (langue indisponible, traduction en échec...).
    """
    text: str
    lang: str
    issues: list = field(default_factory=list)

try:
    from transformers import BlipProcessor, BlipForConditionalGeneration
    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False

try:
    from transformers import DetrImageProcessor, DetrForObjectDetection
    OBJDETECT_AVAILABLE = True
except ImportError:
    OBJDETECT_AVAILABLE = False

# Combine les deux flags ci-dessus : whether a purely local computation
# is possible (les deux modèles). Voir DETECTION_AVAILABLE dans
# tkpostcards.libs.scan_add pour comment ceci se combine avec
# ``external_helper`` (soit l'un, soit l'autre suffit).
DETECTION_LIBS_AVAILABLE = TRANSFORMERS_AVAILABLE and OBJDETECT_AVAILABLE

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_AVAILABLE = True
except ImportError:
    TRANSLATOR_AVAILABLE = False


# Mots-artefacts connus du modèle BLIP (voir par exemple
# https://huggingface.co/Salesforce/blip-image-captioning-large/discussions/20) :
# des mots sans signification ("arafed", "araffe"...), absents de tout
# dictionnaire, que le modèle insère fréquemment dans ses légendes --
# vraisemblablement hérités de son jeu d'entraînement. Ils sont
# généralement précédés d'un article ("a"/"an") qui, avec le mot-artefact,
# tenait la place d'un simple "a" dans la légende voulue par le modèle.
#
# NOTE : dupliqué (volontairement, pas importé) dans
# simpostcards.libs.detection_backend, pour le calcul distant -- garder
# les deux synchronisés en cas de modification.
_BLIP_ARTIFACT_WORDS = ("arafed", "araffed", "araffe", "araffes")
_BLIP_ARTIFACT_RE = re.compile(
    r"\b(?:an?\s+)?(?:" + "|".join(_BLIP_ARTIFACT_WORDS) + r")\b", re.IGNORECASE
)


def _strip_blip_artifacts(text):
    """
    Retire les mots-artefacts connus de BLIP (voir _BLIP_ARTIFACT_WORDS)
    d'une légende générée. Un éventuel article ("a"/"an") précédant
    immédiatement le mot-artefact est retiré avec lui, et remplacé par un
    simple "a" si de la légende suit encore (pour rester grammatical,
    ex : "an arafed cat..." -> "a cat..."), ou entièrement supprimé sinon
    (fin de légende). Les espaces multiples résultants sont nettoyés.

    Renvoie ``text`` inchangé si rien n'a été trouvé (ou si ``text`` est
    vide), et l'original si jamais le nettoyage viderait complètement la
    chaîne (cas limite improbable, filet de sécurité).
    """
    if not text:
        return text

    def _replace(match):
        return "a" if text[match.end():].strip() else ""

    cleaned = _BLIP_ARTIFACT_RE.sub(_replace, text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or text


class PostcardDetection:
    """
    Détecte le contenu visuel d'une image (typiquement le recto d'une
    carte postale) de deux façons complémentaires :

    - une description globale (légende), via un modèle BLIP de "image
      captioning" (voir ``caption`` / ``to_string``) ;
    - la liste des objets identifiés, via un modèle DETR de détection
      d'objets (voir ``detect_objects`` / ``to_objects_string``).

    Dans les deux cas, le résultat (généré en anglais par les modèles,
    localement ou via ``external_helper``) est traduit localement dans
    la langue voulue.

    Les modèles ne sont chargés qu'à la première utilisation réelle de
    chaque fonctionnalité (voir _ensure_model() / _ensure_objects_model()),
    ce qui permet d'instancier PostcardDetection() sans coût si une
    seule des deux détections est finalement utilisée (ou aucune) --
    sans effet quand ``external_helper`` est utilisé (rien n'est jamais
    chargé localement dans ce cas).
    """

    def __init__(
        self,
        model_name="Salesforce/blip-image-captioning-large",
        objects_model_name="facebook/detr-resnet-101",
        lang="fr",
        device=None,
        # Seuil de confiance minimal (0-1) pour qu'une détection
        # d'objet soit retenue, et nombre maximal d'objets renvoyés.
        # Valeurs volontairement conservatrices (seuil élevé, peu
        # d'objets) : on préfère ici la qualité (peu de faux positifs,
        # des libellés fiables) à l'exhaustivité de la détection.
        objects_threshold=0.92,
        max_objects=10,
        # Libellés (anglais, tels que renvoyés par DETR, voir
        # https://huggingface.co/facebook/detr-resnet-101#coco-detection-id2label
        # pour la liste complète des classes COCO) à toujours exclure des
        # résultats de detect_objects()/to_objects_string(), quelle que
        # soit leur confiance : utile pour les faux positifs récurrents
        # d'un modèle donné sur ce type d'images (ex : DETR confond
        # régulièrement certains motifs de cartes postales anciennes
        # avec des "tie"). Comparaison insensible à la casse.
        excluded_objects=None,
        # Début de légende imposé à BLIP ("conditional captioning") :
        # le modèle COMPLÈTE ce texte au lieu de partir de zéro, ce qui
        # garantit que la légende commence par "a postcard of ..." (et
        # non "a photo of ..." ou rien). Chaîne vide/None : légende
        # libre, comme avant.
        # ~ caption_prompt="a postcard of",
        caption_prompt="",
        debug=False,
        external_helper=None,
        external_helper_timeout=None,
    ):
        self.model_name = model_name
        self.objects_model_name = objects_model_name
        self.lang = lang
        self.objects_threshold = objects_threshold
        self.max_objects = max_objects
        self.caption_prompt = (caption_prompt or "").strip() or None
        self.debug = debug
        # Libellés (dans self.lang, ex: "cravate" en français, ou déjà
        # en anglais, ex: "tie") à toujours exclure des résultats de
        # detect_objects()/to_objects_string(), quelle que soit leur
        # confiance : utile pour les faux positifs récurrents d'un
        # modèle donné sur ce type d'images (ex : DETR confond
        # régulièrement certains motifs de cartes postales anciennes
        # avec des "tie"). Chaque mot est comparé aux libellés bruts
        # DETR (toujours en anglais, voir
        # https://huggingface.co/facebook/detr-resnet-101#coco-detection-id2label) ;
        # comme la personne les tape naturellement dans sa langue
        # (self.lang), ils sont aussi traduits vers l'anglais une fois
        # à la construction (voir _to_english_words) pour reconnaître
        # "cravate" aussi bien que "tie". Comparaison insensible à la
        # casse dans tous les cas. Ce filtrage est TOUJOURS appliqué
        # localement (voir detect_objects()), que le calcul brut vienne
        # d'un modèle local ou d'``external_helper`` -- les exclusions
        # sont propres à cette installation/cet utilisateur, pas
        # envoyées au serveur distant.
        self.excluded_objects = self._to_english_words(excluded_objects or [])

        # device : même logique que PostcardSearcher
        # (libpostcards.similar) : si fourni (ex : [DEFAULT]
        # torch_device de postcards.conf, ou --torch-device en ligne de
        # commande), on l'utilise tel quel ; sinon détection
        # automatique (cuda si disponible, sinon cpu). Si torch n'est
        # pas installé, on retombe sur "cpu" sans lever d'erreur, pour
        # permettre d'instancier PostcardDetection() sans avoir torch
        # installé (l'erreur explicite n'intervient qu'à l'utilisation
        # réelle, dans _ensure_model()). Sans effet quand
        # ``external_helper`` est utilisé (device choisi par le
        # serveur -- voir [simpostcards] detection_device).
        if device:
            self.device = device
        else:
            try:
                import torch
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                self.device = "cpu"

        self.processor = None
        self.model = None

        self.objects_processor = None
        self.objects_model = None

        # Signalements (libpostcards.langs.Issue, dédoublonnés par code et
        # langue) accumulés pendant la vie de l'instance : voir describe().
        self.issues = []
        # Passe à True quand la langue demandée s'est révélée indisponible :
        # on cesse alors de la demander (inutile et bruyant dans les logs du
        # serveur, un lot de cent cartes ne doit pas répéter cent fois la
        # même erreur) ; le texte reste en anglais, et ``lang`` = "en".
        self._lang_unavailable = False
        self._translator_unavailable = False

        # URI d'un serveur simpostcards (ex : "http://simpostcards:8004",
        # voir [DEFAULT] external_helper dans postcards.conf) utilisé
        # pour déléguer le calcul (légende BLIP et/ou détection d'objets
        # DETR) à ce serveur AU LIEU d'un calcul local -- voir caption()
        # et detect_objects(). Vide/None (par défaut) : calcul local,
        # comme avant (transformers + torch requis).
        self.external_helper = (external_helper or "").strip() or None
        self.external_helper_timeout = (
            60.0 if external_helper_timeout is None else external_helper_timeout)

    # --------------------------------------------------

    def _to_english_words(self, labels):
        """
        Retourne l'ensemble des mots de ``labels`` (dans ``self.lang``),
        complété par leur traduction anglaise quand elle a pu être
        obtenue (ex : "cravate" -> {"cravate", "tie"}) -- pour permettre
        de renseigner ``excluded_objects`` dans sa propre langue plutôt
        qu'en vocabulaire COCO/DETR (anglais).

        Ne lève jamais d'exception : si la traduction d'un mot échoue
        (``deep_translator`` absent, hors ligne, ``self.lang`` déjà
        anglais...), seul le mot original (en minuscules) est conservé
        pour celui-ci -- ce qui reste correct si la personne a
        justement tapé le mot anglais directement.
        """
        words = set()
        needs_translation = (
            TRANSLATOR_AVAILABLE
            and self.lang
            and not self.lang.lower().startswith("en")
        )
        for label in labels:
            label = label.strip()
            if not label:
                continue
            words.add(label.lower())
            if needs_translation:
                try:
                    translated = GoogleTranslator(
                        source=self.lang, target="en"
                    ).translate(label)
                except Exception as e:
                    if self.debug:
                        print(f"Could not translate excluded object {label!r}: {e}")
                    continue
                if translated:
                    words.add(translated.strip().lower())
        return words

    # --------------------------------------------------

    @staticmethod
    def _silence_transformers_logging():
        """
        Réduit la verbosité de `transformers` au chargement des modèles :
        les "LOAD REPORT" (clés UNEXPECTED/MISSING) qu'elle affiche par
        défaut sont normaux pour ces modèles (ex : DETR n'a pas besoin de
        `num_batches_tracked`, présent dans le checkpoint car sauvegardé
        avec des BatchNorm2d standards plutôt que FrozenBatchNorm2d) et
        n'indiquent aucun problème de chargement.
        """
        try:
            from transformers import logging as hf_logging
            hf_logging.set_verbosity_error()
        except ImportError:
            pass

    # --------------------------------------------------

    def _ensure_model(self):
        """Charge le modèle BLIP (transformers) s'il ne l'est pas déjà."""
        if self.model is not None:
            return

        if not TRANSFORMERS_AVAILABLE:
            raise ImportError(
                "transformers and torch are required to run content "
                "detection. Install them with: pip install pypostcards[detection]"
            )

        self._silence_transformers_logging()
        self.processor = BlipProcessor.from_pretrained(self.model_name)
        self.model = BlipForConditionalGeneration.from_pretrained(self.model_name)
        self.model = self.model.to(self.device)
        self.model.eval()

    # --------------------------------------------------

    def _ensure_objects_model(self):
        """Charge le modèle DETR (transformers) s'il ne l'est pas déjà."""
        if self.objects_model is not None:
            return

        if not OBJDETECT_AVAILABLE:
            raise ImportError(
                "transformers and torch are required to run object "
                "detection. Install them with: pip install pypostcards[detection]"
            )

        self._silence_transformers_logging()
        self.objects_processor = DetrImageProcessor.from_pretrained(self.objects_model_name)
        self.objects_model = DetrForObjectDetection.from_pretrained(self.objects_model_name)
        self.objects_model = self.objects_model.to(self.device)
        self.objects_model.eval()

    # --------------------------------------------------

    def _load_image(self, image):
        """
        Retourne une image PIL RGB à partir de ``image``, qui peut être :
        - une instance ``PIL.Image.Image`` déjà chargée, utilisée telle
          quelle (pas de round-trip disque) ;
        - un chemin de fichier (str / Path), ou tout objet acceptable
          par ``PIL.Image.open`` (ex : un objet fichier déjà ouvert).
        """
        img = image if isinstance(image, Image.Image) else Image.open(image)
        return img.convert("RGB")

    # --------------------------------------------------

    def _post_to_helper(self, image, **fields):
        """
        POST *image* (convertie en PNG en mémoire, pas de fichier
        temporaire) vers ``{self.external_helper}/api/compute_detection``
        avec les champs de formulaire *fields* en plus, et renvoie le
        JSON de la réponse. Factorisé entre caption()/detect_objects().
        """
        img = self._load_image(image)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        response = requests.post(
            f"{self.external_helper.rstrip('/')}/api/compute_detection",
            files={"image": ("image.png", buf, "image/png")},
            data=fields,
            timeout=self.external_helper_timeout,
        )
        response.raise_for_status()
        return response.json()

    # --------------------------------------------------

    def caption(self, image):
        """
        Génère une légende (en anglais, langue native de BLIP)
        décrivant le contenu de ``image`` (chemin de fichier ou
        ``PIL.Image`` déjà chargée), sans traduction.

        Si ``self.external_helper`` est configuré, délègue à ce
        serveur simpostcards au lieu d'un calcul local -- transformers/
        torch ne sont alors pas nécessaires ici.

        Les mots-artefacts connus de BLIP (ex : "arafed", voir
        _BLIP_ARTIFACT_WORDS) sont retirés automatiquement du résultat,
        que le calcul soit local ou distant.
        """
        if self.external_helper:
            payload = self._post_to_helper(
                image, caption="true", objects="false",
                **self._prompt_fields())
            return payload["caption"]

        self._ensure_model()

        import torch

        img = self._load_image(image)
        if self.caption_prompt:
            inputs = self.processor(
                img, text=self.caption_prompt, return_tensors="pt"
            ).to(self.device)
            # Le prompt consomme une partie du budget de tokens par
            # défaut (20) : on l'élargit pour ne pas tronquer la légende.
            gen_kwargs = {"max_new_tokens": 40, "num_beams": 3}
        else:
            inputs = self.processor(img, return_tensors="pt").to(self.device)
            gen_kwargs = {}

        with torch.no_grad():
            out = self.model.generate(**inputs, **gen_kwargs)

        raw = self.processor.decode(out[0], skip_special_tokens=True)
        return _strip_blip_artifacts(raw)

    def _prompt_fields(self):
        """Champ de formulaire ``prompt`` envoyé à ``external_helper``
        (ignoré par un serveur plus ancien : légende libre alors)."""
        return {"prompt": self.caption_prompt} if self.caption_prompt else {}

    # --------------------------------------------------

    def translate(self, text, lang=None):
        """
        Traduit ``text`` (typiquement la légende anglaise renvoyée par
        caption()) vers ``lang`` (par défaut : ``self.lang``, ex :
        "fr"), localement (``deep_translator``).

        Retourne ``text`` tel quel si la langue cible est l'anglais, si
        ``text`` est vide, ou si la traduction est impossible (voir
        :meth:`describe` pour savoir POURQUOI, et dans quelle langue est
        le texte renvoyé) : la légende anglaise reste toujours disponible,
        la traduction est une amélioration, pas un prérequis.
        """
        return self._translate_local(text, lang or self.lang).text

    # --------------------------------------------------

    def _marker_lang(self, lang):
        """Code de langue à enregistrer pour un texte traduit en ``lang``."""
        if not lang or is_english(lang):
            return "en"
        try:
            return resolve_language(lang) or normalize_code(lang)
        except TranslatorUnavailable:
            return normalize_code(lang)

    def _report(self, code, lang, message):
        """Consigne (logs, une fois par code+langue) et renvoie un Issue ;
        ajouté à ``self.issues`` s'il n'y est pas déjà."""
        log_once(LOG, ("detection", code, lang), "%s", message)
        issue = Issue(code, "detection", lang, message)
        if not any(i.code == code and i.lang == lang for i in self.issues):
            self.issues.append(issue)
        if code == LANGUAGE_UNAVAILABLE:
            self._lang_unavailable = True
        elif code == TRANSLATOR_UNAVAILABLE:
            self._translator_unavailable = True
        return issue

    def _translate_local(self, text, lang):
        """Traduction locale -> DetectionText (jamais d'exception)."""
        if not lang or is_english(lang) or not text:
            return DetectionText(text or "", self._marker_lang(lang), [])
        if self._lang_unavailable or self._translator_unavailable:
            return DetectionText(text, "en", [])   # déjà signalé : une fois
        try:
            out = translate_texts([text], lang)[0]
        except LanguageUnavailable as exc:
            return DetectionText(text, "en", [self._report(
                LANGUAGE_UNAVAILABLE, lang,
                "Detection language %r is not available, keeping the "
                "English text (translate it later with \"kttools "
                "detect-translate\"): %s" % (lang, exc))])
        except TranslatorUnavailable as exc:
            return DetectionText(text, "en", [self._report(
                TRANSLATOR_UNAVAILABLE, lang,
                "No translation engine available, keeping the English text "
                "(translate it later with \"kttools detect-translate\"): "
                "%s" % exc)])
        except TranslationError as exc:
            return DetectionText(text, "en", [self._report(
                TRANSLATION_FAILED, lang,
                "Translation to %r failed, keeping the English text "
                "(translate it later with \"kttools detect-translate\"): "
                "%s" % (lang, exc))])
        return DetectionText(out, self._marker_lang(lang), [])

    def _lang_fields(self, lang):
        """Champs de formulaire ``lang`` à envoyer au serveur : seulement
        quand une traduction est utile et possible."""
        if not lang or is_english(lang) or self._lang_unavailable \
                or self._translator_unavailable:
            return {}
        return {"lang": lang}

    def _from_payload(self, english, payload, lang, pick):
        """DetectionText à partir de la réponse du serveur ; ``pick(tr)``
        extrait le texte traduit du bloc ``translation``."""
        translation = payload.get("translation")
        if translation is None:
            # Pas de bloc translation : langue non demandée (anglais, ou
            # indisponible, déjà signalé) ou serveur trop ancien pour
            # traduire -> traduction locale, comme avant.
            return self._translate_local(english, lang)
        status = translation.get("status")
        if status == "ok":
            return DetectionText(pick(translation) or english,
                                 translation.get("lang") or self._marker_lang(lang),
                                 [])
        code = {LANGUAGE_UNAVAILABLE: LANGUAGE_UNAVAILABLE,
                TRANSLATOR_UNAVAILABLE: TRANSLATOR_UNAVAILABLE}.get(
                    status, TRANSLATION_FAILED)
        message = translation.get("message") or (
            "the server could not translate to %r (%s)" % (lang, status))
        return DetectionText(english, "en", [self._report(
            code, lang,
            "Detection language %r: %s Keeping the English text (translate "
            "it later with \"kttools detect-translate\")." % (lang, message))])

    def check_language(self, lang=None):
        """Vérifie TOUT DE SUITE que la langue de traduction est
        disponible (sur ``external_helper`` s'il est configuré, sinon ici),
        pour prévenir l'utilisateur avant un lot au lieu de le découvrir
        carte après carte. Renvoie None si tout va bien, sinon l'
        :class:`libpostcards.langs.Issue` (déjà consigné dans les logs et
        dans ``self.issues``) ; le texte restera alors en anglais.
        Jamais d'exception : la détection reste possible sans traduction.
        """
        lang = lang or self.lang
        if not lang or is_english(lang):
            return None
        try:
            if self.external_helper:
                HelperTranslator(
                    self.external_helper,
                    self.external_helper_timeout).check_language(lang)
            elif resolve_language(lang) is None:
                from .langs import language_unavailable_error
                raise language_unavailable_error(lang, "detection")
        except LanguageUnavailable as exc:
            return self._report(
                LANGUAGE_UNAVAILABLE, lang,
                "Detection language %r is not available, detected texts "
                "will stay in English (translate them later with "
                "\"kttools detect-translate\"): %s" % (lang, exc))
        except TranslatorUnavailable as exc:
            return self._report(
                TRANSLATOR_UNAVAILABLE, lang,
                "No translation engine available, detected texts will stay "
                "in English: %s" % exc)
        except (TranslationError, requests.RequestException):
            return None   # indisponibilité passagère : on essaiera à l'usage
        return None

    # --------------------------------------------------

    def to_string(self, image, lang=None):
        """
        Méthode principale, sur le même principe que
        ``PostcardOCR.to_string`` : détecte le contenu de ``image``
        (chemin de fichier OU image PIL déjà chargée) et retourne la
        légende traduite dans ``lang`` (par défaut : ``self.lang``).
        """
        return self.describe(image, lang=lang).text

    def describe(self, image, lang=None):
        """Comme :meth:`to_string`, mais renvoie un :class:`DetectionText` :
        le texte, SA LANGUE (à enregistrer dans ``detected_content_lang`` ;
        "en" s'il n'a pas pu être traduit) et les signalements éventuels.

        Avec ``external_helper``, la langue est envoyée au serveur qui
        traduit lui-même (``POST /api/compute_detection``, champ ``lang``)."""
        lang = lang or self.lang
        if self.external_helper:
            payload = self._post_to_helper(
                image, caption="true", objects="false",
                **self._prompt_fields(), **self._lang_fields(lang))
            return self._from_payload(
                payload["caption"], payload, lang, lambda t: t.get("caption"))
        return self._translate_local(self.caption(image), lang)

    # --------------------------------------------------

    def detect_objects(self, image, threshold=None, max_objects=None, excluded_objects=None):
        """
        Détecte les objets présents dans ``image`` (chemin de fichier
        ou ``PIL.Image`` déjà chargée) via le modèle DETR, sans
        traduction.

        Si ``self.external_helper`` est configuré, délègue le calcul
        brut (avant filtrage des exclusions et troncature à
        ``max_objects``, toujours appliqués localement -- voir plus
        bas) à ce serveur simpostcards au lieu d'un calcul local --
        transformers/torch ne sont alors pas nécessaires ici.

        Retourne une liste de ``(label, score)`` (libellés anglais,
        tels que renvoyés par le modèle), dédoublonnée par libellé (un
        même objet détecté à plusieurs endroits ne garde que sa
        meilleure occurrence), triée par confiance décroissante, et
        limitée à :

        - ``threshold`` (par défaut : ``self.objects_threshold``) :
          score de confiance minimal (0-1) pour qu'une détection soit
          retenue ;
        - ``max_objects`` (par défaut : ``self.max_objects``) : nombre
          maximal d'objets renvoyés, une fois triés ;
        - ``excluded_objects`` (par défaut : ``self.excluded_objects``) :
          libellés (dans ``lang`` par défaut, sinon ``self.lang`` --
          voir ``_to_english_words``) systématiquement écartés avant
          tri/troncature -- utile pour retirer les faux positifs
          récurrents d'un modèle donné (ex : "tie"/"cravate").

        Les valeurs par défaut sont volontairement conservatrices
        (seuil élevé, peu d'objets) : on privilégie ici la qualité des
        détections (peu de faux positifs) à leur exhaustivité.
        """
        threshold = self.objects_threshold if threshold is None else threshold
        max_objects = self.max_objects if max_objects is None else max_objects
        excluded = (
            self.excluded_objects if excluded_objects is None
            else self._to_english_words(excluded_objects)
        )

        detections, _payload = self._objects_candidates(
            image, threshold, max_objects, excluded)
        return detections

    def _objects_candidates(self, image, threshold, max_objects, excluded, lang=None):
        """``(détections, réponse_brute_du_serveur)``. Les détections sont
        filtrées (exclusions) et tronquées (``max_objects``) ICI, sur les
        libellés anglais, que le calcul vienne du serveur ou d'un modèle
        local ; la réponse brute (``{}`` en local) porte le bloc
        ``translation`` éventuel -- voir :meth:`describe_objects`."""
        payload = {}
        if self.external_helper:
            fields = self._lang_fields(lang) if lang else {}
            payload = self._post_to_helper(
                image, caption="false", objects="true",
                threshold=str(threshold), **fields)
            best_by_label = [(item["label"], item["score"]) for item in payload["objects"]]
        else:
            best_by_label = self._detect_objects_local(image, threshold)

        detections = [(label, score) for label, score in best_by_label
                      if label.lower() not in excluded]

        if max_objects is not None:
            detections = detections[:max_objects]

        return detections, payload

    def _detect_objects_local(self, image, threshold):
        """Calcul local (DETR) des détections brutes -- dédoublonnées
        par libellé (meilleur score), triées par confiance décroissante,
        AVANT filtrage des exclusions/troncature (voir detect_objects,
        qui applique ces deux étapes ensuite, identiquement que ce
        résultat vienne d'ici ou d'``external_helper``)."""
        self._ensure_objects_model()

        import torch

        img = self._load_image(image)
        inputs = self.objects_processor(images=img, return_tensors="pt").to(self.device)

        with torch.no_grad():
            outputs = self.objects_model(**inputs)

        # (height, width) attendu par post_process_object_detection ;
        # img.size est (width, height), d'où l'inversion.
        target_sizes = torch.tensor([img.size[::-1]])
        results = self.objects_processor.post_process_object_detection(
            outputs, target_sizes=target_sizes, threshold=threshold
        )[0]

        best_by_label = {}
        for score, label_id in zip(results["scores"], results["labels"]):
            label = self.objects_model.config.id2label[label_id.item()]
            score = score.item()
            if label not in best_by_label or score > best_by_label[label]:
                best_by_label[label] = score

        return sorted(best_by_label.items(), key=lambda item: item[1], reverse=True)

    # --------------------------------------------------

    def to_objects_string(self, image, lang=None, threshold=None, max_objects=None,
                           excluded_objects=None):
        """
        Méthode principale pour la détection d'objets, sur le même
        principe que ``to_string`` : détecte les objets présents dans
        ``image`` (chemin de fichier OU image PIL déjà chargée) et
        retourne leurs libellés (sans les scores), traduits dans
        ``lang`` (par défaut : ``self.lang``), sous forme de chaîne
        séparée par des virgules (ex : "chat, arbre, bâtiment").

        Une chaîne vide est renvoyée si aucun objet n'atteint le seuil
        de confiance demandé (ou s'ils ont tous été écartés par
        ``excluded_objects``). Voir :meth:`describe_objects` pour obtenir
        aussi la langue du texte et les signalements.
        """
        return self.describe_objects(
            image, lang=lang, threshold=threshold, max_objects=max_objects,
            excluded_objects=excluded_objects).text

    def describe_objects(self, image, lang=None, threshold=None, max_objects=None,
                          excluded_objects=None):
        """Comme :meth:`to_objects_string`, mais renvoie un
        :class:`DetectionText` (texte, langue du texte, signalements). Avec
        ``external_helper``, le serveur traduit les libellés (``lang``
        envoyé avec la requête) ; les exclusions et la troncature
        s'appliquent, elles, aux libellés anglais bruts."""
        lang = lang or self.lang
        threshold = self.objects_threshold if threshold is None else threshold
        max_objects = self.max_objects if max_objects is None else max_objects
        excluded = (
            self.excluded_objects if excluded_objects is None
            else self._to_english_words(excluded_objects)
        )
        detections, payload = self._objects_candidates(
            image, threshold, max_objects, excluded, lang=lang)
        english = [label for label, _score in detections]
        return self._from_payload(
            ", ".join(english), payload, lang,
            lambda t: ", ".join(t.get("labels", {}).get(l, l) for l in english))
