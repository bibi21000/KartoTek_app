# -*- encoding: utf-8 -*-
"""
tkpostcards/libs/scan_profiles.py
----------------------------------

Regroupe tous les paramètres qui contrôlent la correction d'un scan brut
lors de l'étape "prepare" (dérotation/rognage, voir
``libpostcards.scan_corrector.ScanCorrector``) et la transparence de son
fond (voir ``tkpostcards.libs.transparency.TiffBackgroundRemover``) dans
un seul objet nommé et sauvegardable : un "modèle d'import" (aussi
appelé "profil" dans le code).

Pourquoi
--------
Les valeurs qui conviennent aux cartes postales anciennes (CPA -- fond
légèrement crème/gris, bord non dentelé) ne conviennent en général pas
aux cartes semi-modernes (fond blanc pur, bord dentelé/perforé) : la
détection ligne/colonne de ``ScanCorrector.crop_borders()`` ainsi que le
flood-fill de ``TiffBackgroundRemover`` ont besoin d'un réglage différent
selon le papier.

Plutôt qu'un unique réglage ``white_threshold`` partagé par tous les
types de cartes, tkimport/tktools choisissent désormais un *modèle* (par
son nom) avant de préparer/re-détourer les scans. Trois modèles sont
fournis par défaut (voir ``BUILTIN_PROFILES``) :

- ``cpa`` : réglages historiques, adaptés aux cartes anciennes.
- ``semim`` : point de départ pour les cartes semi-modernes (fond blanc,
  bord dentelé), voir sa définition ci-dessous pour le raisonnement
  derrière chaque valeur.
- ``modern`` : point de départ pour les cartes modernes (fond blanc,
  bord droit non dentelé), voir sa définition ci-dessous.

Ce ne sont que des valeurs de départ, à affiner depuis la fenêtre
"Modèles d'import" de tkimport une fois testées sur de vrais scans.

Les modèles sont de simples ``dataclass`` ; la persistance (lecture et
écriture dans ``postcards.conf``) est assurée par ``load_profiles()`` /
``save_profile()`` / ``delete_profile()`` ci-dessous.
"""
from __future__ import annotations

import copy
import configparser
from dataclasses import dataclass, replace
from typing import Optional

# Préfixe des sections configparser dédiées aux modèles : que le modèle
# soit "built-in" (cpa, semim, modern) ou entièrement défini par
# l'utilisateur, sa section s'appelle "[import_profile:<nom>]". Modifier
# un modèle built-in depuis l'IHM se contente donc d'écrire/mettre à
# jour cette section, exactement comme pour un modèle personnalisé :
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
        Identifiant du modèle, aussi utilisé comme nom affiché (ex :
        "cpa"). Doit être unique parmi tous les modèles.
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
        Pixels conservés autour du contenu détecté lors du rognage. À
        augmenter pour les types de cartes où la détection automatique
        a tendance à mordre sur la carte elle-même (ex : bord dentelé),
        par sécurité.
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
    builtin:
        ``True`` pour les modèles fournis avec tkpostcards (actuellement
        "cpa", "semim" et "modern"). Un modèle built-in ne peut pas
        être supprimé -- seulement réinitialisé à ses valeurs par
        défaut -- afin qu'il reste toujours au moins un modèle
        disponible.
    """

    name: str
    white_threshold: int = 240
    white_ratio_threshold: float = 0.98
    crop_margin: int = 20
    angle_range: float = 10.0
    transparency_white_threshold: Optional[int] = None
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
        """Renvoie une copie de ce modèle avec certains champs redéfinis.

        Les clés dont la valeur vaut ``None`` sont ignorées, pour
        pouvoir directement passer des options CLI/UI optionnelles
        (ex : ``profile.with_overrides(white_threshold=cli_value)``)
        sans avoir à tester leur présence au préalable.
        """
        overrides = {k: v for k, v in kwargs.items() if v is not None}
        return replace(self, **overrides)

    def to_dict(self) -> dict:
        """Sérialise ce modèle pour une section configparser (voir
        ``save_profile``). ``transparency_white_threshold`` n'est écrit
        que s'il diffère de ``white_threshold``, pour rester lisible."""
        d = {
            "white_threshold": str(self.white_threshold),
            "white_ratio_threshold": str(self.white_ratio_threshold),
            "crop_margin": str(self.crop_margin),
            "angle_range": str(self.angle_range),
        }
        if self.transparency_white_threshold is not None:
            d["transparency_white_threshold"] = str(self.transparency_white_threshold)
        return d


# ── Modèles fournis par défaut ──────────────────────────────────────────

