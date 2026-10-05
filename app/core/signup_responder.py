"""Gloo writes signup replies; deterministic code owns consent and delivery."""

import json
import re
from pathlib import Path
from datetime import timedelta
from sqlalchemy import select
from app.db import models as m

from app.config import get_settings
from app.llm.agent_loop import RunLogger
from app.llm.gloo_client import GlooUnavailableError
from app.core.message_style import outbound_style_problem
from app.llm.parser import keyword_sensitive
from app.core.cloud_composition import record_composition

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "signup_reply.md"
MONKEY_EMOJIS = ('🐒', '🐵', '🙈', '🙉', '🙊')
MONKEY_PATTERN = re.compile(r'[🐒🐵🙈🙉🙊]\ufe0f?')
LIGHT_EMOJIS = MONKEY_EMOJIS + ('👋', '😊', '👍', '🙌', '✨', '💛')
EMOJI_PATTERN = re.compile(r'[🐒🐵🙈🙉🙊👋😊👍🙌✨💛]\ufe0f?')


def _without_command_footer(text):
    """Remove known trailing notices after consent, preserving the main reply."""
    return re.sub(
        r"(?:^|[,;]?\s+or\s+|\s+)(?:(?:Reply|Text)\s+)?\bSTOP\b(?:\s+to\s+(?:stop|unsubscribe|opt[ -]?out))?"
        r"(?:\s+or\s+HELP(?:\s+for\s+help)?)?[.!]?\s*(?=[🐒🐵🙈🙉🙊👋😊👍🙌✨💛]\ufe0f?\s*$|$)",
        "", text, flags=re.I,
    ).rstrip()


def _without_monkey_emoji(text):
    # Keep the established helper name for callers; strip all permitted light
    # emoji from canonical facts so decoration stays optional, chosen by Gloo.
    return EMOJI_PATTERN.sub('', text).strip()


def _signup_style(text, signup_conversation, allowed_monkeys=()):
    used = False
    def keep_one(match):
        nonlocal used
        emoji = match.group().rstrip('\ufe0f')
        if signup_conversation and not used and emoji in allowed_monkeys:
            used = True
            return emoji
        return ''
    return re.sub(r'[ \t]{2,}', ' ', EMOJI_PATTERN.sub(keep_one, text)).replace('Texty', 'Text Monkey').strip()


