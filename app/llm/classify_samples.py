"""Classify data/sample_texts.json against the real Gloo parser and print a table.

Run: python -m app.llm.classify_samples
Needs GLOO_API_KEY in the environment (.env). Uses real Gloo calls, never SMS.
"""

import json
import sys
from pathlib import Path

from app.config import get_settings
from app.llm.gloo_client import GlooClient
from app.llm.parser import parse_inbound

DATA = Path(__file__).resolve().parents[2] / "data" / "sample_texts.json"


def main() -> int:
    settings = get_settings()
    if not settings.gloo_api_key:
        print("GLOO_API_KEY is not set — copy .env.example to .env and fill it in.")
        return 1

    gloo = GlooClient(settings)
    texts = json.loads(DATA.read_text())["texts"]

    header = f"{'text':<48} {'expected':<13} {'got':<13} {'conf':<5} {'sens':<5} ok"
    print(header + "\n" + "-" * len(header))
    intent_hits = sensitive_hits = 0
    for row in texts:
        parsed = parse_inbound(gloo, row["text"])
        intent_ok = parsed.intent == row["expected_intent"]
        sensitive_ok = parsed.sensitive == row.get("sensitive", False)
        intent_hits += intent_ok
        sensitive_hits += sensitive_ok
        shown = row["text"] if len(row["text"]) <= 46 else row["text"][:43] + "..."
        print(
            f"{shown:<48} {row['expected_intent']:<13} {parsed.intent:<13} "
            f"{parsed.confidence:<5.2f} {str(parsed.sensitive):<5} "
            f"{'OK' if intent_ok and sensitive_ok else 'MISS'}"
        )

    total = len(texts)
    usage = gloo.total_usage()
    print(
        f"\nintent: {intent_hits}/{total} ({intent_hits / total:.0%})   "
        f"sensitive flag: {sensitive_hits}/{total} ({sensitive_hits / total:.0%})\n"
        f"model: {settings.parser_model}   tokens: {usage['input_tokens']} in / "
        f"{usage['output_tokens']} out over {usage['calls']} calls"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
