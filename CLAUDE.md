# CLAUDE.md: Working rules for this repo

## Shared repository, effective October 3, 2026

Jacob selected **jacobthebaer-lab/text-monkey** as the shared private repository for Jacob, Clyde (`Clyde-Kertzer`) and Noah (`clementsnc`). All new work, pushes and pull requests go to https://github.com/jacobthebaer-lab/text-monkey. The default integration branch is `codex/complete-text-monkey`; the retained `main` branch is historical and is not the current integrated product.

Verify `git remote get-url origin` before pushing. It must be `https://github.com/jacobthebaer-lab/text-monkey.git` (or its SSH equivalent). Existing clones can run:

```sh
git remote set-url origin https://github.com/jacobthebaer-lab/text-monkey.git
git fetch origin
```

Preserve uncommitted work before changing branches. Use separate feature branches/worktrees from the current default integration branch, run relevant checks, then commit, push and open pull requests to this new repository. `clementsnc/planning-center-but-better` is the historical upstream; do not push new work there unless Jacob explicitly requests it. Forks do not synchronize automatically.

Clyde and Noah have active write collaborator access, including code pushes and pull-request merges. GitHub personal repositories keep owner-only administration with Jacob; this setup does not grant collaborator admin roles. Cloud can select this new repository. On October 3, 2026 Jacob authorized an optional cloud SMS transport so the Mac can be off; see docs/CLOUD_SMS.md. Cloud coding access alone does not enable real texting.

This project is a text-first volunteer scheduling agent for churches (Gloo AI Hackathon 2026, Agents Track). The full build plan is in `PLAN.md`. Read it before starting any work.

## How to work
- Work one phase at a time from PLAN.md section 20. At the end of each phase: run tests, commit, summarize what was built / stubbed / decided, then stop and wait for the human.
- Prefer simple, readable code over clever code. This will be judged by a code verification team and explained in a 90-second pitch.
- Commit early and often with clear messages. Commit history is our proof that work happened during the competition period.
- Ask before adding dependencies not listed in PLAN.md section 3.

## Safety rules (never break these)
- Never read, print, or commit `.env`, API keys, or `demo_phones.json`.
- Connected texting uses Gloo and the explicitly selected transport with consent and recipient scope. Jacob authorized optional cloud SMS on October 3, 2026; follow docs/CLOUD_SMS.md before activation. Mac tests retain bounded session scope. Tests and evals use mock carriers; synthetic previews cannot establish real delivery.
- All outbound messages are reserved through `app/core/send_gate.py`. Only the configured transport dispatcher may submit committed reservations after fresh policy checks.
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
