from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class UploadAccepted(BaseModel):
    job_id: str
    status: str
    pages: int


class JobStatus(BaseModel):
    id: str
    status: str
    filename: str
    pages: Optional[int] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    attempts: int
    created_at: datetime
    updated_at: datetime


class ErrorResponse(BaseModel):
    request_id: str
    error: str
