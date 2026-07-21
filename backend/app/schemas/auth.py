from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: str = Field(min_length=8, max_length=256)


class AuthStatusResponse(BaseModel):
    setup_required: bool
    authenticated: bool


class AuthSessionResponse(BaseModel):
    username: str
    expires_at: datetime
    csrf_token: str
