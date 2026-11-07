"""Sites flpostcards rattachés aux clés API.

Une clé peut porter ``"site": "<nom>"`` : ses publications sont alors relayées
vers le flpostcards de ce site, et le manager sait mesurer l'espace disque de
ses données. Les sites sont décrits dans ``KV_SITES_FILE`` (rechargé quand il
change) :

    {
      "acme": {
        "url": "http://fl-acme:5000",          # upstream des publications
        "datadir": "/srv/sites/acme/datadir",  # mesure locale (volume monté)
        "sync_token": "...",                   # OU mesure distante, voir ci-dessous
        "sync_prefix": "/api/v1/sync"          # optionnel
      }
    }

Mesure de l'espace disque, par ordre de préférence :
  1. ``datadir`` : parcours du répertoire monté dans ce conteneur (en lecture
     seule suffit). Donne la taille réelle ET l'espace alloué sur le disque.
     Ne demande aucun secret.
  2. ``sync_token`` : interroge ``GET <url><sync_prefix>/usage`` de flpostcards
     (mesure mise en cache 4 h côté flpostcards, invalidée à chaque synchro ;
     donne aussi l'espace alloué). Avec un flpostcards plus ancien, sans ce
     point d'entrée, retombe sur ``/list`` (somme des tailles, sans l'espace
     alloué ni les fichiers protégés). Exige de confier à la passerelle le jeton
     de synchronisation : à ne faire que si le volume ne peut pas être monté.
Sans l'un ni l'autre, la mesure est indisponible.

Une clé SANS ``site`` utilise le flpostcards par défaut (``KV_FLPOSTCARDS_URL``).
Une clé dont le site n'existe pas (ou plus) est REFUSÉE (501) au lieu de
retomber sur le site par défaut : elle écrirait chez un autre client.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from .config import Settings, get_settings, read_json_cached
from pathlib import Path


@dataclass(frozen=True)
class Site:
    name: str
    url: str = ""
    datadir: str = ""
    sync_token: str = ""
    sync_prefix: str = "/api/v1/sync"


@dataclass(frozen=True)
class DiskUsage:
    bytes: int  # somme des tailles de fichiers
    files: int
    disk_bytes: int | None  # espace alloué (blocs) ; None si inconnu (ancien flpostcards)
    source: str  # "datadir" | "remote" | "remote-list"
    measured_at: float | None = None  # mesure distante : horodatage de la mesure
    cached: bool | None = None  # mesure distante : servie depuis le cache de flpostcards


def load_sites(settings: Settings | None = None) -> dict[str, Site]:
    settings = settings or get_settings()
    path = settings.sites_file
    if not Path(path).is_file():
        return {}
    raw = read_json_cached(path)
    sites = {}
    for name, meta in raw.items():
        sites[name] = Site(
            name=name,
            url=str(meta.get("url", "")).strip().rstrip("/"),
            datadir=str(meta.get("datadir", "")).strip(),
            sync_token=str(meta.get("sync_token", "")).strip(),
            sync_prefix="/" + str(meta.get("sync_prefix", "/api/v1/sync")).strip("/"),
        )
    return sites


def local_usage(datadir: str) -> DiskUsage:
    """Parcours de ``datadir`` sans suivre les liens symboliques ; un fichier à
    liens multiples n'est compté qu'une fois."""
    total = disk = files = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, names in os.walk(datadir, followlinks=False):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue  # supprimé pendant le parcours
            if (st.st_mode & 0o170000) != 0o100000:  # fichier ordinaire seulement
                continue
            if st.st_nlink > 1:
                if (st.st_dev, st.st_ino) in seen:
                    continue
                seen.add((st.st_dev, st.st_ino))
            files += 1
            total += st.st_size
            disk += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return DiskUsage(total, files, disk, "datadir")


def remote_usage(site: Site, *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None) -> DiskUsage:
    headers = {"Authorization": "Bearer " + site.sync_token}
    base = site.url + site.sync_prefix
    with httpx.Client(timeout=timeout, transport=transport) as http:
        resp = http.get(base + "/usage", headers=headers)
        if resp.status_code in (404, 405):
            # flpostcards sans /usage : repli sur l'inventaire complet.
            resp = http.get(base + "/list", headers=headers)
            resp.raise_for_status()
            files = resp.json().get("files", {})
            return DiskUsage(sum(int(v[0]) for v in files.values()), len(files),
                             None, "remote-list")
    resp.raise_for_status()
    data = resp.json()
    return DiskUsage(int(data["bytes"]), int(data["files"]), data.get("disk_bytes"),
                     "remote", data.get("measured_at"), data.get("cached"))


def disk_usage(site: Site) -> DiskUsage | None:
    """None si ce site n'a aucun moyen de mesure configuré. Lève OSError /
    httpx.HTTPError si la mesure échoue."""
    if site.datadir:
        if not os.path.isdir(site.datadir):
            raise OSError(f"datadir introuvable : {site.datadir}")
        return local_usage(site.datadir)
    if site.sync_token and site.url:
        return remote_usage(site)
    return None


def human(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if size < 1024 or unit == "To":
            return f"{size:.0f} {unit}" if unit == "o" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} o"
