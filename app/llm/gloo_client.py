"""Gloo AI wrapper: retries, timeout, usage logging (PLAN.md section 4).

All model calls go through GlooClient. On persistent failure it raises
GlooUnavailableError — callers escalate to the coordinator, never guess.
"""

import logging
import json
import time
from datetime import datetime, timezone

import openai
from openai import OpenAI

from app.config import Settings, get_settings
from app.core.message_style import CHURCH_TEXT_INSTRUCTIONS, NO_EM_DASH_INSTRUCTIONS

logger = logging.getLogger("gloo")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 45.0


class GlooUnavailableError(Exception):
    """Gloo could not serve the request. Escalate to a human; do not guess."""


def _require_usable_response(response, tools):
    status = getattr(response, "status", None)
    if (status is not None and status != "completed"
            or getattr(response, "error", None) is not None
            or getattr(response, "incomplete_details", None) is not None):
        raise GlooUnavailableError("Gloo response did not complete; hold for system review")
    # Tool-calling agents legitimately return no message text. Only a requested,
    # complete function call with object arguments counts as usable output. The
    # agent executes every returned call, so mixed invalid batches must hold.
    names = set()
    for tool in tools or []:
        if isinstance(tool, dict) and tool.get("type") == "function":
            definition = tool.get("function", tool)
            if (isinstance(definition, dict) and isinstance(definition.get("name"), str)
                    and definition["name"].strip()):
                names.add(definition["name"])
    has_function_call = False
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "function_call":
            continue
        if (not isinstance(getattr(item, "name", None), str)
                or getattr(item, "name", None) not in names
                or getattr(item, "status", None) not in (None, "completed")
                or not isinstance(getattr(item, "call_id", None), str)
                or not item.call_id.strip()):
            raise GlooUnavailableError("Gloo returned an unusable function call; hold for system review")
        try:
            raw = getattr(item, "arguments", None)
            arguments = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError) as exc:
            raise GlooUnavailableError("Gloo function arguments are invalid; hold for system review") from exc
        if not isinstance(arguments, dict):
            raise GlooUnavailableError("Gloo function arguments must be an object; hold for system review")
        has_function_call = True
    text = getattr(response, "output_text", None)
    if has_function_call or isinstance(text, str) and text.strip():
        return
    raise GlooUnavailableError("Gloo response has no usable text or requested function call")


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
        # Inspect only dynamic input, not static instructions or tool definitions.
        # Known sensitive inputs remain local; callers use the existing held path.
        from app.llm.parser import keyword_sensitive
        def sensitive(value):
            if isinstance(value, str):
                return keyword_sensitive(value)
            if isinstance(value, (list, tuple)):
                return any(sensitive(item) for item in value)
            if isinstance(value, dict):
                return any(sensitive(item) for item in value.values())
            return False
        if sensitive(input):
            raise GlooUnavailableError('Recognized sensitive input requires internal human review')
        instructions = ((instructions + "\n\n") if instructions else "") + CHURCH_TEXT_INSTRUCTIONS + "\n\n" + NO_EM_DASH_INSTRUCTIONS
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self._client.responses.create(
                    model=model, input=input, instructions=instructions, **kwargs
                )
                self._record_usage(model, response)
                _require_usable_response(response, kwargs.get("tools"))
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


class NullGloo:
    """Stands in when GLOO_API_KEY is missing; every call fails closed, so
    callers escalate to a human exactly as they would on an outage."""

    usage_log: list = []

    def create_response(self, **kwargs):
        raise GlooUnavailableError("GLOO_API_KEY is not set")

    def total_usage(self) -> dict:
        return {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def build_gloo(settings: Settings | None = None):
    settings = settings or get_settings()
    if not settings.gloo_api_key:
        return NullGloo()
    return GlooClient(settings)
