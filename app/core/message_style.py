"""Final outbound text rules. Validate without changing reviewed content."""

# Unicode EM DASH, its presentation forms, and two/three-em punctuation.
EM_DASH_CHARACTERS = frozenset("\u2014\ufe31\ufe58\u2e3a\u2e3b")
NO_EM_DASH_INSTRUCTIONS = (
    "For every outgoing SMS or iMessage, use ZERO em dashes, including Unicode "
    "em-dash presentation forms and two/three-em dashes. Use commas or periods "
    "instead. Preserve approved exact wording; never rewrite approved copy. "
    "Ordinary hyphens and en dashes are allowed."
)
CHURCH_TEXT_INSTRUCTIONS = (
    "When composing new volunteer or church administrator texts, use natural "
    "church language and the church's saved name. Technical fixture labels such "
    "as Synthetic, [Fictional], [Fictional history], demo data or test data are "
    "internal metadata, never recipient-facing names or message wording. Use the "
    "natural person, ministry, role and event names without those labels. Keep "
    "the supplied dates, times, availability and staffing facts exact. Do not "
    "invent consent, clearance, bookings or delivery. Preserve already approved "
    "exact copy; changed drafts need fresh review when required."
)
STYLE_BLOCK_REASON = (
    "Text blocked: em dashes are not allowed. Regenerate through Gloo using "
    "commas or periods, then obtain fresh review if required; retry with a new message."
)


class OutboundStyleError(ValueError):
    pass


def outbound_style_problem(body):
    if isinstance(body, str) and EM_DASH_CHARACTERS.intersection(body):
        return STYLE_BLOCK_REASON
    return None


def validate_outbound_style(body):
    """Reject forbidden punctuation before any outbound side effect."""
    problem = outbound_style_problem(body)
    if problem:
        raise OutboundStyleError(problem)
