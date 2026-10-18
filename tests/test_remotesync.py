"""
test_remotesync.py — Tests unitaires de RemoteSync (sans serveur réel).
Exécution : python -m pytest test_remotesync.py -v
"""

import configparser
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from remotesync import RemoteSync, SyncConfig, Protocol, SyncResult, SyncSession, _FTPBackend, _SFTPBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ini(tmp_path: Path, extra: dict | None = None) -> Path:
    cfg = configparser.ConfigParser()
    defaults = {
        "protocol": "ftp",
        "host": "ftp.test.com",
        "port": "21",
        "username": "user",
        "password": "pass",
        "remote_base_dir": "/remote",
        "passive_mode": "true",
        "timeout": "10",
        "delete_orphans": "false",
        "dry_run": "false",
    }
    if extra:
        defaults.update(extra)
    cfg["remotesync"] = defaults
    ini = tmp_path / "config.ini"
    ini.write_text(cfg_to_str(cfg))
    return ini


def cfg_to_str(cfg: configparser.ConfigParser) -> str:
    import io
    buf = io.StringIO()
    cfg.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# SyncConfig
# ---------------------------------------------------------------------------

class TestSyncConfig:
    def test_reads_ftp(self, tmp_path):
        ini = make_ini(tmp_path)
        cfg = SyncConfig.from_ini(ini)
        assert cfg.protocol == Protocol.FTP
        assert cfg.host == "ftp.test.com"
        assert cfg.port == 21
        assert cfg.passive_mode is True

    def test_reads_sftp(self, tmp_path):
        ini = make_ini(tmp_path, {"protocol": "sftp", "port": "22",
                                   "ssh_key_path": "/home/user/.ssh/id_rsa"})
        cfg = SyncConfig.from_ini(ini)
        assert cfg.protocol == Protocol.SFTP
        assert cfg.ssh_key_path == "/home/user/.ssh/id_rsa"

    def test_missing_section_raises(self, tmp_path):
        ini = tmp_path / "empty.ini"
        ini.write_text("[other]\nfoo=bar\n")
        with pytest.raises(ValueError, match="Section"):
            SyncConfig.from_ini(ini)

    def test_unknown_protocol_raises(self, tmp_path):
        ini = make_ini(tmp_path, {"protocol": "gopher"})
        with pytest.raises(ValueError, match="Protocole inconnu"):
            SyncConfig.from_ini(ini)

    def test_dry_run_flag(self, tmp_path):
        ini = make_ini(tmp_path, {"dry_run": "true"})
        cfg = SyncConfig.from_ini(ini)
        assert cfg.dry_run is True


# ---------------------------------------------------------------------------
# RemoteSync.sync_file
# ---------------------------------------------------------------------------

class TestSyncFile:
    def _make_sync(self, tmp_path, mock_backend):
        ini = make_ini(tmp_path)
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        sync._build_backend = _make_backend_factory(mock_backend)
        return sync

    def test_missing_local_file(self, tmp_path):
        sync = self._make_sync(tmp_path, MagicMock())
        result = sync.sync_file("/nonexistent/file.txt")
        assert not result.success
        assert "introuvable" in result.errors[0]

    def test_upload_new_file(self, tmp_path):
        local = tmp_path / "hello.txt"
        local.write_text("bonjour")

        backend = MagicMock()
        backend.remote_mtime.return_value = None   # fichier absent côté distant

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_file(str(local), "/remote/hello.txt")

        assert result.success
        assert "/remote/hello.txt" in result.uploaded
        backend.upload_file.assert_called_once()

    def test_skip_when_remote_newer(self, tmp_path):
        local = tmp_path / "hello.txt"
        local.write_text("bonjour")
        future_mtime = local.stat().st_mtime + 3600  # distant plus récent

        backend = MagicMock()
        backend.remote_mtime.return_value = future_mtime

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_file(str(local), "/remote/hello.txt")

        assert "/remote/hello.txt" in result.skipped
        backend.upload_file.assert_not_called()

    def test_relative_remote_path_prepends_base_dir(self, tmp_path):
        local = tmp_path / "hello.txt"
        local.write_text("bonjour")

        backend = MagicMock()
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_file(str(local), "subdir/hello.txt")

        assert result.uploaded[0] == "/remote/subdir/hello.txt"

    def test_absolute_remote_path_used_as_is(self, tmp_path):
        local = tmp_path / "hello.txt"
        local.write_text("bonjour")

        backend = MagicMock()
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_file(str(local), "/absolute/path/hello.txt")

        assert result.uploaded[0] == "/absolute/path/hello.txt"

    def test_dry_run_no_upload(self, tmp_path):
        local = tmp_path / "file.txt"
        local.write_text("data")

        backend = MagicMock()
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        sync.config.dry_run = True
        result = sync.sync_file(str(local), "/remote/file.txt")

        assert "/remote/file.txt" in result.uploaded
        backend.upload_file.assert_not_called()


