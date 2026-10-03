# CLAUDE.md: Working rules for this repo

This project is a text-first volunteer scheduling agent for churches (Gloo AI Hackathon 2026, Agents Track). The full build plan is in `PLAN.md`. Read it before starting any work.

## How to work
- Work one phase at a time from PLAN.md section 20. At the end of each phase: run tests, commit, summarize what was built / stubbed / decided, then stop and wait for the human.
- Prefer simple, readable code over clever code. This will be judged by a code verification team and explained in a 90-second pitch.
- Commit early and often with clear messages. Commit history is our proof that work happened during the competition period.
- Ask before adding dependencies not listed in PLAN.md section 3.

## Safety rules (never break these)
- Never read, print, or commit `.env`, API keys, or `demo_phones.json`.
- Connected texting uses Gloo and laptop Messages with explicit consent, recipient and session scope. Tests and evals always use the mock provider; synthetic previews cannot establish real delivery. Do not substitute a hosted SMS provider.
- All outbound messages go through `app/core/send_gate.py`. Nothing else may call an SMS provider.
- Hard eligibility rules live in code (`app/core/eligibility.py`), never only in prompts.
- Never edit files in `/evals/cases` or change pass criteria without explicit human approval. When an eval fails, fix code or prompts.
- When a prompt in `/prompts` changes: bump its version header and add an entry to `PROMPTS_CHANGELOG.md` explaining what was wrong and what changed.
- Use synthetic data only. No real names, phone numbers, or church data except demo phones in the gitignored file.

## Gloo AI specifics
- OpenAI SDK, `base_url="https://platform.ai.gloo.com/ai/v2/guarded"`, key from `GLOO_API_KEY`.
- Responses API with pinned model names from env vars.
- Tools use the nested schema: `{"type": "function", "function": {...}}`.
- Log token usage from every response.
- Docs: https://docs.gloo.com/llms.txt
