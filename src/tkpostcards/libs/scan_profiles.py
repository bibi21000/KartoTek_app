# -*- encoding: utf-8 -*-
"""
tkpostcards/libs/scan_profiles.py
----------------------------------

Regroupe tous les paramètres qui contrôlent la correction d'un scan brut
lors de l'étape "prepare" (dérotation/rognage, voir
``libpostcards.scan_corrector.ScanCorrector``) et la transparence de son
fond (voir ``tkpostcards.libs.transparency.TiffBackgroundRemover``) dans
un seul objet nommé et sauvegardable : un "profil d'import" (classe
``ScanProfile``).

Pourquoi
--------
Les valeurs qui conviennent aux cartes postales anciennes (CPA -- fond
légèrement crème/gris, bord non dentelé) ne conviennent en général pas
aux cartes semi-modernes (fond blanc pur, bord dentelé/perforé) : la
détection ligne/colonne de ``ScanCorrector.crop_borders()`` ainsi que le
flood-fill de ``TiffBackgroundRemover`` ont besoin d'un réglage différent
selon le papier.

Plutôt qu'un unique réglage ``white_threshold`` partagé par tous les
types de cartes, tkimport/tktools choisissent désormais un *profil* (par
son nom) avant de préparer/re-détourer les scans. Ces profils sont
fournis par défaut (voir ``BUILTIN_PROFILES``) :

- ``cpa`` : réglages historiques, adaptés aux cartes anciennes.
- ``semim`` : point de départ pour les cartes semi-modernes (fond blanc,
  bord dentelé), voir sa définition ci-dessous pour le raisonnement
  derrière chaque valeur.
- ``jagg`` : variante expérimentale de "semim" pour le rognage d'un bord
  dentelé à faible contraste, via détection d'enveloppe quadrilatère sur
  une copie réduite plutôt que suivi de contour par gradient -- voir sa
  définition ci-dessous.
- ``modern`` : point de départ pour les cartes modernes (fond blanc,
  bord droit non dentelé), voir sa définition ci-dessous.
- ``null`` : n'applique AUCUN traitement (ni rotation, ni détection des
  bords, ni transparence) -- le fichier source est simplement recopié
  tel quel. Pensé pour importer une carte déjà travaillée à la main
  (typiquement sous GIMP), qu'il ne faut surtout pas retoucher une
  deuxième fois.
- ``draft`` : redresse la photo et la rogne grossièrement (marges très
  généreuses, pas de transparence) pour préparer un traitement manuel
  ultérieur -- mieux vaut laisser trop de données que d'en supprimer
  trop.

Chaque profil porte aussi une courte ``description`` (affichée dans
l'IHM "Profils d'import" de tkimport) qui résume à qui/quoi il est
destiné.

Ce ne sont que des valeurs de départ, à affiner depuis la fenêtre
"Profils d'import" de tkimport une fois testées sur de vrais scans.

Les profils sont de simples ``dataclass`` ; la persistance (lecture et
écriture dans ``postcards.conf``) est assurée par ``load_profiles()`` /
``save_profile()`` / ``delete_profile()`` ci-dessous.
"""
from __future__ import annotations

import copy
import configparser
from dataclasses import dataclass, replace
from typing import Optional

# Préfixe des sections configparser dédiées aux profils : que le profil
# soit "built-in" (cpa, semim, modern) ou entièrement défini par
# l'utilisateur, sa section s'appelle "[import_profile:<nom>]". Modifier
# un profil built-in depuis l'IHM se contente donc d'écrire/mettre à
# jour cette section, exactement comme pour un profil personnalisé :
# "réinitialiser" (voir delete_profile) supprime simplement la section,
# ce qui fait réapparaître les valeurs codées en dur.
SECTION_PREFIX = "import_profile:"

DEFAULT_PROFILE_NAME = "cpa"


