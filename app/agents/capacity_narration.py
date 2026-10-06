"""Gloo arranges supported observations and human next steps, without new claims."""
import hashlib
import json
from pathlib import Path

from app.config import get_settings
from app.core.message_style import outbound_style_problem
from app.llm.agent_loop import run_agent
from app.llm.gloo_client import NullGloo
from app.llm.tools import ToolDef

PROMPT = Path(__file__).resolve().parents[2] / "prompts/capacity_agent.md"
PREFIXES = ("", "Worth reviewing: ", "For your review: ")


def narrate(ctx, flags, logger):
    """Only publish a completed tool run, bound to each flag's exact facts.

    The summary must contain a supplied observation verbatim, with an optional
    warm lead-in. Next steps are supplied human actions, never model-granted
    authority. Free model prose cannot introduce diagnoses or invented facts.
    """
    if not flags:
        logger.close("no_capacity_flags")
        return
    facts, drafts = {}, {}
    for flag in flags:
        source = {"flag_id": flag.id, "type": flag.type, "kind": flag.kind,
            "evidence": flag.evidence, "observation": flag.summary, "next_step": flag.suggested_action}
        token = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        facts[flag.id] = {**source, "source_hash": token,
            "allowed_summaries": [prefix + flag.summary for prefix in PREFIXES
                if len(prefix + flag.summary) <= 300 and not outbound_style_problem(prefix + flag.summary)]}

    def read(args):
        return {"flags": list(facts.values())}

    def compose(args):
        identity = args.get("flag_id")
        fact = facts.get(identity) if type(identity) is int else None
        if not fact or args.get("source_hash") != fact["source_hash"]:
            return {"error": "Unknown flag or changed evidence; read the supplied facts"}
        if (set(args) != {"flag_id", "source_hash", "summary", "suggested_action"}
                or args.get("summary") not in fact["allowed_summaries"]
                or args.get("suggested_action") != fact["next_step"]
                or outbound_style_problem(args.get("suggested_action"))):
            return {"error": "Use a supplied observation and human next step exactly; no additional claims"}
        drafts[identity] = {"state": "ready", "source_hash": fact["source_hash"],
            "summary": args["summary"], "suggested_action": args["suggested_action"]}
        return {"flag_id": identity, "narration_prepared": True, "contacted": False}

    gloo = getattr(ctx, "gloo", None) or NullGloo()
    settings = getattr(gloo, "settings", get_settings())
    tools = {
        "read_capacity_facts": ToolDef("read_capacity_facts", "Read scanned evidence and supported wording only",
            {"type": "object", "properties": {}, "additionalProperties": False}, read),
        "narrate_flag": ToolDef("narrate_flag", "Prepare evidence-bound explanation and a human next step; no sends or mutations",
            {"type": "object", "additionalProperties": False, "properties": {
                "flag_id": {"type": "integer"}, "source_hash": {"type": "string"},
                "summary": {"type": "string"}, "suggested_action": {"type": "string"}},
                "required": ["flag_id", "source_hash", "summary", "suggested_action"]}, compose),
    }
    result = run_agent(gloo, logger, model=settings.agent_model, instructions=PROMPT.read_text(),
        user_input=json.dumps({"scan_at": ctx.clock.now().isoformat(), "flags": list(facts.values())}, ensure_ascii=False),
        tools=tools, max_steps=settings.max_agent_steps)
    ready = 0
    for flag in flags:
        fact = facts[flag.id]
        draft = drafts.get(flag.id) if result["outcome"] == "completed" else None
        if flag.evidence != fact["evidence"]:
            draft = None
        narration = draft or {"state": "held", "source_hash": fact["source_hash"],
            "outcome": result["outcome"] if result["outcome"] != "completed" else "validated_narration_missing"}
        flag.evidence = {**flag.evidence, "observation": fact["observation"],
            "next_step": fact["next_step"], "narration": narration}
        flag.summary = draft["summary"] if draft else "Gloo narration held. Review the structured evidence."
        flag.suggested_action = draft["suggested_action"] if draft else None
        ready += bool(draft)
    logger.step("decision", result={"ready": ready, "held": len(flags) - ready, "contacted": False})
    logger.close(result["outcome"] if ready == len(flags) else "capacity_narration_held")
