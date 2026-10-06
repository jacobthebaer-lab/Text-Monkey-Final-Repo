# Reviewed Planning Center role bindings

Imported namespaced roles do not automatically share local role interests,
availability windows, monthly caps or qualifications. This workflow explicitly
connects an imported native need to one existing local role. It changes local
shift role IDs and saves a signed position scope, preserving the original native
event/shift links and the existing role policy. It does not create a person,
grant a qualification, schedule anyone or write to Planning Center.

## Configure the selected store

The optional admin API requires `PCO_POSITION_MAPPING_ENABLED=true`, the existing
confirmed Supabase admin identity and email allowlist, selected PCO organization
and service IDs, and `PCO_REVIEW_SIGNING_KEY_PATH`. The signing key must contain
at least 32 private bytes and use owner-only permissions. Keep the same private
key available to subsequent imports and staffing reconciliation. Key loss or
changed role policy holds a signed binding until corrected or explicitly reviewed
again. The flag defaults to false; staffing write/poll flags remain separate.

Bindings use existing `policies` and `pco_position_scopes` tables. The API does not
run migrations. Choose the authoritative scheduling store, import the selected
future plan and verify its actual times first. The isolated webhook receiver
accepts `--role-signing-key-file` for import verification and exposes no usable
admin mapping API. It initializes only the original scoped PCO receiver tables.
Standalone import callers must configure their session with
`planning_center_role_bindings.configure_session(session, settings)` or supply
the same private signing-key bytes under its exported `KEY` in `session.info`.
The existing staffing tick configures its sessions from settings.

## Review and apply

1. In the signed-in Planning Center review panel, load mapping choices. Select an
   imported future service slot, exact native position and existing local role.
   No local-name match selects a native position automatically.
2. Review the displayed native scope, local role, required qualifications, actual
   event times and all slots affected. The server reads native organization,
   plan, time, team, position and needed-position identity. Duplicate native
   position names within a team hold the mapping.
3. Apply that exact signed review within ten minutes using the same admin identity.
   The server rereads native facts and then locks/rereads the local context before
   applying. A changed selection, policy, event, source link or prior review requires
   fresh review. Different-role rebinding is refused if any affected slot has
   assignment history, including cancelled assignments.
4. Confirm the returned local role and affected shift IDs. Later imports preserve
   the reviewed canonical role while validating the signed policy and native
   position/need identity. Changed plan times or role policy hold the import.
   Explicit same-role policy review remains possible without editing preferences.

The equivalent authenticated API uses GET
`/api/planning-center/role-bindings/catalogue`, POST
`/api/planning-center/role-bindings/proposal` with `shift_id`, `local_role_id`,
`team_id`, `position_id`, `plan_time_id`, then POST
`/api/planning-center/role-bindings` with those same fields plus the returned
`review_hash` and exact `review_token`. Clients cannot supply a different actor
or local role policy. Both responses report `native_writes:false` and
`execution_enabled:false`.

Canonical bindings continue enforcing actual consent, intake readiness,
qualifications, availability and caps during outbound staffing. Inbound native
confirmed/unconfirmed rows also need local eligibility before a local assignment
can be established. Native confirmation does not grant training or clearance.
Person identity, native membership, notification behavior and actual staffing
write/readback remain separate acceptance checks.
