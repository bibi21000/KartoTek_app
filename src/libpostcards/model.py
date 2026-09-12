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

import hashlib
import json
import logging
import secrets
import sqlite3
import time
import unicodedata
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
    status      TEXT NOT NULL DEFAULT 'active'  -- active | exchanged | trade
);
"""

# Valeurs autorisées pour le champ ``status`` d'une carte.
CARD_STATUSES = ("active", "exchanged", "trade")
DEFAULT_CARD_STATUS = "active"

_DDL_TRAVELS = """
CREATE TABLE IF NOT EXISTS travels (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    title2      TEXT,
    distance_m  INTEGER,
    distance_km REAL,
    start_lat   REAL,
    start_lon   REAL,
    end_lat     REAL,
    end_lon     REAL,
    count       INTEGER,
    cards       TEXT,       -- JSON array sérialisé [{id, title}, ...]
    mdate       INTEGER,    -- timestamp UNIX de dernière modification de "cards"
    position    INTEGER NOT NULL DEFAULT 0  -- ordre d'affichage, voir travels.json
);
"""

_DDL_POIS = """
CREATE TABLE IF NOT EXISTS pois (
    id          TEXT PRIMARY KEY,
    description TEXT,
    coord_lat   REAL,
    coord_lon   REAL
);
"""

_DDL_COLLECTIONS = """
CREATE TABLE IF NOT EXISTS collections (
    name         TEXT PRIMARY KEY,
    position     INTEGER NOT NULL,   -- ordre d'affichage dans "collections"
    map_position INTEGER             -- ordre dans "collections_map", NULL si absente du sous-ensemble
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
    "status": DEFAULT_CARD_STATUS,
}


# ---------------------------------------------------------------------------
# Helpers de (dé)sérialisation
# ---------------------------------------------------------------------------

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
        "status": card.get("status") or DEFAULT_CARD_STATUS,
    }


