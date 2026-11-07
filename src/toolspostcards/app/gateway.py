"""Passerelle : authentifie la clé API, vérifie que son niveau accorde la
capacité exigée par la route (routes.py), puis relaie la requête telle
quelle vers simpostcards / flpostcards.

Le corps est relayé en flux (pas de copie en mémoire), ``Content-Length`` et
``Authorization`` sont conservés : ce dernier porte le jeton propre à chaque
serveur, qu'il continue de vérifier lui-même (défense en profondeur).
"""
from __future__ import annotations

import logging
from typing import AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .auth import ApiError, Client, forbidden_for, get_client
from .config import Settings, get_settings
from .db import get_stats
from .levels import get_levels
from .sites import load_sites
from .routes import build_routes, resolve, upstream_url

logger = logging.getLogger("kartotek_tools")

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})
# En-têtes de requête jamais transmis : la clé API ne doit pas sortir de la
# passerelle ; Host est recalculé pour l'upstream.
_DROP_REQUEST = _HOP_BY_HOP | {"host", "x-api-key"}
# En-têtes de réponse jamais transmis (uvicorn pose les siens ; les cookies
# de session d'un upstream n'ont rien à faire chez un client de la passerelle).
_DROP_RESPONSE = _HOP_BY_HOP | {"server", "date", "set-cookie"}


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(settings.upstream_timeout, connect=5.0)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


class _BodyTooLarge(Exception):
    pass


def _safe_path(path: str) -> bool:
    """Refuse tout chemin qui pourrait être interprété différemment par la
    passerelle et par l'upstream.

    Le chemin autorisé est celui qui est relayé : sans ce contrôle,
    ``/api/v1/sync/../../api/compute_travel`` serait accordé au titre de
    « publish » puis normalisé en une route gold par le client HTTP.
    """
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False
    return not any(seg in (".", "..") for seg in path.split("/"))


def _upstream_headers(request: Request) -> list[tuple[str, str]]:
    headers = [
        (k, v) for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST
    ]
    peer = request.client.host if request.client else None
    if peer:
        prior = request.headers.get("x-forwarded-for")
        headers = [(k, v) for k, v in headers if k.lower() != "x-forwarded-for"]
        headers.append(("X-Forwarded-For", f"{prior}, {peer}" if prior else peer))
    return headers


async def _count(client: Client, capability: str, **counters) -> None:
    """Incrémente les compteurs de la clé. Ne doit jamais faire échouer la
    requête : un incident de comptage est journalisé, pas propagé."""
    try:
        await run_in_threadpool(
            get_stats().record_call, client.key_id, client.name, client.level,
            capability, **counters,
        )
    except Exception:
        logger.exception("Compteur d'usage non enregistré (clé '%s')", client.name)


def _int_header(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


async def _limited_body(request: Request, limit: int) -> AsyncIterator[bytes]:
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise _BodyTooLarge
        yield chunk


async def proxy(
    full_path: str,
    request: Request,
    client: Client = Depends(get_client),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    path = "/" + full_path
    if not _safe_path(path):
        raise ApiError(400, "Chemin invalide")

    route, path_known = resolve(build_routes(settings), request.method, path)
    if route is None:
        if path_known:
            raise ApiError(405, "Méthode non autorisée")
        raise ApiError(404, "Route inconnue")

    # Un niveau commercial n'accorde jamais "admin" ; seule une clé qui le
    # porte explicitement (scopes) passe. Même vérification pour toutes.
    if not client.can(route.capability):
        # Les refus sont comptés : ils montrent quels clients butent sur
        # une capacité qu'ils pourraient acheter.
        await _count(client, route.capability, denied=1)
        raise forbidden_for(client, route.capability, get_levels())

    base = upstream_url(settings, route.upstream)
    if route.upstream == "flpostcards" and client.site:
        # Clé rattachée à un site : ses publications vont chez lui, JAMAIS sur
        # le site par défaut si le sien est introuvable (autre client).
        site = load_sites(settings).get(client.site)
        base = site.url if site else ""
        if not base:
            logger.error("Clé '%s' : site '%s' non configuré", client.name, client.site)
    if not base:
        raise ApiError(501, f"Service non disponible: {route.upstream}")

    limit = settings.max_body_mb * 1024 * 1024
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, f"Requête trop volumineuse (max {settings.max_body_mb} Mo)")

    has_body = (
        declared is not None and declared != "0"
    ) or "chunked" in request.headers.get("transfer-encoding", "").lower()

    url = base + quote(path, safe="/:@!$&'()*+,;=-._~")
    if request.url.query:
        url += "?" + request.url.query

    http: httpx.AsyncClient = request.app.state.http
    upstream_request = http.build_request(
        request.method,
        url,
        headers=_upstream_headers(request),
        content=_limited_body(request, limit) if has_body else None,
    )

    try:
        upstream = await http.send(upstream_request, stream=True)
    except _BodyTooLarge:
        raise ApiError(413, f"Requête trop volumineuse (max {settings.max_body_mb} Mo)")
    except httpx.TimeoutException:
        logger.warning("Timeout upstream %s %s (client '%s')", route.upstream, path, client.name)
        await _count(client, route.capability, calls=1, errors=1, bytes_in=_int_header(declared))
        raise ApiError(504, f"Délai dépassé: {route.upstream}")
    except httpx.HTTPError:
        logger.exception("Upstream %s injoignable", route.upstream)
        await _count(client, route.capability, calls=1, errors=1, bytes_in=_int_header(declared))
        raise ApiError(502, f"Service injoignable: {route.upstream}")

    # bytes_out = Content-Length annoncé par l'upstream (0 si absent, ex. flux) ;
    # bytes_in = Content-Length annoncé par le client.
    await _count(
        client, route.capability, calls=1,
        errors=1 if upstream.status_code >= 400 else 0,
        bytes_in=_int_header(declared),
        bytes_out=_int_header(upstream.headers.get("content-length")),
    )

    logger.info(
        "%s %s -> %s %d (client '%s', niveau %s)",
        request.method, path, route.upstream, upstream.status_code,
        client.name, client.level,
    )

    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    # Reprend les en-têtes tels quels (Content-Length, Content-Encoding, et
    # d'éventuels doublons) : le corps est relayé sans être décodé.
    response.raw_headers = [
        (k, v) for k, v in upstream.headers.raw if k.decode("latin-1").lower() not in _DROP_RESPONSE
    ]
    return response
