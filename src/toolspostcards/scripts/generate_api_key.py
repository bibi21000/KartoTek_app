#!/usr/bin/env python3
"""Génère une clé API brute (à donner au client) et son hash SHA256
(à mettre dans api_clients.json sur le serveur).

Usage: python3 generate_api_key.py
"""
import hashlib
import secrets

if __name__ == "__main__":
    raw_key = secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    print("Clé API brute (à donner au client, ne pas stocker sur le serveur) :")
    print(f"  {raw_key}\n")
    print("Hash à mettre dans api_clients.json sur le serveur :")
    print(f"  {hashed}")
