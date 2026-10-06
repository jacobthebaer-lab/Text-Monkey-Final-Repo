"""Minimal coordinator tools over the reviewed planning-pattern module."""
from app.core.policies import PolicyStore
from app.db import models as m
from app.llm.tools import ToolDef


def planning_tools(ctx, coordinator, context_read, remember_review):
    def module():
        # Keep existing coordinator actions usable while the separate reviewed
        # module is being integrated. Missing support never becomes a fallback.
        try:
            from app.core import planning_patterns
        except ImportError:
            raise ValueError("Planning pattern tools are held until the reviewed module is installed") from None
        return planning_patterns

    def rhythm(args):
        if not context_read():
            return {"error": "Read current coordinator context before choosing a volunteer"}
        if (set(args) - {"volunteer_id", "stage_review"} or type(args.get("volunteer_id")) is not int
                or type(args.get("stage_review", False)) is not bool):
            return {"error": "Use an existing volunteer ID and a boolean stage_review"}
        volunteer = ctx.session.get(m.Volunteer, args["volunteer_id"], populate_existing=True)
        if volunteer is None:
            return {"error": "Unknown volunteer ID"}
        patterns = module()
        report = patterns.learned_patterns(ctx.session, volunteer, ctx.clock.now(),
            str(PolicyStore(ctx.session).church_tz()))
        if not args.get("stage_review"):
            return report
        if not report.get("proposal") or report.get("held"):
            return {**report, "approval_ids": [], "held": report.get("held") or "Insufficient completed-event evidence"}
        # The model cannot provide arbitrary patterns. Only the module's actual
        # completed-history proposal can be staged. Missing December history can
        # never introduce a new annual absence preference.
        review = patterns.stage_pattern_review(ctx.session, volunteer, report["proposal"], ctx.clock.now(),
            reason=f"Coordinator {coordinator.id} proposes review of a completed-history serving rhythm; no future booking or consent inferred")
        remember_review(review.id)
        return {**report, "approval_ids": [review.id], "applied": False}

    def seasonal(args):
        if not context_read():
            return {"error": "Read current coordinator context and church timezone first"}
        if set(args) != {"month"} or not isinstance(args["month"], str):
            return {"error": "Provide only the YYYY-MM month"}
        return module().seasonal_staffing_report(ctx.session, args["month"], ctx.clock.now(),
            str(PolicyStore(ctx.session).church_tz()))

    return {
        "review_learned_serving_pattern": ToolDef("review_learned_serving_pattern",
            "Read a completed-history rhythm proposal, optionally stage exact human preference review; never infer annual absence",
            {"type": "object", "additionalProperties": False, "properties": {
                "volunteer_id": {"type": "integer"}, "stage_review": {"type": "boolean"}},
                "required": ["volunteer_id"]}, rhythm),
        "read_seasonal_staffing": ToolDef("read_seasonal_staffing",
            "Read actual same-month/type/role slot comparisons across prior years; recommendations only",
            {"type": "object", "additionalProperties": False, "properties": {
                "month": {"type": "string"}}, "required": ["month"]}, seasonal),
    }