# ---------------------------------------------------------------------------
# RemoteSync.sync_directory
# ---------------------------------------------------------------------------

def _make_backend_factory(mock_backend):
    """Retourne une factory qui donne toujours le même mock (context manager)."""
    mock_backend.__enter__ = lambda s: s
    mock_backend.__exit__ = MagicMock(return_value=False)
    return lambda: mock_backend



# ---------------------------------------------------------------------------
# RemoteSync.fetch_file
# ---------------------------------------------------------------------------

class TestFetchFile:
    def _make_sync(self, tmp_path, mock_backend):
        ini = make_ini(tmp_path)
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        sync._build_backend = _make_backend_factory(mock_backend)
        return sync

    def test_download_creates_file(self, tmp_path):
        dest = tmp_path / "downloaded.txt"

        def fake_download(remote_path, local_path):
            local_path.write_bytes(b"contenu distant")

        backend = MagicMock()
        backend.download_file.side_effect = fake_download
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/file.txt", str(dest))

        assert result.success
        assert str(dest) in result.uploaded
        assert dest.exists()
        assert dest.read_bytes() == b"contenu distant"

    def test_local_path_as_directory(self, tmp_path):
        """Si local_path est un répertoire, le fichier y est déposé avec son nom distant."""
        dest_dir = tmp_path / "downloads"
        dest_dir.mkdir()

        def fake_download(remote_path, local_path):
            local_path.write_bytes(b"data")

        backend = MagicMock()
        backend.download_file.side_effect = fake_download
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/report.pdf", str(dest_dir))

        expected = dest_dir / "report.pdf"
        assert result.success
        assert str(expected) in result.uploaded
        assert expected.exists()

    def test_local_path_none_uses_cwd(self, tmp_path, monkeypatch):
        """Sans local_path, le fichier est déposé dans le répertoire courant."""
        monkeypatch.chdir(tmp_path)

        def fake_download(remote_path, local_path):
            local_path.write_bytes(b"cwd")

        backend = MagicMock()
        backend.download_file.side_effect = fake_download
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/data.csv")

        expected = tmp_path / "data.csv"
        assert result.success
        assert str(expected) in result.uploaded

    def test_relative_remote_path_prepends_base_dir(self, tmp_path):
        dest = tmp_path / "out.txt"

        backend = MagicMock()
        backend.download_file.side_effect = lambda r, l: l.write_bytes(b"ok")
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("subdir/out.txt", str(dest))

        assert result.success
        called_remote = backend.download_file.call_args[0][0]
        assert called_remote == "/remote/subdir/out.txt"

    def test_overwrite_false_skips_when_local_newer(self, tmp_path):
        dest = tmp_path / "local.txt"
        dest.write_text("local content")
        future_local_mtime = dest.stat().st_mtime
        older_remote_mtime = future_local_mtime - 3600

        backend = MagicMock()
        backend.remote_mtime.return_value = older_remote_mtime

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/local.txt", str(dest), overwrite=False)

        assert result.success
        assert str(dest) in result.skipped
        backend.download_file.assert_not_called()

    def test_overwrite_false_downloads_when_remote_newer(self, tmp_path):
        dest = tmp_path / "old.txt"
        dest.write_text("old content")
        local_mtime = dest.stat().st_mtime
        newer_remote_mtime = local_mtime + 3600

        backend = MagicMock()
        backend.remote_mtime.return_value = newer_remote_mtime
        backend.download_file.side_effect = lambda r, l: l.write_bytes(b"new")

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/old.txt", str(dest), overwrite=False)

        assert result.success
        assert str(dest) in result.uploaded
        backend.download_file.assert_called_once()

    def test_overwrite_true_always_downloads(self, tmp_path):
        dest = tmp_path / "existing.txt"
        dest.write_text("old")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.download_file.side_effect = lambda r, l: l.write_bytes(b"new")

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/existing.txt", str(dest), overwrite=True)

        assert result.success
        backend.download_file.assert_called_once()

    def test_dry_run_no_download(self, tmp_path):
        dest = tmp_path / "dry.txt"

        backend = MagicMock()
        backend.remote_mtime.return_value = None

        sync = self._make_sync(tmp_path, backend)
        sync.config.dry_run = True
        result = sync.fetch_file("/remote/dry.txt", str(dest))

        assert result.success
        assert str(dest) in result.uploaded
        backend.download_file.assert_not_called()
        assert not dest.exists()

    def test_connection_error_reported(self, tmp_path):
        dest = tmp_path / "fail.txt"

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.download_file.side_effect = OSError("connexion perdue")

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/fail.txt", str(dest))

        assert not result.success
        assert any("connexion perdue" in e for e in result.errors)

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "deep" / "nested" / "file.txt"

        def fake_download(remote_path, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"nested")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.download_file.side_effect = fake_download

        sync = self._make_sync(tmp_path, backend)
        result = sync.fetch_file("/remote/file.txt", str(dest))

        assert result.success
        assert dest.exists()


