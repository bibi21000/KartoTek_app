# -*- encoding: utf-8 -*-
"""
Delcampe : objets suivis, temps restant, vendeur et moyens de paiement
=======================================================================

Sans accès à l'API (réservée aux comptes partenaires), on lit le site web avec
la session de l'utilisateur, exactement comme son navigateur :

1. la liste « Objets suivis » (``/<lang>/buy/favorites/watching-list``) donne,
   pour chaque objet, le vendeur, le prix et la date de fin (epoch Unix lue
   dans ``data-ws-date-end`` : le temps restant est donc calculé à l'instant de
   l'affichage, pas figé à celui du chargement de la page) ;
2. les moyens de paiement sont une propriété du VENDEUR : on ne les lit qu'UNE
   fois par vendeur (page boutique, à défaut page d'un de ses objets), puis on
   les garde dans un cache disque (``cache_days`` jours, 7 par défaut).

Authentification : le cookie de session copié depuis le navigateur (jamais
le mot de passe : la connexion automatique se heurte souvent à un captcha).
Le cookie équivaut à un mot de passe : ne le journalisez pas, ne le partagez
pas. S'il expire, :class:`DelcampeAuthError` demande de le recopier.

Courtoisie : une requête à la fois, ``delay`` secondes entre deux requêtes
(1 s par défaut), nouvelles tentatives espacées sur 429/5xx.

Le parsing (``parse_*``) est séparé du réseau et testable sur des pages
enregistrées. Il s'appuie sur les attributs ``data-*`` et les classes
sémantiques du site plutôt que sur les libellés, donc sur la langue
d'affichage seulement pour les noms des moyens de paiement et le texte
« Vous êtes en tête ».
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import _

logger = logging.getLogger(__name__)

BASE_URL = "https://www.delcampe.net"
DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) kttools-delcampe/1.0 (personal use)"
LIST_TYPES = ("open", "open_with_offer", "closed", "all")
_LOGOUT_MARK = "my-account/logout"


class DelcampeError(Exception):
    """Erreur générique (réseau, page inattendue, protection anti-bot...)."""


class DelcampeAuthError(DelcampeError):
    """Session absente, refusée ou expirée."""


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------

@dataclass
class WatchedItem:
    item_id: str
    title: str
    url: str
    seller_id: str
    seller_nick: str
    seller_rating: str = ""            # "100%"
    seller_feedbacks: Optional[int] = None
    sale_type: str = ""                # "bid" | "fixed" | ""
    price_text: str = ""               # "3,00 €"
    price: Optional[float] = None
    currency: str = ""
    original_price_text: str = ""      # prix barré (réduction)
    end_ts: Optional[int] = None       # epoch Unix
    end_label: str = ""                # "24/10/2026 à 17:47" tel qu'affiché
    bid_status: str = ""               # "1 enchère • Vous êtes en tête"
    leading: Optional[bool] = None

    def seconds_left(self, now: Optional[float] = None) -> Optional[float]:
        if self.end_ts is None:
            return None
        return self.end_ts - (time.time() if now is None else now)


@dataclass
class Seller:
    seller_id: str
    nick: str
    url: str = ""
    shop_url: str = ""
    rating: str = ""
    feedbacks: Optional[int] = None
    payment_methods: Optional[list[str]] = None   # None = inconnu
    payment_codes: list[str] = field(default_factory=list)
    payments_source: str = ""                     # "store" | "item" | "cache" | ""


@dataclass
class SellerGroup:
    seller: Seller
    items: list[WatchedItem]

    def total(self) -> Optional[tuple[float, str]]:
        """Somme des prix courants, si tous dans la même devise."""
        if not self.items or any(i.price is None for i in self.items):
            return None
        currencies = {i.currency for i in self.items}
        if len(currencies) != 1:
            return None
        return sum(i.price for i in self.items), currencies.pop()


# ---------------------------------------------------------------------------
# Parsing (pur : aucune requête réseau)
# ---------------------------------------------------------------------------

def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el is not None else ""


def parse_price(text: str) -> tuple[Optional[float], str]:
    """``"1 234,50 €"`` -> ``(1234.5, "€")`` ; ``(None, "")`` si illisible."""
    m = re.match(r"^\s*([\d\s\u00a0\u202f.,]+?)\s*([^\d\s.,][^\d]*)?\s*$", text or "")
    if not m:
        return None, ""
    num = re.sub(r"[\s\u00a0\u202f]", "", m.group(1))
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".")
    else:
        num = num.replace(",", ".")
    try:
        return float(num), (m.group(2) or "").strip()
    except ValueError:
        return None, ""


def is_logged_in(html_text: str) -> bool:
    return _LOGOUT_MARK in html_text


def parse_watching_list(
    html_text: str, base_url: str = BASE_URL
) -> tuple[list[WatchedItem], int, Optional[int]]:
    """``(objets, nombre_de_pages, nombre_total_d_objets_suivis)``."""
    soup = BeautifulSoup(html_text, "html.parser")
    items: list[WatchedItem] = []
    for box in soup.select("[data-watched-item-container]"):
        item_id = (box.get("data-watched-item-container") or "").strip()
        link = box.select_one("a.item-title.item-link")
        nick_a = box.select_one("a.nickname")
        if not item_id or link is None or nick_a is None:
            logger.warning("Objet suivi illisible (markup inattendu) : %r", item_id)
            continue
        m = re.search(r"/profile/(\d+)", nick_a.get("href", ""))
        seller_id = m.group(1) if m else ""
        if not seller_id:  # repli : lien « Voir la boutique »
            shop = box.select_one("a[href*='seller_ids']")
            m = re.search(r"seller_ids[^=]*=(\d+)", shop.get("href", "") if shop else "")
            seller_id = m.group(1) if m else nick_a.get_text(strip=True)

        rating = box.select_one("a.evaluation .percentage")
        number = box.select_one("a.evaluation .number")
        feedbacks = None
        if number is not None:
            digits = re.sub(r"\D", "", number.get_text())
            feedbacks = int(digits) if digits else None

        disp = box.select_one(".display-price")
        price_el = None
        if disp is not None:
            for s in disp.select("strong.no-margin"):
                if "line-through" not in (s.get("class") or []):
                    price_el = s
                    break
        old_el = disp.select_one("strong.line-through") if disp is not None else None
        price_text = _text(price_el)
        price, currency = parse_price(price_text)

        icon = disp.select_one(".selling-type-icon") if disp is not None else None
        classes = (icon.get("class") or []) if icon is not None else []
        sale_type = "bid" if "bid" in classes else "fixed" if "fixed-price" in classes else ""

        end_ts = None
        end_in = box.select_one("input[data-ws-date-end]")
        if end_in is not None and (end_in.get("value") or "").strip().isdigit():
            end_ts = int(end_in["value"])

        status_el = box.select_one(".block-push strong[data-original-title]")
        bid_status = " ".join((status_el.get("data-original-title") or "").split()) if status_el else ""

        href = link.get("href", "")
        items.append(WatchedItem(
            item_id=item_id,
            title=" ".join((link.get("title") or link.get_text()).split()),
            url=urljoin(base_url, href),
            seller_id=seller_id,
            seller_nick=nick_a.get_text(strip=True),
            seller_rating=_text(rating),
            seller_feedbacks=feedbacks,
            sale_type=sale_type,
            price_text=price_text,
            price=price,
            currency=currency,
            original_price_text=_text(old_el),
            end_ts=end_ts,
            end_label=_text(box.select_one("span.sale-end")),
            bid_status=bid_status,
            leading=("en tête" in bid_status.lower() or "leading" in bid_status.lower()
                     or "highest" in bid_status.lower()) if bid_status else None,
        ))

    pages = 1
    span = soup.select_one("[data-pagination-page-number]")
    if span is not None and (span.get("data-pagination-page-number") or "").isdigit():
        pages = max(1, int(span["data-pagination-page-number"]))
    total = None
    cnt = soup.select_one("[data-watched-items-count]")
    if cnt is not None:
        digits = re.sub(r"\D", "", cnt.get_text())
        total = int(digits) if digits else None
    return items, pages, total


def parse_payment_methods(html_text: str) -> Optional[tuple[list[str], list[str]]]:
    """
    Moyens de paiement du vendeur d'après une page boutique ou une page objet :
    ``(libellés, codes)`` ou ``None`` si la page n'en affiche pas.

    On ne lit que les zones « vendeur » (``#payment-methods`` de la boutique,
    ``.payment-methods-info`` et fiche vendeur de la page objet), pas les
    listes par zone d'expédition de l'onglet Conditions.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for selector in ("#payment-methods", ".payment-methods-info", "[data-seller-info]"):
        for root in soup.select(selector):
            lis = root.select("ul.payment-methods li")
            if not lis:
                continue
            labels: list[str] = []
            codes: list[str] = []
            for li in lis:
                label = " ".join((li.get("title") or "").split())
                if not label or label in labels:
                    continue
                labels.append(label)
                icon = li.select_one("i[class*='icon-']")
                code = ""
                for c in (icon.get("class") or []) if icon is not None else []:
                    if c.startswith("icon-"):
                        code = c[len("icon-"):]
                codes.append(code)
            if labels:
                return labels, codes
    return None


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------

