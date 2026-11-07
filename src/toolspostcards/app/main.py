import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse

from .auth import ApiError, Client, get_client, require_scope
from .config import get_settings
from .crypto import get_crypto
from .db import days_ago, get_stats
from .gateway import make_http_client, proxy
from .levels import get_levels
from .request_page import router as request_page_router
from .models import MeRead, SecretRead, SecretWrite, WriteAck
from .store import get_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kartotek_tools")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Échoue au démarrage (et non à la première requête) si les niveaux de
    # clés sont mal définis.
    get_levels()
    app.state.http = make_http_client(get_settings())
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="Kartotek_tools API",
    description=(
        "Interface publique de simpostcards (et passerelle de publication "
        "vers flpostcards) : chaque clé API a un niveau "
        "(guest, basic, premium, gold...) qui détermine les routes accessibles."
    ),
    version="3.0.0",
    lifespan=lifespan,
    # Routes doc désactivées ici pour être recréées ci-dessous, protégées
    # par la même authentification X-API-Key que le reste de l'API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError):
    # "error" : clé lue par les clients remotesync ; "detail" : convention FastAPI.
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "detail": exc.detail, **exc.extra},
    )


@app.get("/health", tags=["monitoring"])
def health():
    return {"status": "ok"}


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    # Public par nature (les robots ne pourraient sinon jamais la lire) :
    # aucune fuite d'information sensible, juste une directive "n'indexe rien".
    static_dir = Path(__file__).parent / "static"
    return FileResponse(static_dir / "robots.txt", media_type="text/plain")


@app.get("/openapi.json", include_in_schema=False)
def get_openapi_json(client: Client = Depends(get_client)):
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def get_docs(client: Client = Depends(get_client)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Docs")


@app.get("/redoc", include_in_schema=False)
def get_redoc(client: Client = Depends(get_client)):
    return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")


def _usage_summary(client: Client) -> dict:
    """Consommation des 30 derniers jours, par capacité."""
    if not client.key_id:
        return {}
    try:
        by_cap = get_stats().totals_by_capability(client.key_id, days_ago(30))
    except Exception:
        logger.exception("Lecture des compteurs impossible")
        return {}
    return {
        "since": days_ago(30),
        "calls": sum(t["calls"] for t in by_cap.values()),
        "by_capability": {c: t["calls"] for c, t in by_cap.items()},
    }


@app.get("/v1/me", response_model=MeRead, tags=["account"])
def me(client: Client = Depends(get_client)):
    """Niveau et capacités de la clé utilisée (pour qu'une appli cliente
    sache quelles fonctions proposer avant de les appeler)."""
    level = get_levels().get(client.level) if client.level else None
    return MeRead(
        name=client.name,
        level=client.level,
        description=level.description if level else None,
        capabilities=sorted(client.capabilities | set(client.scopes)),
        expires_at=client.expires_at,
        site=client.site,
        usage=_usage_summary(client),
    )


@app.get("/v1/secrets/{name}", response_model=SecretRead, tags=["secrets"])
def read_secret(name: str, client: Client = Depends(require_scope("read:secrets"))):
    ciphertext = get_store().read(name, client.name)
    payload = json.loads(get_crypto().decrypt(ciphertext))
    logger.info("Lecture secret '%s' par client '%s'", name, client.name)
    return SecretRead(value=payload["value"], metadata=payload.get("metadata", {}))


@app.put("/v1/secrets/{name}", response_model=WriteAck, tags=["secrets"])
def write_secret(
    name: str,
    payload: SecretWrite,
    client: Client = Depends(require_scope("write:secrets")),
):
    plaintext = json.dumps({"value": payload.value, "metadata": payload.metadata})
    ciphertext = get_crypto().encrypt(plaintext)
    version = get_store().write(name, ciphertext, client.name)
    logger.info("Écriture secret '%s' (v%d) par client '%s'", name, version, client.name)
    return WriteAck(version=version)


@app.delete("/v1/secrets/{name}", status_code=204, tags=["secrets"])
def delete_secret(name: str, client: Client = Depends(require_scope("write:secrets"))):
    get_store().delete(name, client.name)
    logger.info("Suppression secret '%s' par client '%s'", name, client.name)


# Page publique de demande de clé (avant la route fourre-tout ci-dessous).
app.include_router(request_page_router)


# DOIT rester la dernière route : relaie vers simpostcards/flpostcards tout ce
# qui n'est pas géré ci-dessus (voir routes.py pour la table et les niveaux).
app.add_api_route(
    "/{full_path:path}",
    proxy,
    methods=["GET", "POST", "PUT", "DELETE"],
    include_in_schema=False,
)
