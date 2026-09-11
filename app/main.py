from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .agent import Agent, PromptTooLargeError
from .config import get_settings
from .logging_config import configure_logging
from .schemas import AgentRequest, AgentResponse, ErrorResponse

logger = logging.getLogger("papela.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.agent = Agent(settings)
    logger.info("startup", extra={"request_id": None})
    yield
    logger.info("shutdown", extra={"request_id": None})


app = FastAPI(title="PAPELA.AI", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - convert to structured 500
        logger.exception("unhandled", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                request_id=request_id, error="internal error"
            ).model_dump(),
        )
    response.headers["x-request-id"] = request_id
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/agent",
    response_model=AgentResponse,
    responses={400: {"model": ErrorResponse}},
)
async def run_agent(body: AgentRequest, request: Request):
    request_id: str = request.state.request_id
    agent: Agent = request.app.state.agent
    try:
        reply = agent.run(body.prompt, request_id=request_id)
    except (ValueError, PromptTooLargeError) as exc:
        logger.warning("bad_request", extra={"request_id": request_id})
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(request_id=request_id, error=str(exc)).model_dump(),
        )
    return AgentResponse(request_id=request_id, reply=reply)
