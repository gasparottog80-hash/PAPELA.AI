from __future__ import annotations

import logging

from .config import Settings

logger = logging.getLogger("papela.agent")


class PromptTooLargeError(ValueError):
    """Raised when a prompt exceeds the configured size limit."""


class Agent:
    """Core agent. STUB: replace `run` with the real product logic.

    Kept deterministic and side-effect-free so the scaffold is testable
    before the real LLM/tooling backend is wired in.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def run(self, prompt: str, *, request_id: str) -> str:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("empty prompt")
        if len(prompt) > self._settings.max_prompt_chars:
            raise PromptTooLargeError(
                f"prompt exceeds {self._settings.max_prompt_chars} chars"
            )
        logger.info(
            "agent.run", extra={"request_id": request_id}
        )
        # TODO: wire real LLM / tool-calling loop here.
        return f"[stub] received {len(prompt)} chars"
