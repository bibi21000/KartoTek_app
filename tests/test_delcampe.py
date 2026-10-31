"""
Tests de `tkpostcards.libs.delcampe` et de `kttools delcampe watchlist`.

Les fixtures (tests/fixtures/delcampe/) sont des extraits VERBATIM de vraies
pages Delcampe (liste des objets suivis, page objet, boutique vendeur).

    PYTHONPATH=src pytest tests/test_delcampe.py -v
"""
from __future__ import annotations

import configparser
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from tkpostcards.libs import delcampe as dc

FIX = Path(__file__).parent / "fixtures" / "delcampe"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- parsing

def test_parse_price():
    assert dc.parse_price("3,00 €") == (3.0, "€")
    assert dc.parse_price("1 234,50 €") == (1234.5, "€")
    assert dc.parse_price("1\u202f234,50\u00a0€") == (1234.5, "€")
    assert dc.parse_price("10.99 $") == (10.99, "$")
    assert dc.parse_price("n/a") == (None, "")


def test_parse_watching_list():
    items, pages, total = dc.parse_watching_list(fx("watching_list_page1.html"))
    assert (pages, total) == (2, 477)
    assert [i.item_id for i in items] == [
        "2526117047", "983116934", "983117087", "288554159", "1069429543"]

    a = items[0]
    assert (a.seller_id, a.seller_nick) == ("618485", "pastouille74")
    assert (a.seller_rating, a.seller_feedbacks) == ("100%", 15482)
    assert a.title == "CPA : 14 x 9 - LOUHANS - Vue Générale"
    assert a.url == "https://www.delcampe.net/fr/collections/item/2526117047.html"
    assert (a.sale_type, a.price, a.currency, a.price_text) == ("bid", 3.0, "€", "3,00 €")
    assert a.end_ts == 1792856820 and a.end_label == "24/10/2026 à 17:47"
    assert a.leading is None and a.bid_status == ""
    assert a.seconds_left(now=1792856820 - 8) == 8

    b = items[1]  # enchère où l'on est en tête
    assert b.leading is True and "Vous êtes en tête" in b.bid_status

    assert items[3].sale_type == "fixed"
    d = items[4]  # prix réduit : le prix courant n'est PAS le prix barré, ni celui de la modale
    assert (d.price, d.original_price_text) == (8.99, "9,99 €")
    assert "L'élevage" in d.title          # entités HTML décodées

    p2, _, _ = dc.parse_watching_list(fx("watching_list_page2.html"))
    assert p2[0].price == 1234.5            # séparateur de milliers


def test_parse_payment_methods():
    assert dc.parse_payment_methods(fx("store_with_payments.html")) == (
        ["Visa & Mastercard", "Virement bancaire"], ["card", "bank_wire"])
    # page objet : encart vendeur ; la liste par zone d'expédition est ignorée
    assert dc.parse_payment_methods(fx("item_page.html"))[0] == ["Visa & Mastercard", "Virement bancaire"]
    assert dc.parse_payment_methods(fx("store_without_payments.html")) is None


def test_is_logged_in():
    assert dc.is_logged_in(fx("watching_list_page1.html"))
    assert not dc.is_logged_in(fx("login.html"))


# ---------------------------------------------------------------- cookie

def test_cookie_parsing(tmp_path):
    assert dc.parse_cookie_header("Cookie: a=1; b=x=y ;junk; c=3") == {"a": "1", "b": "x=y", "c": "3"}
    assert dc.load_cookies("a=1") == {"a": "1"}
    assert dc.load_cookies("", "") == {}
    raw = tmp_path / "raw.txt"
    raw.write_text("PHPSESSID=abc; remember=zz")
    assert dc.load_cookies("", str(raw)) == {"PHPSESSID": "abc", "remember": "zz"}
    nets = tmp_path / "cookies.txt"
    nets.write_text("# Netscape HTTP Cookie File\n"
                    ".delcampe.net\tTRUE\t/\tTRUE\t0\tPHPSESSID\tn1\n"
                    "#HttpOnly_.delcampe.net\tTRUE\t/\tTRUE\t0\tsec\tn2\n"
                    ".autre.com\tTRUE\t/\tTRUE\t0\tnope\tx\n")
    assert dc.load_cookies("", str(nets)) == {"PHPSESSID": "n1", "sec": "n2"}


