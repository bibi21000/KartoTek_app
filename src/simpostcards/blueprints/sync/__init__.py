"""
Blueprint « sync » de simpostcards : réception de fichiers par HTTPS, pour le
transport ``protocol = https`` de ``tkpostcards.libs.remotesync``.

Activé par ``[simpostcards] sync_enabled = true`` (voir postcards.conf.sample) ;
désactivé -- et non enregistré du tout -- par défaut.

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
    ne dépasse MAX_CONTENT_LENGTH, quelle que soit la taille du fichier.
  * Chaque fichier est écrit dans ``<base>/.sync-staging/`` puis ``rename()``
    sur sa destination : jamais de fichier à moitié écrit visible par
    flpostcards (important pour postcards.sqlite), mtime positionné à celui
    du poste (la comparaison de mtime fonctionne donc d'un run à l'autre).
  * Compression zlib par fichier, uniquement pour les types compressibles
    (json, sqlite, pkl, texte...) : inutile sur du JPEG.
  * CRC32 de chaque fichier/partie vérifié à la réception.

Authentification : ``Authorization: Bearer <sync_token>`` sur chaque requête
(comparaison en temps constant ; 10 échecs en 60 s depuis une même IP =>
429 pendant le reste de la minute).

Deux chemins d'accès, un seul serveur
-------------------------------------
  * LAN : le client parle directement à simpostcards, en HTTP simple
    (``host = http://...`` + ``allow_http = true`` côté client). Acceptable sur
    un réseau de confiance seulement : le jeton circule en clair.
  * Internet : nginx (TLS) -> passerelle (toolspostcards, clé API par niveau)
    -> simpostcards. Le jeton reste vérifié ICI quoi qu'il arrive (défense en
    profondeur ; la passerelle n'est pas supposée connaître ce secret mais
    doit transmettre l'en-tête ``Authorization`` tel quel). La clé API
    (en-tête ``X-API-Key`` côté client) n'est ni lue ni vérifiée par ce
    blueprint : c'est l'affaire de la passerelle.

Ce que la passerelle doit laisser passer (tout sous ``sync_url_prefix``) :
méthodes GET, POST, PUT et DELETE ; corps de requête jusqu'à
``MAX_CONTENT_LENGTH`` (nginx : ``client_max_body_size``, 1 Mo par défaut !
sinon régler ``max_request_mb`` côté client) ; ``Content-Length`` conservé
(pas de réécriture en chunked) ; délais suffisants pour un lot de plusieurs Mo.
Niveaux d'accès suggérés : tout sauf ``POST /restart`` = « hébergement » ;
``POST /restart`` mérite un niveau plus strict (ou n'être joignable que depuis
le LAN) puisqu'il redémarre l'appli web.

Derrière une passerelle, ``request.remote_addr`` est celui de la passerelle :
déclarez-la dans ``sync_trusted_proxies`` pour que le blocage des échecs
d'authentification et les journaux utilisent l'IP réelle du client
(``X-Forwarded-For``, lu seulement si la requête vient d'un proxy déclaré).

Confinement : tout chemin est résolu sous ``sync_base_dir`` ; ``..``, chemins
absolus hors base, octets nuls et liens symboliques sortant de la base sont
refusés, ainsi que le répertoire interne ``.sync-staging``.

Format d'une requête ``POST /put?root=<dir>&session=<hex>`` (corps binaire) ::

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

Autres points d'entrée (tous relatifs au préfixe, ``/sync/v1`` par défaut) ::

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
``POST /restart`` ne prend AUCUN paramètre : ce qui est exécuté est fixé par la
configuration du serveur, jamais par le client. Par ordre de priorité :

  1. ``sync_restart_command`` : commande lancée sans shell (shlex), p. ex.
     ``systemctl --user restart flpostcards`` ou ``docker restart flpostcards`` ;
  2. ``sync_restart_pidfile`` (+ ``sync_restart_signal``, HUP par défaut) :
     signal envoyé au maître gunicorn de flpostcards ;
  3. ``sync_restart_url`` (+ ``sync_restart_token``) : POST vers
     ``/api/v1/admin/restart`` de flpostcards (typiquement en réseau interne,
     ``http://flpostcards:5000/...``), qui n'a alors pas à être exposé.

La réponse 202 part AVANT le redémarrage (délai ``sync_restart_delay``, 0,5 s
par défaut) ; 501 si rien n'est configuré.
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import stat as stat_mod
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from typing import Any

from flask import Blueprint, Response, current_app, request, send_file

bp = Blueprint("sync", __name__)

PROTOCOL_VERSION = 1
STAGING = ".sync-staging"

_HDR = struct.Struct(">I")
_MAX_HEADER = 64 * 1024
_SESSION_RE = re.compile(r"[0-9a-f]{16,64}")
_DEFAULT_MAX_BODY = 25 * 1024 * 1024
_STAGING_TTL = 24 * 3600
_AUTH_WINDOW = 60.0
_AUTH_MAX_FAILURES = 10


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
    return _json({"error": "request body too large (MAX_CONTENT_LENGTH)"}, 413)


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
            "[simpostcards] sync_enabled = true exige un sync_token (aléatoire, "
            "ex: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\")"
        )
    if len(token) < 16:
        raise RuntimeError("[simpostcards] sync_token : 16 caractères minimum")
    base = app.config.get("SYNC_BASE_DIR") or ""
    if not base or not os.path.isdir(base):
        raise RuntimeError(
            f"[simpostcards] sync_base_dir doit être un répertoire existant (reçu {base!r})"
        )
    prefix = app.config.get("SYNC_URL_PREFIX") or ""
    if not prefix.startswith("/"):
        raise RuntimeError("[simpostcards] sync_url_prefix doit commencer par « / »")
    nets = []
    for item in (app.config.get("SYNC_TRUSTED_PROXIES") or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise RuntimeError(
                f"[simpostcards] sync_trusted_proxies : {item!r} n'est pas une IP/CIDR valide"
            )
    app.config["SYNC_TRUSTED_NETS"] = nets


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

def _is_trusted(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in current_app.config.get("SYNC_TRUSTED_NETS") or ())


def _client_ip() -> str:
    """IP réelle du client. ``X-Forwarded-For`` n'est lu que si la requête
    arrive d'un proxy déclaré (sync_trusted_proxies) -- sinon n'importe qui
    pourrait forger l'en-tête pour échapper au blocage ; la chaîne est
    remontée depuis la droite jusqu'au premier saut non déclaré."""
    remote = request.remote_addr or "?"
    if not _is_trusted(remote):
        return remote
    chain = [a.strip() for a in request.headers.get("X-Forwarded-For", "").split(",") if a.strip()]
    for addr in reversed(chain):
        if not _is_trusted(addr):
            return addr
    return chain[0] if chain else remote