@dataclass
class ScanProfile:
    """Un ensemble nommé de paramètres de correction de scan.

    Attributs
    ---------
    name:
        Identifiant du profil, aussi utilisé comme nom affiché (ex :
        "cpa"). Doit être unique parmi tous les profils.
    white_threshold:
        Niveau de gris (0-255) considéré comme "blanc" par
        ``ScanCorrector.crop_borders()`` pour détecter/rogner les
        marges de la carte, et, sauf si
        ``transparency_white_threshold`` le redéfinit, par
        ``TiffBackgroundRemover`` pour détourer le fond.
    white_ratio_threshold:
        Fraction minimale (0-1) de pixels "blancs" pour qu'une
        ligne/colonne soit considérée comme faisant partie de la marge.
        À baisser pour les cartes dont la marge n'est pas parfaitement
        uniforme (ex : un bord dentelé/perforé laisse apparaître un peu
        de fond à travers la carte elle-même, ligne par ligne).
    crop_margin:
        Pixels conservés autour du contenu détecté lors du rognage initial
        (avant détection d'angle/redressement). À augmenter pour les types
        de cartes où la détection automatique a tendance à mordre sur la
        carte elle-même (ex : bord dentelé), par sécurité.
    final_crop_margin:
        Pixels conservés autour du contenu lors du rognage final, une fois
        l'image redressée (supprime les bandes blanches ajoutées par la
        rotation). Distinct de ``crop_margin`` : c'est ce rognage-là qui
        détermine le cadrage réellement livré -- pour un bord dentelé, une
        marge trop faible ici laisse les pointes du dentelé toucher le bord
        du canevas, sans aucune marge de sécurité en cas de léger écart
        d'angle.
    angle_range:
        Plage de recherche +/- en degrés utilisée par les passes de
        secours de la dérotation (voir ``ScanCorrector.detect_angle``)
        pour les cas difficiles.
    transparency_white_threshold:
        Niveau de gris (0-255) utilisé par ``TiffBackgroundRemover``,
        indépendamment de ``white_threshold``, si l'étape de
        transparence a besoin d'une sensibilité différente de celle du
        rognage (ex : un bord dentelé qui doit rester opaque alors que
        le fond autour doit devenir transparent). ``None`` signifie
        "utiliser white_threshold".
    transparency_band:
        Largeur en pixels de la bande, le long de chacun des 4 bords,
        au-delà de laquelle le détourage du fond ne peut plus se
        propager. ``None`` (défaut) = aucune restriction (comportement
        historique).

        Indispensable dès qu'un côté de la carte (typiquement le verso,
        souvent presque entièrement blanc) est de la même couleur que le
        fond du scan : le papier de la carte et le fond peuvent alors
        être statistiquement impossibles à distinguer par la seule
        couleur (même grain de numérisation), et un détourage non borné
        "traverse" le bord de la carte et grignote son intérieur par
        petites taches, au lieu de s'arrêter dessus. Utiliser une valeur
        supérieure à ``final_crop_margin`` (le fond réellement restant
        après rognage ne peut de toute façon pas dépasser cette largeur).
    use_contour_geometry:
        Si ``True``, utilise le contour physique réel de la carte
        (détecté par gradient, indépendamment de la couleur) plutôt que
        les heuristiques classiques basées sur la couleur/ligne-colonne,
        à deux endroits du traitement :
        - le rognage final (``ScanCorrector.crop_borders_by_contour``) :
          garantit une marge de sécurité uniforme autour de la forme
          réelle détectée, là où le rognage ligne/colonne classique
          moyenne sur toute la largeur/hauteur et peut laisser une marge
          quasi nulle (voire rogner dans le contenu) au point le plus
          défavorable d'une carte non rectangulaire ;
        - le détourage du fond (``TiffBackgroundRemover.
          make_border_transparent_by_contour_cv2``) : épouse la forme
          réelle de la carte plutôt qu'un rectangle, et évite les
          bandes de fond non détourées dans les coins d'une carte non
          rectangulaire (coin coupé, forme légèrement trapézoïdale...).

        Plus lent que les méthodes classiques -- mettre à ``False`` si
        cette latence n'est pas souhaitée pour un profil donné ; les
        deux étapes se rabattent de toute façon automatiquement sur leur
        méthode classique respective si le contour détecté n'est pas
        fiable (bord trop peu contrasté, contour non fermé...). Défaut :
        ``False`` (comportement historique).

        Sans effet si ``use_contour_geometry`` vaut ``False``.
    contour_denoise:
        Applique un filtre bilatéral (lissage qui préserve les bords,
        contrairement à un flou gaussien classique) avant la détection
        de contour. Réduit le bruit de grain du papier qui, sinon, peut
        être amplifié par ``contour_clahe`` au point de rendre la
        détection de contour inexploitable. Défaut : ``False``.
    contour_clahe:
        Applique une égalisation d'histogramme adaptative *locale*
        (CLAHE) avant la détection de contour : amplifie les variations
        locales de luminosité, ce qui fait ressortir une légère
        décoloration du bord même sur une carte globalement déjà très
        claire (fond blanc sur blanc), là où un simple étirement de
        contraste global ne changerait presque rien. Amplifie aussi le
        bruit de numérisation -- combiner avec ``contour_denoise`` pour
        l'atténuer. Défaut : ``False``.
    contour_auto_canny:
        Calcule automatiquement les seuils de l'algorithme de Canny à
        partir de la médiane d'intensité de chaque image, plutôt que
        d'utiliser des seuils fixes : utile si l'exposition/l'éclairage
        varie sensiblement d'un scan à l'autre. Défaut : ``False``.
    use_quad_geometry:
        Si ``True`` (prioritaire sur ``use_contour_geometry`` pour le
        rognage final), détecte l'enveloppe quadrilatère approximative
        de la carte sur une copie réduite du scan (~1500px de large),
        par seuil de couleur entre le blanc du fond et le blanc
        (généralement légèrement teinté) du papier -- voir
        ``libpostcards.contour_utils.detect_card_quad`` -- puis rogne
        l'original en PLEINE résolution à la boîte englobante de ce
        quadrilatère (+ marge généreuse). Plus robuste que
        ``use_contour_geometry`` sur un bord dentelé à faible contraste :
        réduire d'abord l'image lisse le bruit de numérisation qui,
        sinon, empêche un simple seuil de couleur de séparer proprement
        carte et fond à pleine résolution. Le détourage fin (qui suit
        le bord réel, y compris dentelé) reste ensuite piloté par
        ``use_contour_geometry``/``contour_*`` comme d'habitude -- seul
        le ROGNAGE change de méthode. Se rabat automatiquement sur le
        rognage classique si aucun quadrilatère plausible n'est trouvé
        (ex : un côté entièrement blanc, carte et fond de couleur
        réellement identique, pas seulement bruitée). Défaut : ``False``.
    skip_processing:
        Si ``True``, ignore complètement la correction du scan : pas de
        rognage, pas de détection/correction d'angle, pas de mise en
        transparence du fond -- le fichier source est recopié tel quel
        (octet pour octet) vers la destination par
        :func:`~.scan_prepare.make_corrector`. Prioritaire sur tous les
        autres champs (qui deviennent alors sans effet). Pensé pour le
        profil ``null`` : importer une carte déjà entièrement travaillée
        à la main (typiquement sous GIMP) sans risquer de la retoucher
        une deuxième fois. Défaut : ``False``.
    skip_transparency:
        Si ``True``, effectue quand même le rognage/redressement mais
        saute l'étape de mise en transparence du fond (le fond reste
        blanc/opaque). Sans effet si ``skip_processing`` vaut ``True``
        (qui saute déjà tout, transparence comprise). Pensé pour le
        profil ``draft`` : la détection de fond peut supprimer des
        pixels à tort, ce qu'on veut éviter tant que le cadrage n'a pas
        été validé/affiné à la main. Défaut : ``False``.
    description:
        Courte description en français, affichée dans l'IHM "Profils
        d'import" de tkimport (et par ``tktools scan profiles``), pour
        rappeler à quel type de carte (ou à quel usage) ce profil est
        destiné. Purement informatif : sans effet sur le traitement.
    builtin:
        ``True`` pour les profils fournis avec tkpostcards (actuellement
        "cpa", "semim", "jagg", "modern", "null" et "draft"). Un profil
        built-in ne peut pas être supprimé -- seulement réinitialisé à
        ses valeurs par défaut -- afin qu'il reste toujours au moins un
        profil disponible.
    """

    name: str
    white_threshold: int = 240
    white_ratio_threshold: float = 0.98
    crop_margin: int = 20
    final_crop_margin: int = 5
    angle_range: float = 20.0
    transparency_white_threshold: Optional[int] = None
    transparency_band: Optional[int] = None
    use_contour_geometry: bool = False
    contour_denoise: bool = False
    contour_clahe: bool = False
    contour_auto_canny: bool = False
    use_quad_geometry: bool = False
    skip_processing: bool = False
    skip_transparency: bool = False
    description: str = ""
    builtin: bool = False

    @property
    def effective_transparency_threshold(self) -> int:
        """Valeur effectivement transmise à ``TiffBackgroundRemover``."""
        return (
            self.white_threshold
            if self.transparency_white_threshold is None
            else self.transparency_white_threshold
        )

    def with_overrides(self, **kwargs) -> "ScanProfile":
        """Renvoie une copie de ce profil avec certains champs redéfinis.

        Les clés dont la valeur vaut ``None`` sont ignorées, pour
        pouvoir directement passer des options CLI/UI optionnelles
        (ex : ``profile.with_overrides(white_threshold=cli_value)``)
        sans avoir à tester leur présence au préalable.
        """
        overrides = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **overrides)

    def to_dict(self) -> dict:
        """Sérialise ce profil pour une section configparser (voir
        ``save_profile``). ``transparency_white_threshold`` n'est écrit
        que s'il diffère de ``white_threshold``, pour rester lisible."""
        d = {
            "white_threshold": str(self.white_threshold),
            "white_ratio_threshold": str(self.white_ratio_threshold),
            "crop_margin": str(self.crop_margin),
            "final_crop_margin": str(self.final_crop_margin),
            "angle_range": str(self.angle_range),
        }
        if self.transparency_white_threshold is not None:
            d["transparency_white_threshold"] = str(self.transparency_white_threshold)
        if self.transparency_band is not None:
            d["transparency_band"] = str(self.transparency_band)
        d["use_contour_geometry"] = str(self.use_contour_geometry)
        d["contour_denoise"] = str(self.contour_denoise)
        d["contour_clahe"] = str(self.contour_clahe)
        d["contour_auto_canny"] = str(self.contour_auto_canny)
        d["use_quad_geometry"] = str(self.use_quad_geometry)
        d["skip_processing"] = str(self.skip_processing)
        d["skip_transparency"] = str(self.skip_transparency)
        if self.description:
            d["description"] = self.description
        return d