# ---------------------------------------------------------------- regroupement / rendu

def _items():
    return dc.parse_watching_list(fx("watching_list_page1.html"))[0] + \
           dc.parse_watching_list(fx("watching_list_page2.html"))[0]


def _sellers(items, pay=("Visa & Mastercard",)):
    return {s: dc.Seller(s, n, payment_methods=list(pay)) for s, n in
            {i.seller_id: i.seller_nick for i in items}.items()}


def test_group_by_seller_orders():
    items = _items()
    sellers = _sellers(items)
    g = dc.group_by_seller(items, sellers, "end")
    assert [x.seller.nick for x in g] == ["pastouille74", "lucas1378", "ephrim", "thalie39", "nepso45"]
    lucas = g[1]
    assert [i.item_id for i in lucas.items] == ["983116934", "983117087"]
    assert lucas.total() == (4.5, "€")
    assert [x.seller.nick for x in dc.group_by_seller(items, sellers, "name")][:2] == ["ephrim", "lucas1378"]
    assert dc.group_by_seller(items, sellers, "count")[0].seller.nick == "lucas1378"


def test_total_none_when_mixed_currency():
    a = dc.WatchedItem("1", "t", "u", "s", "n", price=1.0, currency="€")
    b = dc.WatchedItem("2", "t", "u", "s", "n", price=1.0, currency="$")
    assert dc.SellerGroup(dc.Seller("s", "n"), [a, b]).total() is None


def test_render_text_json_csv():
    items = _items()
    sellers = _sellers(items)
    sellers["618485"].payment_methods = ["Visa & Mastercard", "Virement bancaire"]
    sellers["618485"].rating, sellers["618485"].feedbacks = "100%", 15482
    sellers["12638"].payment_methods = None
    groups = dc.group_by_seller(items, sellers)
    now = 1792856820 - 8
    text = dc.render_text(groups, now=now)
    assert "5 sellers" not in text and "6 watched item(s), 5 seller(s)" in text
    assert "pastouille74  (100%, 15482)" in text
    assert "Payment : Visa & Mastercard, Virement bancaire" in text
    assert "00:00:08" in text and "[LEADING]" in text
    assert "Payment : ?" in text                              # moyens de paiement inconnus
    assert "4,50 €" in text                                    # total lucas1378
    data = dc.to_jsonable(groups, now=now)
    first = data[0]["items"][0]
    assert first["seconds_left"] == 8 and first["end_iso"] == "2026-10-24T15:47:00+00:00"
    assert data[1]["seller"]["total"] == {"amount": 4.5, "currency": "€"}
    csv_text = dc.render_csv(groups, now=now)
    assert csv_text.splitlines()[0].startswith("seller_id,seller,rating")
    assert len(csv_text.splitlines()) == 7


@pytest.mark.parametrize("sec,expected", [
    (None, "?"), (0, "ended"), (-5, "ended"), (8, "00:00:08"), (4389, "01:13:09"),
    (90000, "1d 01h"), (95000, "1d 02h")])
def test_format_remaining(sec, expected):
    assert dc.format_remaining(sec) == expected


# ---------------------------------------------------------------- faux serveur Delcampe

class FakeDelcampe:
    def __init__(self):
        self.log: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body, status=200, headers=None):
                raw = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                outer.log.append(self.path)
                if u.path == "/fr/my-account/login":
                    return self._send(fx("login.html"))
                if "sid=ok" not in (self.headers.get("Cookie") or ""):
                    return self._send("", 302, {"Location": "/fr/my-account/login"})
                if u.path == "/fr/buy/favorites/watching-list":
                    page = q.get("page", ["1"])[0]
                    return self._send(fx(f"watching_list_page{page}.html"))
                if u.path == "/fr/collections/search":
                    sid = q["seller_ids[0]"][0]
                    return self._send(fx("store_without_payments.html" if sid == "99850"
                                         else "store_with_payments.html"))
                if u.path.startswith("/fr/collections/item/"):
                    return self._send(fx("item_page.html"))
                self._send("nope", 404)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def count(self, prefix):
        return sum(1 for p in self.log if p.startswith(prefix))


@pytest.fixture
def site():
    s = FakeDelcampe()
    yield s
    s.httpd.shutdown()


def make_client(site, cookie="sid=ok"):
    return dc.DelcampeClient(dc.parse_cookie_header(cookie), base_url=site.url, delay=0)