def _row_to_card(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict carte."""
    d = dict(row)
    lat, lon = d.pop("coord_lat", None), d.pop("coord_lon", None)
    d["coord"] = [lat, lon] if (lat is not None and lon is not None) else None
    for field in ("address", "poi", "collections", "doubles"):
        raw = d.get(field)
        d[field] = json.loads(raw) if raw else []
    return d


def _travel_to_row(travel: dict) -> dict:
    """Convertit un dict trajet en ligne SQL (champs plats)."""
    start = travel.get("start") or []
    end = travel.get("end") or []
    return {
        "id": str(travel["id"]),
        "title": travel.get("title"),
        "title2": travel.get("title2"),
        "distance_m": travel.get("distance_m"),
        "distance_km": travel.get("distance_km"),
        "start_lat": start[0] if len(start) > 0 else None,
        "start_lon": start[1] if len(start) > 1 else None,
        "end_lat": end[0] if len(end) > 0 else None,
        "end_lon": end[1] if len(end) > 1 else None,
        "count": travel.get("count"),
        "cards": json.dumps(travel.get("cards") or [], ensure_ascii=False),
        "mdate": travel.get("mdate"),
        "position": travel.get("position") or 0,
    }


def _row_to_travel(row: sqlite3.Row) -> dict:
    """Convertit une ligne SQL en dict trajet."""
    d = dict(row)
    d["start"] = [d.pop("start_lat"), d.pop("start_lon")]
    d["end"] = [d.pop("end_lat"), d.pop("end_lon")]
    raw_cards = d.get("cards")
    d["cards"] = json.loads(raw_cards) if raw_cards else []
    return d


def _backfill_travel_positions(travels: dict) -> bool:
    """
    S'assure que chaque entrée de ``travels`` (dict ``travel_id ->
    {...}``, format travels.json) a un champ ``position`` numérique.

    Les entrées qui n'en ont pas encore (travels.json antérieur à
    l'introduction de ce champ) reçoivent une position stable, à la
    suite des positions déjà attribuées explicitement -- départagées
    entre elles par id (ordre alphabétique, celui qu'affichait
    tkmanager avant les boutons +/-), pas une position recalculée
    séparément à chaque entrée au fil de l'eau : c'est justement ce
    qui causait des sauts d'ordre incohérents en éditant un trajet
    sans toucher à son rang (voir read_travels_json/write_travel_json).

    Modifie ``travels`` EN PLACE. Retourne True si au moins une entrée
    a été complétée (l'appelant sait alors qu'il doit persister le
    résultat).
    """
    missing = sorted(
        tid for tid, t in travels.items()
        if not isinstance(t.get("position"), (int, float))
    )
    if not missing:
        return False
    existing_positions = [
        t.get("position") for t in travels.values()
        if isinstance(t.get("position"), (int, float))
    ]
    next_position = (max(existing_positions) + 1) if existing_positions else 0
    for tid in missing:
        travels[tid]["position"] = next_position
        next_position += 1
    return True


def _poi_to_row(poi: dict) -> dict:
    """Convertit un dict POI en ligne SQL (champs plats)."""
    coord = poi.get("coord") or []
    return {
        "id": str(poi["id"]),
        "description": poi.get("description"),
        "coord_lat": coord[0] if len(coord) > 0 else None,
        "coord_lon": coord[1] if len(coord) > 1 else None,
    }


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
        self.collections_json = self.datadir / "collections.json"
        self.updates_json  = self.datadir / "updates.json"
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
            self._conn.executescript(_DDL_AUTHS + _DDL_COLLECTIONS)
            self._conn.commit()

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

            # Ajout défensif de travels.position (ordre d'affichage, voir
            # travels.json / reorder_travels_json) sur une base existante
            # créée avant son introduction. DEFAULT 0 : les trajets
            # existants se retrouvent tous à la même position tant qu'ils
            # n'ont pas été explicitement réordonnés (list_travels() les
            # départage alors par id, voir plus bas).
            try:
                self._conn.execute(
                    "ALTER TABLE travels ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
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

            # Ajout défensif de cards.detected_content / detected_objects sur
            # une base existante créée avant leur introduction (CREATE TABLE
            # IF NOT EXISTS, ci-dessus, ne modifie pas une table déjà
            # présente sans ces colonnes).
            for _col in ("detected_content", "detected_objects"):
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

    def read_travel(self, travel_id: str) -> dict | None:
        """
        Lit un trajet depuis la base SQLite.

        Retourne None si l'id n'existe pas.
        """
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM travels WHERE id = ?", (travel_id,))
        row = cur.fetchone()
        return _row_to_travel(row) if row else None

    def list_travels(self) -> list[dict]:
        """Retourne la liste de tous les trajets, dans l'ordre d'affichage
        (colonne ``position``, voir travels.json / reorder_travels_json —
        c'est cet ordre que suit la page /travel/ de flpostcards). ``id``
        en second critère pour départager les trajets à égalité de
        position (ex : tous à la position par défaut 0, base migrée
        avant l'introduction de ce champ, jamais réordonnée depuis)."""
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM travels ORDER BY position, id")
        return [_row_to_travel(r) for r in cur.fetchall()]

    def write_travel(self, travel: dict) -> None:
        """
        Écrit (INSERT OR REPLACE) un trajet dans la base SQLite.

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
        """Supprime un trajet de la base."""
        conn = self._get_conn()
        conn.execute("DELETE FROM travels WHERE id = ?", (travel_id,))
        conn.commit()

    def reorder_travels(self, ordered_ids: list[str]) -> None:
        """
        Met à jour uniquement la colonne ``position`` de la table SQL
        ``travels`` (contrairement à reorder_travels_json, qui met à
        jour travels.json -- la source de vérité persistante).

        À quoi ça sert : tkmanager (boutons +/- de TravelManagerView)
        appelle les deux à la suite, pour que le nouvel ordre soit
        visible sur /travel/ de flpostcards tout de suite, sans
        attendre la prochaine régénération complète des trajets
        (ParcoursCartes.travels(), qui recalcule aussi distance/cartes
        via ortools -- coûteux, inutile pour un simple changement
        d'ordre). Cette mise à jour SQL n'est cependant que le
        raccourci immédiat : c'est bien travels.json (reorder_travels_json)
        qui reste la source de vérité, reprise à la prochaine
        régénération.

        Les ids de ``ordered_ids`` absents de la table (trajet jamais
        encore calculé) sont ignorés silencieusement (UPDATE ... WHERE
        id = ? ne fait rien s'il ne matche aucune ligne) : leur
        position sera reprise depuis travels.json à son premier calcul.
        """
        conn = self._get_conn()
        conn.executemany(
            "UPDATE travels SET position = ? WHERE id = ?",
            [(position, travel_id) for position, travel_id in enumerate(ordered_ids)],
        )
        conn.commit()

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
        conn.executescript(_DDL_CARDS + _DDL_TRAVELS + _DDL_POIS + _DDL_COLLECTIONS + _DDL_AUTHS + _DDL_INDEXES)
        conn.commit()
        logger.info("Base créée : %s", self.db_path)

        # Import des collections depuis collections.json et des POIs
        # depuis pois.json (avant les cartes, pour que _ensure_poi
        # puisse ignorer les ids déjà pleinement décrits dans pois.json)
        self.sync_collections()
        self.sync_pois()

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
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?)"
                # Contenu détecté automatiquement (BLIP + DETR sur le
                # recto, voir tkpostcards.libs.detection) : inclus dans
                # la recherche textuelle au même titre que les champs
                # saisis/OCR'isés manuellement.
                " OR unaccent_lower(detected_content) LIKE unaccent_lower(?)"
                " OR unaccent_lower(detected_objects) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 9)

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
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?)"
                # Contenu détecté automatiquement (BLIP + DETR sur le
                # recto, voir tkpostcards.libs.detection) : inclus dans
                # la recherche textuelle au même titre que les champs
                # saisis/OCR'isés manuellement.
                " OR unaccent_lower(detected_content) LIKE unaccent_lower(?)"
                " OR unaccent_lower(detected_objects) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 9)

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
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?)"
                # Contenu détecté automatiquement (BLIP + DETR sur le
                # recto, voir tkpostcards.libs.detection) : inclus dans
                # la recherche textuelle au même titre que les champs
                # saisis/OCR'isés manuellement.
                " OR unaccent_lower(detected_content) LIKE unaccent_lower(?)"
                " OR unaccent_lower(detected_objects) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 9)

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
            like = f"%{search}%"
            conditions.append(
                "(unaccent_lower(title) LIKE unaccent_lower(?)"
                " OR unaccent_lower(title2) LIKE unaccent_lower(?)"
                " OR unaccent_lower(description) LIKE unaccent_lower(?)"
                " OR unaccent_lower(verso_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(recto_text) LIKE unaccent_lower(?)"
                " OR unaccent_lower(address) LIKE unaccent_lower(?)"
                " OR unaccent_lower(poi) LIKE unaccent_lower(?)"
                # Contenu détecté automatiquement (BLIP + DETR sur le
                # recto, voir tkpostcards.libs.detection) : inclus dans
                # la recherche textuelle au même titre que les champs
                # saisis/OCR'isés manuellement.
                " OR unaccent_lower(detected_content) LIKE unaccent_lower(?)"
                " OR unaccent_lower(detected_objects) LIKE unaccent_lower(?))"
            )
            params.extend([like] * 9)

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

    def _read_collections_json(self) -> dict:
        """Read collections.json and return the raw dict
        ``{"collections": [...], "collections_map": [...]}``.

        Returns an empty dict if the file is absent or unreadable.
        """
        try:
            with self.collections_json.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_collections_json(
        self, collections: list[str], collections_map: list[str]
    ) -> None:
        """Atomically write collections.json."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        data = {
            "collections": list(collections),
            "collections_map": list(collections_map),
        }
        tmp = self.collections_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.collections_json)

    def _write_collections_db(
        self, collections: list[str], collections_map: list[str]
    ) -> None:
        """Replace the content of the SQLite ``collections`` table with
        ``collections``/``collections_map`` (full rewrite, order preserved
        via the ``position``/``map_position`` columns).
        """
        conn = self._get_conn()
        map_positions = {name: i for i, name in enumerate(collections_map)}
        rows = [
            (name, position, map_positions.get(name))
            for position, name in enumerate(collections)
        ]
        conn.execute("DELETE FROM collections")
        if rows:
            conn.executemany(
                "INSERT INTO collections (name, position, map_position) "
                "VALUES (?, ?, ?)",
                rows,
            )
        conn.commit()

    def get_collections(self) -> tuple[list[str], list[str]]:
        """Return ``(collections, collections_map)`` from SQLite.

        ``collections.json`` reste la source de vérité (modifiable via
        tkmanager, synchronisée vers le serveur distant par
        ``tkpostcards.libs.publish``) ; la table SQLite ``collections``
        en est une copie tenue à jour par ``set_collections()`` /
        ``sync_collections()``, utilisée ici en lecture pour éviter de
        réouvrir et reparser le JSON à chaque requête (même principe que
        les POIs : ``pois.json`` / table ``pois``).

        Si ``collections_map`` est vide, retombe sur la liste complète
        des collections.
        """
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT name, map_position FROM collections ORDER BY position"
        )
        rows = cur.fetchall()
        collections = [row["name"] for row in rows]
        collections_map = [
            row["name"]
            for row in sorted(
                (r for r in rows if r["map_position"] is not None),
                key=lambda r: r["map_position"],
            )
        ]
        if not collections_map:
            collections_map = list(collections)
        return collections, collections_map

    def set_collections(
        self, collections: list[str], collections_map: list[str]
    ) -> None:
        """Write collections.json (source de vérité) puis recopie le
        résultat dans la table SQLite ``collections``.
        """
        self._write_collections_json(collections, collections_map)
        self._write_collections_db(collections, collections_map)

    def sync_collections(self) -> int:
        """Synchronise collections.json → SQLite (recopie complète).

        Appelée par ``generate()`` (comme ``sync_pois()``) pour peupler
        la table ``collections`` depuis le JSON lors d'une régénération
        complète de la base. Retourne le nombre de collections importées.
        """
        data = self._read_collections_json()
        collections = [str(c) for c in (data.get("collections") or [])]
        collections_map = [str(c) for c in (data.get("collections_map") or [])]
        self._write_collections_db(collections, collections_map)
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

        The JSON file is updated atomically after the SQLite write.
        """
        row = _poi_to_row(poi)
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


    # ------------------------------------------------------------------
    # Travel models (travels.json only — no SQLite)
    # ------------------------------------------------------------------
    # travels.json format:
    # {
    #   "seille": {
    #     "id": "seille",
    #     "title": "La Seille de sa source à la Saône",
    #     "title2": null,
    #     "start": [46.697018, 5.657401],
    #     "collection": "Seille",
    #     "position": 0
    #   },
    #   ...
    # }

    def read_travels_json(self) -> dict:
        """Read travels.json and return {travel_id: {...}}.

        Accepts both the dict format ``{id: {...}}`` and the legacy list
        format ``[{...}, ...]`` (converting it automatically).
        Returns an empty dict if the file is absent or unreadable.

        Comble aussi défensivement le champ ``position`` (ordre
        d'affichage) des entrées qui n'en ont pas encore (travels.json
        antérieur à l'introduction de ce champ) : elles reçoivent une
        position stable, à la suite des positions déjà attribuées, en
        les départageant par id -- sans ce comblement, chaque entrée
        manquante recevait sa position au coup par coup, à la première
        occasion (write_travel_json), ce qui la faisait sauter en
        position 0 ou en doublon avec une autre au lieu de rester à sa
        place, y compris en l'éditant simplement (sans vouloir changer
        l'ordre). Persisté immédiatement si au moins une entrée a été
        complétée, pour que ce comblement ne se reproduise qu'une fois.
        """
        if not self.travels_json.exists():
            return {}
        with self.travels_json.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            result = data
        elif isinstance(data, list):
            # Convert list → dict, using the "id" field as key
            result = {}
            for entry in data:
                if isinstance(entry, dict) and entry.get("id"):
                    result[str(entry["id"])] = entry
        else:
            return {}

        if _backfill_travel_positions(result):
            self._write_travels_json(result)
            logger.info(
                "read_travels_json : position manquante complétée pour "
                "un ou plusieurs trajets"
            )
        return result

    def _write_travels_json(self, travels: dict) -> None:
        """Atomically write {travel_id: {...}} to travels.json."""
        self.datadir.mkdir(parents=True, exist_ok=True)
        tmp = self.travels_json.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(travels, fh, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.travels_json)

    def write_travel_json(self, travel: dict) -> None:
        """Insert or replace a travel model entry in travels.json.

        The ``id`` field is required. All other fields are optional.

        ``position`` (ordre d'affichage sur la page /travel/ de
        flpostcards, voir reorder_travels_json) est géré ici plutôt que
        laissé à l'appelant : si ``travel`` n'en fournit pas (c'est le
        cas du formulaire TravelManagerView dans tkmanager, qui n'édite
        que title/title2/collection/start — la position ne se change
        que via les boutons +/-, voir reorder_travels_json), la position
        déjà en place est conservée pour une entrée existante, ou la
        nouvelle entrée est placée en fin de liste.
        """
        travel_id = str(travel.get("id", "")).strip()
        if not travel_id:
            raise ValueError("Travel id must not be empty")
        travels = self.read_travels_json()
        entry = dict(travel)
        entry["id"] = travel_id
        if entry.get("position") is None:
            existing = travels.get(travel_id)
            if existing is not None and existing.get("position") is not None:
                entry["position"] = existing["position"]
            else:
                positions = [
                    t.get("position") for t in travels.values()
                    if isinstance(t.get("position"), (int, float))
                ]
                entry["position"] = (max(positions) + 1) if positions else 0
        travels[travel_id] = entry
        self._write_travels_json(travels)
        logger.info("write_travel_json : trajet %s enregistré", travel_id)

    def reorder_travels_json(self, ordered_ids: list[str]) -> None:
        """
        Redéfinit l'ordre d'affichage (champ ``position``) des trajets
        dans travels.json : ``ordered_ids`` doit contenir l'identifiant
        de CHAQUE trajet existant, dans l'ordre d'affichage voulu (même
        principe que get_collections/set_collections pour les
        collections).

        Utilisé par tkmanager pour les boutons +/- de réorganisation
        (TravelManagerView) : après un échange local dans la liste
        affichée, la liste complète des ids dans le nouvel ordre est
        repassée ici en une fois.

        Le nouvel ordre n'atteint la page /travel/ de flpostcards
        qu'après la prochaine régénération des trajets (ParcoursCartes.
        travels(), voir "tktools similar travels"/scheduled job — cette
        méthode ne touche que travels.json, pas la table SQL "travels"
        que list_travels() interroge).

        Lève ValueError si ``ordered_ids`` ne correspond pas exactement
        aux trajets existants.
        """
        travels = self.read_travels_json()
        if set(ordered_ids) != set(travels.keys()):
            raise ValueError(
                "reorder_travels_json: ordered_ids ne correspond pas "
                "exactement aux trajets existants"
            )
        for position, travel_id in enumerate(ordered_ids):
            travels[travel_id]["position"] = position
        self._write_travels_json(travels)
        logger.info(
            "reorder_travels_json : nouvel ordre enregistré (%d trajets)",
            len(travels),
        )

    def delete_travel_json(self, travel_id: str) -> bool:
        """Remove a travel model entry from travels.json.

        Returns True if it existed.
        """
        travels = self.read_travels_json()
        if travel_id not in travels:
            return False
        del travels[travel_id]
        if travels:
            self._write_travels_json(travels)
        else:
            try:
                self.travels_json.unlink()
            except OSError:
                pass
        logger.info("delete_travel_json : trajet %s supprimé", travel_id)
        return True
