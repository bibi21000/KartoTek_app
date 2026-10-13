#!/usr/bin/env python3
"""Génère la clé maîtresse de chiffrement (Fernet) et l'écrit avec des
permissions restrictives (600). À exécuter UNE SEULE FOIS lors de
l'installation initiale — regénérer cette clé rend tous les secrets déjà
stockés illisibles (voir rotate_master_key.py pour changer de clé
proprement).

Usage: sudo python3 generate_master_key.py /opt/kartotek_tools/secrets/master.key
"""
import os
import sys

from cryptography.fernet import Fernet

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: generate_master_key.py <chemin_du_fichier>")
        sys.exit(1)

    path = sys.argv[1]
    if os.path.exists(path):
        print(f"ERREUR: {path} existe déjà. Ne pas écraser une clé existante.")
        sys.exit(1)

    key = Fernet.generate_key()

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)

    print(f"Clé maîtresse écrite dans {path} (permissions 600).")
    print("Sauvegardez une copie chiffrée de ce fichier hors de ce serveur :")
    print("sans lui, aucun secret stocké ne pourra être déchiffré.")
