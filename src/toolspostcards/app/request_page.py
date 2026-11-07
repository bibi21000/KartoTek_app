"""Page publique « demander une clé API » (GET/POST /request-key).

La demande est seulement *enregistrée* : aucune clé n'est créée ni envoyée
automatiquement. L'opérateur la traite avec le manager
(``python -m app.manage requests list / approve / reject``) et transmet la clé.

Page publique donc exposée aux abus : champ piège (honeypot), taille des
champs bornée, limite par IP et par heure (comptée en base, donc valable avec
plusieurs workers), plafond de demandes en attente, doublon d'email en attente
ignoré silencieusement (même réponse, pour ne rien révéler). Les IP ne sont
conservées que sous forme de hash.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from .config import get_settings
from .db import get_stats
from .levels import get_levels

router = APIRouter()

MAX_BODY = 16 * 1024
LIMITS = {"name": 100, "email": 200, "org": 100, "use_case": 1000}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

T = {
    "fr": {
        "title": "Demander une clé API",
        "intro": "Renseignez ce formulaire : votre demande sera examinée et, si elle est acceptée, la clé vous sera envoyée par email.",
        "name": "Nom", "email": "Email", "org": "Organisation (facultatif)",
        "use_case": "Usage prévu", "level": "Niveau souhaité",
        "privacy": "Votre email n'est utilisé que pour vous répondre au sujet de cette demande.",
        "submit": "Envoyer la demande",
        "thanks": "Demande reçue", "thanks_body": "Merci ! Votre demande a été enregistrée (référence #{id}). Vous recevrez une réponse par email.",
        "e_missing": "Merci de remplir tous les champs obligatoires.",
        "e_email": "Adresse email invalide.", "e_level": "Niveau inconnu.",
        "e_rate": "Trop de demandes depuis votre adresse. Réessayez plus tard.",
        "e_closed": "Les demandes sont temporairement fermées. Réessayez plus tard.",
        "e_big": "Formulaire trop volumineux.",
        "back": "Retour",
    },
    "en": {
        "title": "Request an API key",
        "intro": "Fill in this form: your request will be reviewed and, if accepted, the key will be sent to you by email.",
        "name": "Name", "email": "Email", "org": "Organization (optional)",
        "use_case": "Intended use", "level": "Requested level",
        "privacy": "Your email is only used to reply about this request.",
        "submit": "Send request",
        "thanks": "Request received", "thanks_body": "Thank you! Your request has been recorded (reference #{id}). You will get a reply by email.",
        "e_missing": "Please fill in all required fields.",
        "e_email": "Invalid email address.", "e_level": "Unknown level.",
        "e_rate": "Too many requests from your address. Please try again later.",
        "e_closed": "Requests are temporarily closed. Please try again later.",
        "e_big": "Form too large.",
        "back": "Back",
    },
}

_CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1c1c1e;--card:#fff;--bd:#d0d0d5;--ac:#1a5fb4;--err:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#18181b;--fg:#ececf0;--card:#232328;--bd:#3d3d45;--ac:#78aeed;--err:#f2837c}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:34rem;margin:0 auto;padding:2rem 1rem}
form,.msg{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.25rem}
label{display:block;margin-top:1rem;font-weight:600}
input,select,textarea{width:100%;box-sizing:border-box;margin-top:.3rem;padding:.55rem;font:inherit;
 color:inherit;background:var(--bg);border:1px solid var(--bd);border-radius:6px}
button{margin-top:1.25rem;padding:.6rem 1.2rem;font:inherit;font-weight:600;color:#fff;background:var(--ac);
 border:0;border-radius:6px;cursor:pointer}
.err{color:var(--err);font-weight:600}.hint{font-size:.875rem;opacity:.75}
.hp{position:absolute;left:-9999px}
"""


def _lang(request: Request) -> str:
    forced = request.query_params.get("lang")
    if forced in T:
        return forced
    accept = request.headers.get("accept-language", "").lower()
    return "fr" if accept.startswith("fr") else "en"


def _page(lang: str, title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "no-store"})


