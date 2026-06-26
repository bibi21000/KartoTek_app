"""
remotesync.py — Bibliothèque de synchronisation de fichiers vers un serveur distant.
Protocoles supportés : FTP, FTPS, FTP/TLS, SFTP (SSH).
Configuration via fichier INI.
"""

from __future__ import annotations

import configparser
import ftplib
import hashlib
import logging
import os
import stat
import threading
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------

class Protocol(str, Enum):
    FTP   = "ftp"
    FTPS  = "ftps"
    FTPTLS = "ftptls"
    SFTP  = "sftp"


@dataclass
class SyncConfig:
    """Paramètres lus depuis le fichier INI."""
    protocol: Protocol
    host: str
    port: int
    username: str
    password: str = ""
    ssh_key_path: str = ""
    remote_base_dir: str = "/"
    passive_mode: bool = True
    timeout: int = 30
    delete_orphans: bool = False   # supprimer les fichiers distants absents en local
    dry_run: bool = False          # simuler sans transférer
    logdir: str = ""               # répertoire de stockage des logs de session
    max_workers: int = 5           # transferts simultanés lors de sync_directory

    @classmethod
    def from_ini(cls, path: str | Path, section: str = "remotesync") -> "SyncConfig":
        cfg = configparser.ConfigParser()
        cfg.read(str(path))
        if section not in cfg:
            raise ValueError(f"Section [{section}] introuvable dans {path}")
        s = cfg[section]
        proto_str = s.get("protocol", "ftp").lower()
        try:
            protocol = Protocol(proto_str)
        except ValueError:
            raise ValueError(f"Protocole inconnu : {proto_str!r}. Valeurs acceptées : {[p.value for p in Protocol]}")

        default_ports = {Protocol.FTP: 21, Protocol.FTPS: 990,
                         Protocol.FTPTLS: 21, Protocol.SFTP: 22}
        port = int(s.get("port", default_ports[protocol]))

        return cls(
            protocol=protocol,
            host=s.get("host", ""),
            port=port,
            username=s.get("username", ""),
            password=s.get("password", ""),
            ssh_key_path=s.get("ssh_key_path", ""),
            remote_base_dir=s.get("remote_base_dir", "/"),
            passive_mode=s.getboolean("passive_mode", True),
            timeout=int(s.get("timeout", 30)),
            delete_orphans=s.getboolean("delete_orphans", False),
            dry_run=s.getboolean("dry_run", False),
            # logdir est dans [DEFAULT] (hérité par toutes les sections)
            logdir=s.get("logdir", ""),
            max_workers=int(s.get("max_workers", 5)),
        )


# ---------------------------------------------------------------------------
# Entrée de log d'une opération au sein d'une session
# ---------------------------------------------------------------------------

@dataclass
class _SyncEntry:
    """Une opération (fichier ou répertoire) effectuée pendant la session."""
    label: str          # chemin local ou libellé fourni par l'appelant
    remote: str         # cible distante
    kind: str           # "file" | "directory"
    started_at: datetime
    ended_at: datetime
    result: "SyncResult"


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------

@dataclass
class SyncResult:
    """
    Résultat d'une opération de synchronisation (fichier ou répertoire).

    Peut être agrégé dans une :class:`SyncSession` pour suivre plusieurs
    opérations au sein d'une même session de travail.
    """
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    def __str__(self) -> str:
        return (
            f"SyncResult(uploaded={len(self.uploaded)}, skipped={len(self.skipped)}, "
            f"deleted={len(self.deleted)}, errors={len(self.errors)})"
        )


# ---------------------------------------------------------------------------
# SyncSession — agrégation de plusieurs SyncResult
# ---------------------------------------------------------------------------

