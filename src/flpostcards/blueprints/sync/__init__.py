"""
Blueprint « sync » de flpostcards : réception de fichiers par HTTP(S), pour le
transport ``protocol = https`` de ``tkpostcards.libs.remotesync``.

Les fichiers sont écrits directement dans le DATADIR de l'appli (celui que
flpostcards lit : postcards.sqlite, collections.json, images...) et l'appli
peut être redémarrée par le même canal une fois la publication terminée.

Activé par ``[flask] sync_enabled = true`` (voir postcards.conf.sample) ;
désactivé -- et non enregistré du tout -- par défaut : c'est un point d'entrée
en ÉCRITURE sur le disque.

Pourquoi un protocole dédié plutôt que « un PUT par fichier » ?
---------------------------------------------------------------
Une publication, ce sont des dizaines de milliers de petits fichiers (vignettes
JPEG) plus quelques gros (postcards.sqlite). Avec HTTP, ce qui coûte, c'est
le nombre d'allers-retours, pas le volume. Comme SFTP le fait avec ses
connexions persistantes et ses lots, ce protocole minimise les allers-retours :

  * ``GET  /list``   : tout l'inventaire distant (taille + mtime) en UNE requête
                       (parcours fait côté serveur, réponse gzip). Le client
                       calcule seul ce qu'il faut envoyer : plus de stat/MDTM
                       par fichier, plus de listing répertoire par répertoire.
  * ``POST /put``    : un LOT de fichiers par requête (trames concaténées, voir
                       ci-dessous). Les répertoires sont créés ici, à la volée :
                       plus de mkdir séparé.
  * Gros fichiers découpés en parties (``o``/``t``), envoyées en parallèle sur
    plusieurs connexions, puis assemblées par ``POST /commit``. Aucune requête
    ne dépasse ``sync_max_body_mb``, quelle que soit la taille du fichier.
  * Chaque fichier est écrit dans ``<datadir>/.sync-staging/`` puis ``rename()``
    sur sa destination : jamais de fichier à moitié écrit visible par l'appli
    (important pour postcards.sqlite : ``Model._get_conn`` détecte le
    changement d'inode et se reconnecte tout seul), mtime positionné à celui
    du poste (la comparaison de mtime fonctionne donc d'un run à l'autre).
  * Compression zlib par fichier, uniquement pour les types compressibles
    (json, sqlite, pkl, texte...) : inutile sur du JPEG.
  * CRC32 de chaque fichier/partie vérifié à la réception.

Authentification : ``Authorization: Bearer <sync_token>`` sur chaque requête
(comparaison en temps constant). 10 échecs en 60 s depuis une même IP => 429
pendant le reste de la minute. L'IP est ``request.remote_addr`` tel que
corrigé par le ProxyFix de l'appli : ``[flask] trusted_proxies`` doit donc
valoir le nombre de proxys réellement placés devant flpostcards.

Deux chemins d'accès, un seul serveur
-------------------------------------
  * LAN : le client parle directement à flpostcards, en HTTP simple
    (``host = http://...`` + ``allow_http = true`` côté client). Acceptable sur
    un réseau de confiance seulement : le jeton circule en clair.
  * Internet : nginx (TLS) -> passerelle (toolspostcards, clé API par niveau)
    -> flpostcards. Le jeton reste vérifié ICI quoi qu'il arrive (défense en
    profondeur ; la passerelle doit transmettre l'en-tête ``Authorization`` tel
    quel). La clé API (``X-API-Key`` côté client) n'est ni lue ni vérifiée par
    ce blueprint : c'est l'affaire de la passerelle.

Ce site étant public, le reverse proxy qui l'expose à Internet doit ne laisser
atteindre ``<sync_url_prefix>/`` qu'à la passerelle (ou au LAN) : le jeton
protège, mais mieux vaut ne pas exposer ce point d'entrée à tous.

Ce que la passerelle doit laisser passer (tout sous ``sync_url_prefix``) :
méthodes GET, POST, PUT et DELETE ; corps de requête jusqu'à ``sync_max_body_mb``
(nginx : ``client_max_body_size``, 1 Mo par défaut ! sinon régler
``max_request_mb`` côté client) ; ``Content-Length`` conservé (pas de
réécriture en chunked) ; délais suffisants pour un lot de plusieurs Mo.
Niveaux d'accès suggérés : tout sauf ``POST /restart`` = « hébergement » ;
``POST /restart`` mérite un niveau plus strict (ou n'être joignable que depuis
le LAN) puisqu'il redémarre l'appli web.

Confinement : tout chemin est résolu sous le DATADIR ; ``..``, chemins
absolus hors base, octets nuls et liens symboliques sortant de la base sont
refusés, ainsi que le répertoire interne ``.sync-staging``. Les données
VIVANTES du serveur sont en outre protégées (ni lues, ni écrites, ni
supprimées, ni listées -- donc jamais prises pour des « orphelins ») :
``refresh_tokens.sqlite*`` (sessions des utilisateurs), ``push_watch_state.json``,
``indexnow_watch_state.json`` et le répertoire de cache ; ``[flask]
sync_protected`` en ajoute d'autres (motifs fnmatch sur le 1er composant).

Format d'une requête ``POST <prefix>/put?root=<dir>&session=<hex>`` (corps binaire) ::

    repeat:
        uint32 BE  N              longueur de l'en-tête (0 = fin du corps)
        N octets   JSON UTF-8     {"p": chemin relatif à root, "m": mtime,
                                   "o": offset, "t": taille totale du fichier,
                                   "u": taille décompressée, "s": taille
                                   stockée, "z": 0|1 (zlib), "c": crc32}
        s octets                  données (éventuellement compressées)

    o == 0 et t == u  : fichier complet, installé immédiatement ;
    sinon             : partie [o, o+u) du fichier, rangée en staging jusqu'à
                        ``POST /commit``.

Réponse : ``{"ok": [p, ...], "errors": [[p, message], ...]}``. Un fichier en
erreur n'empêche pas les suivants du même lot.

Autres points d'entrée (tous relatifs au préfixe, ``/api/v1/sync`` par défaut) ::

    GET    /info              capacités, limites de taille
    GET    /list?path=        inventaire récursif {"files": {rel: [taille, mtime]}}
    GET    /ls?path=          contenu direct d'un répertoire
    GET    /stat?path=        {"exists", "is_dir", "size", "mtime"}
    POST   /mkdir             {"path"}
    POST   /commit            {"root", "session", "files": [{"p","t","m"}],
                               "delete": [rel], "done": bool}
    PUT    /lock?path=        création exclusive d'un fichier vide (O_EXCL) ;
                              409 s'il existe déjà
    GET    /file?path=        téléchargement (Range/If-Modified-Since gérés)
    DELETE /file?path=        suppression d'un fichier
    POST   /restart           redémarre l'appli web (voir ci-dessous)

Redémarrage de l'appli web
--------------------------
``POST /restart`` ne prend AUCUN paramètre. Comme l'appli à redémarrer est
celle qui répond, tout se fait en interne (désactivable par ``[flask]
sync_allow_restart = false``, qui répond alors 501) :

  1. le cache JSON est purgé (``data_cache.invalidate()``) : indispensable avec
     un cache Redis partagé, que le redémarrage seul ne viderait pas ;
  2. sous gunicorn, SIGHUP est envoyé au processus maître : TOUS les workers
     sont remplacés progressivement, sans interruption de service. Attention :
     avec ``--preload``, gunicorn ne ré-exécute pas le code de l'appli au HUP
     (donc postcards.conf n'est pas relu) ; sans gunicorn, le worker courant
     reçoit SIGTERM (même mécanisme que ``/api/v1/admin/restart``), à charge du
     superviseur (Docker, systemd) de le relancer.

La réponse 202 part AVANT le redémarrage (0,5 s de délai).
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import signal
import stat as stat_mod
import struct
import threading
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, request, send_file

from flpostcards.extensions import limiter

bp = Blueprint("sync", __name__)

PROTOCOL_VERSION = 1
STAGING = ".sync-staging"

_HDR = struct.Struct(">I")
_MAX_HEADER = 64 * 1024
_SESSION_RE = re.compile(r"[0-9a-f]{16,64}")
_DEFAULT_MAX_BODY_MB = 32.0
_STAGING_TTL = 24 * 3600
_AUTH_WINDOW = 60.0
_AUTH_MAX_FAILURES = 10
# Données vivantes de flpostcards, jamais touchées par une synchronisation.
_PROTECTED_DEFAULT = (
    "refresh_tokens.sqlite*",
    "push_watch_state.json",
    "indexnow_watch_state.json",
    "cache",
)


class SyncError(Exception):
    """Erreur applicative renvoyée au client en JSON ``{"error": ...}``."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@bp.errorhandler(SyncError)
