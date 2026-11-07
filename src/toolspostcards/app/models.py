from pydantic import BaseModel, Field


class SecretWrite(BaseModel):
    value: str = Field(..., description="Valeur secrète (ex: la clé API distante)")
    metadata: dict[str, str] = Field(default_factory=dict)


class SecretRead(BaseModel):
    value: str
    metadata: dict[str, str] = Field(default_factory=dict)


class WriteAck(BaseModel):
    version: int


class MeRead(BaseModel):
    name: str
    level: str | None = None
    description: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    site: str | None = None
    usage: dict = Field(default_factory=dict)
