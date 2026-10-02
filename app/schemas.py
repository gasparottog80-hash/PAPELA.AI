from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class UploadAccepted(BaseModel):
    job_id: str
    status: str
    pages: int


class JobStatus(BaseModel):
    id: str
    status: str
    filename: str
    pages: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int
    created_at: datetime
    updated_at: datetime
    purged_at: datetime | None = None


class ErrorResponse(BaseModel):
    request_id: str
    code: str
    error: str