BUILTIN_PROFILES: dict[str, ScanProfile] = {
    "cpa": ScanProfile(
        name="cpa",
        # Réglages historiques (anciens défauts de ScanCorrector /
        # TiffBackgroundRemover) : un fond légèrement non-blanc (typique
        # des cartes postales anciennes) reste détecté correctement avec
        # un seuil assez élevé, un ratio de blanc strict et une petite
        # marge.
        white_threshold=240,
        white_ratio_threshold=0.98,
        crop_margin=20,
        angle_range=10.0,
        transparency_white_threshold=None,
        builtin=True,
    ),
    "semim": ScanProfile(
        name="semim",
        # Point de départ pour les cartes semi-modernes : fond blanc
        # pur (seuil relevé, plus proche de 255, pour continuer à
        # distinguer un papier réellement blanc d'une zone imprimée
        # presque blanche) combiné à un bord dentelé/perforé qui laisse
        # localement apparaître du fond à travers la carte, ligne par
        # ligne/colonne par colonne -- on baisse donc le ratio de blanc
        # pour que ces lignes/colonnes soient quand même reconnues
        # comme marge, et on augmente la marge de rognage pour que la
        # détection ne morde pas sur le bord dentelé lui-même. À affiner
        # depuis la fenêtre "Modèles d'import" une fois testé sur de
        # vrais scans.
        white_threshold=250,
        white_ratio_threshold=0.90,
        crop_margin=40,
        angle_range=10.0,
        transparency_white_threshold=None,
        builtin=True,
    ),
    "modern": ScanProfile(
        name="modern",
        # Point de départ pour les cartes modernes : fond blanc pur,
        # comme "semim" (seuil relevé à 250), mais bord droit/non
        # dentelé -- la marge redevient donc aussi nette et régulière
        # que pour "cpa" : ratio de blanc strict et petite marge de
        # rognage suffisent, pas besoin des tolérances ajoutées pour le
        # bord dentelé de "semim". À affiner depuis la fenêtre "Modèles
        # d'import" une fois testé sur de vrais scans.
        white_threshold=250,
        white_ratio_threshold=0.98,
        crop_margin=20,
        angle_range=10.0,
        transparency_white_threshold=None,
        builtin=True,
    ),
}


# ── Persistance (postcards.conf) ────────────────────────────────────────

def _profile_from_section(name: str, section: configparser.SectionProxy,
                           builtin: bool = False) -> ScanProfile:
    """Construit un ScanProfile à partir d'une section configparser,
    complétant les clés manquantes avec les valeurs du modèle built-in
    de même nom (s'il existe), ou avec les défauts de ScanProfile
    sinon."""
    base = BUILTIN_PROFILES.get(name)
    default = base or ScanProfile(name=name)

    white_threshold = section.getint("white_threshold", fallback=default.white_threshold)
    white_ratio_threshold = section.getfloat(
        "white_ratio_threshold", fallback=default.white_ratio_threshold)
    crop_margin = section.getint("crop_margin", fallback=default.crop_margin)
    angle_range = section.getfloat("angle_range", fallback=default.angle_range)

    transp_raw = section.get("transparency_white_threshold", fallback="").strip()
    transparency_white_threshold = int(transp_raw) if transp_raw else None

    return ScanProfile(
        name=name,
        white_threshold=white_threshold,
        white_ratio_threshold=white_ratio_threshold,
        crop_margin=crop_margin,
        angle_range=angle_range,
        transparency_white_threshold=transparency_white_threshold,
        builtin=builtin,
    )


def load_profiles(cfg: configparser.ConfigParser) -> "dict[str, ScanProfile]":
    """Renvoie tous les modèles disponibles (modèles built-in, redéfinis
    ou non par une section de même nom, plus tout modèle purement
    personnalisé), indexés par nom, dans un ordre stable : les built-in
    d'abord (dans l'ordre de ``BUILTIN_PROFILES``), puis les modèles
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
    à l'identique pour un modèle built-in (on enregistre alors juste une
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
    # si elle a été remise à "None" (= "identique à white_threshold")
    # depuis l'IHM.
    if (profile.transparency_white_threshold is None
            and cfg.has_option(section_name, "transparency_white_threshold")):
        cfg.remove_option(section_name, "transparency_white_threshold")


def delete_profile(cfg: configparser.ConfigParser, name: str) -> None:
    """Supprime un modèle personnalisé, ou réinitialise un modèle
    built-in à ses valeurs par défaut (en supprimant sa section de
    redéfinition, si elle existe). Ne fait rien si le modèle n'a pas de
    section (déjà à ses valeurs par défaut / inexistant)."""
    section_name = SECTION_PREFIX + name
    if cfg.has_section(section_name):
        cfg.remove_section(section_name)


def rename_profile(cfg: configparser.ConfigParser, old_name: str, new_name: str) -> None:
    """Renomme un modèle personnalisé (interdit pour les built-in, à
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


# ── Modèle actif ([tkimport] scan_profile) ──────────────────────────────

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
    """Raccourci : charge tous les modèles et renvoie celui actuellement
    sélectionné dans ``[section] scan_profile`` (repli sur
    ``cpa`` si le nom enregistré n'existe plus)."""
    profiles = load_profiles(cfg)
    name = get_active_profile_name(cfg, section=section)
    return profiles.get(name) or profiles[DEFAULT_PROFILE_NAME]
