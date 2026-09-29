"""GlooClient retry policy and usage logging, against a fake OpenAI client."""

import httpx
import openai
import pytest

from app.config import Settings
from app.llm.gloo_client import GlooClient, GlooUnavailableError

SETTINGS = Settings(gloo_api_key="test-key")


class FakeUsage:
    input_tokens = 100
    output_tokens = 20


class FakeResponse:
    output_text = '{"ok": true}'
    usage = FakeUsage()


def _status_error(code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://example.test/responses")
    response = httpx.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


class FakeOpenAI:
    """Yields queued errors, then a FakeResponse forever."""

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = 0
        self.responses = self

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return FakeResponse()


def make_client(errors=()):
    sleeps = []
    fake = FakeOpenAI(errors)
    client = GlooClient(SETTINGS, client=fake, sleeper=sleeps.append)
    return client, fake, sleeps


def test_success_records_usage():
    client, fake, sleeps = make_client()
    response = client.create_response(model="m", input="hi")
    assert response.output_text == '{"ok": true}'
    assert client.total_usage() == {"input_tokens": 100, "output_tokens": 20, "calls": 1}
    assert sleeps == []


def test_retries_on_429_with_backoff():
    client, fake, sleeps = make_client([_status_error(429), _status_error(503)])
    client.create_response(model="m", input="hi")
    assert fake.calls == 3
    assert sleeps == [1, 2]


def test_gives_up_after_max_attempts():
    client, fake, sleeps = make_client([_status_error(500)] * 3)
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="m", input="hi")
    assert fake.calls == 3
    assert sleeps == [1, 2]


def test_non_retryable_error_fails_immediately():
    client, fake, sleeps = make_client([_status_error(401)])
    with pytest.raises(GlooUnavailableError):
        client.create_response(model="m", input="hi")
    assert fake.calls == 1
    assert sleeps == []