class TestSyncDirectory:
    def _make_sync(self, tmp_path, mock_backend, extra_ini=None):
        params = {"delete_orphans": "false", "max_workers": "2"}
        if extra_ini:
            params.update(extra_ini)
        ini = make_ini(tmp_path, params)
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        sync._build_backend = _make_backend_factory(mock_backend)
        return sync

    def test_missing_local_dir(self, tmp_path):
        sync = self._make_sync(tmp_path, MagicMock())
        result = sync.sync_directory("/nonexistent/dir")
        assert not result.success

    def test_syncs_all_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("a")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_text("b")

        backend = MagicMock()
        backend.remote_mtime.return_value = None   # tout est nouveau
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_directory(str(src), "/remote")

        assert result.success
        assert len(result.uploaded) == 2
        assert backend.upload_file.call_count == 2

    def test_max_workers_from_config(self, tmp_path):
        """Vérifie que max_workers est bien lu depuis la config."""
        ini = make_ini(tmp_path, {"max_workers": "3"})
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        assert sync.config.max_workers == 3

    def test_max_workers_argument_overrides_config(self, tmp_path):
        """max_workers passé en argument est prioritaire sur la config."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("a")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend, {"max_workers": "5"})

        # On passe max_workers=1 en argument — doit fonctionner sans erreur
        result = sync.sync_directory(str(src), "/remote", max_workers=1)
        assert result.success
        assert len(result.uploaded) == 1

    def test_parallel_uploads_all_complete(self, tmp_path):
        """Tous les fichiers sont uploadés même avec plusieurs workers."""
        src = tmp_path / "src"
        src.mkdir()
        for i in range(10):
            (src / f"file_{i}.txt").write_text(f"content {i}")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend, {"max_workers": "4"})
        result = sync.sync_directory(str(src), "/remote")

        assert result.success
        assert len(result.uploaded) == 10

    def test_skip_when_remote_newer(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "a.txt"
        f.write_text("a")
        future_mtime = f.stat().st_mtime + 3600

        backend = MagicMock()
        backend.remote_mtime.return_value = future_mtime
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_directory(str(src), "/remote")

        assert result.success
        assert len(result.skipped) == 1
        assert len(result.uploaded) == 0
        backend.upload_file.assert_not_called()

    def test_relative_remote_dir_prepends_base_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("a")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_directory(str(src), "subdir")

        assert result.success
        assert result.uploaded[0] == "/remote/subdir/a.txt"

    def test_absolute_remote_dir_used_as_is(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "b.txt").write_text("b")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.list_remote.return_value = []

        sync = self._make_sync(tmp_path, backend)
        result = sync.sync_directory(str(src), "/absolute/dir")

        assert result.uploaded[0] == "/absolute/dir/b.txt"

    def test_delete_orphans(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("a")

        backend = MagicMock()
        backend.remote_mtime.return_value = None
        backend.list_remote.return_value = ["/remote/a.txt", "/remote/orphan.txt"]

        sync = self._make_sync(tmp_path, backend)
        sync.config.delete_orphans = True
        result = sync.sync_directory(str(src), "/remote")

        assert "/remote/orphan.txt" in result.deleted
        backend.delete_remote.assert_called_once_with("/remote/orphan.txt")


# ---------------------------------------------------------------------------
# RemoteSync._resolve_remote
# ---------------------------------------------------------------------------

class TestResolveRemote:
    def _make_sync(self, tmp_path) -> RemoteSync:
        ini = make_ini(tmp_path, {"remote_base_dir": "/public_html"})
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        sync._backend = MagicMock()
        return sync

    def test_absolute_unchanged(self, tmp_path):
        sync = self._make_sync(tmp_path)
        assert sync._resolve_remote("/var/www/index.html") == "/var/www/index.html"

    def test_relative_prepends_base(self, tmp_path):
        sync = self._make_sync(tmp_path)
        assert sync._resolve_remote("sub/index.html") == "/public_html/sub/index.html"

    def test_relative_no_leading_slash(self, tmp_path):
        sync = self._make_sync(tmp_path)
        assert sync._resolve_remote("file.txt") == "/public_html/file.txt"

    def test_base_dir_trailing_slash_handled(self, tmp_path):
        ini = make_ini(tmp_path, {"remote_base_dir": "/public_html/"})
        sync = RemoteSync.__new__(RemoteSync)
        sync.config = SyncConfig.from_ini(ini)
        sync._backend = MagicMock()
        # Pas de double slash
        assert sync._resolve_remote("file.txt") == "/public_html/file.txt"


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------

class TestSyncResult:
    def test_success_when_no_errors(self):
        r = SyncResult(uploaded=["f1"], skipped=["f2"])
        assert r.success

    def test_failure_when_errors(self):
        r = SyncResult(errors=["something went wrong"])
        assert not r.success

    def test_str(self):
        r = SyncResult(uploaded=["a", "b"], skipped=["c"], errors=["e"])
        assert "uploaded=2" in str(r)
        assert "errors=1" in str(r)


# ---------------------------------------------------------------------------
# SyncSession
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, logdir: str = "") -> SyncConfig:
    ini = make_ini(tmp_path, {"logdir": logdir} if logdir else {})
    return SyncConfig.from_ini(ini)


class TestSyncSession:

    def test_add_accumulates_results(self, tmp_path):
        cfg = _make_config(tmp_path)
        session = SyncSession(cfg, label="test")

        r1 = SyncResult(uploaded=["/r/a.txt"], skipped=["/r/b.txt"])
        r2 = SyncResult(uploaded=["/r/c.txt"], errors=["oops"])

        session.add(r1, local="local/a.txt", remote="/r/a.txt", kind="file")
        session.add(r2, local="local/c.txt", remote="/r/c.txt", kind="file")

        assert session.total_uploaded == 2
        assert session.total_skipped == 1
        assert session.total_errors == 1
        assert len(session._entries) == 2

    def test_success_true_when_no_errors(self, tmp_path):
        cfg = _make_config(tmp_path)
        session = SyncSession(cfg)
        session.add(SyncResult(uploaded=["f"]), local="f", remote="/r/f")
        assert session.success

    def test_success_false_when_errors(self, tmp_path):
        cfg = _make_config(tmp_path)
        session = SyncSession(cfg)
        session.add(SyncResult(errors=["boom"]), local="f", remote="/r/f")
        assert not session.success

    def test_summary_contains_counts(self, tmp_path):
        cfg = _make_config(tmp_path)
        session = SyncSession(cfg, label="mysession")
        session.add(
            SyncResult(uploaded=["a", "b"], skipped=["c"], deleted=["d"], errors=["e"]),
            local="src/", remote="/r/", kind="directory",
        )
        s = session.summary()
        assert "mysession" in s
        assert "↑2" in s
        assert "↷1" in s
        assert "✗1" in s
        assert "⚠1" in s

    def test_save_creates_log_file(self, tmp_path):
        logdir = tmp_path / "logs"
        cfg = _make_config(tmp_path, logdir=str(logdir))
        session = SyncSession(cfg, label="deploy")

        r = SyncResult(uploaded=["/r/index.html"], skipped=["/r/style.css"])
        session.add(r, local="dist/", remote="/r/", kind="directory")

        log_path = session.save()

        assert log_path.exists()
        assert log_path.parent == logdir
        assert log_path.suffix == ".log"
        assert "deploy" in log_path.name

    def test_save_uses_explicit_logdir_override(self, tmp_path):
        cfg = _make_config(tmp_path, logdir=str(tmp_path / "default_logs"))
        session = SyncSession(cfg, label="override")
        session.add(SyncResult(uploaded=["x"]), local="x", remote="/x")

        override_dir = tmp_path / "custom_logs"
        log_path = session.save(logdir=str(override_dir))

        assert log_path.parent == override_dir

    def test_save_report_content(self, tmp_path):
        logdir = tmp_path / "logs"
        cfg = _make_config(tmp_path, logdir=str(logdir))
        session = SyncSession(cfg, label="content_check")

        r = SyncResult(
            uploaded=["/r/new.js"],
            skipped=["/r/old.css"],
            deleted=["/r/gone.html"],
            errors=["network timeout"],
        )
        session.add(r, local="dist/", remote="/r/", kind="directory")

        log_path = session.save()
        content = log_path.read_text(encoding="utf-8")

        assert "content_check" in content.upper() or "content_check" in content
        assert "/r/new.js" in content
        assert "/r/old.css" in content
        assert "/r/gone.html" in content
        assert "network timeout" in content
        assert "ÉCHEC" in content  # erreurs présentes → statut ÉCHEC

    def test_save_fallback_to_cwd_when_no_logdir(self, tmp_path, monkeypatch):
        cfg = _make_config(tmp_path)  # logdir vide
        cfg.logdir = ""
        session = SyncSession(cfg, label="fallback")
        session.add(SyncResult(uploaded=["f"]), local="f", remote="/f")

        monkeypatch.chdir(tmp_path)
        log_path = session.save()

        assert log_path.parent == tmp_path
        assert log_path.exists()

    def test_label_sanitized_in_filename(self, tmp_path):
        logdir = tmp_path / "logs"
        cfg = _make_config(tmp_path, logdir=str(logdir))
        session = SyncSession(cfg, label="mon déploiement/prod")
        session.add(SyncResult(), local=".", remote="/")

        log_path = session.save()
        # Les espaces et / doivent être remplacés par _
        assert " " not in log_path.name
        assert "/" not in log_path.name
