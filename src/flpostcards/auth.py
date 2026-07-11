"""
flpostcards/auth.py - Authentification par JWT (access token) + refresh
token stocké en base (table ``refresh_tokens``, voir
``libpostcards/model.py``).

Principe (configuration : postcards.conf [flask]) :

  - access token  : JWT signé HS256, courte durée (``JWT_ACCESS_TTL_S``,
                     15 min par défaut). Stateless : sa validité tient
                     uniquement à sa signature et à son expiration, le
                     serveur n'a rien à vérifier en base pour
                     l'accepter. Envoyé dans l'en-tête
                     ``Authorization: Bearer <access_token>`` à chaque
                     appel à un endpoint protégé.

  - refresh token : chaîne aléatoire opaque, longue durée
                     (``JWT_REFRESH_TTL_S``, 30 jours par défaut). Seul
                     son hash SHA-256 est stocké en base
                     (``Model.create_refresh_token`` / ``verify_...`` /
                     ``revoke_...``) : contrairement à l'access token,
                     il est donc révocable (déconnexion à distance,
                     téléphone volé) — ce qu'un JWT stateless seul ne
                     permet pas.

Chaque serveur KartoTek garde son propre secret de signature
(postcards.conf [flask] secret_key) : pas de dépendance
inter-serveurs, cohérent avec le modèle "un manager par site".

Routes associées (flpostcards/blueprints/api/__init__.py) :
  POST /api/v1/auth/login       {email, password}   -> {access_token, refresh_token, ...}
  POST /api/v1/auth/refresh     {refresh_token}      -> {access_token, refresh_token, ...} (rotation)
  POST /api/v1/auth/logout      {refresh_token}      -> révoque ce token
  POST /api/v1/auth/logout-all  (auth requise)        -> révoque tous les refresh tokens de l'utilisateur
"""

from __future__ import annotations

import time
from functools import wraps

import jwt
from flask import current_app, jsonify, request

# Claim "type" attendu pour un access token — évite qu'un refresh token
# égaré (ou tout autre JWT) ne soit accepté à la place d'un access
# token si jamais il était, par erreur, signé avec le même secret.
_ACCESS_TOKEN_TYPE = "access"


def create_access_token(email: str) -> str:
    """Génère un access token JWT (HS256) pour ``email``."""
    now = int(time.time())
    expires_in = current_app.config["JWT_ACCESS_TTL_S"]
    payload = {
        "sub": email,
        "type": _ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + expires_in,
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    """
    Décode et vérifie un access token (signature + expiration + type).
    Retourne le payload si valide, sinon ``None`` — ne lève jamais
    d'exception, pratique pour un simple test inline ou un décorateur.
    """
    try:
        payload = jwt.decode(
            token, current_app.config["SECRET_KEY"], algorithms=["HS256"]
        )
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _ACCESS_TOKEN_TYPE:
        return None
    return payload


def get_bearer_token() -> str | None:
    """Extrait le token de l'en-tête ``Authorization: Bearer <token>``, ou None si absent/mal formé."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


def current_auth_email() -> str | None:
    """
    Retourne l'email authentifié pour la requête courante (à partir de
    l'en-tête ``Authorization``), ou ``None`` si le token est absent,
    invalide ou expiré. Vérification "douce" (pas d'erreur HTTP) — pour
    protéger un endpoint, préférer le décorateur ``require_auth``.
    """
    token = get_bearer_token()
    if token is None:
        return None
    payload = decode_access_token(token)
    if payload is None:
        return None
    return payload.get("sub")


def require_auth(view):
    """
    Décorateur protégeant un endpoint Flask : exige un access token JWT
    valide dans ``Authorization: Bearer <token>``. L'email authentifié
    est passé à la vue via le paramètre ``auth_email``.

    401 ``{"error": "unauthorized"}`` si le token est absent, invalide,
    expiré, ou d'un type incorrect (ex: un refresh token utilisé ici
    par erreur).
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        email = current_auth_email()
        if email is None:
            return jsonify({"error": "unauthorized"}), 401
        kwargs["auth_email"] = email
        return view(*args, **kwargs)
    return wrapper


def issue_token_pair(email: str, device_info: str | None = None) -> dict:
    """
    Émet une nouvelle paire (access_token, refresh_token) pour
    ``email`` : crée le refresh token en base (``Model.create_refresh_token``)
    et signe l'access token JWT correspondant. Utilisé par
    ``/api/v1/auth/login`` et ``/api/v1/auth/refresh`` (après rotation).
    """
    refresh_ttl = current_app.config["JWT_REFRESH_TTL_S"]
    expires_at = int(time.time()) + refresh_ttl
    refresh_token = current_app.model.create_refresh_token(
        email, expires_at, device_info
    )
    return {
        "access_token": create_access_token(email),
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": current_app.config["JWT_ACCESS_TTL_S"],
    }