class SyncSession:
    """
    Regroupe plusieurs opérations de synchronisation effectuées lors d'une
    même session de travail et permet de sauvegarder un rapport lisible.

    Exemple d'utilisation ::

        session = SyncSession(config)

        r1 = sync.sync_file("dist/app.js", "/public_html/app.js")
        session.add(r1, local="dist/app.js", remote="/public_html/app.js", kind="file")

        r2 = sync.sync_directory("dist/", "/public_html")
        session.add(r2, local="dist/", remote="/public_html", kind="directory")

        session.save()          # écrit le rapport dans logdir
        print(session.summary())
    """

    def __init__(self, config: "SyncConfig", label: str = ""):
        self.config = config
        self.label = label or "session"
        self.started_at: datetime = datetime.now(tz=timezone.utc)
        self._entries: list[_SyncEntry] = []

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def add(
        self,
        result: SyncResult,
        *,
        local: str,
        remote: str,
        kind: str = "file",
    ) -> None:
        """
        Enregistre le résultat d'une opération dans la session.

        :param result: :class:`SyncResult` retourné par ``sync_file`` ou
                       ``sync_directory``.
        :param local:  Chemin local utilisé (pour le rapport).
        :param remote: Chemin distant cible (pour le rapport).
        :param kind:   ``"file"`` ou ``"directory"``.
        """
        now = datetime.now(tz=timezone.utc)
        # started_at estimé : heure courante moins durée implicite (inconnue) —
        # on conserve l'heure d'ajout comme timestamp de fin.
        entry = _SyncEntry(
            label=local,
            remote=remote,
            kind=kind,
            started_at=now,
            ended_at=now,
            result=result,
        )
        self._entries.append(entry)

    @property
    def total_uploaded(self) -> int:
        return sum(len(e.result.uploaded) for e in self._entries)

    @property
    def total_skipped(self) -> int:
        return sum(len(e.result.skipped) for e in self._entries)

    @property
    def total_deleted(self) -> int:
        return sum(len(e.result.deleted) for e in self._entries)

    @property
    def total_errors(self) -> int:
        return sum(len(e.result.errors) for e in self._entries)

    @property
    def success(self) -> bool:
        return self.total_errors == 0

    def summary(self) -> str:
        """Retourne un résumé compact sur une ligne."""
        status = "OK" if self.success else "ERREURS"
        return (
            f"[{status}] Session «{self.label}» — "
            f"{len(self._entries)} opération(s) : "
            f"↑{self.total_uploaded} uploadé(s), "
            f"↷{self.total_skipped} ignoré(s), "
            f"✗{self.total_deleted} supprimé(s), "
            f"⚠{self.total_errors} erreur(s)"
        )

    def save(self, logdir: Optional[str] = None) -> Path:
        """
        Écrit le rapport de session dans un fichier texte lisible.

        Le nom du fichier est ``<label>_<YYYYMMDD_HHMMSS>.log``.

        :param logdir: Répertoire de destination. Si omis, utilise
                       ``config.logdir``. Si les deux sont vides, écrit dans
                       le répertoire courant.
        :returns: Chemin du fichier créé.
        :raises OSError: Si le répertoire ne peut pas être créé ou le fichier
                         ne peut pas être écrit.
        """
        dest_dir = Path(logdir or self.config.logdir or ".")
        dest_dir.mkdir(parents=True, exist_ok=True)

        ts = self.started_at.strftime("%Y%m%d_%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.label)
        filename = f"{safe_label}_{ts}.log"
        log_path = dest_dir / filename

        log_path.write_text(self._render_report(), encoding="utf-8")
        logger.info("Rapport de session écrit dans %s", log_path)
        return log_path

    # ------------------------------------------------------------------
    # Rendu interne
    # ------------------------------------------------------------------

    def _render_report(self) -> str:
        now = datetime.now(tz=timezone.utc)
        lines: list[str] = []

        def sep(char: str = "─", width: int = 72) -> str:
            return char * width

        lines += [
            sep("═"),
            f"  RAPPORT DE SYNCHRONISATION — {self.label.upper()}",
            sep("═"),
            f"  Début de session : {self.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"  Fin de session   : {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"  Serveur          : {self.config.host}:{self.config.port}"
            f"  [{self.config.protocol.value.upper()}]",
            f"  Répertoire base  : {self.config.remote_base_dir}",
            f"  Mode simulation  : {'OUI' if self.config.dry_run else 'NON'}",
            sep(),
            f"  RÉSUMÉ : {len(self._entries)} opération(s) | "
            f"↑{self.total_uploaded} uploadé(s) | "
            f"↷{self.total_skipped} ignoré(s) | "
            f"✗{self.total_deleted} supprimé(s) | "
            f"⚠{self.total_errors} erreur(s)",
            sep("═"),
            "",
        ]

        for i, entry in enumerate(self._entries, 1):
            r = entry.result
            status = "✔ OK" if r.success else "✘ ERREUR(S)"
            kind_label = "Fichier" if entry.kind == "file" else "Répertoire"
            lines += [
                f"  [{i}/{len(self._entries)}] {kind_label} — {status}",
                f"  Local  : {entry.label}",
                f"  Distant: {entry.remote}",
                sep("·"),
            ]

            if r.uploaded:
                lines.append(f"  Uploadé(s) [{len(r.uploaded)}] :")
                lines += [f"    + {f}" for f in r.uploaded]

            if r.skipped:
                lines.append(f"  Ignoré(s)  [{len(r.skipped)}] (déjà à jour) :")
                lines += [f"    = {f}" for f in r.skipped]

            if r.deleted:
                lines.append(f"  Supprimé(s)[{len(r.deleted)}] :")
                lines += [f"    - {f}" for f in r.deleted]

            if r.errors:
                lines.append(f"  Erreur(s)  [{len(r.errors)}] :")
                lines += [f"    ! {e}" for e in r.errors]

            lines.append("")

        lines += [
            sep("═"),
            f"  Statut final : {'SUCCÈS' if self.success else 'ÉCHEC (voir erreurs ci-dessus)'}",
            sep("═"),
        ]

        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Backend abstrait
# ---------------------------------------------------------------------------

class _BaseBackend(ABC):
    def __init__(self, cfg: SyncConfig):
        self.cfg = cfg

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def upload_file(self, local_path: Path, remote_path: str) -> None: ...

    @abstractmethod
    def remote_mtime(self, remote_path: str) -> Optional[float]:
        """Retourne le timestamp de modification distant, ou None si inconnu."""
        ...

    @abstractmethod
    def makedirs(self, remote_dir: str) -> None: ...

    @abstractmethod
    def list_remote(self, remote_dir: str) -> list[str]:
        """Liste récursive des fichiers sous remote_dir (chemins relatifs à remote_dir)."""
        ...

    @abstractmethod
    def download_file(self, remote_path: str, local_path: Path) -> None:
        """Télécharge un fichier distant vers local_path."""
        ...

    @abstractmethod
    def delete_remote(self, remote_path: str) -> None: ...

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()


# ---------------------------------------------------------------------------
# Backend FTP / FTPS / FTP+TLS
# ---------------------------------------------------------------------------

class _FTPBackend(_BaseBackend):
    def __init__(self, cfg: SyncConfig):
        super().__init__(cfg)
        self._ftp: Optional[ftplib.FTP] = None

    def connect(self) -> None:
        p = self.cfg.protocol
        if p == Protocol.FTPS:
            self._ftp = ftplib.FTP_TLS()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
            self._ftp.auth()
            self._ftp.prot_p()
        elif p == Protocol.FTPTLS:
            self._ftp = ftplib.FTP_TLS()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
            self._ftp.login(self.cfg.username, self.cfg.password)
            self._ftp.prot_p()
        else:  # plain FTP
            self._ftp = ftplib.FTP()
            self._ftp.connect(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)

        if p != Protocol.FTPTLS:
            self._ftp.login(self.cfg.username, self.cfg.password)

        if self.cfg.passive_mode:
            self._ftp.set_pasv(True)

        logger.info("FTP connecté à %s:%s", self.cfg.host, self.cfg.port)

    def disconnect(self) -> None:
        if self._ftp:
            try:
                self._ftp.quit()
            except Exception:
                self._ftp.close()
            self._ftp = None

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.makedirs(os.path.dirname(remote_path))
        with open(local_path, "rb") as fh:
            self._ftp.storbinary(f"STOR {remote_path}", fh)

    def remote_mtime(self, remote_path: str) -> Optional[float]:
        try:
            resp = self._ftp.sendcmd(f"MDTM {remote_path}")
            # Format : "213 YYYYMMDDHHMMSS"
            ts_str = resp[4:].strip()
            dt = datetime.strptime(ts_str, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def makedirs(self, remote_dir: str) -> None:
        if not remote_dir or remote_dir == "/":
            return
        parts = remote_dir.replace("\\", "/").split("/")
        path = ""
        for part in parts:
            if not part:
                continue
            path += "/" + part
            try:
                self._ftp.mkd(path)
            except ftplib.error_perm as e:
                if "550" not in str(e):  # 550 = déjà existant
                    raise

    def list_remote(self, remote_dir: str) -> list[str]:
        result: list[str] = []
        try:
            items = self._ftp.nlst(remote_dir)
        except ftplib.error_temp:
            return result
        for item in items:
            try:
                # Essayer d'entrer dedans → c'est un répertoire
                self._ftp.cwd(item)
                self._ftp.cwd("/")
                sub = self.list_remote(item)
                result.extend(sub)
            except ftplib.error_perm:
                result.append(item)
        return result

    def download_file(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "wb") as fh:
            self._ftp.retrbinary(f"RETR {remote_path}", fh.write)

    def delete_remote(self, remote_path: str) -> None:
        self._ftp.delete(remote_path)


# ---------------------------------------------------------------------------
# Backend SFTP (SSH)
# ---------------------------------------------------------------------------

class _SFTPBackend(_BaseBackend):
    def __init__(self, cfg: SyncConfig):
        super().__init__(cfg)
        self._ssh = None
        self._sftp = None

    def connect(self) -> None:
        try:
            import paramiko  # type: ignore
        except ImportError:
            raise ImportError(
                "Le module 'paramiko' est requis pour le protocole SFTP.\n"
                "Installez-le avec : pip install paramiko"
            )

        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = dict(
            hostname=self.cfg.host,
            port=self.cfg.port,
            username=self.cfg.username,
            timeout=self.cfg.timeout,
        )
        if self.cfg.ssh_key_path:
            connect_kwargs["key_filename"] = self.cfg.ssh_key_path
        else:
            connect_kwargs["password"] = self.cfg.password

        self._ssh.connect(**connect_kwargs)
        self._sftp = self._ssh.open_sftp()
        logger.info("SFTP connecté à %s:%s", self.cfg.host, self.cfg.port)

    def disconnect(self) -> None:
        if self._sftp:
            self._sftp.close()
        if self._ssh:
            self._ssh.close()
        self._sftp = self._ssh = None

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.makedirs(os.path.dirname(remote_path))
        self._sftp.put(str(local_path), remote_path)

    def remote_mtime(self, remote_path: str) -> Optional[float]:
        try:
            attrs = self._sftp.stat(remote_path)
            return float(attrs.st_mtime)
        except Exception:
            return None

    def makedirs(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        parts = remote_dir.replace("\\", "/").split("/")
        path = ""
        for part in parts:
            if not part:
                continue
            path += "/" + part
            try:
                self._sftp.stat(path)
            except FileNotFoundError:
                self._sftp.mkdir(path)

    def list_remote(self, remote_dir: str) -> list[str]:
        result: list[str] = []
        try:
            attrs_list = self._sftp.listdir_attr(remote_dir)
        except Exception:
            return result
        for attr in attrs_list:
            full = remote_dir.rstrip("/") + "/" + attr.filename
            if stat.S_ISDIR(attr.st_mode):
                result.extend(self.list_remote(full))
            else:
                result.append(full)
        return result

    def download_file(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._sftp.get(remote_path, str(local_path))

    def delete_remote(self, remote_path: str) -> None:
        self._sftp.remove(remote_path)


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class RemoteSync:
    """
    Synchronise des fichiers locaux vers un serveur distant.

    Exemple d'utilisation ::

        sync = RemoteSync("config.ini")
        result = sync.sync_directory("/var/www/html", "/public_html")
        result = sync.sync_file("/var/www/html/index.html", "/public_html/index.html")
        print(result)
    """

    def __init__(self, config_path: str | Path, section: str = "remotesync"):
        self.config = SyncConfig.from_ini(config_path, section)

    # ------------------------------------------------------------------
    # API publique
    # ------------------------------------------------------------------

    def sync_file(
        self,
        local_path: str | Path,
        remote_path: Optional[str] = None,
    ) -> SyncResult:
        """
        Synchronise un fichier unique.

        :param local_path:  Chemin local du fichier source.
        :param remote_path: Chemin distant du fichier cible.
                            - Si omis : ``remote_base_dir/<nom_du_fichier>``
                            - Si relatif (ne commence pas par «/») :
                              ``remote_base_dir/<remote_path>``
                            - Si absolu : utilisé tel quel.
        """
        result = SyncResult()
        local = Path(local_path)
        if not local.is_file():
            result.errors.append(f"Fichier local introuvable : {local}")
            return result

        if remote_path is None:
            remote_path = self.config.remote_base_dir.rstrip("/") + "/" + local.name
        else:
            remote_path = self._resolve_remote(remote_path)

        try:
            with self._build_backend() as backend:
                self._sync_single(backend, local, remote_path, result)
        except Exception as exc:
            logger.exception("Erreur de connexion")
            result.errors.append(str(exc))
        return result

    def sync_directory(
        self,
        local_dir: str | Path,
        remote_dir: Optional[str] = None,
        max_workers: Optional[int] = None,
    ) -> SyncResult:
        """
        Synchronise récursivement un répertoire local vers le serveur distant
        en utilisant des transferts simultanés.

        Chaque worker maintient sa propre connexion au serveur pour garantir
        la thread-safety (les connexions FTP/SFTP ne sont pas partagées).

        :param local_dir:   Répertoire local source.
        :param remote_dir:  Répertoire distant cible.
                            - Si omis : ``remote_base_dir``
                            - Si relatif : ``remote_base_dir/<remote_dir>``
                            - Si absolu : utilisé tel quel.
        :param max_workers: Nombre de connexions/transferts simultanés.
                            Priorité : argument > ``config.max_workers`` (défaut 5).
        """
        result = SyncResult()
        lock = threading.Lock()
        local = Path(local_dir)

        if not local.is_dir():
            result.errors.append(f"Répertoire local introuvable : {local}")
            return result

        if remote_dir is None:
            remote_dir = self.config.remote_base_dir
        else:
            remote_dir = self._resolve_remote(remote_dir)

        workers = max_workers if max_workers is not None else self.config.max_workers
        workers = max(1, workers)

        # ── 1. Inventaire local ───────────────────────────────────────────────
        local_files = {
            f.relative_to(local).as_posix()
            for f in local.rglob("*")
            if f.is_file()
        }

        # ── 2. Pré-création des répertoires distants (sérialisée) ─────────────
        # On crée l'arborescence avant le lancement des workers pour éviter
        # les conditions de course sur makedirs entre threads.
        remote_dirs_needed = {
            remote_dir.rstrip("/") + "/" + os.path.dirname(rel)
            for rel in local_files
        }
        try:
            with self._build_backend() as probe:
                for d in sorted(remote_dirs_needed):
                    d = d.rstrip("/")
                    if d:
                        probe.makedirs(d)

                # Récupérer aussi la liste des fichiers distants (pour orphelins)
                remote_files_list: list[str] = (
                    probe.list_remote(remote_dir)
                    if self.config.delete_orphans else []
                )
        except Exception as exc:
            logger.exception("Erreur lors de la préparation des répertoires distants")
            result.errors.append(f"Préparation distante : {exc}")
            return result

        # ── 3. Transferts parallèles — connexion persistante par worker ──────────
        #
        # ARCHITECTURE CORRIGÉE :
        # Avant : _worker(fichier) → 1 connexion SSH par fichier  ← bug (avalanche)
        # Après : _worker(lot)    → 1 connexion SSH pour N fichiers du lot
        #
        # On distribue les fichiers en lots équilibrés (round-robin), puis chaque
        # worker ouvre UNE connexion pour traiter tous les fichiers de son lot.

        sorted_files = sorted(local_files)
        # Découpage en lots : chaque worker reçoit un sous-ensemble de fichiers
        # distribué de façon interleaved pour équilibrer la charge (pas de split
        # en tranches consécutives qui favoriserait les gros répertoires uniques).
        batches: list[list[str]] = [[] for _ in range(workers)]
        for i, rel in enumerate(sorted_files):
            batches[i % workers].append(rel)

        def _worker_batch(batch: list[str]) -> None:
            """
            Traite un lot de fichiers sur UNE connexion persistante.
            Ouvre la connexion une seule fois, itère sur les fichiers, ferme.
            """
            if not batch:
                return
            try:
                with self._build_backend() as backend:
                    for rel in batch:
                        local_file = local / rel
                        remote_file = remote_dir.rstrip("/") + "/" + rel
                        try:
                            remote_mtime = backend.remote_mtime(remote_file)
                            local_mtime = local_file.stat().st_mtime

                            if remote_mtime is not None and local_mtime <= remote_mtime:
                                with lock:
                                    result.skipped.append(remote_file)
                                logger.debug("[SKIP]   %s (déjà à jour)", remote_file)
                                continue

                            action = "[DRY-RUN]" if self.config.dry_run else "[UPLOAD]"
                            logger.info("%s %s → %s", action, local_file, remote_file)

                            if not self.config.dry_run:
                                backend.upload_file(local_file, remote_file)

                            with lock:
                                result.uploaded.append(remote_file)

                        except Exception as exc:
                            msg = f"{remote_file}: {exc}"
                            logger.error("[ERROR]  %s", msg)
                            with lock:
                                result.errors.append(msg)

            except Exception as exc:
                # Erreur de connexion : tous les fichiers du lot échouent
                for rel in batch:
                    remote_file = remote_dir.rstrip("/") + "/" + rel
                    msg = f"{remote_file}: erreur de connexion : {exc}"
                    logger.error("[ERROR]  %s", msg)
                    with lock:
                        result.errors.append(msg)

        try:
            # On ne soumet que les lots non vides (si workers > nb fichiers)
            non_empty_batches = [b for b in batches if b]
            with ThreadPoolExecutor(max_workers=len(non_empty_batches) or 1) as pool:
                futures = {pool.submit(_worker_batch, batch): batch
                           for batch in non_empty_batches}
                for future in as_completed(futures):
                    exc = future.exception()
                    if exc:
                        batch = futures[future]
                        msg = f"Lot [{batch[0]}…]: erreur inattendue : {exc}"
                        logger.error("[ERROR]  %s", msg)
                        with lock:
                            result.errors.append(msg)
        except Exception as exc:
            logger.exception("Erreur du pool de threads")
            result.errors.append(str(exc))
            return result

        # ── 4. Suppression des orphelins distants (sérialisée) ────────────────
        if self.config.delete_orphans and remote_files_list:
            try:
                with self._build_backend() as backend:
                    for rf in remote_files_list:
                        rel = rf[len(remote_dir):].lstrip("/")
                        if rel not in local_files:
                            if not self.config.dry_run:
                                backend.delete_remote(rf)
                            with lock:
                                result.deleted.append(rf)
                            logger.info("[DELETED] %s", rf)
            except Exception as exc:
                logger.exception("Erreur lors de la suppression des orphelins")
                result.errors.append(f"Suppression orphelins : {exc}")

        logger.info(
            "sync_directory terminé — workers=%d | ↑%d uploadé(s) | ↷%d ignoré(s) | "
            "✗%d supprimé(s) | ⚠%d erreur(s)",
            workers,
            len(result.uploaded), len(result.skipped),
            len(result.deleted), len(result.errors),
        )
        return result

    def fetch_file(
        self,
        remote_path: str,
        local_path: Optional[str | Path] = None,
        overwrite: bool = True,
    ) -> SyncResult:
        """
        Télécharge un fichier depuis le serveur distant vers le système local.

        :param remote_path: Chemin du fichier sur le serveur.
                            - Si relatif : ``remote_base_dir/<remote_path>``
                            - Si absolu  : utilisé tel quel.
        :param local_path:  Destination locale.
                            - Si omis : le fichier est déposé dans le répertoire
                              courant avec le même nom que le fichier distant.
                            - Si un répertoire : le fichier y est déposé avec
                              son nom d'origine.
                            - Si un chemin de fichier : utilisé tel quel.
        :param overwrite:   Si ``False`` et que le fichier local existe déjà et
                            est plus récent que le fichier distant, le téléchar-
                            gement est ignoré (comportement miroir de sync_file).
                            Par défaut ``True`` (téléchargement systématique).
        :returns: :class:`SyncResult` —
                  ``uploaded`` contient le chemin local du fichier téléchargé,
                  ``skipped`` contient le chemin local si ignoré.
        """
        result = SyncResult()
        remote_path = self._resolve_remote(remote_path)
        remote_name = remote_path.rstrip("/").split("/")[-1]

        # Résolution de la destination locale
        if local_path is None:
            dest = Path.cwd() / remote_name
        else:
            dest = Path(local_path)
            if dest.is_dir():
                dest = dest / remote_name

        try:
            with self._build_backend() as backend:
                # Vérification overwrite / mtime
                if not overwrite and dest.exists():
                    remote_mtime = backend.remote_mtime(remote_path)
                    local_mtime = dest.stat().st_mtime
                    if remote_mtime is None or local_mtime >= remote_mtime:
                        result.skipped.append(str(dest))
                        logger.debug(
                            "[SKIP] %s (local déjà à jour)", dest
                        )
                        return result

                if self.config.dry_run:
                    logger.info("[DRY-RUN] %s ← %s", dest, remote_path)
                    result.uploaded.append(str(dest))
                    return result

                logger.info("[DOWNLOAD] %s ← %s", dest, remote_path)
                backend.download_file(remote_path, dest)
                result.uploaded.append(str(dest))

        except Exception as exc:
            msg = f"{remote_path} → {dest} : {exc}"
            result.errors.append(msg)
            logger.error("[ERROR] %s", msg)
            # Nettoyer un fichier partiellement téléchargé
            if dest.exists() and dest.stat().st_size == 0:
                try:
                    dest.unlink()
                except OSError:
                    pass

        return result

    # ------------------------------------------------------------------
    # Méthodes internes
    # ------------------------------------------------------------------

    def _resolve_remote(self, remote: str) -> str:
        """
        Résout un chemin distant :
        - chemin absolu (commence par «/») → retourné inchangé
        - chemin relatif → ``remote_base_dir/remote``
        """
        if remote.startswith("/"):
            return remote
        return self.config.remote_base_dir.rstrip("/") + "/" + remote

    def _build_backend(self) -> _BaseBackend:
        """Instancie un nouveau backend (nouvelle connexion) à chaque appel."""
        if self.config.protocol == Protocol.SFTP:
            return _SFTPBackend(self.config)
        return _FTPBackend(self.config)

    def _sync_single(
        self,
        backend: _BaseBackend,
        local: Path,
        remote: str,
        result: SyncResult,
        lock: Optional[threading.Lock] = None,
    ) -> None:
        """Décide d'uploader ou de sauter un fichier, met à jour result.

        Peut être appelé depuis un thread (passer lock) ou en mode séquentiel.
        """
        def _append(lst: list, value: str) -> None:
            if lock:
                with lock:
                    lst.append(value)
            else:
                lst.append(value)

        remote_mtime = backend.remote_mtime(remote)
        local_mtime = local.stat().st_mtime

        if remote_mtime is not None and local_mtime <= remote_mtime:
            _append(result.skipped, remote)
            logger.debug("[SKIP]   %s (distant plus récent ou identique)", remote)
            return

        action = "[DRY-RUN]" if self.config.dry_run else "[UPLOAD]"
        logger.info("%s %s → %s", action, local, remote)

        if not self.config.dry_run:
            try:
                backend.upload_file(local, remote)
                _append(result.uploaded, remote)
            except Exception as exc:
                msg = f"{remote}: {exc}"
                _append(result.errors, msg)
                logger.error("[ERROR]  %s", msg)
        else:
            _append(result.uploaded, remote)