def _on_sync_error(exc: SyncError):
    return _json({"error": exc.message}, exc.status)


@bp.errorhandler(413)
def _on_too_large(_exc):
    return _json({"error": "request body too large (sync_max_body_mb)"}, 413)


# ---------------------------------------------------------------------------
# Validation de la configuration (appelée par create_app)
# ---------------------------------------------------------------------------

def validate_config(app) -> None:
    """Refuse de démarrer avec une configuration sync dangereuse ou incomplète
    (mieux vaut une erreur au démarrage qu'un point d'entrée d'écriture
    silencieusement ouvert)."""
    token = app.config.get("SYNC_TOKEN") or ""
    if token in ("", "change-me-please"):
        raise RuntimeError(
            "[flask] sync_enabled = true exige un sync_token (aléatoire, "
            "ex: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\")"
        )
    if len(token) < 16:
        raise RuntimeError("[flask] sync_token : 16 caractères minimum")
    datadir = app.config.get("DATADIR")
    if not datadir or not Path(datadir).is_dir():
        raise RuntimeError(f"[flask] sync_enabled : datadir introuvable ({datadir!r})")
    prefix = app.config.get("SYNC_URL_PREFIX") or ""
    if not prefix.startswith("/") or prefix == "/":
        raise RuntimeError("[flask] sync_url_prefix doit commencer par « / » (et n'être pas « / »)")
    app.config["SYNC_PROTECTED_PATTERNS"] = _build_protected_patterns(app)
    try:
        if float(app.config.get("SYNC_MAX_BODY_MB")) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise RuntimeError("[flask] sync_max_body_mb doit être un nombre > 0")