def test_full_flow_and_cache(site, tmp_path):
    client = make_client(site)
    pages = []
    items = client.fetch_watching_list(on_page=lambda *a: pages.append(a))
    assert len(items) == 6 and [p[0] for p in pages] == [1, 2]
    assert site.count("/fr/buy/favorites/watching-list") == 2
    assert "type=open" in site.log[0] and "sorting=end_date_asc" in site.log[0]

    cache = dc.SellerCache(tmp_path / "c.json", ttl_days=7)
    sellers = dc.build_sellers(items, client, cache)
    # 5 vendeurs : 5 boutiques + 1 repli sur page objet (lucas1378 : boutique sans encart)
    assert site.count("/fr/collections/search") == 5
    assert site.count("/fr/collections/item/") == 1
    assert sellers["618485"].payment_methods == ["Visa & Mastercard", "Virement bancaire"]
    assert sellers["618485"].payments_source == "store"
    assert sellers["99850"].payments_source == "item"
    assert sellers["99850"].payment_methods == ["Visa & Mastercard", "Virement bancaire"]

    # 2e passage : tout vient du cache, aucune requête vendeur
    before = len(site.log)
    sellers2 = dc.build_sellers(items, make_client(site), dc.SellerCache(tmp_path / "c.json"))
    assert len(site.log) == before
    assert {s.payments_source for s in sellers2.values()} == {"cache"}
    # --refresh : on relit
    dc.build_sellers(items, make_client(site), dc.SellerCache(tmp_path / "c.json"), refresh=True)
    assert site.count("/fr/collections/search") == 10
    # cache expiré
    assert dc.SellerCache(tmp_path / "c.json", ttl_days=7).get("618485", now=2 ** 40) is None


def test_no_payments_makes_no_seller_request(site):
    items = make_client(site).fetch_watching_list()
    before = len(site.log)
    sellers = dc.build_sellers(items, None)
    assert len(site.log) == before and all(s.payment_methods is None for s in sellers.values())


def test_expired_session_is_reported(site):
    with pytest.raises(dc.DelcampeAuthError):
        make_client(site, "sid=expired").fetch_watching_list()


def test_max_pages(site):
    assert len(make_client(site).fetch_watching_list(max_pages=1)) == 5


def test_bad_list_type(site):
    with pytest.raises(ValueError):
        make_client(site).fetch_watching_list("nimporte")


# ---------------------------------------------------------------- CLI

def _ns(site, tmp_path, **kw):
    conf = configparser.ConfigParser()
    conf["delcampe"] = {"cookie": "sid=ok", "base_url": site.url, "delay": "0",
                        "cache_file": str(tmp_path / "cache.json"), **kw}
    return SimpleNamespace(conf=conf)


def test_cli_json(site, tmp_path):
    from tkpostcards.scripts.tktools import delcampe_watchlist
    res = CliRunner().invoke(delcampe_watchlist, ["--format", "json"], obj=_ns(site, tmp_path))
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert [g["seller"]["nick"] for g in data][0] == "pastouille74"
    assert sum(len(g["items"]) for g in data) == 6
    lucas = next(g for g in data if g["seller"]["nick"] == "lucas1378")
    assert lucas["seller"]["payment_methods"] == ["Visa & Mastercard", "Virement bancaire"]
    assert lucas["items"][0]["leading"] is True


def test_cli_text_to_file_and_no_cookie(site, tmp_path):
    from tkpostcards.scripts.tktools import delcampe_watchlist
    out = tmp_path / "out.txt"
    res = CliRunner().invoke(delcampe_watchlist, ["--no-payments", "-o", str(out), "--order", "name"],
                             obj=_ns(site, tmp_path))
    assert res.exit_code == 0, res.output
    assert "ephrim" in out.read_text(encoding="utf-8") and site.count("/fr/collections/search") == 0

    ns = _ns(site, tmp_path)
    ns.conf["delcampe"]["cookie"] = ""
    res = CliRunner().invoke(delcampe_watchlist, [], obj=ns, env={"DELCAMPE_COOKIE": ""})
    assert res.exit_code != 0 and "No session cookie" in res.output

    ns.conf["delcampe"]["cookie"] = "sid=expired"
    res = CliRunner().invoke(delcampe_watchlist, [], obj=ns)
    assert res.exit_code != 0 and "session expired" in res.output