def parse_cookie_header(value: str) -> dict[str, str]:
    """``"a=1; b=2"`` (éventuellement précédé de ``Cookie:``) -> ``{a: 1, b: 2}``."""
    value = re.sub(r"^\s*cookie\s*:\s*", "", value.strip(), flags=re.I)
    out: dict[str, str] = {}
    for part in value.split(";"):
        name, sep, val = part.strip().partition("=")
        if sep and name:
            out[name.strip()] = val.strip()
    return out


def load_cookies(cookie: str = "", cookie_file: str = "") -> dict[str, str]:
    """
    Cookies de session. Priorité : ``cookie`` (en-tête brut), puis
    ``cookie_file`` (en-tête brut OU fichier ``cookies.txt`` au format
    Netscape, tel qu'exporté par les extensions de navigateur).
    """
    if cookie and cookie.strip():
        return parse_cookie_header(cookie)
    if cookie_file:
        text = Path(cookie_file).expanduser().read_text(encoding="utf-8", errors="replace")
        jar: dict[str, str] = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) == 7:
                if "delcampe" in fields[0]:
                    jar[fields[5]] = fields[6]
        return jar or parse_cookie_header(text)
    return {}


# ---------------------------------------------------------------------------
# Cache des vendeurs
# ---------------------------------------------------------------------------

