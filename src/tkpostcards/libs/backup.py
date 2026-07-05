# -*- encoding: utf-8 -*-
import os


class _ProgressFileWrapper:
    """
    Enveloppe un objet fichier pour signaler les octets lus/écrits à un
    tracker de progression (typiquement une instance tqdm, mais tout objet
    exposant une méthode .update(n) convient).

    Si `progress` vaut None, le wrapper se contente de relayer les appels
    sans overhead de suivi.
    """

    def __init__(self, fileobj, progress):
        self._fileobj = fileobj
        self._progress = progress

    def read(self, size=-1):
        data = self._fileobj.read(size)
        if self._progress is not None and data:
            self._progress.update(len(data))
        return data

    def write(self, data):
        n = self._fileobj.write(data)
        if self._progress is not None:
            self._progress.update(len(data))
        return n

    def __getattr__(self, name):
        # Délègue tout le reste (close, tell, seek, etc.) au fichier réel
        return getattr(self._fileobj, name)


def _set_progress_total(progress, total):
    """
    Configure le total sur l'objet de progression si possible (ex: tqdm).
    Ne fait rien si l'objet ne supporte pas cet attribut (objet minimal,
    ou progress=None).
    """
    if progress is None:
        return
    try:
        progress.total = total
        refresh = getattr(progress, "refresh", None)
        if callable(refresh):
            refresh()
    except AttributeError:
        pass


class PostcardBackup:

    @staticmethod
    def _iter_source_files(source_dir):
        """
        Liste tous les fichiers qui seront inclus dans l'archive, sous
        forme de tuples (chemin_absolu, arcname).
        """
        files = []

        cards_dir = os.path.join(source_dir, 'cards')
        if os.path.isdir(cards_dir):
            for root, _dirs, filenames in os.walk(cards_dir):
                for name in filenames:
                    full_path = os.path.join(root, name)
                    arcname = os.path.join(
                        'cards', os.path.relpath(full_path, cards_dir)
                    )
                    files.append((full_path, arcname))

        verification_dir = os.path.join(source_dir, 'verification')
        if os.path.isdir(verification_dir):
            for root, _dirs, filenames in os.walk(verification_dir):
                for name in filenames:
                    full_path = os.path.join(root, name)
                    arcname = os.path.join(
                        'verification', os.path.relpath(full_path, cards_dir)
                    )
                    files.append((full_path, arcname))

        for f in ['travels.json', 'pois.json']:
            fname = os.path.join(source_dir, f)
            if os.path.isfile(fname):
                files.append((fname, f))

        return files

    @staticmethod
    def create_backup(source_dir, archive_path=None, compression_level=10, progress=None):
        """
        Crée une archive tar compressée en zstd.

        :param archive_path: chemin de l'archive à créer.
            - Si None : un nom par défaut horodaté est généré
              (ex: "archive_2026_07_05_14_30_00.tar.zst") dans le
              répertoire courant.
            - Si c'est un répertoire existant : le nom par défaut
              horodaté est généré et placé dans ce répertoire.
            - Sinon : utilisé tel quel comme chemin de l'archive.
        :param progress: objet de suivi de progression optionnel (ex: une
            instance tqdm). Doit exposer une méthode .update(n) où n est le
            nombre d'octets traités. Peut être None (pas de suivi) ou tout
            autre objet compatible.
        """
        import tarfile
        import datetime
        import pyzstd

        source_dir = os.path.abspath(source_dir)

        default_name = (
            "archive_" + datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S") + ".tar.zst"
        )
        if archive_path is None:
            archive_path = default_name
        elif os.path.isdir(archive_path):
            archive_path = os.path.join(archive_path, default_name)

        files_to_add = PostcardBackup._iter_source_files(source_dir)
        total_size = sum(os.path.getsize(path) for path, _ in files_to_add)
        _set_progress_total(progress, total_size)

        # pyzstd.ZstdFile se comporte comme un objet fichier classique
        # (contrairement à zstandard, pas besoin de stream_writer séparé),
        # il peut donc être passé directement à tarfile.
        with pyzstd.ZstdFile(
            archive_path, mode="w", level_or_option=compression_level
        ) as zf:
            with tarfile.open(fileobj=zf, mode="w|") as tar:
                for full_path, arcname in files_to_add:
                    tarinfo = tar.gettarinfo(full_path, arcname=arcname)
                    with open(full_path, "rb") as f:
                        wrapped = _ProgressFileWrapper(f, progress)
                        tar.addfile(tarinfo, fileobj=wrapped)

        return archive_path

    @staticmethod
    def extract_backup(archive_path, destination_dir, progress=None):
        """
        Extrait une archive tar.zst.

        :param progress: objet de suivi de progression optionnel (ex: une
            instance tqdm). Doit exposer une méthode .update(n). Peut être
            None (pas de suivi) ou tout autre objet compatible.

            Note: en lecture "streaming" (mode r|), la taille décompressée
            n'est pas connue à l'avance. Le suivi se base donc sur la
            taille compressée du fichier archive sur disque : la
            progression reste fluide et représentative, sans être exacte
            à l'octet près.
        """
        import tarfile
        import pyzstd

        os.makedirs(destination_dir, exist_ok=True)

        total_size = os.path.getsize(archive_path)
        _set_progress_total(progress, total_size)

        with open(archive_path, "rb") as raw_archive_file:
            # On enveloppe le fichier brut (octets compressés) pour suivre
            # la progression, puis on le passe à ZstdFile qui décompresse
            # à la volée et se comporte comme un objet fichier classique.
            archive_file = _ProgressFileWrapper(raw_archive_file, progress)
            with pyzstd.ZstdFile(archive_file, mode="r") as zf:
                with tarfile.open(fileobj=zf, mode="r|") as tar:
                    tar.extractall(path=destination_dir)
