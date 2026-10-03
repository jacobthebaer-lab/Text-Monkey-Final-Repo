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
- Cloud experiment policy, October 3, 2026: competition compliance is mandatory. Google Voice's Acceptable Use Policy prohibits automated/scripted texts, so Google Voice automation is permanently held regardless of completed ID verification, cookies, environment switches or superadmin approval. ID approval permits manual Google Voice use only, with personal-phone forwarding off as requested. The cloud build supports disconnected simulation and generic backend infrastructure; it must not import Google sessions or send automated Google texts. Future production transport is registered Twilio with a dedicated number per church and requires separately authorized registration/provisioning. Preserve the current Mac-connected demo; do not activate paid services, broad sends or production transport from synthetic evidence.
- Use Gloo for incoming interpretation and outgoing composition. Hold messages when Gloo is unavailable; do not silently send templates or switch AI providers.
- Jacob requires ZERO em dashes in every outgoing SMS/iMessage, across signup, recovery, reminders, invitations, replacement and admin updates. Use commas or periods. Enforce this before enqueue and native delivery, including presentation forms. Never silently rewrite reviewed/approved bodies or their hashes; regenerate through Gloo and obtain fresh review when required. Ordinary hyphens and en dashes remain allowed.
- Application code must enforce consent, scheduling, eligibility, quiet hours and review requirements.
- Deduplicate concise admin status updates three hours before each event.
- Verify actual delivery separately from synthetic previews. Paused scheduling or Messages cannot establish working background updates.
- Never commit private credentials, real phones, conversations, databases or native delivery receipts.
- Follow the README for portable synthetic setup. Cloud coding access does not establish connected runtime readiness.

## Hackathon rules

Read [the official-rules reference](docs/HACKATHON_RULES.md) when preparing judged
work, demos or submissions. Preserve competition-period history, required consent
and license evidence. Keep official requirements separate from project policies;
never invent a required YES response or SMS footer. Do not claim team eligibility,
individual rule acceptance or submission completion without human verification.
This reference does not authorize changing Jacob's exact copy or enabling delivery.
