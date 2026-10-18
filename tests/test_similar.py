#!/usr/bin/env python3
"""
Petit script de test pour l'endpoint POST /api/v1/similar de flpostcards.

/api/v1/similar nécessite désormais un access token JWT (Authorization:
Bearer <token>) : ce script commence par se connecter via
POST /api/v1/auth/login pour l'obtenir.

Usage :
    python test_similar.py photo.jpg --email alice@example.com --password s3cret
    python test_similar.py photo.jpg --email alice@example.com --password s3cret --threshold 60
    python test_similar.py photo.jpg --token eyJhbGciOi...           # si vous avez déjà un access token
    python test_similar.py photo.jpg --email alice@example.com --password s3cret --base-url http://mondomaine

Dépendance : requests (pip install requests)
"""

from __future__ import annotations

import argparse
import sys

import requests


def login(base_url: str, email: str, password: str, timeout: float) -> str:
    """Appelle POST /api/v1/auth/login et retourne l'access_token obtenu."""
    url = f"{base_url}/api/v1/auth/login"
    print(f"POST {url}  (email={email})")
    response = requests.post(
        url, json={"email": email, "password": password}, timeout=timeout
    )
    print(f"Status: {response.status_code}")
    payload = response.json()
    if response.status_code != 200:
        print("Erreur de connexion :", payload.get("error", payload), file=sys.stderr)
        sys.exit(1)
    print(f"Connecté (access token valide {payload['expires_in']}s).\n")
    return payload["access_token"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Chemin de la photo à identifier")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8001",
        help="Base de l'URL de flpostcards (défaut : %(default)s)",
    )
    parser.add_argument("--email", help="Email du compte (table auths)")
    parser.add_argument("--password", help="Mot de passe du compte")
    parser.add_argument(
        "--token",
        help="Access token JWT déjà obtenu (alternative à --email/--password)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Seuil de similarité 0-100 (défaut : celui du serveur)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout de chaque requête en secondes (défaut : %(default)s)",
    )
    args = parser.parse_args()

    if not args.token and not (args.email and args.password):
        parser.error("fournir soit --token, soit --email et --password")

    access_token = args.token or login(
        args.base_url, args.email, args.password, args.timeout
    )

    url = f"{args.base_url}/api/v1/similar"
    data = {}
    if args.threshold is not None:
        data["threshold"] = args.threshold

    try:
        with open(args.image, "rb") as fh:
            files = {"image": (args.image, fh)}
            print(f"POST {url}  (threshold={args.threshold})")
            response = requests.post(
                url,
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=args.timeout,
            )
    except FileNotFoundError:
        print(f"Fichier introuvable : {args.image}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Erreur réseau : {exc}", file=sys.stderr)
        return 1

    print(f"Status: {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        print("Réponse non-JSON :", response.text, file=sys.stderr)
        return 1

    if response.status_code != 200:
        print("Erreur :", payload.get("error", payload), file=sys.stderr)
        return 1

    if not payload:
        print("Aucune carte similaire trouvée.")
        return 0

    print(f"{len(payload)} carte(s) trouvée(s) :\n")
    for card in payload:
        print(f"  id={card['id']:<6} score={card['score']:>4}")
        print(f"    uri_div3  : {card['uri_div3']}")
        print(f"    uri_div10 : {card['uri_div10']}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
