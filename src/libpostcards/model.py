"""
libpostcards/model.py - Accès centralisé aux données : JSON, SQLite

Gère les cartes postales (cards) et les trajets (travels).
Pas de dépendance externe hormis la bibliothèque standard.

Recommandation pour votre script de publication :

bash# Bon : écrire la nouvelle base à côté, puis remplacement atomique
cp nouvelle_base.sqlite datadir/postcards.sqlite.tmp
mv datadir/postcards.sqlite.tmp datadir/postcards.sqlite

# À éviter : écraser directement le fichier en place
cp nouvelle_base.sqlite datadir/postcards.sqlite

mv / os.replace (remplacement atomique du fichier, change l'inode) → fonctionne de manière fiable avec ce mécanisme, même si gunicorn a une connexion active en cours.

cp en place (écrasement du contenu d'un fichier déjà ouvert par une connexion WAL active) → reste risqué indépendamment de mon code, car SQLite en mode WAL associe son fichier -shm à l'état du fichier au moment de l'ouverture ; écraser le contenu en place pendant qu'une connexion le tient ouvert peut produire des lectures incohérentes, peu importe la détection de changement côté applicatif.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import secrets
import sqlite3
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth hashing (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------
# Stored format: "pbkdf2$<iterations>$<hex-salt>$<hex-digest>"
# An empty password always fails verification.

_HASH_ALGO       = "sha256"
_HASH_ITERATIONS = 260_000   # OWASP 2023 recommendation for PBKDF2-SHA256
_SALT_BYTES      = 32


def _hash_password(password: str) -> str:
    """Return a salted PBKDF2 hash of *password* suitable for storage."""
    salt   = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, _HASH_ITERATIONS
    )
    return f"pbkdf2${_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Return True if *password* matches the stored PBKDF2 hash.

    Returns False immediately if *password* is empty, if *stored* is
    None/empty, or if the format is unrecognised.
    """
    if not password or not stored:
        return False
    try:
        scheme, iterations_s, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if scheme != "pbkdf2":
        return False
    try:
        iterations = int(iterations_s)
        salt       = bytes.fromhex(salt_hex)
        expected   = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        _HASH_ALGO, password.encode("utf-8"), salt, iterations
    )
    return secrets.compare_digest(candidate, expected)

def _strip_accents(value: str | None) -> str | None:
    """Remove diacritics (accents) from a string and lowercase it.

    Used both to normalize values stored in SQLite (via a custom SQL
    function) and to normalize search terms, so that searches are
    accent-insensitive: "dodanes", "dôdanes" and "dodânes" all match
    each other.
    """
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        ch for ch in normalized if not unicodedata.combining(ch)
    )
    return without_accents.lower()

# ---------------------------------------------------------------------------
# Schéma SQL
# ---------------------------------------------------------------------------

_DDL_CARDS = """
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    title2      TEXT,
    description TEXT,
    recto_ocr   TEXT,
    verso_ocr   TEXT,
    detected_content TEXT,
    detected_objects TEXT,
    -- Langue du texte stocké dans detected_content / detected_objects
    -- ("fr", "en" = non traduit...) ; NULL = inconnue (cartes antérieures),
    -- "manual" = modifié à la main (jamais retraduit automatiquement).
    detected_content_lang TEXT,
    detected_objects_lang TEXT,
    date        TEXT,
    cdate       INTEGER,
    mdate       INTEGER,
    address     TEXT,       -- JSON array sérialisé
    recto_text  TEXT,
    verso_text  TEXT,
    coord_lat   REAL,
    coord_lon   REAL,
    poi         TEXT,       -- JSON array sérialisé
    collections TEXT,       -- JSON array sérialisé
    doubles     TEXT,       -- JSON array sérialisé
    links       TEXT,       -- JSON array sérialisé d'identifiants de liens (table links)
    status      TEXT NOT NULL DEFAULT 'active', -- active | exchanged | trade
    storage     TEXT NOT NULL DEFAULT ''        -- lieu de stockage physique ('' = non renseigné)
);
"""

# Valeurs autorisées pour le champ ``status`` d'une carte.
CARD_STATUSES = ("active", "exchanged", "trade")
DEFAULT_CARD_STATUS = "active"

_DDL_TRAVELS = """
-- Résultat du calcul de parcours (ParcoursCartes) d'une collection dont
-- le champ "travel" est vrai dans collections.json. ``id`` est l'id de la
-- collection ; titre, description et ordre d'affichage viennent de la
-- table collections (jointure, voir Model.list_travels).
CREATE TABLE IF NOT EXISTS travels (
    id          TEXT PRIMARY KEY,
    distance_m  INTEGER,
    distance_km REAL,
    start_lat   REAL,
    start_lon   REAL,
    end_lat     REAL,
    end_lon     REAL,
    count       INTEGER,
    cards       TEXT,       -- JSON array sérialisé [{id, title}, ...]
    mdate       INTEGER     -- timestamp UNIX de dernière modification de "cards"
);
"""

_DDL_POIS = """
CREATE TABLE IF NOT EXISTS pois (
    id          TEXT PRIMARY KEY,
    description TEXT,
    coord_lat   REAL,
    coord_lon   REAL,
    links       TEXT,       -- JSON array sérialisé d'identifiants de liens (table links)
    mdate       INTEGER     -- timestamp UNIX de dernière modification (description/coord/liens)
);
"""

_DDL_LINKS = """
CREATE TABLE IF NOT EXISTS links (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    description TEXT,
    url         TEXT NOT NULL,
    check_date  TEXT,       -- date ISO (AAAA-MM-JJ) de la dernière vérification du lien
    mdate       INTEGER     -- timestamp UNIX de dernière modification (titre/description/url)
);
"""

_DDL_COLLECTIONS = """
CREATE TABLE IF NOT EXISTS collections (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    description TEXT,
    published   INTEGER NOT NULL DEFAULT 0,   -- 1 = visible sur flpostcards
    links       TEXT,                         -- JSON array sérialisé d'identifiants de liens (table links)
    sort_order  INTEGER NOT NULL DEFAULT 0,   -- champ "order" : ordre d'affichage des collections
    on_map      INTEGER NOT NULL DEFAULT 1,   -- champ "map" : 1 = proposée sur la carte /map/ (collections_map)
    travel      INTEGER NOT NULL DEFAULT 0,   -- champ "travel" : 1 = un travel (parcours) est défini
    start_lat   REAL,                         -- champ "start" : point de départ du travel
    start_lon   REAL
);
"""

_DDL_AUTHS = """
CREATE TABLE IF NOT EXISTS auths (
    email   TEXT PRIMARY KEY,
    auth    TEXT
);
"""

# La table refresh_tokens ne vit plus dans postcards.sqlite (cf.
# _DDL_REFRESH_TOKENS_DB plus bas) : cette base est intégralement
# écrasée à chaque publication/synchronisation (copie depuis le poste
# de travail vers le serveur, voir PostcardPublish.publish), ce qui
# invaliderait toutes les sessions (déconnexion de tous les
# utilisateurs) à chaque déploiement. Elle réside donc dans un fichier
# sqlite séparé, propre au serveur flpostcards, jamais écrasé par une
# synchronisation. `auths`, à l'inverse, reste ici : géré depuis
# l'outil de bureau (AuthManagerView) et destiné à être publié.

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cards_cdate ON cards (cdate);
CREATE INDEX IF NOT EXISTS idx_cards_mdate ON cards (mdate);
"""

# ---------------------------------------------------------------------------
# Base sqlite séparée pour refresh_tokens (cf. commentaire ci-dessus) :
# fichier propre à flpostcards, distinct de postcards.sqlite, jamais
# touché par generate()/sync() ni par la publication.
# ---------------------------------------------------------------------------

_DDL_REFRESH_TOKENS_DB = """
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,   -- sha256 du refresh token, jamais le token en clair
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    revoked_at    INTEGER,
    device_info   TEXT                    -- optionnel : user-agent / identifiant appareil, pour affichage "sessions actives"
);
CREATE INDEX IF NOT EXISTS idx_refresh_tokens_email ON refresh_tokens(email);
"""

# Valeurs par défaut pour une carte vide
_CARD_DEFAULTS: dict[str, Any] = {
    "id": None,
    "title": None,
    "title2": None,
    "description": None,
    "recto_ocr": None,
    "verso_ocr": None,
    "detected_content": None,
    "detected_objects": None,
    "detected_content_lang": None,
    "detected_objects_lang": None,
    "date": None,
    "cdate": None,
    "mdate": None,
    "address": [],
    "recto_text": None,
    "verso_text": None,
    "coord": None,
    "poi": [],
    "collections": [],
    "doubles": [],
    "links": [],
    "status": DEFAULT_CARD_STATUS,
    "storage": "",
}


# ---------------------------------------------------------------------------
# Helpers de (dé)sérialisation
# ---------------------------------------------------------------------------

def _clean_storage(value: Any) -> str:
    """Normalise le lieu de stockage physique d'une carte : texte sans
    espaces superflus (début/fin, répétés), ``""`` si non renseigné."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _clean_id_list(values: Any) -> list[str]:
    """Normalise une liste d'identifiants (ex : ``links`` d'une carte) :
    chaînes sans espaces superflus, sans vides ni doublons, ordre conservé."""
    result: list[str] = []
    seen: set[str] = set()
    for value in (values or []):
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _card_to_row(card: dict) -> dict:
    """Convertit un dict carte en ligne SQL (champs plats)."""
    coord = card.get("coord") or []
    return {
        "id": str(card["id"]),
        "title": card.get("title"),
        "title2": card.get("title2"),
        "description": card.get("description"),
        "recto_ocr": card.get("recto_ocr"),
        "verso_ocr": card.get("verso_ocr"),
        "detected_content": card.get("detected_content"),
        "detected_objects": card.get("detected_objects"),
        "detected_content_lang": card.get("detected_content_lang"),
        "detected_objects_lang": card.get("detected_objects_lang"),
        "date": card.get("date"),
        "cdate": card.get("cdate"),
        "mdate": card.get("mdate"),
        "address": json.dumps(card.get("address") or [], ensure_ascii=False),
        "recto_text": card.get("recto_text"),
        "verso_text": card.get("verso_text"),
        "coord_lat": coord[0] if len(coord) > 0 else None,
        "coord_lon": coord[1] if len(coord) > 1 else None,
        "poi": json.dumps(card.get("poi") or [], ensure_ascii=False),
        "collections": json.dumps(card.get("collections") or [], ensure_ascii=False),
        "doubles": json.dumps(card.get("doubles") or [], ensure_ascii=False),
        "links": json.dumps(_clean_id_list(card.get("links")), ensure_ascii=False),
        "status": card.get("status") or DEFAULT_CARD_STATUS,
        "storage": _clean_storage(card.get("storage")),
    }


def _row_to_card(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict carte."""
    d = dict(row)
    lat, lon = d.pop("coord_lat", None), d.pop("coord_lon", None)
    d["coord"] = [lat, lon] if (lat is not None and lon is not None) else None
    for field in ("address", "poi", "collections", "doubles", "links"):
        raw = d.get(field)
        d[field] = json.loads(raw) if raw else []
    return d


def _travel_to_row(travel: dict) -> dict:
    """Convertit un dict trajet calculé en ligne SQL (champs plats).
    Titre, description et ordre ne sont pas stockés ici : ils viennent
    de la collection (voir ``Model.list_travels``)."""
    start = travel.get("start") or []
    end = travel.get("end") or []
    return {
        "id": str(travel["id"]),
        "distance_m": travel.get("distance_m"),
        "distance_km": travel.get("distance_km"),
        "start_lat": start[0] if len(start) > 0 else None,
        "start_lon": start[1] if len(start) > 1 else None,
        "end_lat": end[0] if len(end) > 0 else None,
        "end_lon": end[1] if len(end) > 1 else None,
        "count": travel.get("count"),
        "cards": json.dumps(travel.get("cards") or [], ensure_ascii=False),
        "mdate": travel.get("mdate"),
    }


def _row_to_travel(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL (travels JOIN collections) en dict
    trajet : champs calculés + ``title``, ``description`` et ``order``
    de la collection."""
    d = dict(row)
    d["start"] = [d.pop("start_lat"), d.pop("start_lon")]
    d["end"] = [d.pop("end_lat"), d.pop("end_lon")]
    raw_cards = d.get("cards")
    d["cards"] = json.loads(raw_cards) if raw_cards else []
    return d


def _poi_to_row(poi: dict) -> dict:
    """Convertit un dict POI en ligne SQL (champs plats)."""
    coord = poi.get("coord") or []
    return {
        "id": str(poi["id"]),
        "description": poi.get("description"),
        "coord_lat": coord[0] if len(coord) > 0 else None,
        "coord_lon": coord[1] if len(coord) > 1 else None,
        "links": json.dumps(_clean_id_list(poi.get("links")), ensure_ascii=False),
        "mdate": _clean_mdate(poi.get("mdate")),
    }


def _clean_mdate(value: Any) -> int | None:
    """Normalise un timestamp UNIX de modification : ``None`` si vide ou
    invalide."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean_check_date(value: Any) -> str | None:
    """Normalise la date de vérification d'un lien : ``None`` si vide,
    sinon une date ISO ``AAAA-MM-JJ`` (les heures éventuelles sont
    ignorées). Lève ``ValueError`` si la valeur n'est pas une date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValueError(
            f"Date de vérification invalide (attendu AAAA-MM-JJ) : {text!r}"
        ) from None