# ---------------------------------------------------------------------------
# Authentification et limites
# ---------------------------------------------------------------------------

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def _max_body() -> int:
    return int(float(current_app.config.get("SYNC_MAX_BODY_MB") or _DEFAULT_MAX_BODY_MB)
               * 1024 * 1024)


@bp.before_request
def _authenticate() -> None:
    token = current_app.config.get("SYNC_TOKEN") or ""
    if not token:
        raise SyncError(404, "not found")
    ip = request.remote_addr or "?"       # corrigée par ProxyFix (trusted_proxies)
    now = time.monotonic()
    with _failures_lock:
        recent = [t for t in _failures.get(ip, ()) if now - t < _AUTH_WINDOW]
        if recent:
            _failures[ip] = recent
        else:
            _failures.pop(ip, None)
        throttled = len(recent) >= _AUTH_MAX_FAILURES
    if throttled:
        raise SyncError(429, "too many failed attempts")

    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    if not (scheme.lower() == "bearer" and secrets.compare_digest(
            value.strip().encode("utf-8"), token.encode("utf-8"))):
        with _failures_lock:
            if len(_failures) > 1000:  # borne mémoire face à un balayage d'IP
                _failures.clear()
            _failures.setdefault(ip, []).append(now)
        current_app.logger.warning("sync : jeton invalide (IP=%s)", ip)
        raise SyncError(401, "unauthorized")

    # Flask n'impose aucun MAX_CONTENT_LENGTH global à flpostcards (et on n'en
    # pose pas un, qui s'appliquerait aux autres routes) : on borne ici.
    length = request.content_length
    if request.method in ("POST", "PUT") and request.endpoint == "sync.put" and length is None:
        raise SyncError(411, "Content-Length required")
    if length is not None and length > _max_body():
        raise SyncError(413, "request body too large (sync_max_body_mb)")


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

def _base() -> str:
    return os.path.realpath(current_app.config["DATADIR"])


def _build_protected_patterns(app) -> list[str]:
    """Motifs fnmatch des noms (1er composant) à ne jamais toucher : défauts +
    ``[flask] sync_protected`` + répertoire de cache s'il est dans le datadir."""
    patterns = list(_PROTECTED_DEFAULT)
    extra = app.config.get("SYNC_PROTECTED") or ""
    patterns += [p.strip() for p in extra.replace(";", ",").split(",") if p.strip()]
    cachedir = app.config.get("CACHEDIR")
    if cachedir:
        try:
            rel = os.path.relpath(os.path.realpath(cachedir),
                                  os.path.realpath(app.config["DATADIR"]))
            if not rel.startswith("..") and rel != ".":
                patterns.append(rel.split(os.sep)[0])
        except ValueError:
            pass
    return patterns