# ── Profils fournis par défaut ──────────────────────────────────────────

BUILTIN_PROFILES: dict[str, ScanProfile] = {
    "cpa": ScanProfile(
        name="cpa",
        description="Cartes postales anciennes",
        # Réglages historiques (anciens défauts de ScanCorrector /
        # TiffBackgroundRemover) : un fond légèrement non-blanc (typique
        # des cartes postales anciennes) reste détecté correctement avec
        # un seuil assez élevé, un ratio de blanc strict et une petite
        # marge.
        white_threshold=240,
        white_ratio_threshold=0.98,
        crop_margin=20,
        final_crop_margin=5,
        angle_range=20.0,
        transparency_white_threshold=None,
        transparency_band=None,
        use_contour_geometry=False,
        use_quad_geometry=False,
        builtin=True,
    ),
    "semim": ScanProfile(
        name="semim",
        description="Cartes postales semi-modernes",
        # Point de départ pour les cartes semi-modernes : fond blanc
        # pur (seuil relevé, plus proche de 255, pour continuer à
        # distinguer un papier réellement blanc d'une zone imprimée
        # presque blanche) combiné à un bord dentelé/perforé qui laisse
        # localement apparaître du fond à travers la carte, ligne par
        # ligne/colonne par colonne -- on baisse donc le ratio de blanc
        # pour que ces lignes/colonnes soient quand même reconnues
        # comme marge. crop_margin ET final_crop_margin sont tous deux
        # augmentés (et alignés), et généreux (250/180px) : le rognage
        # final (après redressement) est celui qui détermine le cadrage
        # réellement livré, et une marge insuffisante ici tronque
        # carrément le dentelé avant même que le détourage n'ait la
        # moindre chance de le voir (le rognage supprime des pixels,
        # le détourage se contente de les rendre transparents) --
        # observé concrètement : avec une marge trop courte, le corps
        # du dentelé (les fines crêtes entre les pointes, à faible
        # contraste) disparaît complètement, ne laissant qu'un bord
        # plat.
        #
        # transparency_band borne aussi le détourage du fond à une
        # bande de 220px près des bords : sur un côté (typiquement le
        # verso) presque entièrement blanc, le papier de la carte et le
        # fond du scan peuvent être statistiquement impossibles à
        # distinguer par la seule couleur, et un détourage non borné
        # "traverse" le bord et grignote l'intérieur de la carte par
        # petites taches. 220px = confortablement au-dessus de
        # final_crop_margin (180px), le fond réellement restant après
        # rognage ne peut de toute façon pas dépasser cette largeur.
        # (Plafonné par ailleurs à 5% de la plus petite dimension de
        # l'image traitée, voir MAX_BAND_FRACTION dans transparency.py,
        # pour rester sûr sur un scan réduit.)
        #
        # use_contour_geometry=True : ces cartes semi-modernes sont
        # aussi les plus susceptibles de ne pas être parfaitement
        # rectangulaires (coin coupé, forme légèrement trapézoïdale) --
        # le détourage/rognage par contour épouse alors la forme réelle
        # au lieu de laisser des zones de fond non détourées dans les
        # coins, ou une marge de rognage quasi nulle au point le plus
        # défavorable.
        #
        # contour_denoise=True + contour_clahe=True : le bord dentelé
        # de ces cartes est parfois à très faible contraste (à peine
        # plus foncé que le fond), insuffisant pour Canny seul. CLAHE
        # (égalisation d'histogramme locale) fait ressortir cette
        # légère décoloration, mais amplifie aussi le bruit de grain du
        # papier -- d'où le débruitage (filtre bilatéral, préserve les
        # bords) appliqué avant, pour ne pas amplifier ce bruit en même
        # temps. contour_auto_canny reste à False : pas encore concluant
        # en tests, à réévaluer.
        #
        # À affiner depuis la fenêtre "Profils d'import" une fois testé
        # sur de vrais scans.
        white_threshold=250,
        white_ratio_threshold=0.90,
        crop_margin=250,
        final_crop_margin=180,
        angle_range=20.0,
        transparency_white_threshold=None,
        transparency_band=220,
        use_contour_geometry=True,
        contour_denoise=True,
        contour_clahe=True,
        contour_auto_canny=False,
        use_quad_geometry=False,
        builtin=True,
    ),
    "jagg": ScanProfile(
        name="jagg",
        description="Cartes postales semi-modernes (2ème version)",
        # Variante EXPÉRIMENTALE de "semim" : au lieu de suivre le
        # contour réel par gradient (Canny) pour le rognage -- fragile
        # sur un bord dentelé à faible contraste, voir "semim" -- on
        # détecte d'abord l'enveloppe approximative de la carte
        # (quadrilatère) sur une copie réduite (~1500px) par seuil de
        # couleur, PUIS on rogne l'original en pleine résolution à
        # cette enveloppe + marge généreuse (voir
        # use_quad_geometry/crop_borders_by_quad). Réduire d'abord lisse
        # le bruit de numérisation qui, sinon, empêche un simple seuil
        # de couleur de séparer proprement le blanc du fond de celui
        # (généralement légèrement teinté) du papier à pleine
        # résolution.
        #
        # Le détourage fin (celui qui doit effectivement suivre le
        # dentelé, pas juste l'enveiller) reste ensuite piloté par
        # use_contour_geometry/contour_* comme pour "semim" : seule la
        # méthode de ROGNAGE change. crop_margin/final_crop_margin sont
        # volontairement très généreux (l'enveloppe quadrilatère ne
        # suit pas le détail fin du dentelé, qui doit donc tenir tout
        # entier dans cette marge pour ne pas être tronqué avant même
        # que le détourage fin n'ait pu le voir).
        white_threshold=250,
        white_ratio_threshold=0.90,
        crop_margin=250,
        final_crop_margin=180,
        angle_range=20.0,
        transparency_white_threshold=None,
        transparency_band=220,
        use_contour_geometry=True,
        contour_denoise=True,
        contour_clahe=True,
        contour_auto_canny=False,
        use_quad_geometry=True,
        builtin=True,
    ),
    "modern": ScanProfile(
        name="modern",
        description="Cartes postales modernes",
        # Point de départ pour les cartes modernes : fond blanc pur,
        # comme "semim" (seuil relevé à 250), mais bord droit/non
        # dentelé -- la marge redevient donc aussi nette et régulière
        # que pour "cpa" : ratio de blanc strict et petites marges de
        # rognage (initiale et finale) suffisent, pas besoin des
        # tolérances ajoutées pour le bord dentelé de "semim". À
        # affiner depuis la fenêtre "Profils d'import" une fois testé
        # sur de vrais scans.
        white_threshold=250,
        white_ratio_threshold=0.98,
        crop_margin=20,
        final_crop_margin=5,
        angle_range=20.0,
        transparency_white_threshold=None,
        transparency_band=None,
        use_contour_geometry=False,
        use_quad_geometry=False,
        builtin=True,
    ),
    "null": ScanProfile(
        name="null",
        description="Aucun traitement",
        # Aucun réglage ci-dessous n'a d'effet : skip_processing=True
        # court-circuite tout dans make_corrector() (voir scan_prepare
        # .make_corrector) -- le fichier source est recopié tel quel
        # (octet pour octet), sans rognage, sans détection/correction
        # d'angle et sans mise en transparence du fond. Pensé pour
        # importer une carte déjà entièrement retouchée à la main
        # (typiquement sous GIMP) qu'il ne faut surtout pas traiter une
        # deuxième fois.
        skip_processing=True,
        builtin=True,
    ),
    "draft": ScanProfile(
        name="draft",
        description="Redresse et redimensionne pour un traitement manuel",
        # Étape de dégrossissage avant reprise manuelle : on redresse
        # quand même l'image (angle_range=20.0, comme les autres
        # profils) mais on se contente d'un rognage grossier, ligne/
        # colonne classique (pas de use_contour_geometry/
        # use_quad_geometry : plus rapide, et surtout on ne cherche pas
        # à coller précisément au bord réel de la carte). white_threshold
        # et white_ratio_threshold sont volontairement stricts (250 /
        # 0.98, comme "modern") et crop_margin/final_crop_margin très
        # généreux (300px) : mieux vaut laisser trop de fond blanc
        # autour de la carte que d'en rogner ne serait-ce qu'un peu --
        # la marge de sécurité normalement fine pour un profil définitif
        # devient ici volontairement large, en attendant un rognage
        # précis fait à la main.
        #
        # skip_transparency=True : ne pas détourer le fond -- la
        # détection de fond peut à tort rendre transparents des pixels
        # de la carte elle-même, ce qui supprimerait des données avant
        # même la reprise manuelle.
        white_threshold=250,
        white_ratio_threshold=0.98,
        crop_margin=300,
        final_crop_margin=300,
        angle_range=20.0,
        transparency_white_threshold=None,
        transparency_band=None,
        use_contour_geometry=False,
        use_quad_geometry=False,
        skip_transparency=True,
        builtin=True,
    ),
}


