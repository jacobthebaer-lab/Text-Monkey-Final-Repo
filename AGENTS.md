# Text Monkey agent instructions

## Shared repository, effective October 3, 2026

Jacob selected **jacobthebaer-lab/text-monkey** as the shared private repository for Jacob, Clyde (`Clyde-Kertzer`) and Noah (`clementsnc`). All new work, pushes and pull requests go to https://github.com/jacobthebaer-lab/text-monkey. The default integration branch is `codex/complete-text-monkey`; the retained `main` branch is historical and is not the current integrated product.

Verify `git remote get-url origin` before pushing. It must be `https://github.com/jacobthebaer-lab/text-monkey.git` (or its SSH equivalent). Existing clones can run:

```sh
git remote set-url origin https://github.com/jacobthebaer-lab/text-monkey.git
git fetch origin
```

Preserve uncommitted work before changing branches. Use separate feature branches/worktrees from the current default integration branch, run relevant checks, then commit, push and open pull requests to this new repository. `clementsnc/planning-center-but-better` is the historical upstream; do not push new work there unless Jacob explicitly requests it. Forks do not synchronize automatically.

Clyde and Noah have active write collaborator access, including code pushes and pull-request merges. GitHub personal repositories keep owner-only administration with Jacob; this setup does not grant collaborator admin roles. Cloud can select this new repository, but laptop Messages remains required for connected transport and real delivery checks.

## Application requirements

- Use existing admin settings for event schedules, staffing and admin recipients.
- Use Messages on the coordinator's Mac for texting transport; do not substitute a hosted SMS provider.
- Use Gloo for incoming interpretation and outgoing composition. Hold messages when Gloo is unavailable; do not silently send templates or switch AI providers.
- Application code must enforce consent, scheduling, eligibility, quiet hours and review requirements.
- Deduplicate concise admin status updates three hours before each event.
- Verify actual delivery separately from synthetic previews. Paused scheduling or Messages cannot establish working background updates.
- Never commit private credentials, real phones, conversations, databases or native delivery receipts.
- Follow the README for portable synthetic setup. Cloud coding access does not establish connected runtime readiness.
