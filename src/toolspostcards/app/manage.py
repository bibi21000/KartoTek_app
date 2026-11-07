#!/usr/bin/env python3
"""Manager des clés API de toolspostcards.

À lancer là où se trouvent les fichiers de configuration (typiquement dans le
conteneur) :

    python -m app.manage keys list
    docker exec -it kartotek-vault-api python -m app.manage keys list

Les modifications sont prises en compte par la passerelle sans redémarrage.

  keys list [--all] [--level L] [--disk] [--json]
                                           clés, statut, site, compteurs (aujourd'hui,
                                           30 jours, total, erreurs, refus, octets) ;
                                           --disk : espace occupé par le site rattaché
  keys show REF                            détail, espace disque du site, compteurs par capacité
  sites [--json]                           sites : clés rattachées, appels, taille des données
  keys create --name N --level L [--site S] ...   crée une clé (affichée une seule fois)
  keys revoke REF [--reason TXT]           révoque (réversible : keys enable)
  keys enable REF                          réactive une clé révoquée/désactivée
  keys set REF [--level L] [--expires D | --no-expiry] [--add-scope S] [--remove-scope S]
  keys rotate REF [--keep-old]             nouvelle clé, mêmes droits ; ancienne révoquée
  keys delete REF --yes                    supprime la clé (l'historique d'usage reste)
  usage [--days N] [--key REF] [--json]    compteurs par clé (ou par jour pour --key)
  requests list [--status pending|approved|rejected|all]
  requests show ID
  requests approve ID --level L [--expires D]   crée la clé (affichée une seule fois)
  requests reject ID [--reason TXT]

REF = identifiant (12 caractères, 6 suffisent s'ils sont uniques) ou nom exact.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import keys as K
from .auth import _is_expired
from .config import get_settings
from .db import days_ago, get_stats
from .levels import get_levels
from .sites import disk_usage, human, load_sites

OK_STATUS = "active"


def _clients_file(args) -> str:
    return args.clients_file or get_settings().clients_file


def _levels() -> list[str]:
    return get_levels().names()


def _check_level(level: str) -> None:
    if level not in _levels():
        raise K.KeysError(f"niveau inconnu '{level}' (connus : {', '.join(_levels())})")


def _check_date(value: str) -> None:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise K.KeysError("date ISO attendue, ex: 2027-01-31")


def _table(rows: list[list], header: list[str]) -> str:
    cells = [header] + [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(header))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _ts(value) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _show_new_key(raw: str, hashed: str, meta: dict) -> None:
    print("Clé API brute (à transmettre au client ; elle ne sera plus jamais affichée) :")
    print(f"  {raw}\n")
    print(f"Identifiant : {K.key_id(hashed)}   nom : {meta['name']}   niveau : {meta.get('level')}")


# ------------------------------------------------------------------ keys
def _check_site(name: str) -> None:
    if name not in load_sites():
        known = ", ".join(load_sites()) or "aucun (voir KV_SITES_FILE)"
        raise K.KeysError(f"site inconnu '{name}' (connus : {known})")


def _disk_cell(site_name: str | None, cache: dict) -> str:
    """Espace occupé par les données du site (mesuré une fois par site)."""
    if not site_name:
        return "-"
    if site_name not in cache:
        site = load_sites().get(site_name)
        try:
            cache[site_name] = disk_usage(site) if site else None
            cache[site_name] = cache[site_name] or "n/a"
        except Exception as exc:  # mesure impossible : on l'indique, on continue
            cache[site_name] = f"erreur ({type(exc).__name__})"
    usage = cache[site_name]
    if isinstance(usage, str):
        return usage
    return human(usage.disk_bytes if usage.disk_bytes is not None else usage.bytes)


def _counters(totals: dict, key: str) -> dict:
    return totals.get(key, {})


def keys_list(args) -> None:
    clients = K.load(_clients_file(args))
    store = get_stats()
    today_c = store.totals_by_key(days_ago(0))
    month_c = store.totals_by_key(days_ago(30))
    total_c = store.totals_by_key("0000-00-00")
    disk_cache: dict = {}
    rows, out = [], []
    for h, m in sorted(clients.items(), key=lambda kv: kv[1].get("created_at", "")):
        st = K.status(m, _is_expired)
        if not args.all and st != OK_STATUS:
            continue
        if args.level and m.get("level") != args.level:
            continue
        kid = K.key_id(h)
        t, mo, al = (_counters(c, kid) for c in (today_c, month_c, total_c))
        row = [kid, m.get("name"), m.get("level") or "-", st, m.get("site") or "-",
               (m.get("expires_at") or "")[:10],
               t.get("calls", 0), mo.get("calls", 0), al.get("calls", 0),
               mo.get("errors", 0), mo.get("denied", 0),
               human(mo.get("bytes_in", 0) + mo.get("bytes_out", 0)),
               _ts(al.get("last_ts"))]
        entry = {"id": kid, "status": st, **m,
                 "calls_today": t.get("calls", 0), "calls_30d": mo.get("calls", 0),
                 "calls_total": al.get("calls", 0), "errors_30d": mo.get("errors", 0),
                 "denied_30d": mo.get("denied", 0),
                 "bytes_30d": mo.get("bytes_in", 0) + mo.get("bytes_out", 0)}
        if args.disk:
            row.append(_disk_cell(m.get("site"), disk_cache))
            usage = disk_cache.get(m.get("site"))
            entry["disk"] = ({"bytes": usage.bytes, "files": usage.files,
                              "disk_bytes": usage.disk_bytes}
                             if hasattr(usage, "bytes") else usage)
        rows.append(row)
        out.append(entry)
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("Aucune clé" + ("" if args.all else " active (--all pour tout voir)") + ".")
        return
    head = ["ID", "NOM", "NIVEAU", "STATUT", "SITE", "EXPIRE", "AUJ.", "30J", "TOTAL",
            "ERR 30J", "REFUSÉS 30J", "OCTETS 30J", "DERNIER APPEL"]
    if args.disk:
        head.append("DISQUE SITE")
    print(_table(rows, head))


def keys_show(args) -> None:
    clients = K.load(_clients_file(args))
    h, m = K.resolve(clients, args.ref)
    print(f"Identifiant : {K.key_id(h)}")
    print(f"Statut      : {K.status(m, _is_expired)}")
    for field in ("name", "level", "site", "scopes", "email", "note", "created_at",
                  "expires_at", "revoked_at", "revoke_reason", "request_id"):
        if m.get(field) not in (None, "", []):
            print(f"{field:<12}: {m[field]}")
    if m.get("site"):
        _print_site_disk(m["site"])
    for label, days in (("aujourd'hui", 0), ("30 derniers jours", 30), ("total", 36500)):
        totals = get_stats().totals_by_capability(K.key_id(h), days_ago(days))
        print(f"\nCompteurs ({label}) :")
        if totals:
            rows = [[c, t["calls"], t["errors"], t["denied"], human(t["bytes_in"]),
                     human(t["bytes_out"])] for c, t in totals.items()]
            rows.append(["TOTAL", sum(t["calls"] for t in totals.values()),
                         sum(t["errors"] for t in totals.values()),
                         sum(t["denied"] for t in totals.values()),
                         human(sum(t["bytes_in"] for t in totals.values())),
                         human(sum(t["bytes_out"] for t in totals.values()))])
            print(_table(rows, ["CAPACITÉ", "APPELS", "ERREURS", "REFUSÉS", "IN", "OUT"]))
        else:
            print("  aucun appel")


def _print_site_disk(site_name: str) -> None:
    site = load_sites().get(site_name)
    if site is None:
        print(f"{'disque':<12}: site '{site_name}' absent de KV_SITES_FILE")
        return
    try:
        usage = disk_usage(site)
    except Exception as exc:
        print(f"{'disque':<12}: mesure impossible ({type(exc).__name__}: {exc})")
        return
    if usage is None:
        print(f"{'disque':<12}: n/a (ni datadir ni sync_token pour le site '{site_name}')")
        return
    line = f"{human(usage.bytes)} en {usage.files} fichiers"
    if usage.disk_bytes is not None:
        line += f", {human(usage.disk_bytes)} alloués sur le disque"
    note = f"site '{site_name}', mesure {usage.source}"
    if usage.measured_at:
        note += f" du {_ts(usage.measured_at)}" + (" (cache)" if usage.cached else "")
    print(f"{'disque':<12}: {line} ({note})")


def sites_list(args) -> None:
    sites = load_sites()
    if not sites:
        print("Aucun site (voir KV_SITES_FILE).")
        return
    clients = K.load(_clients_file(args))
    store = get_stats()
    month = store.totals_by_key(days_ago(30))
    rows, out = [], []
    for name, site in sorted(sites.items()):
        keys = [(h, m) for h, m in clients.items() if m.get("site") == name]
        active = [1 for _, m in keys if K.status(m, _is_expired) == OK_STATUS]
        calls = sum(month.get(K.key_id(h), {}).get("calls", 0) for h, _ in keys)
        try:
            usage = disk_usage(site)
            err = None
        except Exception as exc:
            usage, err = None, f"erreur ({type(exc).__name__})"
        rows.append([name, len(keys), len(active), calls,
                     human(usage.bytes) if usage else (err or "n/a"),
                     human(usage.disk_bytes) if usage else "",
                     usage.files if usage else ""])
        out.append({"site": name, "keys": len(keys), "active_keys": len(active),
                    "calls_30d": calls,
                    "bytes": usage.bytes if usage else None,
                    "disk_bytes": usage.disk_bytes if usage else None,
                    "files": usage.files if usage else None, "error": err})
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(_table(rows, ["SITE", "CLÉS", "ACTIVES", "APPELS 30J", "TAILLE", "DISQUE", "FICHIERS"]))


def keys_create(args) -> None:
    if not args.level and not args.scope:
        raise K.KeysError("donnez au moins --level ou --scope")
    if args.level:
        _check_level(args.level)
    if args.expires:
        _check_date(args.expires)
    if args.site:
        _check_site(args.site)
    meta = {"name": args.name}
    for field, value in (("level", args.level), ("site", args.site),
                         ("scopes", args.scope or None),
                         ("expires_at", args.expires), ("email", args.email),
                         ("note", args.note)):
        if value:
            meta[field] = value
    raw, hashed = K.add(_clients_file(args), meta)
    _show_new_key(raw, hashed, meta)


def _set_status(args, *, revoke: bool) -> None:
    path = _clients_file(args)
    clients = K.load(path)
    h, m = K.resolve(clients, args.ref)
    if revoke:
        m.update(disabled=True, revoked_at=K.now_iso())
        if args.reason:
            m["revoke_reason"] = args.reason
        print(f"Clé {K.key_id(h)} ({m['name']}) révoquée.")
    else:
        for field in ("disabled", "revoked_at", "revoke_reason"):
            m.pop(field, None)
        print(f"Clé {K.key_id(h)} ({m['name']}) réactivée.")
    K.save(path, clients)


def keys_set(args) -> None:
    path = _clients_file(args)
    clients = K.load(path)
    h, m = K.resolve(clients, args.ref)
    if args.level:
        _check_level(args.level)
        m["level"] = args.level
    if args.expires:
        _check_date(args.expires)
        m["expires_at"] = args.expires
    if args.no_expiry:
        m.pop("expires_at", None)
    if args.site:
        _check_site(args.site)
        m["site"] = args.site
    if args.no_site:
        m.pop("site", None)
    scopes = list(m.get("scopes", []))
    for s in args.add_scope or []:
        if s not in scopes:
            scopes.append(s)
    scopes = [s for s in scopes if s not in (args.remove_scope or [])]
    if scopes:
        m["scopes"] = scopes
    else:
        m.pop("scopes", None)
    K.save(path, clients)
    print(f"Clé {K.key_id(h)} ({m['name']}) mise à jour : niveau={m.get('level')} "
          f"site={m.get('site') or '-'} expire={m.get('expires_at') or '-'} scopes={m.get('scopes', [])}")


def keys_rotate(args) -> None:
    path = _clients_file(args)
    clients = K.load(path)
    h, m = K.resolve(clients, args.ref)
    fresh = {k: v for k, v in m.items()
             if k not in ("disabled", "revoked_at", "revoke_reason", "created_at")}
    fresh["rotated_from"] = K.key_id(h)
    raw, new_hash = K.add(path, fresh)
    if not args.keep_old:
        clients = K.load(path)
        clients[h].update(disabled=True, revoked_at=K.now_iso(), revoke_reason="rotated")
        K.save(path, clients)
    _show_new_key(raw, new_hash, fresh)
    print("Ancienne clé " + ("conservée active (--keep-old)." if args.keep_old else "révoquée."))


def keys_delete(args) -> None:
    if not args.yes:
        raise K.KeysError("suppression définitive : ajoutez --yes (préférez 'keys revoke')")
    path = _clients_file(args)
    clients = K.load(path)
    h, m = K.resolve(clients, args.ref)
    del clients[h]
    K.save(path, clients)
    print(f"Clé {K.key_id(h)} ({m['name']}) supprimée.")


# ----------------------------------------------------------------- usage
def usage(args) -> None:
    since = days_ago(args.days)
    store = get_stats()
    if args.key:
        h, m = K.resolve(K.load(_clients_file(args)), args.key)
        data = store.totals_by_day(since, K.key_id(h))
        head = ["JOUR", "APPELS", "ERREURS", "REFUSÉS", "OCTETS IN", "OCTETS OUT"]
        rows = [[d["day"], d["calls"], d["errors"], d["denied"], d["bytes_in"], d["bytes_out"]]
                for d in data]
        title = f"Usage de {K.key_id(h)} ({m['name']}) depuis {since}"
    else:
        clients = K.load(_clients_file(args))
        names = {K.key_id(h): m.get("name") for h, m in clients.items()}
        data = [{"key_id": k, "name": names.get(k, "(supprimée)"), **v}
                for k, v in store.totals_by_key(since).items()]
        data.sort(key=lambda d: -d["calls"])
        head = ["ID", "NOM", "APPELS", "ERREURS", "REFUSÉS", "OCTETS IN", "OCTETS OUT"]
        rows = [[d["key_id"], d["name"], d["calls"], d["errors"], d["denied"],
                 d["bytes_in"], d["bytes_out"]] for d in data]
        title = f"Usage par clé depuis {since}"
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(title)
        print(_table(rows, head) if rows else "  aucun appel")


# -------------------------------------------------------------- requests
def requests_list(args) -> None:
    status = None if args.status == "all" else args.status
    rows = [[r["id"], _ts(r["created_at"]), r["status"], r["name"], r["email"],
             r["level_wanted"], (r["use_case"][:40] + "…") if len(r["use_case"]) > 40 else r["use_case"]]
            for r in get_stats().list_requests(status)]
    print(_table(rows, ["ID", "REÇUE", "STATUT", "NOM", "EMAIL", "NIVEAU DEMANDÉ", "USAGE"])
          if rows else "Aucune demande.")


def _get_request(request_id: int) -> dict:
    r = get_stats().get_request(request_id)
    if r is None:
        raise K.KeysError(f"demande #{request_id} introuvable")
    return r


def requests_show(args) -> None:
    r = _get_request(args.id)
    for field in ("id", "status", "name", "email", "org", "level_wanted", "use_case",
                  "key_id", "note"):
        if r.get(field) not in (None, ""):
            print(f"{field:<13}: {r[field]}")
    print(f"{'reçue':<13}: {_ts(r['created_at'])}")
    if r["decided_at"]:
        print(f"{'traitée':<13}: {_ts(r['decided_at'])}")


def requests_approve(args) -> None:
    r = _get_request(args.id)
    if r["status"] != "pending":
        raise K.KeysError(f"demande #{r['id']} déjà traitée ({r['status']})")
    _check_level(args.level)
    if args.expires:
        _check_date(args.expires)
    if args.site:
        _check_site(args.site)
    meta = {"name": r["name"] + (f" ({r['org']})" if r["org"] else ""),
            "level": args.level, "email": r["email"], "request_id": r["id"]}
    if args.expires:
        meta["expires_at"] = args.expires
    if args.site:
        meta["site"] = args.site
    raw, hashed = K.add(_clients_file(args), meta)
    get_stats().decide_request(r["id"], "approved", key_id=K.key_id(hashed))
    _show_new_key(raw, hashed, meta)
    print(f"\nÀ envoyer à : {r['email']}")


def requests_reject(args) -> None:
    r = _get_request(args.id)
    if r["status"] != "pending":
        raise K.KeysError(f"demande #{r['id']} déjà traitée ({r['status']})")
    get_stats().decide_request(r["id"], "rejected", note=args.reason)
    print(f"Demande #{r['id']} rejetée.")


# ------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m app.manage", description="Manager des clés API de toolspostcards")
    ap.add_argument("--clients-file", help="api_clients.json (défaut : KV_CLIENTS_FILE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    keys = sub.add_parser("keys", help="gérer les clés").add_subparsers(dest="sub", required=True)
    p = keys.add_parser("list"); p.set_defaults(fn=keys_list)
    p.add_argument("--all", action="store_true", help="inclut révoquées/expirées")
    p.add_argument("--level"); p.add_argument("--json", action="store_true")
    p.add_argument("--disk", action="store_true",
                   help="ajoute l'espace disque du site rattaché (peut être lent)")
    p = keys.add_parser("show"); p.set_defaults(fn=keys_show); p.add_argument("ref")
    p = keys.add_parser("create"); p.set_defaults(fn=keys_create)
    p.add_argument("--name", required=True); p.add_argument("--level")
    p.add_argument("--scope", action="append"); p.add_argument("--expires")
    p.add_argument("--email"); p.add_argument("--note"); p.add_argument("--site")
    p = keys.add_parser("revoke"); p.set_defaults(fn=lambda a: _set_status(a, revoke=True))
    p.add_argument("ref"); p.add_argument("--reason")
    p = keys.add_parser("enable"); p.set_defaults(fn=lambda a: _set_status(a, revoke=False))
    p.add_argument("ref")
    p = keys.add_parser("set"); p.set_defaults(fn=keys_set); p.add_argument("ref")
    p.add_argument("--level"); p.add_argument("--expires")
    p.add_argument("--no-expiry", action="store_true")
    p.add_argument("--site"); p.add_argument("--no-site", action="store_true")
    p.add_argument("--add-scope", action="append"); p.add_argument("--remove-scope", action="append")
    p = keys.add_parser("rotate"); p.set_defaults(fn=keys_rotate); p.add_argument("ref")
    p.add_argument("--keep-old", action="store_true")
    p = keys.add_parser("delete"); p.set_defaults(fn=keys_delete); p.add_argument("ref")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("sites", help="sites flpostcards rattachés (taille des données)")
    p.set_defaults(fn=sites_list); p.add_argument("--json", action="store_true")

    p = sub.add_parser("usage", help="compteurs d'appels"); p.set_defaults(fn=usage)
    p.add_argument("--days", type=int, default=30); p.add_argument("--key")
    p.add_argument("--json", action="store_true")

    req = sub.add_parser("requests", help="demandes de clé").add_subparsers(dest="sub", required=True)
    p = req.add_parser("list"); p.set_defaults(fn=requests_list)
    p.add_argument("--status", default="pending",
                   choices=["pending", "approved", "rejected", "all"])
    p = req.add_parser("show"); p.set_defaults(fn=requests_show); p.add_argument("id", type=int)
    p = req.add_parser("approve"); p.set_defaults(fn=requests_approve)
    p.add_argument("id", type=int); p.add_argument("--level", required=True)
    p.add_argument("--expires"); p.add_argument("--site")
    p = req.add_parser("reject"); p.set_defaults(fn=requests_reject)
    p.add_argument("id", type=int); p.add_argument("--reason")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except K.KeysError as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
