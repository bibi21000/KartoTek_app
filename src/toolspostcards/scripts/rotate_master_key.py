#!/usr/bin/env python3
"""Fait tourner la clé maîtresse sans perdre les secrets déjà stockés.

Étapes effectuées :
  1. lit chaque secret avec l'ANCIENNE clé
  2. le re-chiffre avec la NOUVELLE clé
  3. écrit la nouvelle clé à la place de l'ancienne

À exécuter avec le service API arrêté (pour éviter les écritures
concurrentes pendant la rotation).

Usage:
  python3 rotate_master_key.py \
      --db /opt/kartotek_tools/data/secrets.db \
      --old-key /opt/kartotek_tools/secrets/master.key \
      --new-key /opt/kartotek_tools/secrets/master.key.new
"""
import argparse
import os
import shutil
import sqlite3

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--old-key", required=True)
    parser.add_argument("--new-key", required=True)
    args = parser.parse_args()

    old_fernet = Fernet(open(args.old_key, "rb").read().strip())
    new_fernet = Fernet(open(args.new_key, "rb").read().strip())

    conn = sqlite3.connect(args.db)
    rows = conn.execute("SELECT name, ciphertext FROM secrets").fetchall()

    print(f"{len(rows)} secret(s) à re-chiffrer...")
    for name, ciphertext in rows:
        plaintext = old_fernet.decrypt(ciphertext)
        new_ciphertext = new_fernet.encrypt(plaintext)
        conn.execute(
            "UPDATE secrets SET ciphertext = ? WHERE name = ?",
            (new_ciphertext, name),
        )
        print(f"  - {name} : OK")

    conn.commit()
    conn.close()

    backup_path = args.old_key + ".bak"
    shutil.copy2(args.old_key, backup_path)
    shutil.move(args.new_key, args.old_key)
    os.chmod(args.old_key, 0o600)

    print("Rotation terminée.")
    print(f"Ancienne clé sauvegardée dans {backup_path} (à supprimer une fois vérifié).")
    print("Redémarrez maintenant kartotek-tools.service.")


if __name__ == "__main__":
    main()