def default_cache_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "kttools" / "delcampe_sellers.json"


class SellerCache:
    """Moyens de paiement par vendeur, sur disque (écriture atomique)."""

    def __init__(self, path: Optional[Path] = None, ttl_days: float = 7.0):
        self.path = Path(path) if path else default_cache_file()
        self.ttl = ttl_days * 86400
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, seller_id: str, now: Optional[float] = None) -> Optional[dict]:
        entry = self.data.get(str(seller_id))
        if not entry or not isinstance(entry.get("payment_methods"), list):
            return None
        if (time.time() if now is None else now) - entry.get("fetched_at", 0) > self.ttl:
            return None
        return entry

    def put(self, seller: Seller) -> None:
        self.data[str(seller.seller_id)] = {
            "nick": seller.nick,
            "payment_methods": seller.payment_methods,
            "payment_codes": seller.payment_codes,
            "fetched_at": time.time(),
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Cache vendeurs non enregistré (%s) : %s", self.path, exc)


# ---------------------------------------------------------------------------
# Client HTTP
# ---------------------------------------------------------------------------

class DelcampeClient:
    def __init__(
        self,
        cookies: dict[str, str],
        lang: str = "fr",
        base_url: str = BASE_URL,
        delay: float = 1.0,
        user_agent: str = DEFAULT_UA,
        timeout: float = 30.0,
        session=None,
    ):
        import requests  # import tardif : dépendance optionnelle (extra ktmanager)

        self._requests = requests
        self.base_url = base_url.rstrip("/")
        self.lang = lang
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self._last = 0.0
        self.requests_made = 0
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": f"{lang},en;q=0.5",
        })
        host = urlparse(self.base_url).hostname or ""
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=host)

    # -- réseau ----------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path: str, params: Optional[dict] = None) -> str:
        url = urljoin(self.base_url + "/", path.lstrip("/")) if not path.startswith("http") else path
        resp = None
        for attempt in range(4):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self.requests_made += 1
            except self._requests.RequestException as exc:
                if attempt == 3:
                    raise DelcampeError(_("Network error on {url}: {exc}").format(url=url, exc=exc))
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                try:
                    wait = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** (attempt + 1)
                logger.warning("HTTP %d sur %s : nouvel essai dans %.0fs", resp.status_code, url, wait)
                time.sleep(min(wait, 60))
                continue
            break
        assert resp is not None
        if resp.status_code in (401, 403):
            raise DelcampeAuthError(
                _("HTTP {code} from Delcampe: session refused or anti-bot protection. "
                  "Copy a fresh cookie from your browser (and its User-Agent, see "
                  "[delcampe] user_agent).").format(code=resp.status_code))
        if resp.status_code >= 400:
            raise DelcampeError(f"HTTP {resp.status_code} sur {url}")
        if re.search(r"/(login|connexion|sign-?in)(/|$|\?)", urlparse(resp.url).path):
            raise DelcampeAuthError(_("Delcampe redirected to the login page: session expired."))
        if "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = "utf-8"
        return resp.text

    # -- pages -----------------------------------------------------------

    def fetch_watching_list(
        self,
        list_type: str = "open",
        size: int = 480,
        max_pages: Optional[int] = None,
        on_page: Optional[Callable[[int, int, int, Optional[int]], None]] = None,
    ) -> list[WatchedItem]:
        """Tous les objets suivis (triés par date de fin, pages suivies)."""
        if list_type not in LIST_TYPES:
            raise ValueError(f"type invalide : {list_type!r}")
        items: list[WatchedItem] = []
        seen: set[str] = set()
        page = 1
        while True:
            html_text = self.get(
                f"/{self.lang}/buy/favorites/watching-list",
                params={"page": page, "size": size, "type": list_type,
                        "sorting": "end_date_asc"},
            )
            if not is_logged_in(html_text):
                raise DelcampeAuthError(
                    _("Not logged in: the cookie is missing, wrong or expired."))
            page_items, pages, total = parse_watching_list(html_text, self.base_url)
            for it in page_items:
                if it.item_id not in seen:
                    seen.add(it.item_id)
                    items.append(it)
            if on_page:
                on_page(page, pages, len(items), total)
            if not page_items or page >= pages or (max_pages and page >= max_pages):
                break
            page += 1
        return items

    def fetch_payment_methods(
        self, seller: Seller, sample_item: Optional[WatchedItem] = None
    ) -> Optional[tuple[list[str], list[str], str]]:
        """``(libellés, codes, source)`` : page boutique, à défaut page d'un objet."""
        try:
            html_text = self.get(f"/{self.lang}/collections/search",
                                 params={"seller_ids[0]": seller.seller_id, "size": 12})
            found = parse_payment_methods(html_text)
            if found:
                return found[0], found[1], "store"
        except DelcampeAuthError:
            raise
        except DelcampeError as exc:
            logger.warning("Boutique de %s illisible : %s", seller.nick, exc)
        if sample_item is not None:
            try:
                found = parse_payment_methods(self.get(sample_item.url))
                if found:
                    return found[0], found[1], "item"
            except DelcampeAuthError:
                raise
            except DelcampeError as exc:
                logger.warning("Objet %s illisible : %s", sample_item.item_id, exc)
        return None