def _is_protected(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat)
               for pat in current_app.config["SYNC_PROTECTED_PATTERNS"])


def _split(rel: Any) -> list[str]:
    if not isinstance(rel, str) or "\x00" in rel or len(rel) > 4096:
        raise SyncError(400, "invalid path")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise SyncError(400, "invalid path")
    if parts and (parts[0] == STAGING or _is_protected(parts[0])):
        raise SyncError(403, "forbidden path")
    return parts


def _join(root: str, rel: str) -> str:
    return "/".join(_split(root) + _split(rel))


def _resolve(rel: Any) -> str:
    """Chemin absolu de ``rel`` sous la base, symlinks résolus et vérifiés."""
    base = _base()
    parts = _split(rel)
    full = os.path.join(base, *parts) if parts else base
    real = os.path.realpath(full)
    if real != base and not real.startswith(base + os.sep):
        raise SyncError(403, "path escapes base directory")
    # un lien symbolique interne ne doit pas mener à une donnée protégée
    if real != base and (
        os.path.relpath(real, base).split(os.sep)[0] == STAGING
        or _is_protected(os.path.relpath(real, base).split(os.sep)[0])
    ):
        raise SyncError(403, "forbidden path")
    return full


def _resolve_file(rel: Any) -> str:
    path = _resolve(rel)
    if path == _base():
        raise SyncError(400, "path designates the base directory")
    return path


def _hidden_at_top(name: str) -> bool:
    return name == STAGING or _is_protected(name)


# ---------------------------------------------------------------------------
# Réponses
# ---------------------------------------------------------------------------

