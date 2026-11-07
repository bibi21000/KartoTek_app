#!/usr/bin/env python3
"""Génère une clé API (à donner au client) et l'entrée à déclarer côté serveur.

Exemples :
  # affiche la clé et l'entrée JSON à coller dans api_clients.json
  python3 generate_api_key.py --name acme --level basic

  # clé expirant le 31/01/2027, ajoutée directement au fichier des clients
  python3 generate_api_key.py --name acme --level gold --expires 2027-01-31 \\
      --clients-file /opt/kartotek_vault/secrets/api_clients.json

  # clé « technique » sans niveau, uniquement des scopes (coffre de secrets)
  python3 generate_api_key.py --name admin-cli --scope read:secrets --scope write:secrets

Sans dépendance externe (utilisable via ``docker run`` comme avant). Si
--levels-file est donné, le niveau est validé contre ce fichier ; sinon
contre les niveaux par défaut (guest, basic, premium, gold).
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_LEVELS = ["guest", "basic", "premium", "gold"]


def new_key() -> tuple[str, str]:
    raw_key = "kt_" + secrets.token_urlsafe(32)
    return raw_key, hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def add_to_clients_file(path: str, hashed: str, entry: dict) -> None:
    """Ajout atomique (fichier temporaire + rename, permissions conservées)."""
    clients = {}
    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        with open(path, encoding="utf-8") as fh:
            clients = json.load(fh)
    clients[hashed] = entry
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clients, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", required=True, help="nom du client (journaux)")
    ap.add_argument("--level", help="niveau : guest, basic, premium, gold...")
    ap.add_argument("--scope", action="append", default=[],
                    help="droit supplémentaire (répétable), ex: read:secrets")
    ap.add_argument("--expires", help="date d'expiration ISO (ex: 2027-01-31)")
    ap.add_argument("--clients-file", help="api_clients.json à compléter")
    ap.add_argument("--levels-file", help="api_levels.json, pour valider --level")
    args = ap.parse_args()

    if not args.level and not args.scope:
        ap.error("donnez au moins --level ou --scope")
    if args.level:
        levels = DEFAULT_LEVELS
        if args.levels_file:
            with open(args.levels_file, encoding="utf-8") as fh:
                levels = list(json.load(fh))
        if args.level not in levels:
            ap.error(f"niveau inconnu '{args.level}' (connus : {', '.join(levels)})")
    if args.expires:
        try:
            datetime.fromisoformat(args.expires)
        except ValueError:
            ap.error("--expires : date ISO attendue, ex: 2027-01-31")

    entry: dict = {
        "name": args.name,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if args.level:
        entry["level"] = args.level
    if args.scope:
        entry["scopes"] = args.scope
    if args.expires:
        entry["expires_at"] = args.expires

    raw_key, hashed = new_key()
    print("Clé API brute (à donner au client, ne pas stocker sur le serveur) :")
    print(f"  {raw_key}\n")
    if args.clients_file:
        add_to_clients_file(args.clients_file, hashed, entry)
        print(f"Entrée ajoutée à {args.clients_file} (rechargée sans redémarrage).")
    else:
        print("Entrée à ajouter dans api_clients.json sur le serveur :")
        print(json.dumps({hashed: entry}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