# ---------------------------------------------------------------------------
# Regroupement
# ---------------------------------------------------------------------------

def build_sellers(
    items: Iterable[WatchedItem],
    client: Optional[DelcampeClient],
    cache: Optional[SellerCache] = None,
    refresh: bool = False,
    on_seller: Optional[Callable[[Seller], None]] = None,
) -> dict[str, Seller]:
    """
    Un :class:`Seller` par vendeur. Les moyens de paiement viennent du cache
    s'il est frais, sinon d'UNE requête par vendeur (désactivée si ``client``
    est ``None``). Seules les réponses réussies sont mises en cache.
    """
    first: dict[str, WatchedItem] = {}
    for it in items:
        first.setdefault(it.seller_id, it)
    sellers: dict[str, Seller] = {}
    base = client.base_url if client else BASE_URL
    lang = client.lang if client else "fr"
    for sid, it in first.items():
        seller = Seller(
            seller_id=sid, nick=it.seller_nick,
            url=f"{base}/{lang}/collections/user/profile/{sid}-{it.seller_nick}",
            shop_url=f"{base}/{lang}/collections/store/{it.seller_nick}",
            rating=it.seller_rating, feedbacks=it.seller_feedbacks,
        )
        sellers[sid] = seller
        entry = None if refresh or cache is None else cache.get(sid)
        if entry is not None:
            seller.payment_methods = list(entry["payment_methods"])
            seller.payment_codes = list(entry.get("payment_codes", []))
            seller.payments_source = "cache"
        elif client is not None:
            found = client.fetch_payment_methods(seller, it)
            if found:
                seller.payment_methods, seller.payment_codes, seller.payments_source = found
                if cache is not None:
                    cache.put(seller)
        if on_seller:
            on_seller(seller)
    if cache is not None:
        cache.save()
    return sellers


