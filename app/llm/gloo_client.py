"""Gloo AI wrapper: retries, timeout, usage logging (PLAN.md section 4).

All model calls go through GlooClient. On persistent failure it raises
GlooUnavailableError — callers escalate to the coordinator, never guess.
"""

import logging
import time
from datetime import datetime, timezone

import openai
from openai import OpenAI

from app.config import Settings, get_settings

logger = logging.getLogger("gloo")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 45.0


class GlooUnavailableError(Exception):
    """Gloo could not serve the request. Escalate to a human; do not guess."""


class GlooClient:
    def __init__(
        self,
        settings: Settings | None = None,
        client=None,  # injectable for tests
        sleeper=time.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client or OpenAI(
            api_key=self.settings.gloo_api_key,
            base_url=self.settings.gloo_base_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,  # we own the retry policy
        )
        self._sleep = sleeper
        self.max_attempts = max_attempts
        self.usage_log: list[dict] = []  # one entry per successful response

    def create_response(self, *, model: str, input, instructions: str | None = None, **kwargs):
        """Call the Responses API with exponential backoff on 429/5xx."""
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.responses.create(
                    model=model, input=input, instructions=instructions, **kwargs
                )
                self._record_usage(model, response)
                return response
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                last_error = e
            except openai.APIStatusError as e:
                if e.status_code not in RETRYABLE_STATUS:
                    raise GlooUnavailableError(f"Gloo error {e.status_code}: {e}") from e
                last_error = e
            if attempt < self.max_attempts - 1:
                delay = 2**attempt
                logger.warning("Gloo call failed (%s); retrying in %ss", last_error, delay)
                self._sleep(delay)
        raise GlooUnavailableError(f"Gloo unavailable after {self.max_attempts} attempts") from last_error

    def _record_usage(self, model: str, response) -> None:
        usage = getattr(response, "usage", None)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        }
        self.usage_log.append(entry)
        logger.info(
            "gloo usage model=%s input_tokens=%s output_tokens=%s",
            model,
            entry["input_tokens"],
            entry["output_tokens"],
        )

    def total_usage(self) -> dict:
        return {
            "input_tokens": sum(e["input_tokens"] for e in self.usage_log),
            "output_tokens": sum(e["output_tokens"] for e in self.usage_log),
            "calls": len(self.usage_log),
        }
