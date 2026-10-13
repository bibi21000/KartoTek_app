import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse

from .auth import Client, get_client, require_scope
from .crypto import get_crypto
from .models import SecretRead, SecretWrite, WriteAck
from .store import get_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kartotek_tools")

app = FastAPI(
    title="Kartotek_tools API",
    description=(
        "Serveur d'outils distants pour KartoTek."
    ),
    version="2.0.0",
    # Routes doc désactivées ici pour être recréées ci-dessous, protégées
    # par la même authentification X-API-Key que le reste de l'API.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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