def _form(lang: str, values: dict, error: str | None = None, status: int = 200) -> HTMLResponse:
    t = T[lang]
    esc = lambda k: html.escape(values.get(k, ""))  # noqa: E731
    options = "".join(
        f'<option value="{html.escape(lv)}"{" selected" if lv == (values.get("level") or "guest") else ""}>'
        f"{html.escape(lv)} — {html.escape(get_levels().get(lv).description)}</option>"
        for lv in get_levels().names()
    )
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    body = (
        f"<p>{html.escape(t['intro'])}</p>{err}"
        f'<form method="post" action="?lang={lang}">'
        f'<label>{t["name"]}<input name="name" required maxlength="{LIMITS["name"]}" value="{esc("name")}"></label>'
        f'<label>{t["email"]}<input name="email" type="email" required maxlength="{LIMITS["email"]}" value="{esc("email")}"></label>'
        f'<label>{t["org"]}<input name="org" maxlength="{LIMITS["org"]}" value="{esc("org")}"></label>'
        f'<label>{t["level"]}<select name="level">{options}</select></label>'
        f'<label>{t["use_case"]}<textarea name="use_case" rows="5" required maxlength="{LIMITS["use_case"]}">{esc("use_case")}</textarea></label>'
        # Champ piège : invisible pour un humain, rempli par les robots.
        '<div class="hp" aria-hidden="true"><label>Website<input name="website" tabindex="-1" autocomplete="off"></label></div>'
        f'<p class="hint">{t["privacy"]}</p><button type="submit">{t["submit"]}</button></form>'
    )
    return _page(lang, t["title"], body, status)


def client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@router.get("/request-key", response_class=HTMLResponse, include_in_schema=False)
async def request_key_form(request: Request):
    settings = get_settings()
    if not settings.requests_enabled:
        return HTMLResponse("Not found", status_code=404)
    return _form(_lang(request), {})


@router.post("/request-key", response_class=HTMLResponse, include_in_schema=False)
async def request_key_submit(request: Request):
    settings = get_settings()
    if not settings.requests_enabled:
        return HTMLResponse("Not found", status_code=404)
    lang = _lang(request)
    t = T[lang]

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        return _form(lang, {}, t["e_big"], 413)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            return _form(lang, {}, t["e_big"], 413)
    fields = {k: v[0].strip() for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}

    # Robot : on répond comme si c'était accepté, sans rien enregistrer.
    if fields.get("website"):
        return _page(lang, t["thanks"], f'<p class="msg">{html.escape(t["thanks_body"].format(id=0))}</p>')

    values = {k: fields.get(k, "")[: LIMITS.get(k, 50)] for k in
              ("name", "email", "org", "use_case", "level")}
    if not (values["name"] and values["email"] and values["use_case"]):
        return _form(lang, values, t["e_missing"], 422)
    if not _EMAIL.match(values["email"]):
        return _form(lang, values, t["e_email"], 422)
    level = values["level"] or "guest"
    if get_levels().get(level) is None:
        return _form(lang, values, t["e_level"], 422)

    store = get_stats()
    ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()[:16]

    def _save() -> tuple[str, int]:
        if store.count_recent_requests(ip_hash, time.time() - 3600) >= settings.request_rate_per_hour:
            return "rate", 0
        if store.has_pending_email(values["email"]):
            return "ok", 0  # doublon : même réponse, rien de nouveau
        if store.count_pending() >= settings.requests_max_pending:
            return "closed", 0
        return "ok", store.add_request(
            name=values["name"], email=values["email"], org=values["org"],
            use_case=values["use_case"], level_wanted=level, ip_hash=ip_hash)

    outcome, request_id = await run_in_threadpool(_save)
    if outcome == "rate":
        return _form(lang, values, t["e_rate"], 429)
    if outcome == "closed":
        return _form(lang, values, t["e_closed"], 503)
    return _page(
        lang, t["thanks"],
        f'<p class="msg">{html.escape(t["thanks_body"].format(id=request_id or "—"))}</p>',
    )
