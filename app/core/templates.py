"""Templated message text. Warm, brief, no guilt, SMS-length.

These are the pre-approved messages the send gate lets through without model
involvement. Anything the model writes goes out with kind="ai" instead.
"""

MAX_SMS_LEN = 320


def first_name(full_name: str) -> str:
    return full_name.split()[0]


def reminder(name: str, role_name: str, when_text: str, service_text: str) -> str:
    return (
        f"Hi {first_name(name)}! Just a reminder: you're serving in the {role_name} "
        f"{when_text} ({service_text}). Thank you for serving! "
        "Reply C to confirm or X if something came up."
    )


def assignment_confirmation(name: str, role_name: str, when_text: str) -> str:
    return (
        f"Hi {first_name(name)}! You're confirmed for {role_name} {when_text}. "
        "Thank you for serving!"
    )


def filled_thanks(name: str) -> str:
    return (
        f"Hi {first_name(name)} — it's been filled, thank you so much for being "
        "willing to help!"
    )


def availability_ask(name: str, month_name: str) -> str:
    return (
        f"Hi {first_name(name)}! Which {month_name} Sundays can you serve? "
        "Reply with dates, 'same as usual', or 'not this month'."
    )


def cancellation_ack(name: str) -> str:
    return (
        f"Thanks for letting us know, {first_name(name)} — you're off the schedule "
        "for that one. We'll find someone, no worries at all!"
    )


def clarify_which_shift(name: str, options: list[str]) -> str:
    numbered = " ".join(f"{i}) {opt}" for i, opt in enumerate(options, start=1))
    return (
        f"Hi {first_name(name)}, quick check — which one can't you make? "
        f"{numbered} Reply with the number."
    )


def approval_request(cancelled_name: str, shift_text: str, candidate_names: list[str]) -> str:
    names = ", ".join(first_name(n) for n in candidate_names)
    return (
        f"{first_name(cancelled_name)} cancelled {shift_text}. "
        f"I'd like to ask {names}. Reply YES to send."
    )


def thanks_anyway(name: str) -> str:
    return (
        f"Thank you so much for being willing, {first_name(name)}! "
        "We've got this one covered another way — we're grateful for you."
    )


def partial_thanks(name: str) -> str:
    return (
        f"Thank you, {first_name(name)}! We need the full time covered for this "
        "one, so no worries at all — we'll keep looking. So grateful you offered."
    )


def clarify_generic(name: str) -> str:
    return (
        f"Hi {first_name(name)}, I want to make sure I get this right — "
        "could you say a little more about what you need?"
    )


def pastor_alert(volunteer_name: str, excerpt: str) -> str:
    excerpt = excerpt if len(excerpt) <= 120 else excerpt[:117] + "..."
    return (
        f"Heads up: {volunteer_name} texted something that may need personal care: "
        f'"{excerpt}". Please reach out directly — automated replies to them are paused.'
    )


def unknown_number(church_name: str) -> str:
    return (
        f"Hi! This number is for {church_name} volunteers. "
        "Please contact the church office if you need help."
    )


def stop_confirm(church_name: str) -> str:
    return (
        f"You've been unsubscribed from {church_name} volunteer texts and won't "
        "receive any more messages. Text START to rejoin."
    )


def start_confirm(church_name: str) -> str:
    return f"Welcome back! You'll receive {church_name} volunteer texts again."
