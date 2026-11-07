# -*- encoding: utf-8 -*-
"""
libpostcards/linkmeta.py - Aide à la saisie des liens (table ``links``,
voir libpostcards.model.Model.write_link) :

- :func:`normalize_url` : nettoie une URL saisie à la main ;
- :func:`suggest_link_id` : propose un identifiant de lien à partir de
  l'URL (``https://fr.wikipedia.org/wiki/Louhans`` -> ``wikipedia-louhans``) ;
- :func:`fetch_link_metadata` : télécharge la page et en extrait titre et
  description (balises OpenGraph, avec repli sur ``<title>`` et
  ``<meta name="description">``).

Aucune dépendance au-delà de ``requests`` : l'extraction s'appuie sur
``html.parser`` de la bibliothèque standard.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

import requests

# Taille maximale lue pour une page (le <head> suffit : og:* y figurent).
_MAX_BYTES = 512 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (compatible; pypostcards-linkcheck/1.0; "
    "+https://github.com/bibi21000/pypostcards)"
)

# Suffixes "génériques" de second niveau (co.uk, com.au...) : le nom
# significatif est alors le label précédent.
_GENERIC_SLDS = {"co", "com", "org", "net", "gov", "edu", "ac", "asso"}


def normalize_url(url: str | None) -> str:
    """Nettoie une URL saisie : espaces retirés, ``https://`` ajouté si
    aucun schéma n'est indiqué. Retourne ``""`` si ``url`` est vide."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url.lstrip("/")
    return url


def _slug(text: str) -> str:
    """Minuscules, sans accents, caractères non alphanumériques -> ``-``."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def suggest_link_id(url: str, existing: set[str] | list[str] | None = None) -> str:
    """Propose un identifiant de lien à partir de ``url``.

    L'identifiant est formé du nom du site (``www.`` et extension
    retirés) suivi du dernier segment significatif du chemin (sans
    extension) : ``https://fr.wikipedia.org/wiki/Louhans`` ->
    ``wikipedia-louhans``, ``https://www.louhans.fr/`` -> ``louhans``.

    Si ``existing`` est fourni, un suffixe ``-2``, ``-3``... est ajouté
    tant que l'identifiant est déjà pris. Retourne ``""`` si l'URL n'a
    pas d'hôte exploitable.
    """
    parsed = urlparse(normalize_url(url))
    host = (parsed.hostname or "").lower()
    if not host:
        return ""

    labels = [p for p in host.split(".") if p and p != "www"]
    try:
        ipaddress.ip_address(host)
        labels = [host]  # une adresse IP n'a pas de "nom de site" : on la garde entière
    except ValueError:
        pass
    if len(labels) >= 3 and labels[-2] in _GENERIC_SLDS:
        site = labels[-3]
    elif len(labels) >= 2:
        site = labels[-2]
    else:
        site = labels[0] if labels else host

    segments = [unquote(seg) for seg in parsed.path.split("/") if seg]
    last = ""
    if segments:
        last = re.sub(r"\.[a-zA-Z0-9]{1,5}$", "", segments[-1])
        # Un segment purement numérique ne dit rien : on le garde quand
        # même s'il est seul (ex : /12345), mais derrière le précédent.
        if last.isdigit() and len(segments) > 1:
            last = f"{segments[-2]}-{last}"

    base = "-".join(p for p in (_slug(site), _slug(last)) if p) or _slug(host)
    base = base[:60].strip("-")

    taken = {str(x) for x in (existing or [])}
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


class _MetaParser(HTMLParser):
    """Extrait les balises <meta> OpenGraph/description et <title>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_metadata(html: str) -> dict:
    """Extrait ``{"title", "description", "site_name"}`` d'un document
    HTML (chaînes vides si absents)."""
    parser = _MetaParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # HTML très malformé : on garde ce qui a été lu
        pass
    meta = parser.meta
    return {
        "title": _clean_text(meta.get("og:title") or meta.get("twitter:title")
                             or parser.title),
        "description": _clean_text(meta.get("og:description")
                                   or meta.get("twitter:description")
                                   or meta.get("description")),
        "site_name": _clean_text(meta.get("og:site_name")),
    }


def fetch_link_metadata(url: str, timeout: float = 8.0) -> dict:
    """Télécharge ``url`` et retourne ``{"title", "description",
    "site_name", "final_url", "status_code"}``.

    Lève ``ValueError`` si l'URL n'est pas http(s) et
    ``requests.RequestException`` en cas d'erreur réseau ou de statut
    HTTP >= 400 (le lien est alors considéré comme non valide : l'appelant
    ne doit pas mettre à jour sa date de vérification).
    """
    url = normalize_url(url)
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise ValueError(f"URL invalide : {url!r}")

    with requests.get(
        url,
        headers={"User-Agent": _USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                 "Accept-Language": "fr,en;q=0.8"},
        timeout=timeout,
        stream=True,
        allow_redirects=True,
    ) as response:
        response.raise_for_status()
        ctype = response.headers.get("Content-Type", "").lower()
        result = {
            "title": "", "description": "", "site_name": "",
            "final_url": response.url, "status_code": response.status_code,
        }
        if ctype and "html" not in ctype and "xml" not in ctype:
            # Lien valide mais pas une page web (PDF, image...) : pas de
            # métadonnées à extraire.
            return result

        raw = b""
        for chunk in response.iter_content(chunk_size=16384):
            raw += chunk
            if len(raw) >= _MAX_BYTES or b"</head>" in raw.lower():
                break
        encoding = response.encoding
        if not encoding or encoding.lower() == "iso-8859-1":
            # requests applique ISO-8859-1 par défaut aux text/* sans
            # charset : on préfère la détection par le contenu.
            encoding = response.apparent_encoding or "utf-8"
        html = raw.decode(encoding, errors="replace")

    result.update(parse_metadata(html))
    return result
