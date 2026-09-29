"""Generic tool-calling loop over the Gloo Responses API (PLAN.md section 4).

RunLogger gives every agent run an auditable trail: agent_runs/agent_steps
rows plus a JSONL line per step in logs/session-YYYY-MM-DD.jsonl (the session
log required by the challenge). Deterministic code around the loop logs its
own 'decision' steps into the same run.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.clock import Clock
from app.db import models as m
from app.llm.gloo_client import GlooClient, GlooUnavailableError
from app.llm.tools import ToolDef

logger = logging.getLogger("agent_loop")

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


class RunLogger:
    """One agent_runs row + its agent_steps + the JSONL session log."""

    def __init__(
        self,
        session: Session,
        clock: Clock,
        *,
        agent: str,
        trigger: str,
        model: str | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        self.log_dir = log_dir or DEFAULT_LOG_DIR
        self.run = m.AgentRun(
            agent=agent, trigger=trigger[:200], model=model, started_at=clock.now()
        )
        session.add(self.run)
        session.flush()
        self._step_no = 0

    def step(self, step_type: str, *, tool_name: str | None = None, arguments=None, result=None) -> None:
        self._step_no += 1
        now = self.clock.now()
        if result is not None and not isinstance(result, dict):
            result = {"value": result}
        self.session.add(
            m.AgentStep(
                run_id=self.run.id,
                step_no=self._step_no,
                type=step_type,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                created_at=now,
            )
        )
        self.session.flush()
        self._append_jsonl(
            {
                "ts": now.isoformat(),
                "run_id": self.run.id,
                "agent": self.run.agent,
                "step_no": self._step_no,
                "type": step_type,
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
            }
        )

    def add_usage(self, usage) -> None:
        self.run.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.run.output_tokens += getattr(usage, "output_tokens", 0) or 0

    def close(self, outcome: str) -> None:
        self.run.ended_at = self.clock.now()
        self.run.steps = self._step_no
        self.run.outcome = outcome[:200]
        self.session.flush()

    def _append_jsonl(self, record: dict) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"session-{self.clock.now().date().isoformat()}.jsonl"
            with open(path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError:  # never let audit logging take the agent down
            logger.exception("could not append to session log")


def run_agent(
    gloo: GlooClient,
    run_logger: RunLogger,
    *,
    model: str,
    instructions: str,
    user_input: str,
    tools: dict[str, ToolDef],
    max_steps: int,
) -> dict:
    """Loop until the model answers without tool calls, or max_steps is hit.

    Returns {"outcome": "completed" | "max_steps" | "gloo_unavailable",
             "final_text": str | None}. Callers escalate on anything but
    completed — never guess.
    """
    input_items: list = [{"role": "user", "content": user_input}]
    tool_schemas = [t.schema() for t in tools.values()]

    for _ in range(max_steps):
        try:
            response = gloo.create_response(
                model=model, input=input_items, instructions=instructions, tools=tool_schemas
            )
        except GlooUnavailableError as e:
            run_logger.step("decision", result={"error": f"gloo unavailable: {e}"})
            return {"outcome": "gloo_unavailable", "final_text": None}
        run_logger.add_usage(getattr(response, "usage", None))
        run_logger.step("model_call", result={"output_types": [getattr(i, "type", "?") for i in response.output]})

        calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            return {"outcome": "completed", "final_text": getattr(response, "output_text", None)}

        for call in calls:
            arguments = _parse_arguments(call.arguments)
            run_logger.step("tool_call", tool_name=call.name, arguments=arguments)
            result = _execute(tools, call.name, arguments)
            run_logger.step("tool_result", tool_name=call.name, result=result)
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(result, default=str),
                }
            )

    run_logger.step("decision", result={"error": "max agent steps reached"})
    return {"outcome": "max_steps", "final_text": None}


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _execute(tools: dict[str, ToolDef], name: str, arguments: dict) -> dict:
    tool = tools.get(name)
    if tool is None:
        return {"error": f"unknown tool: {name}"}
    try:
        result = tool.handler(arguments)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as e:  # tool errors go back to the model, never crash the loop
        logger.exception("tool %s failed", name)
        return {"error": str(e)}