def compose_signup_reply(session, clock, gloo, approved_message, required_phrases=(), *, volunteer=None, phone=None, signup_conversation=False, require_gloo=False, preferred_wording=None, allow_emoji=True, exact_copy=False, recovery=None, factual_context=None):
    approved_message = approved_message if exact_copy else _without_monkey_emoji(approved_message)
    if keyword_sensitive(approved_message):
        raise GlooUnavailableError('Recognized sensitive details require internal human review')
    include_command_notice = volunteer is None or not volunteer.sms_opt_in
    recipient = phone or (volunteer.phone if volunteer is not None else None)
    settings = getattr(gloo, "settings", get_settings())
    # Cloud replies never use the legacy local-template shortcut, even when
    # a deployment forgot to enable the optional Mac signup-composition flag.
    require_gloo = require_gloo or settings.sms_provider == "google_voice"
    selected = session.info.get("mac_test_session")
    if settings.sms_provider == "mac_messages" and settings.mac_bridge_enabled:
        from app.integrations.test_sessions import parse_sessions
        from app.sms.mac_provider import demo_phones
        selected = parse_sessions(settings.mac_test_sessions, demo_phones(settings.mac_demo_phones)).get(recipient)
        if selected is None or not selected.active(clock.now()):
            raise GlooUnavailableError("Reply needs an active recipient test session before reading history")
    elif settings.sms_provider == "google_voice":
        from app.sms.google_voice_provider import GoogleVoiceProvider
        if settings.google_voice_demo_mode:
            from app.integrations.google_voice_demo import RECIPIENT_KEY, sender_fingerprint
            from app.sms.google_voice_provider import GoogleVoiceTestSession
            registration = session.get(m.Policy, RECIPIENT_KEY + recipient)
            if registration and registration.value.get("state") == "active" and registration.value.get("sender_fingerprint") == sender_fingerprint(settings):
                spec = registration.value["session"]
                from datetime import datetime
                selected = GoogleVoiceTestSession(spec["id"], datetime.fromisoformat(spec["starts_at"]), datetime.fromisoformat(spec["expires_at"]))
            else:
                selected = GoogleVoiceProvider(settings).test_sessions.get(recipient)
        else:
            selected = GoogleVoiceProvider(settings).test_sessions.get(recipient)
        if selected is None or not selected.active(clock.now()):
            raise GlooUnavailableError("Reply needs an active cloud recipient session before reading history")
    if include_command_notice and recipient:
        from app.core.conversation import scope
        previous = session.scalar(scope(select(m.Message.id), selected).where(
            m.Message.phone == recipient, m.Message.direction == "out").limit(1))
        include_command_notice = previous is None
    if not include_command_notice and not exact_copy:
        approved_message = _without_command_footer(approved_message)
        required_phrases = tuple(p for p in required_phrases if p not in {"STOP", "HELP"})
    allowed_monkeys = ()
    if signup_conversation and recipient and allow_emoji:
        from app.core.conversation import scope
        recent_out = session.scalars(scope(select(m.Message.body), selected).where(
            m.Message.phone == recipient, m.Message.direction == 'out',
            m.Message.created_at >= clock.now()-timedelta(hours=24),
        ).order_by(m.Message.id.desc()).limit(8)).all()
        # Emoji-free replies are the default. Never decorate adjacent replies.
        if not any(EMOJI_PATTERN.search(body) for body in recent_out[:2]):
            last_monkey = next((EMOJI_PATTERN.search(body).group().rstrip('\ufe0f')
                for body in recent_out if EMOJI_PATTERN.search(body)), None)
            allowed_monkeys = tuple(emoji for emoji in LIGHT_EMOJIS if emoji != last_monkey)
    if (require_gloo or exact_copy or recovery) and gloo is None:
        raise GlooUnavailableError("Gloo is required to compose this message")
    if not (require_gloo or exact_copy or recovery) and (gloo is None or not settings.gloo_signup_replies):
        rendered = _signup_style(approved_message, signup_conversation)
        if outbound_style_problem(rendered):
            raise GlooUnavailableError('Em dashes are not allowed in outgoing texts')
        if signup_conversation and (not _without_monkey_emoji(rendered) or len(rendered) > 600):
            raise GlooUnavailableError("Signup reply exceeds the message limit")
        return rendered
    log = RunLogger(session, clock, agent="signup_reply", trigger="Text signup response", model=settings.parser_model)
    facts = {"approved_message": approved_message, "required_phrases": list(required_phrases),
             "include_command_notice": include_command_notice,
             "signup_conversation": signup_conversation, "product_name": "Text Monkey",
             "allowed_monkey_emojis": [emoji for emoji in allowed_monkeys if emoji in MONKEY_EMOJIS],
             "allowed_emojis": list(allowed_monkeys)}
    facts['exact_copy'] = exact_copy
    if factual_context is not None:
        facts['schedule_context'] = factual_context
    if recovery is not None:
        facts['recovery'] = recovery
    if preferred_wording:
        facts["preferred_wording"] = _without_monkey_emoji(preferred_wording)
    if volunteer is not None:
        from app.core.conversation import scope
        recent = session.scalars(scope(select(m.Message), selected).where(
            m.Message.phone == volunteer.phone, m.Message.volunteer_id == volunteer.id,
            m.Message.created_at >= clock.now()-timedelta(hours=24),
        ).order_by(m.Message.id.desc()).limit(8)).all()
        from app.core.privacy import safe_message_history
        facts.update(sender={"name": volunteer.name, "volunteer_id": volunteer.id},
                     recent_messages=[{"direction": row.direction, "body": row.body}
                                      for row in safe_message_history(session, reversed(recent))])
    try:
        response = gloo.create_response(
            model=settings.parser_model, instructions=PROMPT.read_text(),
            input=json.dumps(facts, ensure_ascii=False),
        )
    except GlooUnavailableError:
        log.close("gloo_unavailable")
        raise
    log.add_usage(getattr(response, "usage", None))
    if outbound_style_problem(getattr(response,'output_text','') or '') and (signup_conversation or require_gloo or exact_copy or recovery):
        # Signup calls compose before SendGate; reject here. Transactional
        # composition inside SendGate uses its final BLOCKED_STYLE outcome.
        log.close('invalid_typography')
        raise GlooUnavailableError('Gloo returned an em dash; nothing was sent')
    if recovery is not None:
        from app.core.signup_recovery import validate_reply
        try:
            text=validate_reply(getattr(response,'output_text','') or '',recovery,approved_message)
        except ValueError as exc:
            import hashlib
            raw=getattr(response,'output_text','') or ''
            log.step('decision',result={'validation_error':str(exc),
                'output_sha256':hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                'output_length':len(raw),'stage':recovery['stage']})
            log.close('invalid_recovery')
            raise GlooUnavailableError('Gloo recovery failed stage validation; nothing was sent') from exc
        log.close('recovery_composed')
        return record_composition(session, recipient, text, selected)
    if exact_copy:
        text = getattr(response, 'output_text', '') or ''
        if factual_context is None:
            text = text.strip()
        if text != approved_message or not text or len(text) > 600 or any(phrase not in text for phrase in required_phrases):
            log.close('invalid_exact_copy')
            raise GlooUnavailableError('Gloo changed the approved exact copy; nothing was sent')
        log.close('exact_copy_composed')
        return record_composition(session, recipient, text, selected)
    text = _signup_style(getattr(response, "output_text", "") or "", signup_conversation, allowed_monkeys)
    if not include_command_notice:
        text = _without_command_footer(text)
        if re.search(r"\b(?:STOP|HELP)\b", text):
            log.close("invalid_reply")
            raise GlooUnavailableError("Gloo repeated command guidance after consent")
    if (re.search(r"\breply\s+(?:YES|NO|Y|N)\b", text, re.I)
            and not re.search(r"\breply\s+(?:YES|NO|Y|N)\b", approved_message, re.I)
            and "YES" not in required_phrases):
        log.close("invalid_reply")
        raise GlooUnavailableError("Gloo added an RSVP instruction without an approved offer")
    if (not _without_monkey_emoji(text) or len(text) > 600 or re.search(r"https?://|www\.", text, re.I)
            or re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u23F0-\u23FF]", _without_monkey_emoji(text))
            or any(phrase not in text for phrase in required_phrases)):
        log.close("invalid_reply")
        raise GlooUnavailableError("Gloo signup reply failed validation; no substitute was sent")
    log.close("reply_composed")
    return record_composition(session, recipient, text, selected)