def _json(obj: Any, status: int = 200) -> Response:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    resp = Response(raw, status=status, mimetype="application/json")
    if len(raw) > 1024 and "gzip" in request.headers.get("Accept-Encoding", "").lower():
        resp.set_data(gzip.compress(raw, 5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise SyncError(400, "JSON object expected")
    return data


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def _staging_dir(*sub: str) -> str:
    d = os.path.join(_base(), STAGING, *sub)
    os.makedirs(d, exist_ok=True)
    return d


def _part_path(session: str, rel: str) -> str:
    if not _SESSION_RE.fullmatch(session or ""):
        raise SyncError(400, "invalid session")
    key = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    return os.path.join(_staging_dir(session), key + ".part")


_last_gc = 0.0


def _gc_staging() -> None:
    """Supprime sessions/temporaires abandonnés (client mort en route),
    au plus une fois toutes les 10 minutes par processus."""
    global _last_gc
    now = time.time()
    if now - _last_gc < 600:
        return
    _last_gc = now
    root = os.path.join(_base(), STAGING)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for e in entries:
        try:
            if e.name == "tmp":
                for f in os.scandir(e.path):
                    if now - f.stat().st_mtime > _STAGING_TTL:
                        os.unlink(f.path)
            elif e.is_dir(follow_symlinks=False) and now - e.stat().st_mtime > _STAGING_TTL:
                shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            continue


def _install(tmp: str, dest: str, mtime: float) -> None:
    if os.path.isdir(dest):
        raise SyncError(409, "destination is a directory")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dest)


# ---------------------------------------------------------------------------
# Réception des lots
# ---------------------------------------------------------------------------

def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise SyncError(400, "truncated request body")
        buf += chunk
    return bytes(buf)


def _store_frame(root: str, session: str, hdr: dict, data: bytes) -> None:
    try:
        rel = hdr["p"]
        u = int(hdr["u"])
        z = int(hdr.get("z", 0))
        mtime = float(hdr["m"])
        offset = int(hdr.get("o", 0))
        total = int(hdr.get("t", u))
        crc = hdr.get("c")
    except (KeyError, TypeError, ValueError):
        raise SyncError(400, "malformed frame header")
    if not math.isfinite(mtime) or mtime < 0 or u < 0 or offset < 0 or total < 0:
        raise SyncError(400, "invalid frame values")

    if z:
        d = zlib.decompressobj()
        try:
            raw = d.decompress(data, u + 1)  # borne : pas de bombe de décompression
        except zlib.error as exc:
            raise SyncError(400, f"corrupt compressed data: {exc}")
        if len(raw) != u or not d.eof:
            raise SyncError(400, "decompressed size mismatch")
    else:
        raw = data
        if len(raw) != u:
            raise SyncError(400, "size mismatch")
    if crc is not None and (zlib.crc32(raw) & 0xFFFFFFFF) != int(crc):
        raise SyncError(422, "crc32 mismatch")

    full_rel = _join(root, rel)
    dest = _resolve_file(full_rel)

    if offset == 0 and total == u:  # fichier complet
        tmp = os.path.join(_staging_dir("tmp"), uuid.uuid4().hex)
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
            _install(tmp, dest, mtime)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return

    if offset + u > total:
        raise SyncError(400, "part exceeds declared total size")
    path = _part_path(session, full_rel)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


@bp.route("/put", methods=["POST"])
def put():
    root = request.args.get("root", "")
    session = request.args.get("session", "")
    _split(root)  # validation précoce
    stream = request.stream
    max_body = _max_body()
    ok: list[str] = []
    errors: list[list] = []
    while True:
        (hlen,) = _HDR.unpack(_read_exact(stream, 4))
        if hlen == 0:
            break
        if hlen > _MAX_HEADER:
            raise SyncError(400, "frame header too large")
        try:
            hdr = json.loads(_read_exact(stream, hlen))
            stored = int(hdr["s"])
        except (ValueError, KeyError, TypeError):
            raise SyncError(400, "malformed frame header")
        if not isinstance(hdr, dict) or stored < 0 or stored > max_body:
            raise SyncError(400, "invalid stored size")
        data = _read_exact(stream, stored)  # toujours consommé : le flux reste aligné
        name = hdr.get("p")
        try:
            _store_frame(root, session, hdr, data)
            ok.append(name)
        except SyncError as exc:
            errors.append([name, exc.message])
        except OSError as exc:
            errors.append([name, f"{type(exc).__name__}: {exc.strerror or exc}"])
    _gc_staging()
    return _json({"ok": ok, "errors": errors})


@bp.route("/commit", methods=["POST"])
def commit():
    body = _json_body()
    root = body.get("root", "")
    session = body.get("session", "")
    ok: list[str] = []
    errors: list[list] = []
    deleted: list[str] = []

    for item in body.get("files") or []:
        name = item.get("p") if isinstance(item, dict) else None
        try:
            total = int(item["t"])
            mtime = float(item["m"])
            full_rel = _join(root, name)
            dest = _resolve_file(full_rel)
            src = _part_path(session, full_rel)
            try:
                size = os.stat(src).st_size
            except FileNotFoundError:
                raise SyncError(404, "no staged data for this file")
            if size != total:
                raise SyncError(422, f"staged size {size} != expected {total}")
            _install(src, dest, mtime)
            ok.append(name)
        except SyncError as exc:
            errors.append([name, exc.message])
        except (KeyError, TypeError, ValueError):
            errors.append([name, "malformed commit entry"])
        except OSError as exc:
            errors.append([name, f"{type(exc).__name__}: {exc.strerror or exc}"])

    for name in body.get("delete") or []:
        try:
            path = _resolve_file(_join(root, name))
            os.remove(path)
            deleted.append(name)
        except FileNotFoundError:
            deleted.append(name)  # déjà absent : objectif atteint
        except SyncError as exc:
            errors.append([name, exc.message])
        except OSError as exc:
            errors.append([name, f"{type(exc).__name__}: {exc.strerror or exc}"])

    if body.get("done") and _SESSION_RE.fullmatch(session or ""):
        shutil.rmtree(os.path.join(_base(), STAGING, session), ignore_errors=True)
    return _json({"ok": ok, "errors": errors, "deleted": deleted})


# ---------------------------------------------------------------------------
# Inventaire et opérations unitaires
# ---------------------------------------------------------------------------

@bp.route("/info")
def info():
    return _json({
        "protocol": PROTOCOL_VERSION,
        "max_body": _max_body(),
        "compression": ["zlib"],
        "restart": bool(current_app.config.get("SYNC_ALLOW_RESTART")),
    })


@bp.route("/list")
def list_tree():
    root = _resolve(request.args.get("path", ""))
    base = _base()
    files: dict[str, list] = {}
    exists = os.path.isdir(root)
    stack = [(root, "")] if exists else []
    while stack:
        directory, prefix = stack.pop()
        try:
            it = os.scandir(directory)
        except OSError:
            continue
        with it:
            for e in it:
                if directory == base and _hidden_at_top(e.name):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, prefix + e.name + "/"))
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        files[prefix + e.name] = [st.st_size, st.st_mtime]
                except OSError:
                    continue
    return _json({"exists": exists, "files": files})