# ── Persistance (postcards.conf) ────────────────────────────────────────

def _profile_from_section(name: str, section: configparser.SectionProxy,
                           builtin: bool = False) -> ScanProfile:
    """Construit un ScanProfile à partir d'une section configparser,
    complétant les clés manquantes avec les valeurs du profil built-in
    de même nom (s'il existe), ou avec les défauts de ScanProfile
    sinon."""
    base = BUILTIN_PROFILES.get(name)
    default = base or ScanProfile(name=name)

    white_threshold = section.getint("white_threshold", fallback=default.white_threshold)
    white_ratio_threshold = section.getfloat(
        "white_ratio_threshold", fallback=default.white_ratio_threshold)
    crop_margin = section.getint("crop_margin", fallback=default.crop_margin)
    final_crop_margin = section.getint("final_crop_margin", fallback=default.final_crop_margin)
    angle_range = section.getfloat("angle_range", fallback=default.angle_range)

    transp_raw = section.get("transparency_white_threshold", fallback="").strip()
    transparency_white_threshold = int(transp_raw) if transp_raw else None

    band_raw = section.get("transparency_band", fallback="").strip()
    transparency_band = int(band_raw) if band_raw else default.transparency_band

    use_contour_geometry = section.getboolean(
        "use_contour_geometry", fallback=default.use_contour_geometry)
    contour_denoise = section.getboolean(
        "contour_denoise", fallback=default.contour_denoise)
    contour_clahe = section.getboolean(
        "contour_clahe", fallback=default.contour_clahe)
    contour_auto_canny = section.getboolean(
        "contour_auto_canny", fallback=default.contour_auto_canny)
    use_quad_geometry = section.getboolean(
        "use_quad_geometry", fallback=default.use_quad_geometry)
    skip_processing = section.getboolean(
        "skip_processing", fallback=default.skip_processing)
    skip_transparency = section.getboolean(
        "skip_transparency", fallback=default.skip_transparency)
    description = section.get("description", fallback=default.description)

    return ScanProfile(
        name=name,
        white_threshold=white_threshold,
        white_ratio_threshold=white_ratio_threshold,
        crop_margin=crop_margin,
        final_crop_margin=final_crop_margin,
        angle_range=angle_range,
        transparency_white_threshold=transparency_white_threshold,
        transparency_band=transparency_band,
        use_contour_geometry=use_contour_geometry,
        contour_denoise=contour_denoise,
        contour_clahe=contour_clahe,
        contour_auto_canny=contour_auto_canny,
        use_quad_geometry=use_quad_geometry,
        skip_processing=skip_processing,
        skip_transparency=skip_transparency,
        description=description,
        builtin=builtin,
    )