def group_by_seller(
    items: Iterable[WatchedItem], sellers: dict[str, Seller], order: str = "end"
) -> list[SellerGroup]:
    """
    Regroupe par vendeur ; objets triés par date de fin. ``order`` :
    ``end`` (vendeur dont un objet se termine le plus tôt d'abord),
    ``name`` (alphabétique), ``count`` (le plus d'objets d'abord).
    """
    buckets: dict[str, list[WatchedItem]] = {}
    for it in items:
        buckets.setdefault(it.seller_id, []).append(it)
    far = float("inf")
    groups = [
        SellerGroup(sellers[sid], sorted(lst, key=lambda i: (i.end_ts if i.end_ts is not None else far, i.item_id)))
        for sid, lst in buckets.items()
    ]
    if order == "name":
        groups.sort(key=lambda g: g.seller.nick.lower())
    elif order == "count":
        groups.sort(key=lambda g: (-len(g.items), g.seller.nick.lower()))
    else:
        groups.sort(key=lambda g: (g.items[0].end_ts if g.items[0].end_ts is not None else far,
                                   g.seller.nick.lower()))
    return groups


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------

def format_remaining(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s <= 0:
        return _("ended")
    if s < 86400:
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 86400}d {s % 86400 // 3600:02d}h"


def format_money(amount: float, currency: str) -> str:
    return f"{amount:.2f}".replace(".", ",") + (f" {currency}" if currency else "")


def _end_local(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts is not None else "?"


def render_text(groups: list[SellerGroup], now: Optional[float] = None, width: int = 70) -> str:
    now = time.time() if now is None else now
    out: list[str] = []
    n_items = sum(len(g.items) for g in groups)
    out.append(_("{n} watched item(s), {s} seller(s)").format(n=n_items, s=len(groups)))
    for g in groups:
        s = g.seller
        head = s.nick
        if s.rating:
            fb = f", {s.feedbacks}" if s.feedbacks is not None else ""
            head += f"  ({s.rating}{fb})"
        head += "  — " + _("{n} item(s)").format(n=len(g.items))
        total = g.total()
        if total:
            head += ", " + format_money(*total)
        out += ["", head]
        pay = ", ".join(s.payment_methods) if s.payment_methods else (
            "?" if s.payment_methods is None else _("none listed"))
        out.append("  " + _("Payment") + " : " + pay)
        out.append("  " + s.shop_url)
        for it in g.items:
            kind = {"bid": _("bid"), "fixed": _("fixed")}.get(it.sale_type, "")
            flag = " " + (_("[LEADING]") if it.leading else _("[outbid?]")) if it.leading is not None else ""
            title = it.title if len(it.title) <= width else it.title[: width - 1] + "…"
            out.append(
                f"    {_end_local(it.end_ts)}  {format_remaining(it.seconds_left(now)):>9}  "
                f"{kind:<5} {it.price_text:>10}{flag}  {it.item_id}  {title}"
            )
    return "\n".join(out)


def to_jsonable(groups: list[SellerGroup], now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    result = []
    for g in groups:
        total = g.total()
        seller = asdict(g.seller)
        seller["total"] = {"amount": total[0], "currency": total[1]} if total else None
        items = []
        for it in g.items:
            d = asdict(it)
            left = it.seconds_left(now)
            d["seconds_left"] = None if left is None else max(0, int(left))
            d["end_iso"] = (datetime.fromtimestamp(it.end_ts, tz=timezone.utc).isoformat()
                            if it.end_ts is not None else None)
            items.append(d)
        result.append({"seller": seller, "items": items})
    return result


def render_csv(groups: list[SellerGroup], now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["seller_id", "seller", "rating", "payment_methods", "item_id", "title",
                "sale_type", "price", "currency", "end_iso", "seconds_left", "bid_status", "url"])
    for g in groups:
        for it in g.items:
            left = it.seconds_left(now)
            w.writerow([
                g.seller.seller_id, g.seller.nick, g.seller.rating,
                "; ".join(g.seller.payment_methods or []), it.item_id, it.title,
                it.sale_type, "" if it.price is None else it.price, it.currency,
                datetime.fromtimestamp(it.end_ts, tz=timezone.utc).isoformat() if it.end_ts is not None else "",
                "" if left is None else max(0, int(left)), it.bid_status, it.url,
            ])
    return buf.getvalue()