@bp.route("/ls")
def ls():
    path = _resolve(request.args.get("path", ""))
    base = _base()
    if not os.path.isdir(path):
        return _json({"exists": False, "entries": []})
    entries = []
    with os.scandir(path) as it:
        for e in it:
            if path == base and _hidden_at_top(e.name):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                mtime = None if is_dir else e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            entries.append([e.name, is_dir, mtime])
    return _json({"exists": True, "entries": entries})


@bp.route("/stat")
def stat_path():
    path = _resolve(request.args.get("path", ""))
    try:
        st = os.stat(path)
    except OSError:
        return _json({"exists": False})
    return _json({
        "exists": True,
        "is_dir": stat_mod.S_ISDIR(st.st_mode),
        "size": st.st_size,
        "mtime": st.st_mtime,
    })


@bp.route("/mkdir", methods=["POST"])
def mkdir():
    path = _resolve(_json_body().get("path", ""))
    try:
        os.makedirs(path, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise SyncError(409, "a file is in the way")
    return _json({"ok": True})


@bp.route("/lock", methods=["PUT"])
def create_lock():
    path = _resolve_file(request.args.get("path", ""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise SyncError(409, "already exists")
    os.close(fd)
    return _json({"created": True}, 201)


@bp.route("/file", methods=["GET"])
def get_file():
    path = _resolve_file(request.args.get("path", ""))
    if not os.path.isfile(path):
        raise SyncError(404, "not found")
    resp = send_file(path, mimetype="application/octet-stream",
                     conditional=True, max_age=0)
    resp.headers["X-Sync-Mtime"] = repr(os.path.getmtime(path))
    return resp


@bp.route("/file", methods=["DELETE"])
def delete_file():
    path = _resolve_file(request.args.get("path", ""))
    try:
        os.remove(path)
    except FileNotFoundError:
        raise SyncError(404, "not found")
    except (IsADirectoryError, PermissionError) as exc:
        raise SyncError(409, f"cannot delete: {exc.strerror or exc}")
    return _json({"deleted": True})


# ---------------------------------------------------------------------------
# Redémarrage de l'appli web (elle-même)
# ---------------------------------------------------------------------------

_restart_lock = threading.Lock()
_restart_pending = False
_kill = os.kill   # indirection : remplaçable dans les tests


def _gunicorn_master_pid() -> int | None:
    """PID du maître gunicorn si ce worker en est un fils direct (Linux)."""
    ppid = os.getppid()
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as fh:
            cmdline = fh.read()
    except OSError:
        return None
    return ppid if b"gunicorn" in cmdline else None


def _restart_now(log) -> None:
    global _restart_pending
    try:
        master = _gunicorn_master_pid()
        if master is not None:
            log.warning("sync : SIGHUP au maître gunicorn (pid %d) : rechargement "
                        "progressif de tous les workers", master)
            _kill(master, signal.SIGHUP)
        else:
            log.warning("sync : pas de gunicorn détecté, SIGTERM à ce worker (pid %d) ; "
                        "le superviseur doit le relancer", os.getpid())
            _kill(os.getpid(), signal.SIGTERM)
    except OSError as exc:
        log.error("sync : échec du redémarrage : %s", exc)
    finally:
        with _restart_lock:
            _restart_pending = False


@bp.route("/restart", methods=["POST"])
@limiter.limit("5 per minute")
def restart():
    global _restart_pending
    if not current_app.config.get("SYNC_ALLOW_RESTART"):
        raise SyncError(501, "restart disabled on this server (sync_allow_restart = false)")
    with _restart_lock:
        if _restart_pending:
            return _json({"status": "already-scheduled"}, 202)
        _restart_pending = True
    current_app.logger.warning("sync : redémarrage programmé (IP=%s)", request.remote_addr)
    # Purge du cache JSON (collections/pois/travels) : avec Redis partagé, le
    # redémarrage seul ne la viderait pas. Voir aussi /api/v1/admin/restart.
    from flpostcards import data_cache
    data_cache.invalidate()
    method = "gunicorn-reload" if _gunicorn_master_pid() else "worker-exit"
    timer = threading.Timer(0.5, _restart_now, args=(current_app.logger,))
    timer.daemon = True
    timer.start()
    return _json({"status": "restarting", "method": method}, 202)