def load_profiles(cfg: configparser.ConfigParser) -> "dict[str, ScanProfile]":
    """Renvoie tous les profils disponibles (profils built-in, redéfinis
    ou non par une section de même nom, plus tout profil purement
    personnalisé), indexés par nom, dans un ordre stable : les built-in
    d'abord (dans l'ordre de ``BUILTIN_PROFILES``), puis les profils
    personnalisés par ordre alphabétique.
    """
    profiles: "dict[str, ScanProfile]" = {}

    for name in BUILTIN_PROFILES:
        section_name = SECTION_PREFIX + name
        if cfg.has_section(section_name):
            profiles[name] = _profile_from_section(name, cfg[section_name], builtin=True)
        else:
            profiles[name] = copy.copy(BUILTIN_PROFILES[name])

    custom_names = sorted(
        section[len(SECTION_PREFIX):]
        for section in cfg.sections()
        if section.startswith(SECTION_PREFIX)
        and section[len(SECTION_PREFIX):] not in BUILTIN_PROFILES
    )
    for name in custom_names:
        profiles[name] = _profile_from_section(name, cfg[SECTION_PREFIX + name], builtin=False)

    return profiles


def save_profile(cfg: configparser.ConfigParser, profile: ScanProfile) -> None:
    """Crée ou met à jour la section correspondant à *profile*. Fonctionne
    à l'identique pour un profil built-in (on enregistre alors juste une
    redéfinition de ses valeurs par défaut) ou personnalisé (on
    enregistre sa définition complète).

    N'écrit pas le fichier lui-même : appeler ``save_config()``
    (tkimport) ou équivalent juste après.
    """
    section_name = SECTION_PREFIX + profile.name
    if not cfg.has_section(section_name):
        cfg.add_section(section_name)
    for key, value in profile.to_dict().items():
        cfg.set(section_name, key, value)
    # Ne pas laisser traîner une redéfinition de transparence obsolète
    # si elle a été remise à "None" (= "identique à white_threshold" /
    # "aucune restriction de bande") depuis l'IHM.
    if (profile.transparency_white_threshold is None
            and cfg.has_option(section_name, "transparency_white_threshold")):
        cfg.remove_option(section_name, "transparency_white_threshold")
    if (profile.transparency_band is None
            and cfg.has_option(section_name, "transparency_band")):
        cfg.remove_option(section_name, "transparency_band")


