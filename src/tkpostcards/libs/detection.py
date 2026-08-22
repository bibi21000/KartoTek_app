# -*- encoding: utf-8 -*-
"""
tkpostcards/libs/detection.py - Détection automatique du contenu du recto
des cartes postales.

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

Suit le même principe que tkpostcards.libs.ocr.PostcardOCR : les
dépendances lourdes (torch, transformers, deep_translator) sont
volontairement importées localement, dans les méthodes qui en ont
besoin, et non ici en tête de module. Cela permet d'importer
tkpostcards.libs.detection (ex : pour introspection ou tests) sans les
avoir installées ; elles ne sont requises qu'au moment où la détection
est réellement effectuée -- voir l'extra "detection" de pyproject.toml.
"""
import re

from PIL import Image

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

    Dans les deux cas, le résultat (généré en anglais par les modèles)
    est traduit dans la langue voulue.

    Les modèles ne sont chargés qu'à la première utilisation réelle de
    chaque fonctionnalité (voir _ensure_model() / _ensure_objects_model()),
    ce qui permet d'instancier PostcardDetection() sans coût si une
    seule des deux détections est finalement utilisée (ou aucune).
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
        debug=False,
    ):
        self.model_name = model_name
        self.objects_model_name = objects_model_name
        self.lang = lang
        self.objects_threshold = objects_threshold
        self.max_objects = max_objects
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
        # casse dans tous les cas.
        self.excluded_objects = self._to_english_words(excluded_objects or [])

        # device : même logique que PostcardSearcher
        # (libpostcards.similar) : si fourni (ex : [DEFAULT]
        # torch_device de postcards.conf, ou --torch-device en ligne de
        # commande), on l'utilise tel quel ; sinon détection
        # automatique (cuda si disponible, sinon cpu). Si torch n'est
        # pas installé, on retombe sur "cpu" sans lever d'erreur, pour
        # permettre d'instancier PostcardDetection() sans avoir torch
        # installé (l'erreur explicite n'intervient qu'à l'utilisation
        # réelle, dans _ensure_model()).
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

    def caption(self, image):
        """
        Génère une légende (en anglais, langue native de BLIP)
        décrivant le contenu de ``image`` (chemin de fichier ou
        ``PIL.Image`` déjà chargée), sans traduction.

        Les mots-artefacts connus de BLIP (ex : "arafed", voir
        _BLIP_ARTIFACT_WORDS) sont retirés automatiquement du résultat.
        """
        self._ensure_model()

        import torch

        img = self._load_image(image)
        inputs = self.processor(img, return_tensors="pt").to(self.device)

        with torch.no_grad():
            out = self.model.generate(**inputs)

        raw = self.processor.decode(out[0], skip_special_tokens=True)
        return _strip_blip_artifacts(raw)

    # --------------------------------------------------

    def translate(self, text, lang=None):
        """
        Traduit ``text`` (typiquement la légende anglaise renvoyée par
        caption()) vers ``lang`` (par défaut : ``self.lang``, ex :
        "fr").

        Retourne ``text`` tel quel si la langue cible est l'anglais,
        si ``text`` est vide, si ``deep_translator`` n'est pas
        installé, ou si la traduction échoue (ex : hors ligne) : la
        légende anglaise reste toujours disponible, la traduction est
        une amélioration, pas un prérequis.
        """
        lang = lang or self.lang

        if not text or not lang or lang.lower().startswith("en"):
            return text

        if not TRANSLATOR_AVAILABLE:
            if self.debug:
                print("deep_translator is not installed, skipping translation")
            return text

        try:
            return GoogleTranslator(source="en", target=lang).translate(text)
        except Exception as e:
            if self.debug:
                print(f"Translation failed ({e}), keeping original caption")
            return text

    # --------------------------------------------------

    def to_string(self, image, lang=None):
        """
        Méthode principale, sur le même principe que
        ``PostcardOCR.to_string`` : détecte le contenu de ``image``
        (chemin de fichier OU image PIL déjà chargée) et retourne la
        légende traduite dans ``lang`` (par défaut : ``self.lang``).
        """
        return self.translate(self.caption(image), lang=lang)

    # --------------------------------------------------

    def detect_objects(self, image, threshold=None, max_objects=None, excluded_objects=None):
        """
        Détecte les objets présents dans ``image`` (chemin de fichier
        ou ``PIL.Image`` déjà chargée) via le modèle DETR, sans
        traduction.

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
        self._ensure_objects_model()

        threshold = self.objects_threshold if threshold is None else threshold
        max_objects = self.max_objects if max_objects is None else max_objects
        excluded = (
            self.excluded_objects if excluded_objects is None
            else self._to_english_words(excluded_objects)
        )

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
            if label.lower() in excluded:
                continue
            score = score.item()
            if label not in best_by_label or score > best_by_label[label]:
                best_by_label[label] = score

        detections = sorted(
            best_by_label.items(), key=lambda item: item[1], reverse=True
        )

        if max_objects is not None:
            detections = detections[:max_objects]

        return detections

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
        ``excluded_objects``).
        """
        detections = self.detect_objects(
            image, threshold=threshold, max_objects=max_objects,
            excluded_objects=excluded_objects,
        )
        labels = ", ".join(label for label, _score in detections)
        return self.translate(labels, lang=lang)