_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


@bp.before_request
def _authenticate() -> None:
    token = current_app.config.get("SYNC_TOKEN") or ""
    if not token:
        raise SyncError(404, "not found")
    ip = _client_ip()
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
    if scheme.lower() == "bearer" and secrets.compare_digest(
        value.strip().encode("utf-8"), token.encode("utf-8")
    ):
        return
    with _failures_lock:
        if len(_failures) > 1000:  # borne mémoire face à un balayage d'IP
            _failures.clear()
        _failures.setdefault(ip, []).append(now)
    current_app.logger.warning("sync : jeton invalide (IP=%s)", ip)
    raise SyncError(401, "unauthorized")


# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------

def _base() -> str:
    base = current_app.config.get("SYNC_BASE_DIR")
    if not base:
        raise SyncError(503, "sync_base_dir not configured")
    return os.path.realpath(base)


def _split(rel: Any) -> list[str]:
    if not isinstance(rel, str) or "\x00" in rel or len(rel) > 4096:
        raise SyncError(400, "invalid path")
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise SyncError(400, "invalid path")
    if parts and parts[0] == STAGING:
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
    return full


def _resolve_file(rel: Any) -> str:
    path = _resolve(rel)
    if path == _base():
        raise SyncError(400, "path designates the base directory")
    return path


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