def delete_profile(cfg: configparser.ConfigParser, name: str) -> None:
    """Supprime un profil personnalisé, ou réinitialise un profil
    built-in à ses valeurs par défaut (en supprimant sa section de
    redéfinition, si elle existe). Ne fait rien si le profil n'a pas de
    section (déjà à ses valeurs par défaut / inexistant)."""
    section_name = SECTION_PREFIX + name
    if cfg.has_section(section_name):
        cfg.remove_section(section_name)


def rename_profile(cfg: configparser.ConfigParser, old_name: str, new_name: str) -> None:
    """Renomme un profil personnalisé (interdit pour les built-in, à
    filtrer côté appelant)."""
    old_section = SECTION_PREFIX + old_name
    new_section = SECTION_PREFIX + new_name
    if cfg.has_section(new_section):
        raise ValueError("A profile named %r already exists" % new_name)
    if cfg.has_section(old_section):
        cfg.add_section(new_section)
        for key, value in cfg.items(old_section):
            cfg.set(new_section, key, value)
        cfg.remove_section(old_section)


# ── Profil actif ([tkimport] scan_profile) ──────────────────────────────

def get_active_profile_name(cfg: configparser.ConfigParser, section: str = "tkimport") -> str:
    """Robust to *section* not existing yet (ex: ``tktools`` invoked
    without ever having run ``tkimport``, so ``[tkimport]`` may be
    absent from postcards.conf)."""
    return cfg.get(section, "scan_profile", fallback=DEFAULT_PROFILE_NAME) or DEFAULT_PROFILE_NAME


def set_active_profile_name(cfg: configparser.ConfigParser, name: str,
                             section: str = "tkimport") -> None:
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, "scan_profile", name)


def get_active_profile(cfg: configparser.ConfigParser, section: str = "tkimport") -> ScanProfile:
    """Raccourci : charge tous les profils et renvoie celui actuellement
    sélectionné dans ``[section] scan_profile`` (repli sur
    ``cpa`` si le nom enregistré n'existe plus)."""
    profiles = load_profiles(cfg)
    name = get_active_profile_name(cfg, section=section)
    return profiles.get(name) or profiles[DEFAULT_PROFILE_NAME]
