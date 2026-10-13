from pydantic import BaseModel, Field


class SecretWrite(BaseModel):
    value: str = Field(..., description="Valeur secrète (ex: la clé API distante)")
    metadata: dict[str, str] = Field(default_factory=dict)


class SecretRead(BaseModel):
    value: str
    metadata: dict[str, str] = Field(default_factory=dict)


class WriteAck(BaseModel):
    version: int