def _link_to_row(link: dict) -> dict:
    """Convertit un dict lien en ligne SQL (champs plats)."""
    url = str(link.get("url") or "").strip()
    if not url:
        raise ValueError("L'URL d'un lien ne peut pas être vide.")
    title = str(link.get("title") or "").strip() or None
    description = str(link.get("description") or "").strip() or None
    return {
        "id": str(link["id"]).strip(),
        "title": title,
        "description": description,
        "url": url,
        "check_date": _clean_check_date(link.get("check_date")),
        "mdate": _clean_mdate(link.get("mdate")),
    }


def _row_to_link(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict lien."""
    return dict(row)


def _clean_bool(value: Any) -> bool:
    """Normalise un booléen lu dans un JSON ou un formulaire (accepte
    aussi "true"/"false", "1"/"0", "yes"/"no")."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "oui")
    return bool(value)


def _clean_order(value: Any, default: int = 0) -> int:
    """Normalise un numéro d'ordre : entier, ou ``default`` si vide ou
    invalide."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_start(value: Any) -> list[float] | None:
    """Normalise un point de départ ``[lat, lon]`` (flottants), ou
    ``None`` s'il est absent ou invalide."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return [float(value[0]), float(value[1])]
    except (TypeError, ValueError):
        return None


def _clean_collection(collection: dict, default_order: int = 0) -> dict:
    """Normalise une collection ``{id, title, description, published,
    order, map, travel, start, links}`` : ``id`` obligatoire (chaîne non vide),
    ``title`` et ``description`` optionnels (``None`` si vides),
    ``published`` booléen (faux par défaut), ``order`` entier (ordre
    d'affichage, ``default_order`` si absent), ``map`` booléen
    (proposée sur la carte /map/, vrai par défaut), ``travel`` booléen
    (la collection a un parcours défini, faux par défaut), ``start``
    point de départ ``[lat, lon]`` de ce parcours (ou ``None``), ``links`` liste d'identifiants de liens (table ``links``). Lève
    ``ValueError`` si l'id est vide."""
    cid = str(collection.get("id") or "").strip()
    if not cid:
        raise ValueError("L'id d'une collection ne peut pas être vide.")
    title = str(collection.get("title") or "").strip() or None
    description = str(collection.get("description") or "").strip() or None
    return {
        "id": cid,
        "title": title,
        "description": description,
        "published": _clean_bool(collection.get("published", False)),
        "order": _clean_order(collection.get("order"), default_order),
        "map": _clean_bool(collection.get("map", True)),
        "travel": _clean_bool(collection.get("travel", False)),
        "start": _clean_start(collection.get("start")),
        "links": _clean_id_list(collection.get("links")),
    }


def _row_to_collection(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict collection (``published`` en
    booléen, ``links`` en liste, ``sort_order`` exposé comme ``order``)."""
    d = dict(row)
    d["order"] = d.pop("sort_order", 0)
    d["published"] = bool(d.get("published"))
    d["map"] = bool(d.pop("on_map", 1))
    d["travel"] = bool(d.get("travel"))
    lat, lon = d.pop("start_lat", None), d.pop("start_lon", None)
    d["start"] = [lat, lon] if lat is not None and lon is not None else None
    raw = d.get("links")
    d["links"] = json.loads(raw) if raw else []
    return d


# Colonnes parcourues par la recherche textuelle (voir _text_search_condition).
# Les deux dernières : contenu détecté automatiquement (BLIP + DETR sur le
# recto, voir libpostcards.detection), inclus au même titre que les champs
# saisis/OCR'isés manuellement.
_TEXT_SEARCH_COLUMNS = (
    "title", "title2", "description", "verso_text", "recto_text",
    "address", "poi", "detected_content", "detected_objects",
)


# Version mémorisée de _strip_accents pour la recherche textuelle : la
# normalisation (NFKD) domine le coût d'une recherche, or les mêmes champs
# sont relus à chaque frappe ("lou", "louh", "louha"...). Le cache est indexé
# par la valeur du champ, donc jamais périmé quand une carte est modifiée.
_fold_cached = functools.lru_cache(maxsize=200_000)(_strip_accents)


def _search_match(words: str, *fields: Any) -> int:
    """Fonction SQL ``search_match(mots, champ1, champ2, ...)``.

    ``words`` : mots recherchés, déjà normalisés (sans accents, minuscules)
    et séparés par ``\\x00``. Renvoie 1 si CHAQUE mot est contenu dans au
    moins un des champs (ET entre les mots, OU entre les champs), 0 sinon.
    Un seul appel Python par carte, quel que soit le nombre de mots.
    """
    folded = [_fold_cached(f if isinstance(f, str) else str(f))
              for f in fields if f]
    for word in words.split("\x00"):
        if not any(word in f for f in folded):
            return 0
    return 1


def _text_search_condition(search: str | None) -> tuple[str, list[str]]:
    """Condition SQL (et ses paramètres) de la recherche textuelle.

    La saisie est découpée en MOTS (séparés par des espaces) : une carte
    correspond si CHAQUE mot se trouve (sans tenir compte des accents ni
    de la casse) dans au moins une des colonnes de ``_TEXT_SEARCH_COLUMNS``
    -- ET entre les mots, OU entre les colonnes. L'ordre des mots et les
    mots intercalés n'ont pas d'importance : "louhans seille" trouve
    "Louhans la Seille" comme "Louhans, le port sur la Seille". Les mots
    peuvent aussi être répartis entre plusieurs champs (ex : "louhans"
    dans le titre, "seille" dans la description).

    Chaque mot est cherché comme sous-chaîne (comme avant pour la saisie
    entière : "seil" trouve "Seille"). Renvoie ``("", [])`` si ``search``
    ne contient aucun mot (vide ou espaces).

    La condition appelle la fonction SQL ``search_match`` (enregistrée par
    ``Model._get_conn``) : un seul appel Python par carte.
    """
    words = [_strip_accents(w) for w in (search or "").split()]
    words = [w for w in words if w]
    if not words:
        return "", []
    sql = "search_match(?, %s) = 1" % ", ".join(_TEXT_SEARCH_COLUMNS)
    return sql, ["\x00".join(words)]


def _status_condition(
    status: str | None,
    exclude_status: str | list[str] | None,
) -> tuple[str | None, list[Any]]:
    """
    Construit la condition SQL de filtrage sur ``cards.status``.

    - ``status`` (prioritaire) : ne garde que les cartes ayant exactement
      ce statut (ex : "trade" ou "exchanged" pour les filtres galerie).
    - ``exclude_status`` : sinon, exclut le(s) statut(s) donné(s) (ex :
      "exchanged" par défaut dans les listes flpostcards). Accepte une
      chaîne unique ou une liste.

    Retourne ``(None, [])`` si aucun filtre n'est demandé.
    """
    if status:
        return "cards.status = ?", [status]
    if exclude_status:
        values = [exclude_status] if isinstance(exclude_status, str) else list(exclude_status)
        if not values:
            return None, []
        placeholders = ", ".join("?" for _ in values)
        return f"cards.status NOT IN ({placeholders})", list(values)
    return None, []


def _row_to_poi(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict POI."""
    d = dict(row)
    lat, lon = d.pop("coord_lat", None), d.pop("coord_lon", None)
    d["coord"] = [lat, lon] if (lat is not None and lon is not None) else None
    raw = d.get("links")
    d["links"] = json.loads(raw) if raw else []
    return d


# ---------------------------------------------------------------------------
# Classe Model
# ---------------------------------------------------------------------------

class Model:
    """
    Accès centralisé aux données cartes postales.

    Paramètres
    ----------
    datadir : str | Path
        Répertoire racine des données (contient cards/ et postcards.sqlite).
    """

    def __init__(self, datadir: str | Path = "data") -> None:
        self.datadir = Path(datadir)
        self.cards_dir = self.datadir / "cards"
        self.db_path = self.datadir / "postcards.sqlite"
        self.pois_json     = self.datadir / "pois.json"
        self.links_json    = self.datadir / "links.json"
        self.collections_json = self.datadir / "collections.json"
        self.updates_json  = self.datadir / "updates.json"
        # Ancien fichier des parcours : fusionné dans collections.json
        # (champs travel/start) puis renommé, voir _migrate_travels_json().
        self.travels_json  = self.datadir / "travels.json"
        self._conn: sqlite3.Connection | None = None
        # Signature (mtime, inode) du fichier sqlite au moment de
        # l'ouverture de la connexion ; permet de détecter un
        # remplacement du fichier (publication d'une nouvelle base)
        # et de rouvrir automatiquement la connexion, sans nécessiter
        # de redémarrer le processus (utile avec gunicorn).
        self._db_signature: tuple[float, int] | None = None

        # Base séparée pour refresh_tokens (cf. _DDL_REFRESH_TOKENS_DB) :
        # propre à flpostcards, jamais écrasée par generate()/sync() ni
        # par une publication. Ouverte paresseusement (uniquement par les
        # méthodes create_refresh_token/verify_refresh_token/...), pour
        # que les autres usages de Model (outil de bureau, scripts) qui
        # ne s'en servent jamais n'aient pas à la créer.
        self.refresh_tokens_db_path = self.datadir / "refresh_tokens.sqlite"
        self._refresh_conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connexion SQLite
    # ------------------------------------------------------------------

    def _current_db_signature(self) -> tuple[float, int] | None:
        """(mtime, inode) du fichier sqlite sur disque, ou None s'il est absent."""
        try:
            stat = self.db_path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_ino)

    def _get_conn(self) -> sqlite3.Connection:
        """
        Retourne (et ouvre si nécessaire) la connexion SQLite.

        Si le fichier sqlite a été remplacé depuis la dernière ouverture
        (mtime ou inode différent, par exemple après publication d'une
        nouvelle base de données), la connexion existante est fermée et
        une nouvelle est ouverte automatiquement.
        """
        current_signature = self._current_db_signature()

        if self._conn is not None and current_signature != self._db_signature:
            logger.info(
                "Changement détecté sur %s, réouverture de la connexion",
                self.db_path,
            )
            self.close()
            # En mode WAL, des fichiers -wal/-shm résiduels de l'ancienne
            # base peuvent subsister si seul le fichier .sqlite principal
            # a été remplacé (ex: publication via cp/mv). S'ils ne sont
            # pas supprimés, la nouvelle connexion risque de lire des
            # pages obsolètes issues de l'ancienne base.
            for suffix in ("-wal", "-shm"):
                stale_path = Path(str(self.db_path) + suffix)
                if stale_path.exists():
                    try:
                        stale_path.unlink()
                    except OSError:
                        logger.warning(
                            "Impossible de supprimer le fichier résiduel %s",
                            stale_path,
                        )

        if self._conn is None:
            self._conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._conn.row_factory = sqlite3.Row
            # Performance : WAL mode + foreign keys
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._db_signature = self._current_db_signature()

            # Garantit la présence de auths/collections même sur une base
            # existante générée avant leur ajout (IF NOT EXISTS : sans
            # effet si déjà présentes). Utile car generate() supprime et
            # recrée tout le fichier sqlite à partir des JSON de cards/ —
            # ces tables n'en font pas partie et seraient sinon perdues à
            # la prochaine régénération sans cette création défensive.
            #
            # Migration de la table collections : une ancienne version
            # (name, position, map_position, ou sans sort_order/on_map/travel)
            # est remplacée par la nouvelle puis repeuplée depuis
            # collections.json (dont l'ancien format est converti, voir
            # sync_collections()).
            legacy_collections = False
            _coll_cols = {
                r[1] for r in self._conn.execute("PRAGMA table_info(collections)")
            }
            if _coll_cols and "travel" not in _coll_cols:
                self._conn.execute("DROP TABLE collections")
                legacy_collections = True
            self._conn.executescript(
                _DDL_AUTHS + _DDL_COLLECTIONS + _DDL_LINKS + _DDL_TRAVELS)
            self._conn.commit()
            if legacy_collections:
                try:
                    self.sync_collections()
                except Exception:
                    logger.exception(
                        "Migration de la table collections : échec de la "
                        "resynchronisation depuis collections.json")

            # Migration défensive, une fois : une base existante générée
            # avant l'introduction de refresh_tokens.sqlite peut encore
            # avoir une table refresh_tokens ici (avec des sessions
            # actives) ; on les transfère vers la nouvelle base dédiée
            # puis on supprime la table d'ici, pour ne pas la laisser
            # traîner (et qu'un futur `generate()` ne perde plus rien
            # qu'il n'est de toute façon plus censé gérer).
            self._migrate_legacy_refresh_tokens()

            # Ajout défensif de travels.mdate sur une base existante créée
            # avant son introduction (CREATE TABLE IF NOT EXISTS, ci-dessus,
            # ne modifie pas une table déjà présente sans cette colonne).
            try:
                self._conn.execute("ALTER TABLE travels ADD COLUMN mdate INTEGER")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # colonne déjà présente

            # Ajout défensif de cards.status sur une base existante créée
            # avant son introduction (CREATE TABLE IF NOT EXISTS, ci-dessus,
            # ne modifie pas une table déjà présente sans cette colonne).
            # Le DEFAULT 'active' est appliqué par SQLite à toutes les
            # lignes existantes lors de l'ajout de la colonne.
            try:
                self._conn.execute(
                    f"ALTER TABLE cards ADD COLUMN status TEXT NOT NULL DEFAULT '{DEFAULT_CARD_STATUS}'"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # colonne déjà présente

            # Ajout défensif de cards.storage (lieu de stockage physique)
            # sur une base existante créée avant son introduction. Le
            # DEFAULT '' est appliqué par SQLite aux lignes existantes ;
            # la valeur réelle est reprise des JSON par sync()/generate().
            try:
                self._conn.execute(
                    "ALTER TABLE cards ADD COLUMN storage TEXT NOT NULL DEFAULT ''"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # colonne déjà présente

            # Ajout défensif de cards.links (liste JSON d'identifiants de
            # liens, voir la table links / links.json) sur une base
            # existante créée avant son introduction. Les lignes
            # existantes restent à NULL (lu comme une liste vide) jusqu'à
            # ce que la carte soit réécrite par write_json/sync().
            try:
                self._conn.execute("ALTER TABLE cards ADD COLUMN links TEXT")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # colonne déjà présente

            # Ajout défensif de pois.links / pois.mdate et links.mdate
            # (liens d'un POI, horodatage de modification utilisé pour le
            # sitemap et IndexNow) sur une base existante créée avant leur
            # introduction. Les lignes existantes restent à NULL (liste
            # vide / jamais modifié) jusqu'à leur prochaine écriture.
            for _table, _col, _type in (("pois", "links", "TEXT"),
                                        ("pois", "mdate", "INTEGER"),
                                        ("links", "mdate", "INTEGER")):
                try:
                    self._conn.execute(
                        f"ALTER TABLE {_table} ADD COLUMN {_col} {_type}")
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass  # colonne déjà présente

            # Ajout défensif de cards.detected_content / detected_objects sur
            # une base existante créée avant leur introduction (CREATE TABLE
            # IF NOT EXISTS, ci-dessus, ne modifie pas une table déjà
            # présente sans ces colonnes).
            for _col in ("detected_content", "detected_objects",
                         "detected_content_lang", "detected_objects_lang"):
                try:
                    self._conn.execute(f"ALTER TABLE cards ADD COLUMN {_col} TEXT")
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass  # colonne déjà présente

            # Fonction SQL personnalisée pour les recherches insensibles
            # aux accents et à la casse (ex: "dodanes", "dôdanes" et
            # "dodânes" doivent toutes se retrouver mutuellement).
            try:
                self._conn.create_function(
                    "unaccent_lower", 1, _strip_accents, deterministic=True
                )
            except sqlite3.NotSupportedError:
                # SQLite build too old to support the `deterministic` flag
                self._conn.create_function("unaccent_lower", 1, _strip_accents)

            # Recherche textuelle multi-mots (voir _text_search_condition).
            try:
                self._conn.create_function(
                    "search_match", -1, _search_match, deterministic=True
                )
            except sqlite3.NotSupportedError:
                self._conn.create_function("search_match", -1, _search_match)

            # Fusion de travels.json dans collections.json (une fois : le
            # fichier est ensuite renommé en travels.json.bak).
            self._db_signature = self._current_db_signature()
            try:
                self._migrate_travels_json()
            except Exception:
                logger.exception(
                    "Migration de travels.json vers collections.json : échec")
            self._db_signature = self._current_db_signature()

        return self._conn

    # ------------------------------------------------------------------
    # Connexion sqlite séparée : refresh_tokens.sqlite
    # ------------------------------------------------------------------
    # Fichier distinct de postcards.sqlite, propre au serveur flpostcards
    # en cours d'exécution : jamais recréé par generate()/sync(), jamais
    # écrasé par une publication (PostcardPublish.publish() ne synchronise
    # que postcards.sqlite). Les sessions (refresh tokens) survivent donc
    # aux déploiements, contrairement à avant où elles vivaient dans
    # postcards.sqlite et étaient effacées à chaque synchronisation.

    def _get_refresh_conn(self) -> sqlite3.Connection:
        """Retourne (et ouvre/crée si nécessaire) la connexion à
        refresh_tokens.sqlite."""
        if self._refresh_conn is None:
            self.datadir.mkdir(parents=True, exist_ok=True)
            self._refresh_conn = sqlite3.connect(
                self.refresh_tokens_db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._refresh_conn.row_factory = sqlite3.Row
            self._refresh_conn.execute("PRAGMA journal_mode=WAL;")
            self._refresh_conn.executescript(_DDL_REFRESH_TOKENS_DB)
            self._refresh_conn.commit()

        return self._refresh_conn

    def _migrate_legacy_refresh_tokens(self) -> None:
        """Migration défensive, une fois : transfère vers
        refresh_tokens.sqlite les éventuelles lignes d'une table
        refresh_tokens encore présente dans postcards.sqlite (base créée
        avant l'introduction de ce fichier séparé), puis supprime cette
        table de postcards.sqlite. Sans effet (rapide) si elle est déjà
        absente.
        """
        conn = self._conn
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'refresh_tokens'"
        )
        if cur.fetchone() is None:
            return

        rows = conn.execute("SELECT * FROM refresh_tokens").fetchall()
        if rows:
            refresh_conn = self._get_refresh_conn()
            cols = rows[0].keys()
            placeholders = ", ".join(f":{c}" for c in cols)
            col_list = ", ".join(cols)
            refresh_conn.executemany(
                f"INSERT OR IGNORE INTO refresh_tokens ({col_list}) "
                f"VALUES ({placeholders})",
                [dict(r) for r in rows],
            )
            refresh_conn.commit()
            logger.info(
                "Migration refresh_tokens : %d ligne(s) transférée(s) "
                "vers %s", len(rows), self.refresh_tokens_db_path,
            )

        conn.execute("DROP TABLE refresh_tokens")
        conn.commit()

    def close(self) -> None:
        """Ferme les connexions SQLite (base principale et refresh_tokens)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._db_signature = None
        if self._refresh_conn is not None:
            self._refresh_conn.close()
            self._refresh_conn = None

    def __enter__(self) -> "Model":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # JSON cards
    # ------------------------------------------------------------------

    def _json_path(self, card_id: str | int) -> Path:
        return self.cards_dir / f"{card_id}.json"

    def load_json(self, card_id: str | int) -> dict:
        """
        Lit le JSON d'une carte depuis cards/.

        Si l'id n'existe pas, retourne un dict avec tous les champs
        initialisés à leur valeur par défaut et l'id fourni.
        """
        path = self._json_path(card_id)
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        # Carte inconnue : retourne un squelette avec l'id
        skeleton = dict(_CARD_DEFAULTS)
        skeleton["id"] = str(card_id)
        now = int(time.time())
        skeleton["cdate"] = now
        skeleton["mdate"] = now
        return skeleton

    def write_json(self, card: dict) -> None:
        """
        Écrit le JSON d'une carte dans cards/ et met à jour la base SQLite.

        Le champ ``mdate`` est automatiquement rafraîchi.

        Si le champ ``doubles`` contient de nouveaux ids par rapport à
        la version précédente, la réciprocité est assurée : pour chaque
        nouvel id ``id2`` ajouté dans ``doubles`` de la carte ``id1``,
        la carte ``id2`` est mise à jour pour inclure ``id1`` dans son
        propre champ ``doubles`` (si ce n'est pas déjà le cas).
        """
        card = dict(card)  # copie défensive
        card_id = str(card["id"])

        # Détermine les nouveaux doublons ajoutés par rapport à l'existant
        old_card = self.load_json(card_id)
        old_doubles = {str(d) for d in (old_card.get("doubles") or [])}
        new_doubles = {str(d) for d in (card.get("doubles") or [])}
        added_doubles = new_doubles - old_doubles

        card["doubles"] = sorted(new_doubles)
        if card.get("status") not in CARD_STATUSES:
            card["status"] = DEFAULT_CARD_STATUS
        card["storage"] = _clean_storage(card.get("storage"))
        card["links"] = _clean_id_list(card.get("links"))
        card["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(card, fh, ensure_ascii=False, indent=2)

        self._upsert_card(card)

        # Crée automatiquement les POIs référencés qui n'existent pas encore
        for poi_id in {str(p) for p in (card.get("poi") or [])}:
            self._ensure_poi(poi_id)

        # Assure la réciprocité pour les nouveaux doublons
        for other_id in added_doubles:
            if other_id == card_id:
                continue
            self._add_double(other_id, card_id)

    def _add_double(self, card_id: str, double_id: str) -> None:
        """
        Ajoute ``double_id`` au champ ``doubles`` de la carte ``card_id``
        (JSON + base), si ce n'est pas déjà présent.
        """
        other = self.load_json(card_id)
        other_doubles = {str(d) for d in (other.get("doubles") or [])}
        if double_id in other_doubles:
            return

        other_doubles.add(double_id)
        other["doubles"] = sorted(other_doubles)
        other["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(other, fh, ensure_ascii=False, indent=2)

        self._upsert_card(other)
        logger.info(
            "Réciprocité doublons : ajout de %s dans doubles de %s",
            double_id, card_id,
        )

    def _upsert_card(self, card: dict) -> None:
        """INSERT OR REPLACE d'une carte dans la base."""
        row = _card_to_row(card)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO cards ({cols}) VALUES ({placeholders})"
        conn = self._get_conn()
        conn.execute(sql, row)
        conn.commit()

    # ------------------------------------------------------------------
    # Travels
    # ------------------------------------------------------------------

    # Un travel (parcours) n'est plus une entité séparée : c'est une
    # collection dont le champ ``travel`` est vrai et dont ``start`` donne
    # le point de départ (collections.json). Son id est celui de la
    # collection ; titre, description et ordre d'affichage sont ceux de la
    # collection. La table ``travels`` ne contient que le résultat du
    # calcul (distance, cartes dans l'ordre du parcours...).

    _TRAVEL_SELECT = (
        "SELECT t.id, t.distance_m, t.distance_km, t.start_lat, t.start_lon, "
        "t.end_lat, t.end_lon, t.count, t.cards, t.mdate, "
        'c.title AS title, c.description AS description, c.sort_order AS "order" '
        "FROM travels t JOIN collections c ON c.id = t.id "
        "WHERE c.travel = 1"
    )

    def read_travel(self, travel_id: str, published_only: bool = True) -> dict | None:
        """
        Lit le parcours calculé d'une collection depuis la base SQLite
        (``travel_id`` = id de la collection).

        ``published_only`` : ne retourne le parcours que si sa collection
        est publiée (c'est ce que montre flpostcards).
        Retourne None si le parcours n'existe pas.
        """
        conn = self._get_conn()
        sql = self._TRAVEL_SELECT + " AND t.id = ?"
        if published_only:
            sql += " AND c.published = 1"
        row = conn.execute(sql, (str(travel_id),)).fetchone()
        return _row_to_travel(row) if row else None

    def list_travels(self, published_only: bool = True) -> list[dict]:
        """Retourne les parcours calculés, dans l'ordre (``order``) de
        leur collection -- c'est l'ordre de la page /travel/ de
        flpostcards. ``published_only`` : seulement ceux dont la
        collection est publiée."""
        conn = self._get_conn()
        sql = self._TRAVEL_SELECT
        if published_only:
            sql += " AND c.published = 1"
        sql += " ORDER BY c.sort_order, c.rowid"
        return [_row_to_travel(r) for r in conn.execute(sql).fetchall()]

    def write_travel(self, travel: dict) -> None:
        """
        Écrit (INSERT OR REPLACE) un parcours calculé dans la base SQLite
        (``travel["id"]`` = id de la collection).

        Le champ ``mdate`` (date de dernière modification) est calculé ici,
        pas fourni par l'appelant : il n'est mis à jour (timestamp UNIX
        courant) que si la liste de cartes (``cards``, le parcours calculé)
        diffère effectivement de la version déjà en base, ou si le trajet
        est nouveau. Sinon la ``mdate`` existante est conservée, même si
        cette méthode est rappelée sans changement réel (ex : régénération
        périodique des trajets). Toute valeur de ``mdate`` passée dans
        ``travel`` est ignorée.
        """
        row = _travel_to_row(travel)
        conn = self._get_conn()

        cur = conn.execute(
            "SELECT cards, mdate FROM travels WHERE id = ?", (row["id"],)
        )
        existing = cur.fetchone()
        if existing is not None and existing["cards"] == row["cards"]:
            row["mdate"] = existing["mdate"]
        else:
            row["mdate"] = int(time.time())

        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO travels ({cols}) VALUES ({placeholders})"
        conn.execute(sql, row)
        conn.commit()

    def delete_travel(self, travel_id: str) -> None:
        """Supprime un parcours calculé de la base."""
        conn = self._get_conn()
        conn.execute("DELETE FROM travels WHERE id = ?", (str(travel_id),))
        conn.commit()

    def prune_travels(self) -> int:
        """Supprime les parcours calculés dont la collection n'existe plus
        ou n'a plus ``travel`` vrai. Retourne le nombre de lignes
        supprimées."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM travels WHERE id NOT IN "
            "(SELECT id FROM collections WHERE travel = 1)")
        conn.commit()
        return cur.rowcount

    def _migrate_travels_json(self) -> None:
        """Fusionne l'ancien travels.json dans collections.json.

        Pour chaque parcours ``{id, title, title2, start, collection,
        position}`` : la collection ``collection`` (créée, publiée, si elle
        n'existe pas) reçoit ``travel = true`` et ``start`` ; son ``title``
        et sa ``description`` sont repris du parcours (``title`` et
        ``title2``) seulement s'ils sont vides (les valeurs déjà saisies
        sur la collection priment) ; ``position`` disparaît (c'est
        ``order`` de la collection qui sert désormais). Les lignes déjà
        calculées de la table ``travels`` sont renommées de l'ancien id du
        parcours vers l'id de la collection.

        Les parcours sans collection, ou en double pour une même
        collection, ne peuvent pas être fusionnés : ils sont ignorés (avec
        un avertissement) mais restent dans la sauvegarde. Une fois la
        fusion écrite, travels.json est renommé en travels.json.bak.
        """
        if not self.travels_json.exists():
            return
        with self.travels_json.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            entries = list(data.values())
        elif isinstance(data, list):
            entries = list(data)
        else:
            entries = []
        entries = [e for e in entries if isinstance(e, dict) and e.get("id")]

        def _pos(e: dict) -> float:
            p = e.get("position")
            return p if isinstance(p, (int, float)) else 0
        entries.sort(key=lambda e: (_pos(e), str(e["id"])))

        collections = self._read_collections_json()
        by_id = {c["id"]: c for c in collections}
        next_order = max((c["order"] for c in collections), default=-1) + 1
        done: set[str] = set()
        renames: list[tuple[str, str]] = []

        for entry in entries:
            tid = str(entry["id"]).strip()
            cid = str(entry.get("collection") or "").strip()
            if not cid:
                logger.warning(
                    "travels.json : le parcours %s n'a pas de collection, "
                    "il ne peut pas être fusionné (voir travels.json.bak)", tid)
                continue
            if cid in done:
                logger.warning(
                    "travels.json : plusieurs parcours pour la collection %s, "
                    "%s ignoré (voir travels.json.bak)", cid, tid)
                continue
            collection = by_id.get(cid)
            if collection is None:
                collection = _clean_collection(
                    {"id": cid, "published": True, "map": False,
                     "order": next_order})
                next_order += 1
                collections.append(collection)
                by_id[cid] = collection
            collection["travel"] = True
            collection["start"] = _clean_start(entry.get("start"))
            if not collection["title"]:
                collection["title"] = str(entry.get("title") or "").strip() or None
            if not collection["description"]:
                collection["description"] = (
                    str(entry.get("title2") or "").strip() or None)
            done.add(cid)
            renames.append((tid, cid))

        collections.sort(key=lambda c: c["order"])
        self._write_collections_json(collections)
        self._write_collections_db(collections)

        # Résultats déjà calculés : l'id du parcours devient celui de la
        # collection.
        conn = self._conn
        cols = ("id", "distance_m", "distance_km", "start_lat", "start_lon",
                "end_lat", "end_lon", "count", "cards", "mdate")
        rows: dict[str, dict] = {}
        for tid, cid in renames:
            r = conn.execute(
                "SELECT * FROM travels WHERE id = ?", (tid,)).fetchone()
            if r is not None:
                rows[cid] = {k: r[k] for k in cols}
                rows[cid]["id"] = cid
        for tid, cid in renames:
            conn.execute("DELETE FROM travels WHERE id IN (?, ?)", (tid, cid))
        for cid, r in rows.items():
            conn.execute(
                f"INSERT INTO travels ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + k for k in cols)})", r)
        conn.commit()

        backup = self.travels_json.with_suffix(".json.bak")
        self.travels_json.replace(backup)
        logger.info(
            "travels.json fusionné dans collections.json (%d parcours), "
            "ancien fichier conservé : %s", len(done), backup)

    # ------------------------------------------------------------------
    # Synchronisation JSON → SQLite
    # ------------------------------------------------------------------

    def sync(self) -> int:
        """
        Lit les JSON présents dans cards/ et met à jour la base SQLite
        uniquement pour les cartes dont le mdate est plus récent que
        celui stocké en base.

        Retourne le nombre de cartes mises à jour.
        """
        if not self.cards_dir.exists():
            logger.warning("cards_dir introuvable : %s", self.cards_dir)
            return 0

        conn = self._get_conn()
        updated = 0

        for json_path in sorted(self.cards_dir.glob("*.json")):
            card_id = json_path.stem
            with json_path.open(encoding="utf-8") as fh:
                card = json.load(fh)

            # Vérifie si la carte est déjà à jour en base
            cur = conn.execute(
                "SELECT mdate FROM cards WHERE id = ?", (str(card_id),)
            )
            row = cur.fetchone()
            file_mdate = card.get("mdate") or 0
            db_mdate = row["mdate"] if row else -1

            if file_mdate > db_mdate:
                self._upsert_card(card)
                for poi_id in {str(p) for p in (card.get("poi") or [])}:
                    self._ensure_poi(poi_id)
                updated += 1
                logger.debug("sync : carte %s mise à jour", card_id)

        logger.info("sync : %d carte(s) mise(s) à jour", updated)
        return updated

    # ------------------------------------------------------------------
    # Génération de la base
    # ------------------------------------------------------------------

    def generate(self) -> int:
        """
        Crée une base SQLite vierge (écrase l'existante si présente)
        puis importe tous les JSON depuis cards/.

        Retourne le nombre de cartes importées.
        """
        # Supprime la base existante
        if self.db_path.exists():
            self.db_path.unlink()
            # Remet la connexion à zéro
            self.close()

        self.datadir.mkdir(parents=True, exist_ok=True)

        conn = self._get_conn()
        conn.executescript(_DDL_CARDS + _DDL_TRAVELS + _DDL_POIS + _DDL_LINKS + _DDL_COLLECTIONS + _DDL_AUTHS + _DDL_INDEXES)
        conn.commit()
        logger.info("Base créée : %s", self.db_path)

        # Import des collections depuis collections.json et des POIs
        # depuis pois.json (avant les cartes, pour que _ensure_poi
        # puisse ignorer les ids déjà pleinement décrits dans pois.json)
        self.sync_collections()
        self.sync_pois()
        self.sync_links()

        if not self.cards_dir.exists():
            logger.warning("cards_dir introuvable : %s", self.cards_dir)
            return 0

        count = 0
        for json_path in sorted(self.cards_dir.glob("*.json")):
            with json_path.open(encoding="utf-8") as fh:
                card = json.load(fh)
            self._upsert_card(card)
            for poi_id in {str(p) for p in (card.get("poi") or [])}:
                self._ensure_poi(poi_id)
            count += 1

        logger.info("generate : %d carte(s) importée(s)", count)
        return count

    # ------------------------------------------------------------------
    # Lecture de cartes depuis la base
    # ------------------------------------------------------------------

    def list_storages(self) -> list[str]:
        """Retourne les lieux de stockage physique distincts présents en
        base (hors valeur vide), triés sans tenir compte de la casse ni des
        accents. Sert à construire la liste de sélection de ktmanager."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT DISTINCT storage FROM cards "
            "WHERE storage IS NOT NULL AND TRIM(storage) != ''"
        )
        values = [r["storage"] for r in cur.fetchall()]
        return sorted(values, key=lambda v: (_strip_accents(v), v))

    def get_card(self, card_id: str | int) -> dict | None:
        """Retourne une carte depuis la base SQLite, ou None si absente."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM cards WHERE id = ?", (str(card_id),))
        row = cur.fetchone()
        return _row_to_card(row) if row else None

    def list_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
        exclude_status: str | list[str] | None = None,
    ) -> list[dict]:
        """
        Liste les cartes avec filtres optionnels.

        Paramètres
        ----------
        collection : str | None
            Filtre sur la collection (recherche dans le champ JSON ``collections``).
        search : str | None
            Recherche textuelle dans title, title2, description,
            verso_text, recto_text, address.
            Recherche textuelle (insensible aux accents et à la casse)
            dans title, title2, description, verso_text, recto_text,
            address, poi.
        limit : int | None
            Nombre maximum de résultats.
        offset : int
            Décalage pour la pagination.
        status : str | None
            Si renseigné, ne retourne que les cartes ayant exactement ce
            statut (ex : "trade", "exchanged"). Prioritaire sur
            ``exclude_status``.
        exclude_status : str | list[str] | None
            Si renseigné (et ``status`` absent), exclut les cartes ayant
            ce(s) statut(s) (ex : "exchanged").
        """
        conditions: list[str] = []
        params: list[Any] = []

        if collection:
            # SQLite : json_each pour chercher dans le tableau JSON
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        status_cond, status_params = _status_condition(status, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)

        if search:
            search_sql, search_params = _text_search_condition(search)
            if search_sql:
                conditions.append(search_sql)
                params.extend(search_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        offset_clause = f"OFFSET {int(offset)}" if offset else ""

        sql = f"SELECT * FROM cards {where} ORDER BY CAST(id AS INTEGER) {limit_clause} {offset_clause}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def count_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        status: str | None = None,
        exclude_status: str | list[str] | None = None,
    ) -> int:
        """Retourne le nombre de cartes (avec les mêmes filtres que list_cards)."""
        conditions: list[str] = []
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        status_cond, status_params = _status_condition(status, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)

        if search:
            search_sql, search_params = _text_search_condition(search)
            if search_sql:
                conditions.append(search_sql)
                params.extend(search_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT COUNT(*) FROM cards {where}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]

    def next_id(self) -> int:
        """
        Détermine le prochain id disponible en inspectant cards/.
        Retourne max(ids existants) + 1, ou 1 si cards/ est vide.
        """
        if not self.cards_dir.exists():
            return 1
        ids = []
        for p in self.cards_dir.glob("*.json"):
            try:
                ids.append(int(p.stem))
            except ValueError:
                pass
        return max(ids) + 1 if ids else 1

    # ------------------------------------------------------------------
    # Cartes uniques (exclusion des doublons)
    # ------------------------------------------------------------------

    # Condition SQL qui sélectionne, pour chaque groupe de doublons,
    # la carte la plus pertinente à afficher :
    #
    #  (A) La carte a des coordonnées GPS ET aucune autre carte du groupe
    #      n'a de GPS avec un id numérique inférieur (départage quand
    #      plusieurs membres ont des GPS).
    #
    #  (B) La carte n'a pas de coordonnées GPS ET aucun membre du groupe
    #      n'en a non plus ET elle a l'id numérique le plus petit du
    #      groupe (même départage que (A), sans la condition GPS).
    #
    # "Groupe" = la carte elle-même + les cartes qui la référencent dans
    # leur champ `doubles` + les cartes qu'elle référence dans le sien.
    #
    # Note : l'alias utilisé pour la carte courante doit être `cards`
    # (pas de sous-alias) car cette condition est injectée dans un WHERE
    # sur la table principale.
    #
    # (B) NE teste PAS "cette carte n'est référencée comme doublon par
    # aucune autre" : write_json()/_add_double() assurent la réciprocité
    # de `doubles` (voir plus haut), si bien que TOUT doublon référence
    # forcément TOUT autre membre de son groupe en retour -- cette
    # condition ne serait donc jamais vraie pour aucun des deux membres,
    # et un groupe sans aucun GPS disparaîtrait entièrement de la
    # galerie (aucune carte n'y satisferait ni (A) ni (B)). D'où le
    # départage par id le plus petit, comme (A).
    _UNIQUE_CARD_CONDITION = (
        # Parenthèses extérieures OBLIGATOIRES : cette condition contient un OR
        # interne (cas A OR cas B). Sans elles, un AND ajouté par
        # " AND ".join(conditions) serait prioritaire sur le OR interne et
        # rendrait les filtres search/collection inopérants pour le cas A
        # (toutes les cartes avec GPS passeraient quel que soit le filtre).
        "("
        # (A) carte avec GPS, préférée dans son groupe
        "  ("
        "    cards.coord_lat IS NOT NULL"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS cg"
        "      WHERE cg.coord_lat IS NOT NULL"
        "        AND CAST(cg.id AS INTEGER) < CAST(cards.id AS INTEGER)"
        "        AND ("
        "          EXISTS ("
        "            SELECT 1 FROM json_each(cards.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cg.id"
        "          )"
        "          OR EXISTS ("
        "            SELECT 1 FROM json_each(cg.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cards.id"
        "          )"
        "        )"
        "    )"
        "  )"
        "  OR"
        # (B) carte sans GPS, aucun membre du groupe n'a de GPS, et
        #     c'est celle avec le plus petit id du groupe
        "  ("
        "    cards.coord_lat IS NULL"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS cg"
        "      WHERE cg.coord_lat IS NOT NULL"
        "        AND ("
        "          EXISTS ("
        "            SELECT 1 FROM json_each(cards.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cg.id"
        "          )"
        "          OR EXISTS ("
        "            SELECT 1 FROM json_each(cg.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cards.id"
        "          )"
        "        )"
        "    )"
        "    AND NOT EXISTS ("
        "      SELECT 1 FROM cards AS cg"
        "      WHERE CAST(cg.id AS INTEGER) < CAST(cards.id AS INTEGER)"
        "        AND ("
        "          EXISTS ("
        "            SELECT 1 FROM json_each(cards.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cg.id"
        "          )"
        "          OR EXISTS ("
        "            SELECT 1 FROM json_each(cg.doubles)"
        "            WHERE CAST(json_each.value AS TEXT) = cards.id"
        "          )"
        "        )"
        "    )"
        "  )"
        ")"
    )

    def list_unique_cards(
        self,
        collection: str | None = None,
        poi: str | None = None,
        search: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        status: str | None = None,
        exclude_status: str | list[str] | None = None,
    ) -> list[dict]:
        """
        Liste les cartes uniques : pour chaque groupe de doublons, retourne
        la carte dont les coordonnées GPS sont renseignées en priorité.
        Si aucun membre du groupe n'a de GPS, retourne celle ayant l'id
        numérique le plus petit (voir ``_UNIQUE_CARD_CONDITION``).

        Paramètres
        ----------
        collection : str | None
            Filtre sur la collection (recherche dans le champ JSON ``collections``).
        poi : str | None
            Filtre sur le point d'intérêt (recherche dans le champ JSON
            ``poi``) : ne retourne que les cartes référençant ce POI. Voir
            flpostcards.blueprints.home.card_detail (section "Points
            d'intérêt" de la fiche carte).
        search : str | None
            Recherche textuelle (insensible aux accents et à la casse)
            dans title, title2, description, verso_text, recto_text,
            address, poi.
        limit : int | None
            Nombre maximum de résultats.
        offset : int
            Décalage pour la pagination.
        status : str | None
            Si renseigné, ne retourne que les cartes ayant exactement ce
            statut. Prioritaire sur ``exclude_status``.
        exclude_status : str | list[str] | None
            Si renseigné (et ``status`` absent), exclut les cartes ayant
            ce(s) statut(s) (ex : "exchanged").
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        if poi:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.poi)"
                "  WHERE value = ?"
                ")"
            )
            params.append(str(poi))

        status_cond, status_params = _status_condition(status, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)

        if search:
            search_sql, search_params = _text_search_condition(search)
            if search_sql:
                conditions.append(search_sql)
                params.extend(search_params)

        where = f"WHERE {' AND '.join(conditions)}"
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        offset_clause = f"OFFSET {int(offset)}" if offset else ""

        sql = (
            f"SELECT * FROM cards {where} "
            f"ORDER BY CAST(id AS INTEGER) {limit_clause} {offset_clause}"
        )
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def list_recent_unique_cards(
        self,
        days: int,
        fallback_count: int,
        collection: str | None = None,
        exclude_status: str | list[str] | None = None,
    ) -> list[dict]:
        """
        Liste les cartes uniques (sans doublons) ajoutées dans les
        ``days`` derniers jours (champ ``cdate``).

        Si aucune carte ne correspond à cette fenêtre, retombe sur les
        ``fallback_count`` derniers ajouts (toujours sans doublons),
        quelle que soit leur ancienneté.

        Paramètres
        ----------
        days : int
            Taille de la fenêtre récente, en jours.
        fallback_count : int
            Nombre de cartes à retourner si la fenêtre récente est vide.
        collection : str | None
            Filtre optionnel sur la collection.
        exclude_status : str | list[str] | None
            Exclut les cartes ayant ce(s) statut(s) (ex : "exchanged").
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        status_cond, status_params = _status_condition(None, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)

        base_where = " AND ".join(conditions)
        conn = self._get_conn()

        # Tentative 1 : cartes ajoutées dans les `days` derniers jours
        cutoff = int(time.time()) - days * 86400
        recent_sql = (
            f"SELECT * FROM cards WHERE {base_where} AND cdate >= ? "
            f"ORDER BY cdate DESC"
        )
        cur = conn.execute(recent_sql, params + [cutoff])
        rows = cur.fetchall()

        if rows:
            return [_row_to_card(r) for r in rows]

        # Repli : les `fallback_count` derniers ajouts, sans contrainte
        # de date (mais toujours sans doublons / avec le filtre collection)
        fallback_sql = (
            f"SELECT * FROM cards WHERE {base_where} "
            f"ORDER BY cdate DESC LIMIT {int(fallback_count)}"
        )
        cur = conn.execute(fallback_sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def count_unique_cards(
        self,
        collection: str | None = None,
        search: str | None = None,
        status: str | None = None,
        exclude_status: str | list[str] | None = None,
    ) -> int:
        """Retourne le nombre de cartes uniques (cf. list_unique_cards).

        Le comptage utilise la même logique de sélection : une seule carte
        par groupe de doublons, en privilégiant celle avec des coordonnées GPS.
        """
        conditions: list[str] = [self._UNIQUE_CARD_CONDITION]
        params: list[Any] = []

        if collection:
            conditions.append(
                "EXISTS ("
                "  SELECT 1 FROM json_each(cards.collections)"
                "  WHERE value = ?"
                ")"
            )
            params.append(collection)

        status_cond, status_params = _status_condition(status, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)

        if search:
            search_sql, search_params = _text_search_condition(search)
            if search_sql:
                conditions.append(search_sql)
                params.extend(search_params)

        where = f"WHERE {' AND '.join(conditions)}"
        sql = f"SELECT COUNT(*) FROM cards {where}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]

    def list_unique_cards_with_coord(
        self,
        after_id: int = 0,
        limit: int = 500,
        exclude_status: str | list[str] | None = None,
    ) -> list[dict]:
        """
        Cartes uniques (sans doublons) possédant des coordonnées GPS,
        triées par id numérique croissant, avec pagination par curseur.

        Contrairement à une pagination OFFSET/LIMIT classique, le
        curseur ``after_id`` (dernier id numérique vu par le client)
        reste valide même si des cartes sont ajoutées, modifiées ou
        supprimées entre deux appels : chaque page ne dépend que de la
        position du dernier id vu, jamais du nombre de lignes qui la
        précèdent. Cela élimine les doublons/cartes manquantes que
        produit une pagination OFFSET quand la base change pendant le
        parcours des pages (écriture concurrente, sync, publication).
        """
        conditions = [
            self._UNIQUE_CARD_CONDITION,
            "cards.coord_lat IS NOT NULL",
            "cards.coord_lon IS NOT NULL",
            "CAST(cards.id AS INTEGER) > ?",
        ]
        params: list[Any] = [after_id]
        status_cond, status_params = _status_condition(None, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)
        where = f"WHERE {' AND '.join(conditions)}"
        sql = (
            f"SELECT * FROM cards {where} "
            f"ORDER BY CAST(cards.id AS INTEGER) LIMIT {int(limit)}"
        )
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return [_row_to_card(r) for r in cur.fetchall()]

    def count_unique_cards_with_coord(
        self,
        exclude_status: str | list[str] | None = None,
    ) -> int:
        """
        Nombre total de cartes uniques (sans doublons) possédant des
        coordonnées GPS. Utilise exactement le même filtre que
        :meth:`list_unique_cards_with_coord`, pour que ``total`` soit
        toujours cohérent avec ce que la pagination peut réellement
        renvoyer.
        """
        conditions = [
            self._UNIQUE_CARD_CONDITION,
            "cards.coord_lat IS NOT NULL",
            "cards.coord_lon IS NOT NULL",
        ]
        params: list[Any] = []
        status_cond, status_params = _status_condition(None, exclude_status)
        if status_cond:
            conditions.append(status_cond)
            params.extend(status_params)
        where = f"WHERE {' AND '.join(conditions)}"
        sql = f"SELECT COUNT(*) FROM cards {where}"
        conn = self._get_conn()
        cur = conn.execute(sql, params)
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Suppression d'une carte
    # ------------------------------------------------------------------

    def delete_card(self, card_id: str | int) -> bool:
        """
        Supprime une carte : fichier JSON dans cards/ et ligne en base.

        Si l'id supprimé apparaît dans le champ ``doubles`` d'autres
        cartes, il en est retiré (JSON + base).

        Retourne True si la carte existait et a été supprimée,
        False si elle n'existait pas.
        """
        card_id = str(card_id)
        path = self._json_path(card_id)
        existed = path.exists()

        # Supprime le fichier JSON
        if existed:
            path.unlink()

        # Supprime la ligne en base
        conn = self._get_conn()
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        conn.commit()

        # Retire card_id du champ doubles des autres cartes
        cur = conn.execute(
            "SELECT id FROM cards WHERE EXISTS ("
            "  SELECT 1 FROM json_each(cards.doubles)"
            "  WHERE CAST(json_each.value AS TEXT) = ?"
            ")",
            (card_id,),
        )
        other_ids = [row["id"] for row in cur.fetchall()]

        for other_id in other_ids:
            self._remove_double(other_id, card_id)

        if existed or other_ids:
            logger.info(
                "delete_card : carte %s supprimée (réf. retirée de %s)",
                card_id, other_ids,
            )

        return existed

    def delete_card_full(
        self,
        card_id: str | int,
        file_format: str = "tiff",
        update_index: bool = True,
    ) -> bool:
        """
        Supprime complètement une carte : entrée en base/JSON
        (cf. :meth:`delete_card`), images sources (``cards/``),
        vignettes (``size_div1``, ``size_div3``, ``size_div10``,
        ``size_div20``) et référence dans l'index de recherche par
        similarité, si celui-ci est disponible.

        :param file_format: extension des images sources dans ``cards/``
            (ex. ``"tiff"``).
        :param update_index: si True, retire la carte de l'index de
            similarité (``postcards.pkl``) lorsque le module
            correspondant est disponible.

        Retourne True si la carte existait en base/JSON et a été
        supprimée, False si elle n'existait pas.
        """
        card_id = str(card_id)

        existed = self.delete_card(card_id)

        for rv in ("R", "V"):
            fname = self.cards_dir / f"{card_id}_{rv}.{file_format}"
            if fname.is_file():
                fname.unlink()

        for d in ("size_div1", "size_div3", "size_div10", "size_div20"):
            for rv in ("R", "V"):
                fname = self.datadir / d / f"{card_id}_{rv}.png"
                if fname.is_file():
                    fname.unlink()

        if update_index:
            try:
                from libpostcards.similar import PostcardSearcher
            except ImportError:
                logger.debug(
                    "delete_card_full : PostcardSearcher indisponible, "
                    "index non mis à jour pour la carte %s", card_id,
                )
            else:
                index_file = self.datadir / "postcards.pkl"
                searcher = PostcardSearcher(datadir=self.datadir)
                searcher.load_index(index_file)
                searcher.index.pop(
                    searcher.relative_path(
                        self.datadir / "size_div1" / f"{card_id}_R.png"
                    ),
                    None,
                )
                searcher.save_index(index_file)

        return existed

    def _remove_double(self, card_id: str, double_id: str) -> None:
        """
        Retire ``double_id`` du champ ``doubles`` de la carte ``card_id``
        (JSON + base), si présent.
        """
        other = self.load_json(card_id)
        other_doubles = {str(d) for d in (other.get("doubles") or [])}
        if double_id not in other_doubles:
            return

        other_doubles.discard(double_id)
        other["doubles"] = sorted(other_doubles)
        other["mdate"] = int(time.time())

        self.cards_dir.mkdir(parents=True, exist_ok=True)
        path = self._json_path(card_id)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(other, fh, ensure_ascii=False, indent=2)

        self._upsert_card(other)

    def _read_pois_json(self) -> dict:
        """Read pois.json and return the dict {poi_id: {...}}.

        Returns an empty dict if the file is absent or unreadable.
        """
        try:
            with self.pois_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_pois_json(self, pois: dict) -> None:
        """Atomically write {poi_id: {...}} to pois.json."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        tmp = self.pois_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(pois, fh, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.pois_json)

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------
    #
    # collections.json (source de vérité, modifiable via tkmanager et
    # synchronisé vers le serveur par tkpostcards.libs.publish) est un
    # tableau d'objets, dans l'ordre d'affichage :
    #
    #   [{"id": "Louhans", "title": "Louhans et environs",
    #     "description": "...", "published": true,
    #     "order": 0, "map": true,
    #     "travel": true, "start": [46.697018, 5.657401],
    #     "links": ["lien-1", "lien-2"]}, ...]
    #
    # ``order`` trie l'affichage des collections ; ``map`` indique si la
    # collection est proposée sur la carte /map/ (liste collections_map,
    # dans l'ordre de ``order``). ``travel`` indique qu'un parcours
    # (page /travel/) est défini pour la collection, ``start`` en est le
    # point de départ [lat, lon] ; le parcours calculé est dans la table
    # ``travels`` (voir ParcoursCartes.travels).
    #
    # La table SQLite ``collections`` en est une copie (même principe que
    # pois.json / table pois) : elle sert aux lectures, pour éviter de
    # rouvrir et reparser le JSON à chaque requête. Les cartes référencent
    # une collection par son ``id`` (champ ``collections`` de la carte).
    #
    # Ancien format (lu et converti automatiquement, voir
    # sync_collections()) : {"collections": ["A", "B"],
    # "collections_map": ["A"]} -- chaque nom devient un ``id`` publié,
    # ``order`` suit l'ordre de la liste et ``map`` vaut vrai pour les
    # noms de ``collections_map`` (pour tous si ce sous-ensemble est vide).

    def _read_collections_json(self) -> list[dict]:
        """Read collections.json and return the normalised list of
        collections (see :func:`_clean_collection`), in file order.

        The legacy format ``{"collections": [...], "collections_map":
        [...]}`` is converted on the fly (every name becomes a published
        collection without title). Returns an empty list if the file is
        absent or unreadable. Invalid entries (no id) and duplicate ids
        are ignored.
        """
        try:
            with self.collections_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return []

        if isinstance(data, dict):
            # Ancien format : simple liste de noms
            map_names = [str(c) for c in (data.get("collections_map") or [])]
            raw = [
                {
                    "id": name, "published": True, "order": i,
                    "map": (not map_names) or str(name) in map_names,
                }
                for i, name in enumerate(data.get("collections") or [])
            ]
        elif isinstance(data, list):
            raw = [
                c if isinstance(c, dict) else {"id": c, "published": True}
                for c in data
            ]
        else:
            return []

        result: list[dict] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw):
            try:
                # Sans "order", l'ordre du fichier fait foi.
                collection = _clean_collection(entry, default_order=index)
            except ValueError:
                continue
            if collection["id"] in seen:
                continue
            seen.add(collection["id"])
            result.append(collection)
        result.sort(key=lambda c: c["order"])   # tri stable
        return result

    def _write_collections_json(self, collections: list[dict]) -> None:
        """Atomically write collections.json (list of collections)."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        tmp = self.collections_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                [_clean_collection(c) for c in collections],
                fh, ensure_ascii=False, indent=2,
            )
        tmp.replace(self.collections_json)

    def _write_collections_db(self, collections: list[dict]) -> None:
        """Replace the content of the SQLite ``collections`` table with
        ``collections`` (full rewrite)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM collections")
        for collection in collections:
            conn.execute(
                "INSERT INTO collections "
                "(id, title, description, published, links, sort_order, on_map, "
                "travel, start_lat, start_lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    collection["id"],
                    collection["title"],
                    collection["description"],
                    1 if collection["published"] else 0,
                    json.dumps(collection["links"], ensure_ascii=False),
                    collection["order"],
                    1 if collection["map"] else 0,
                    1 if collection["travel"] else 0,
                    collection["start"][0] if collection["start"] else None,
                    collection["start"][1] if collection["start"] else None,
                ),
            )
        conn.commit()

    def list_collections(self, published_only: bool = False) -> list[dict]:
        """Return the collections ``[{id, title, description, published,
        order, map, links}]`` from SQLite, sorted by ``order``.

        ``published_only`` : ne retourne que celles dont ``published``
        est vrai (c'est ce que flpostcards affiche).
        """
        conn = self._get_conn()
        sql = "SELECT * FROM collections"
        if published_only:
            sql += " WHERE published = 1"
        sql += " ORDER BY sort_order, rowid"
        return [_row_to_collection(r) for r in conn.execute(sql).fetchall()]

    @staticmethod
    def map_collection_ids(collections: list[dict]) -> list[str]:
        """Ids of the collections offered on the map (/map/) -- the
        ``collections_map`` list: those whose ``map`` field is true, in
        the order of the given list (sorted by ``order``)."""
        return [c["id"] for c in collections if c.get("map")]

    def get_collection(self, collection_id: str) -> dict | None:
        """Return one collection by id (or ``None``)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM collections WHERE id = ?", (str(collection_id),)
        ).fetchone()
        return _row_to_collection(row) if row else None

    def write_collection(self, collection: dict) -> None:
        """Create or update a collection in collections.json (source de
        vérité) then in SQLite.

        ``title``, ``description``, ``published`` and ``links`` are
        replaced by the given values ; ``order``, ``map``, ``travel`` and
        ``start`` too when given, otherwise an existing collection keeps
        its own and a new one goes last (``order`` = highest + 1, ``map``
        true, no travel).
        """
        collections = self._read_collections_json()
        next_order = max((c["order"] for c in collections), default=-1) + 1
        existing = next(
            (c for c in collections if c["id"] == str(collection.get("id")).strip()),
            None,
        )
        collection = dict(collection)
        if existing is not None:
            # Keys not given keep their current value.
            for key in ("order", "map", "travel", "start"):
                if key not in collection:
                    collection[key] = existing[key]
        collection = _clean_collection(
            collection,
            default_order=existing["order"] if existing else next_order)
        if existing is not None:
            collections[collections.index(existing)] = collection
        else:
            collections.append(collection)
        collections.sort(key=lambda c: c["order"])
        self._write_collections_json(collections)
        self._write_collections_db(collections)

    def delete_collection(self, collection_id: str) -> int:
        """Delete a collection from collections.json and SQLite, and
        remove its id from the ``collections`` field of every card that
        references it (cards are rewritten through ``write_json``, like
        for :meth:`delete_link`).

        Returns the number of cards updated.
        """
        collection_id = str(collection_id)
        collections = [
            c for c in self._read_collections_json()
            if c["id"] != collection_id
        ]
        self._write_collections_json(collections)
        self._write_collections_db(collections)
        self.delete_travel(collection_id)

        updated = 0
        for card_id in self._cards_in_collection(collection_id):
            card = self.load_json(card_id)
            current = [str(x) for x in (card.get("collections") or [])]
            if collection_id not in current:
                continue
            card["collections"] = [x for x in current if x != collection_id]
            self.write_json(card)
            updated += 1

        logger.info(
            "delete_collection : collection %s supprimée (%d carte(s) mise(s) à jour)",
            collection_id, updated,
        )
        return updated

    def _cards_in_collection(self, collection_id: str) -> list[str]:
        """Ids des cartes dont le champ ``collections`` contient
        ``collection_id``."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id FROM cards WHERE EXISTS ("
            "  SELECT 1 FROM json_each(cards.collections)"
            "  WHERE json_each.value = ?)",
            (collection_id,),
        )
        return [r["id"] for r in cur.fetchall()]

    def reorder_collections(self, ids: list[str]) -> None:
        """Set the display order: the ``order`` field of the collections
        in ``ids`` becomes 0, 1, 2... in that order, followed by the
        others in their current order. Unknown ids are ignored."""
        collections = self._read_collections_json()
        by_id = {c["id"]: c for c in collections}
        ordered: list[dict] = []
        for cid in ids:
            c = by_id.pop(str(cid), None)
            if c is not None:
                ordered.append(c)
        ordered.extend(c for c in collections if c["id"] in by_id)
        for i, c in enumerate(ordered):
            c["order"] = i
        self._write_collections_json(ordered)
        self._write_collections_db(ordered)

    def sync_collections(self) -> int:
        """Synchronise collections.json → SQLite (recopie complète).

        Appelée par ``generate()`` (comme ``sync_pois()``) pour peupler
        la table ``collections`` depuis le JSON lors d'une régénération
        complète de la base. Si le fichier est encore dans l'ancien
        format (``{"collections": [...], "collections_map": [...]}``),
        il est réécrit dans le nouveau. Retourne le nombre de
        collections importées.
        """
        collections = self._read_collections_json()
        try:
            with self.collections_json.open(encoding="utf-8") as fh:
                is_legacy = isinstance(json.load(fh), dict)
        except (OSError, json.JSONDecodeError):
            is_legacy = False
        if is_legacy:
            try:
                self._write_collections_json(collections)
                logger.info(
                    "collections.json converti au nouveau format "
                    "(%d collection(s))", len(collections))
            except OSError as exc:
                logger.warning(
                    "collections.json : conversion impossible (%s)", exc)
        self._write_collections_db(collections)
        return len(collections)

    def _ensure_poi(self, poi_id: str) -> None:
        """Create a skeleton POI entry if it doesn't exist yet."""
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM pois WHERE id = ?", (poi_id,))
        if cur.fetchone() is not None:
            return
        self.write_poi({"id": poi_id, "description": None, "coord": None})
        logger.info("Nouveau POI créé automatiquement : %s", poi_id)

    # ------------------------------------------------------------------
    # POIs
    # ------------------------------------------------------------------

    def get_poi(self, poi_id: str) -> dict | None:
        """Return a POI from SQLite, or None if absent."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM pois WHERE id = ?", (poi_id,))
        row = cur.fetchone()
        return _row_to_poi(row) if row else None

    def list_pois(self) -> list[dict]:
        """Return the list of all POIs (from SQLite)."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM pois ORDER BY id")
        return [_row_to_poi(r) for r in cur.fetchall()]

    def write_poi(self, poi: dict) -> None:
        """Write (INSERT OR REPLACE) a POI in SQLite and in pois.json.

        ``poi`` : ``{id, description, coord, links}``. ``links`` est la
        liste des identifiants de liens du POI (table ``links``) ; si la
        clé est absente, les liens déjà enregistrés sont conservés.

        ``mdate`` (timestamp UNIX) n'est rafraîchi que si la description,
        les coordonnées ou les liens ont réellement changé : il sert à
        dater les pages des cartes qui référencent ce POI dans le
        sitemap et pour IndexNow (voir :meth:`effective_mdates`).

        The JSON file is updated atomically after the SQLite write.
        """
        existing = self.get_poi(str(poi["id"]))
        poi = dict(poi)
        if "links" not in poi and existing is not None:
            poi["links"] = existing.get("links")
        row = _poi_to_row(poi)
        if existing is not None and self._poi_fingerprint(existing) == \
                self._poi_fingerprint(poi):
            row["mdate"] = existing.get("mdate")
        else:
            row["mdate"] = int(time.time())
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        sql = f"INSERT OR REPLACE INTO pois ({cols}) VALUES ({placeholders})"
        conn = self._get_conn()
        conn.execute(sql, row)
        conn.commit()

        # Update pois.json
        pois = self._read_pois_json()
        pois[str(poi["id"])] = _row_to_poi(conn.execute(
            "SELECT * FROM pois WHERE id = ?", (str(poi["id"]),)
        ).fetchone())
        self._write_pois_json(pois)

    @staticmethod
    def _poi_fingerprint(poi: dict) -> tuple:
        """Contenu d'un POI qui, une fois modifié, change l'affichage des
        cartes qui le référencent (sert à décider si ``mdate`` avance)."""
        coord = poi.get("coord") or []
        return (
            poi.get("description") or None,
            (coord[0], coord[1]) if len(coord) > 1 else None,
            tuple(_clean_id_list(poi.get("links"))),
        )

    def delete_poi(self, poi_id: str) -> bool:
        """Delete a POI from SQLite and from pois.json.

        Returns True if the POI existed.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM pois WHERE id = ?", (poi_id,))
        existed = cur.fetchone() is not None
        conn.execute("DELETE FROM pois WHERE id = ?", (poi_id,))
        conn.commit()

        # Update pois.json
        pois = self._read_pois_json()
        if poi_id in pois:
            del pois[poi_id]
            self._write_pois_json(pois)

        return existed

    def rename_poi(self, old_id: str, new_id: str) -> int:
        """Renomme un POI et met à jour les cartes qui le référencent.

        Le POI ``new_id`` est créé avec la même description et les mêmes
        coordonnées que ``old_id``, qui est ensuite supprimé. Chaque carte
        dont le champ ``poi`` contient ``old_id`` est réécrite (JSON +
        base) pour référencer ``new_id`` à la place, via ``write_json``
        (ce qui garde le fichier ``cards/<id>.json`` et la table
        ``cards`` synchronisés, comme pour toute autre modification).

        Lève ``ValueError`` si ``new_id`` est vide, si ``old_id`` n'existe
        pas, ou si un POI ``new_id`` distinct existe déjà (pas de fusion
        implicite : il faut d'abord supprimer/renommer la cible).

        Retourne le nombre de cartes mises à jour.
        """
        old_id = str(old_id).strip()
        new_id = str(new_id).strip()

        if not new_id:
            raise ValueError("L'identifiant du POI ne peut pas être vide.")
        if new_id == old_id:
            return 0

        old_poi = self.get_poi(old_id)
        if old_poi is None:
            raise ValueError(f"POI introuvable : {old_id}")
        if self.get_poi(new_id) is not None:
            raise ValueError(f"Un POI avec l'identifiant « {new_id} » existe déjà.")

        # Crée le POI sous le nouvel id (mêmes attributs), supprime l'ancien
        self.write_poi({
            "id": new_id,
            "description": old_poi.get("description"),
            "coord": old_poi.get("coord"),
            "links": old_poi.get("links"),
        })
        self.delete_poi(old_id)

        # Met à jour toutes les cartes postales référençant l'ancien id.
        # Pré-filtrage SQL (LIKE) puis vérification exacte en Python, car
        # ``poi`` est une liste JSON sérialisée (pas de colonne dédiée).
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id, poi FROM cards WHERE poi LIKE ?",
            (f'%"{old_id}"%',),
        )
        candidate_ids = [row["id"] for row in cur.fetchall()]

        updated = 0
        for card_id in candidate_ids:
            card = self.load_json(card_id)
            poi_list = [str(p) for p in (card.get("poi") or [])]
            if old_id not in poi_list:
                continue

            new_list = []
            seen = set()
            for p in poi_list:
                p = new_id if p == old_id else p
                if p not in seen:
                    seen.add(p)
                    new_list.append(p)

            card["poi"] = new_list
            self.write_json(card)
            updated += 1

        logger.info(
            "POI renommé : %s -> %s (%d carte(s) mise(s) à jour)",
            old_id, new_id, updated,
        )
        return updated

    def sync_pois(self) -> int:
        """Synchronise pois.json → SQLite.

        Inserts or updates every POI present in pois.json whose entry in
        SQLite is absent or older (based on presence, not mdate — POIs
        have no mdate). Returns the number of POIs written.
        """
        pois = self._read_pois_json()
        if not pois:
            return 0
        conn = self._get_conn()
        count = 0
        for poi_id, poi_data in pois.items():
            poi_data["id"] = poi_id
            row = _poi_to_row(poi_data)
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO pois ({cols}) VALUES ({placeholders})",
                row,
            )
            count += 1
        conn.commit()
        logger.info("sync_pois : %d POI(s) synchronisé(s)", count)
        return count

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------
    # Même principe que les POIs : la table SQLite ``links`` sert aux
    # lectures (et est publiée avec postcards.sqlite), ``links.json``
    # en est la source persistante qui permet de reconstruire la base
    # (voir sync_links(), appelée par generate()). Une carte référence
    # ses liens par identifiant dans son champ ``links``.

    def _read_links_json(self) -> dict:
        """Read links.json and return the dict {link_id: {...}}.

        Returns an empty dict if the file is absent or unreadable.
        """
        try:
            with self.links_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_links_json(self, links: dict) -> None:
        """Atomically write {link_id: {...}} to links.json."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        tmp = self.links_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(links, fh, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.links_json)

    def get_link(self, link_id: str) -> dict | None:
        """Return a link from SQLite, or None if absent."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM links WHERE id = ?", (str(link_id),))
        row = cur.fetchone()
        return _row_to_link(row) if row else None

    def list_links(self) -> list[dict]:
        """Return the list of all links (from SQLite), sorted by id."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM links ORDER BY id")
        return [_row_to_link(r) for r in cur.fetchall()]

    def write_link(self, link: dict) -> None:
        """Write (INSERT OR REPLACE) a link in SQLite and in links.json.

        ``link`` : ``{id, title, description, url, check_date}``. ``id``
        et ``url`` sont obligatoires (``ValueError`` sinon) ;
        ``check_date`` est une date ISO ``AAAA-MM-JJ`` (ou vide).
        The JSON file is updated atomically after the SQLite write.
        """
        link_id = str(link.get("id") or "").strip()
        if not link_id:
            raise ValueError("L'identifiant d'un lien ne peut pas être vide.")
        row = _link_to_row({**link, "id": link_id})
        # ``mdate`` n'avance que si ce qui s'affiche (titre, description,
        # URL) change : une simple revérification (``check_date``) ne doit
        # pas déclencher de nouvelle soumission sitemap/IndexNow.
        existing = self.get_link(link_id)
        shown = ("title", "description", "url")
        if existing is not None and all(existing.get(k) == row[k] for k in shown):
            row["mdate"] = existing.get("mdate")
        else:
            row["mdate"] = int(time.time())
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        conn = self._get_conn()
        conn.execute(
            f"INSERT OR REPLACE INTO links ({cols}) VALUES ({placeholders})", row)
        conn.commit()

        links = self._read_links_json()
        links[link_id] = dict(row)
        self._write_links_json(links)

    def delete_link(self, link_id: str) -> int:
        """Delete a link from SQLite and from links.json, and remove its
        id from the ``links`` field of every card referencing it (cards
        are rewritten through ``write_json``, like for ``rename_poi``).

        Returns the number of cards updated.
        """
        link_id = str(link_id)
        conn = self._get_conn()
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        conn.commit()

        links = self._read_links_json()
        if link_id in links:
            del links[link_id]
            self._write_links_json(links)

        updated = 0
        for card_id in self._cards_referencing_link(link_id):
            card = self.load_json(card_id)
            current = _clean_id_list(card.get("links"))
            if link_id not in current:
                continue
            card["links"] = [x for x in current if x != link_id]
            self.write_json(card)
            updated += 1

        # Même nettoyage pour les POIs qui référencent ce lien.
        for poi_id in self._pois_referencing_link(link_id):
            poi = self.get_poi(poi_id)
            if poi is None:
                continue
            poi["links"] = [x for x in poi.get("links") or [] if x != link_id]
            self.write_poi(poi)

        logger.info(
            "delete_link : lien %s supprimé (%d carte(s) mise(s) à jour)",
            link_id, updated,
        )
        return updated

    def _pois_referencing_link(self, link_id: str) -> list[str]:
        """Ids des POIs dont le champ ``links`` contient ``link_id``."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id FROM pois WHERE EXISTS ("
            "  SELECT 1 FROM json_each(pois.links)"
            "  WHERE CAST(json_each.value AS TEXT) = ?"
            ")",
            (str(link_id),),
        )
        return [row["id"] for row in cur.fetchall()]

    def _cards_referencing_link(self, link_id: str) -> list[str]:
        """Ids des cartes dont le champ ``links`` contient ``link_id``."""
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT id FROM cards WHERE EXISTS ("
            "  SELECT 1 FROM json_each(cards.links)"
            "  WHERE CAST(json_each.value AS TEXT) = ?"
            ")",
            (str(link_id),),
        )
        return [row["id"] for row in cur.fetchall()]

    def rename_link(self, old_id: str, new_id: str) -> int:
        """Renomme un lien et met à jour les cartes qui le référencent.

        Lève ``ValueError`` si ``new_id`` est vide, si ``old_id`` n'existe
        pas, ou si un lien ``new_id`` distinct existe déjà.

        Retourne le nombre de cartes mises à jour.
        """
        old_id = str(old_id).strip()
        new_id = str(new_id).strip()

        if not new_id:
            raise ValueError("L'identifiant du lien ne peut pas être vide.")
        if new_id == old_id:
            return 0

        old_link = self.get_link(old_id)
        if old_link is None:
            raise ValueError(f"Lien introuvable : {old_id}")
        if self.get_link(new_id) is not None:
            raise ValueError(f"Un lien avec l'identifiant « {new_id} » existe déjà.")

        self.write_link({**old_link, "id": new_id})
        # Les cartes sont mises à jour avant la suppression de l'ancien
        # lien : delete_link() ne doit plus en trouver à nettoyer.
        updated = 0
        for card_id in self._cards_referencing_link(old_id):
            card = self.load_json(card_id)
            new_list = _clean_id_list(
                new_id if x == old_id else x for x in (card.get("links") or [])
            )
            card["links"] = new_list
            self.write_json(card)
            updated += 1
        for poi_id in self._pois_referencing_link(old_id):
            poi = self.get_poi(poi_id)
            if poi is None:
                continue
            poi["links"] = _clean_id_list(
                new_id if x == old_id else x for x in (poi.get("links") or [])
            )
            self.write_poi(poi)
        self.delete_link(old_id)

        logger.info(
            "Lien renommé : %s -> %s (%d carte(s) mise(s) à jour)",
            old_id, new_id, updated,
        )
        return updated

    def sync_links(self) -> int:
        """Synchronise links.json → SQLite.

        Insère ou remplace dans SQLite chaque lien présent dans
        links.json (les entrées invalides, sans URL par exemple, sont
        ignorées avec un avertissement). Retourne le nombre de liens
        écrits.
        """
        links = self._read_links_json()
        if not links:
            return 0
        conn = self._get_conn()
        count = 0
        for link_id, link_data in links.items():
            try:
                row = _link_to_row({**link_data, "id": link_id})
            except ValueError as exc:
                logger.warning("sync_links : lien %s ignoré (%s)", link_id, exc)
                continue
            cols = ", ".join(row.keys())
            placeholders = ", ".join(f":{k}" for k in row)
            conn.execute(
                f"INSERT OR REPLACE INTO links ({cols}) VALUES ({placeholders})",
                row,
            )
            count += 1
        conn.commit()
        logger.info("sync_links : %d lien(s) synchronisé(s)", count)
        return count

    # ------------------------------------------------------------------
    # Liens affichés / dates effectives d'une carte
    # ------------------------------------------------------------------

    def resolve_card_links(self, card: dict) -> list[dict]:
        """Liste à plat, sans distinction d'origine, des liens à afficher
        pour une carte : ceux de la carte (``card["links"]``) puis ceux de
        chacun de ses POIs (``poi["links"]``), dans cet ordre, sans
        doublon (un même lien référencé deux fois n'apparaît qu'une fois).

        Chaque élément est le dict du lien (``{id, title, description,
        url, check_date, mdate}``). Les identifiants inconnus sont
        ignorés. Un POI est pris en compte même s'il n'a pas d'autre
        carte associée.
        """
        ids: list[str] = list(_clean_id_list(card.get("links")))
        for poi_id in card.get("poi") or []:
            poi = self.get_poi(str(poi_id))
            if poi is not None:
                ids.extend(poi.get("links") or [])
        result = []
        for link_id in _clean_id_list(ids):
            link = self.get_link(link_id)
            if link is not None:
                result.append(link)
        return result

    def effective_mdates(self, cards: list[dict]) -> dict[str, int]:
        """Date de dernière modification *visible* de chaque carte
        (``{card_id: timestamp}``) : le plus récent de son propre
        ``mdate`` et des ``mdate`` des liens de la carte, de ses POIs et
        des liens de ces POIs.

        Ajouter ou modifier un lien/POI change la page des cartes qui le
        référencent sans toucher à leur JSON : cette date sert donc de
        ``<lastmod>`` au sitemap et de seuil de détection à IndexNow
        (flpostcards.indexnow_watch).
        """
        link_md = {l["id"]: int(l.get("mdate") or 0) for l in self.list_links()}
        pois = {p["id"]: p for p in self.list_pois()}
        result: dict[str, int] = {}
        for card in cards:
            md = int(card.get("mdate") or 0)
            for link_id in card.get("links") or []:
                md = max(md, link_md.get(str(link_id), 0))
            for poi_id in card.get("poi") or []:
                poi = pois.get(str(poi_id))
                if poi is None:
                    continue
                md = max(md, int(poi.get("mdate") or 0))
                for link_id in poi.get("links") or []:
                    md = max(md, link_md.get(str(link_id), 0))
            result[str(card["id"])] = md
        return result

    # ------------------------------------------------------------------
    # Auths
    # ------------------------------------------------------------------

    def get_auth(self, email: str) -> dict | None:
        """Return an auth entry from SQLite, or None if absent.

        The ``auth`` field contains the PBKDF2 hash, not the original
        password. Use :meth:`check_auth` to verify a plain-text password.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM auths WHERE email = ?", (email,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_auths(self) -> list[dict]:
        """Return all auth entries (email only — hash not exposed)."""
        conn = self._get_conn()
        cur = conn.execute("SELECT email FROM auths ORDER BY email")
        return [{"email": row["email"]} for row in cur.fetchall()]

    def write_auth(self, email: str, password: str) -> None:
        """Hash *password* with PBKDF2 and store it for *email*.

        Raises :class:`ValueError` if *password* is empty.
        """
        if not password:
            raise ValueError("Password must not be empty")
        hashed = _hash_password(password)
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO auths (email, auth) VALUES (?, ?)",
            (email, hashed),
        )
        conn.commit()
        logger.info("write_auth : entrée mise à jour pour %s", email)

    def check_auth(self, email: str, password: str) -> bool:
        """Return True if *password* matches the stored hash for *email*.

        Always returns False if *password* is empty, if *email* does not
        exist, or if the stored value cannot be parsed as a PBKDF2 hash.
        """
        if not password:
            return False
        entry = self.get_auth(email)
        if entry is None:
            return False
        return _verify_password(password, entry.get("auth") or "")

    def delete_auth(self, email: str) -> bool:
        """Delete an auth entry. Returns True if it existed."""
        conn = self._get_conn()
        cur = conn.execute("SELECT 1 FROM auths WHERE email = ?", (email,))
        existed = cur.fetchone() is not None
        conn.execute("DELETE FROM auths WHERE email = ?", (email,))
        conn.commit()
        return existed

    # ------------------------------------------------------------------
    # Refresh tokens (auth JWT — voir flpostcards/auth.py)
    # ------------------------------------------------------------------
    # Un refresh token est une chaîne aléatoire opaque, longue durée,
    # dont seul le hash SHA-256 est stocké ici (jamais le token en
    # clair) : permet de vérifier sa validité sans pouvoir le
    # reconstituer à partir de la base, et de le révoquer à tout moment
    # (déconnexion à distance, téléphone volé) — ce qu'un JWT stateless
    # seul ne permet pas. L'access token (JWT signé, courte durée) n'a
    # lui aucune trace en base : sa validité tient uniquement à sa
    # signature et à son expiration.

    def create_refresh_token(
        self,
        email: str,
        expires_at: int,
        device_info: str | None = None,
    ) -> str:
        """
        Génère un nouveau refresh token pour ``email``, stocke son hash
        SHA-256 en base, et retourne le token en clair — à transmettre
        au client immédiatement : il n'est jamais stocké en clair côté
        serveur et ne peut donc plus être récupéré ensuite.
        """
        token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = int(time.time())
        conn = self._get_refresh_conn()
        conn.execute(
            "INSERT INTO refresh_tokens "
            "(email, token_hash, created_at, expires_at, device_info) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, token_hash, now, expires_at, device_info),
        )
        conn.commit()
        return token

    def verify_refresh_token(self, token: str) -> dict | None:
        """
        Retourne l'entrée ``refresh_tokens`` correspondant à ``token``
        si elle existe, n'est pas expirée et n'a pas été révoquée.
        Retourne ``None`` dans tous les autres cas (token inconnu,
        expiré, révoqué, ou vide).
        """
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = self._get_refresh_conn()
        cur = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        entry = dict(row)
        if entry["revoked_at"] is not None:
            return None
        if entry["expires_at"] <= int(time.time()):
            return None
        return entry

    def revoke_refresh_token(self, token: str) -> bool:
        """
        Révoque un refresh token (déconnexion de cet appareil).
        Retourne True s'il existait et n'était pas déjà révoqué.
        """
        if not token:
            return False
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = self._get_refresh_conn()
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (int(time.time()), token_hash),
        )
        conn.commit()
        return cur.rowcount > 0

    def revoke_all_refresh_tokens(self, email: str) -> int:
        """
        Révoque tous les refresh tokens actifs de ``email`` (déconnexion
        de tous les appareils, ex : téléphone volé). Retourne le nombre
        de tokens révoqués.
        """
        conn = self._get_refresh_conn()
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked_at = ? "
            "WHERE email = ? AND revoked_at IS NULL",
            (int(time.time()), email),
        )
        conn.commit()
        return cur.rowcount

    def purge_expired_refresh_tokens(self, grace_days: int = 0) -> int:
        """
        Supprime définitivement les refresh tokens expirés, ou révoqués
        depuis plus de ``grace_days`` jours (nettoyage périodique
        optionnel, pour éviter que la table ne grossisse indéfiniment —
        à appeler par exemple depuis une tâche cron/script de
        maintenance, pas automatiquement à chaque requête). Retourne le
        nombre de lignes supprimées.
        """
        cutoff = int(time.time()) - grace_days * 86400
        conn = self._get_refresh_conn()
        cur = conn.execute(
            "DELETE FROM refresh_tokens WHERE expires_at <= ? "
            "OR (revoked_at IS NOT NULL AND revoked_at <= ?)",
            (cutoff, cutoff),
        )
        conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # Updates (updates.json)
    # ------------------------------------------------------------------

    def read_updates(self) -> list[dict]:
        """Read updates.json and return the list of update entries.

        Each entry has the shape:
          {"email": str, "password": str, "card_id": str,
           "lat": float, "lon": float}

        Returns an empty list if the file is absent, unreadable or invalid.
        Empty-password entries are silently skipped (authentication would
        always fail for them).
        """
        try:
            with self.updates_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                return []
            return [e for e in data if isinstance(e, dict) and e.get("password")]
        except (OSError, json.JSONDecodeError):
            return []

    def _write_updates(self, entries: list[dict]) -> None:
        """Atomically write the updates list back to updates.json."""
        tmp = self.updates_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.updates_json)

    def updates_for_card(self, card_id: str | int) -> list[dict]:
        """Return all update entries for a given card id.

        Only entries whose password matches a stored auth are returned;
        entries with an unknown email or a wrong password are silently
        ignored.
        """
        cid = str(card_id)
        result = []
        for entry in self.read_updates():
            if str(entry.get("card_id")) != cid:
                continue
            if not self.check_auth(entry.get("email", ""), entry.get("password", "")):
                continue
            result.append(entry)
        return result

    def apply_update_gps(self, entry: dict) -> bool:
        """Apply the GPS coordinates from an update entry to the card.

        Updates both the JSON file and the SQLite database.
        Returns True if the card was found and updated.
        """
        card_id = str(entry.get("card_id", ""))
        lat = entry.get("lat")
        lon = entry.get("lon")
        if not card_id or lat is None or lon is None:
            return False
        card = self.load_json(card_id)
        card["coord"] = [float(lat), float(lon)]
        self.write_json(card)
        logger.info("apply_update_gps : carte %s → [%s, %s]", card_id, lat, lon)
        return True

    def delete_update(self, email: str, card_id: str | int) -> bool:
        """Remove all entries matching (email, card_id) from updates.json.

        Returns True if at least one entry was removed.
        """
        cid = str(card_id)
        entries = self.read_updates()
        filtered = [e for e in entries
                    if not (str(e.get("card_id")) == cid and e.get("email") == email)]
        removed = len(entries) - len(filtered)
        if removed:
            if filtered:
                self._write_updates(filtered)
            else:
                # Empty list: remove the file entirely to keep things clean
                try:
                    self.updates_json.unlink()
                except OSError:
                    pass
            logger.info("delete_update : %d entrée(s) supprimée(s) pour carte %s / %s",
                        removed, cid, email)
        return bool(removed)