def _max_body() -> int:
    return int(current_app.config.get("MAX_CONTENT_LENGTH") or _DEFAULT_MAX_BODY)


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
        os.makedirs(_staging_dir("tmp"), exist_ok=True)
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
        "restart": _restart_method() is not None,
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
                if e.name == STAGING and directory == base:
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
            if e.name == STAGING and path == base:
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
# Redémarrage de l'appli web
# ---------------------------------------------------------------------------

_restart_lock = threading.Lock()
_restart_pending = False
_ALLOWED_SIGNALS = {"HUP": "SIGHUP", "TERM": "SIGTERM", "USR1": "SIGUSR1", "USR2": "SIGUSR2"}


def _restart_method() -> str | None:
    cfg = current_app.config
    if cfg.get("SYNC_RESTART_COMMAND"):
        return "command"
    if cfg.get("SYNC_RESTART_PIDFILE"):
        return "pidfile"
    if cfg.get("SYNC_RESTART_URL"):
        return "url"
    return None


def _run_restart(method: str, settings: dict, log) -> None:
    global _restart_pending
    try:
        if method == "command":
            args = shlex.split(settings["command"])
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=120,
                start_new_session=True,
            )
            if proc.returncode == 0:
                log.warning("sync : redémarrage OK (commande %s)", args[0])
            else:
                log.error("sync : commande de redémarrage %s : code %d -- %s",
                          args[0], proc.returncode,
                          (proc.stderr or proc.stdout).strip()[:500])
        elif method == "pidfile":
            with open(settings["pidfile"]) as fh:
                pid = int(fh.read().strip())
            name = _ALLOWED_SIGNALS.get(settings["signal"].upper().removeprefix("SIG"))
            if name is None or not hasattr(signal, name):
                log.error("sync : signal %r non supporté (HUP, TERM, USR1, USR2)",
                          settings["signal"])
                return
            os.kill(pid, getattr(signal, name))
            log.warning("sync : %s envoyé au pid %d (%s)", name, pid, settings["pidfile"])
        elif method == "url":
            url = settings["url"]
            if not url.lower().startswith(("http://", "https://")):
                log.error("sync : sync_restart_url doit être http(s)://")
                return
            headers = {}
            if settings["token"]:
                headers["X-Restart-Token"] = settings["token"]
            req = urllib.request.Request(url, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                log.warning("sync : redémarrage demandé à %s (HTTP %d)", url, resp.status)
    except (OSError, ValueError, subprocess.SubprocessError, urllib.error.URLError) as exc:
        log.error("sync : échec du redémarrage (%s) : %s", method, exc)
    finally:
        with _restart_lock:
            _restart_pending = False


@bp.route("/restart", methods=["POST"])
def restart():
    global _restart_pending
    method = _restart_method()
    if method is None:
        raise SyncError(501, "no restart method configured on this server")
    cfg = current_app.config
    settings = {
        "command": cfg.get("SYNC_RESTART_COMMAND"),
        "pidfile": cfg.get("SYNC_RESTART_PIDFILE"),
        "signal": cfg.get("SYNC_RESTART_SIGNAL") or "HUP",
        "url": cfg.get("SYNC_RESTART_URL"),
        "token": cfg.get("SYNC_RESTART_TOKEN"),
    }
    with _restart_lock:
        if _restart_pending:
            return _json({"status": "already-scheduled", "method": method}, 202)
        _restart_pending = True
    current_app.logger.warning("sync : redémarrage programmé via %s (IP=%s)",
                               method, _client_ip())
    timer = threading.Timer(
        float(cfg.get("SYNC_RESTART_DELAY") or 0.5),
        _run_restart, args=(method, settings, current_app.logger),
    )
    timer.daemon = True
    timer.start()
    return _json({"status": "restarting", "method": method}, 202)
