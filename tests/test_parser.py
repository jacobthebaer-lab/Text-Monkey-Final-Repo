"""Parser validation, retry-once, and the keyword backstop — no real Gloo."""

from app.llm.gloo_client import GlooUnavailableError
from app.llm.parser import ParsedMessage, keyword_self_harm, keyword_sensitive, parse_inbound


class FakeGloo:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def create_response(self, **kwargs):
        self.calls += 1
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item

        class R:
            output_text = item

        return R()


GOOD = '{"intent": "cancel", "shift_hint": "tomorrow", "dates": [], "partial_window": null, "sensitive": false, "severity": "normal", "confidence": 0.9}'


def test_valid_json_parses():
    parsed = parse_inbound(FakeGloo([GOOD]), "cant make it tmrw sorry!!")
    assert parsed.intent == "cancel"
    assert parsed.shift_hint == "tomorrow"
    assert parsed.confidence == 0.9
    assert not parsed.sensitive and not parsed.parse_error


def test_json_with_fences_and_prose_still_parses():
    wrapped = f"Sure! Here you go:\n```json\n{GOOD}\n```"
    assert parse_inbound(FakeGloo([wrapped]), "cant make it").intent == "cancel"


def test_invalid_json_retries_once_then_unclear():
    gloo = FakeGloo(["not json at all", "still not json"])
    parsed = parse_inbound(gloo, "hmm")
    assert gloo.calls == 2
    assert parsed.intent == "unclear"
    assert parsed.parse_error


def test_bad_intent_counts_as_invalid():
    bad = '{"intent": "party", "confidence": 0.9, "sensitive": false}'
    parsed = parse_inbound(FakeGloo([bad, GOOD]), "cant make it")
    assert parsed.intent == "cancel"  # retry succeeded


def test_gloo_down_returns_unclear_not_a_guess():
    parsed = parse_inbound(FakeGloo([GlooUnavailableError("down")]), "cant make it")
    assert parsed.intent == "unclear"
    assert parsed.parse_error


def test_keyword_backstop_adds_sensitivity_model_missed():
    not_sensitive = GOOD  # model said sensitive: false
    parsed = parse_inbound(FakeGloo([not_sensitive]), "my dad was just taken to the ER, can't come")
    assert parsed.sensitive is True


def test_self_harm_keywords_force_urgent():
    parsed = parse_inbound(FakeGloo([GOOD]), "some days I just want to hurt myself")
    assert parsed.sensitive is True
    assert parsed.severity == "urgent"


def test_backstop_even_when_gloo_is_down():
    parsed = parse_inbound(FakeGloo([GlooUnavailableError("down")]), "she passed away last night")
    assert parsed.sensitive is True


def test_keyword_lists():
    assert keyword_sensitive("I'm in the hospital")
    assert keyword_sensitive("surgery next week so I'm out")
    assert not keyword_sensitive("running late for dinner")  # no false hit on 'er'
    assert keyword_self_harm("thinking about suicide")
    assert not keyword_self_harm("the funeral is saturday")


def test_model_cannot_unset_backstop_severity():
    calm = '{"intent": "cancel", "sensitive": false, "severity": "normal", "confidence": 0.95}'
    parsed = parse_inbound(FakeGloo([calm]), "I want to end it all")
    assert parsed.sensitive and parsed.severity == "urgent"
