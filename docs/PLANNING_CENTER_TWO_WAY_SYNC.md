# Scoped Planning Center API sync

The existing service and resource IDs remain the integration identity after a
church rename. Use the completed Text Monkey admin workspace's church name.
Rename the selected ServiceType, teams, Plan titles/series and PlanTime labels
with narrow API PATCHes and fresh readback. Services rejects assignment of the
Organization's `name`; that one setting requires the account settings UI.
Changing labels must preserve UTC intervals, reminders, memberships and status.
Historical synthetic setup tools and competition evidence are retained.

`planning_center_sync.py` reconciles **linked** event titles and intervals in both
directions. A saved common baseline distinguishes local edits from native edits.
Concurrent edits hold both records. Before a PATCH, the intended outcome is
committed; recovery reads back the outcome before deciding whether a field still
needs updating. It never recreates an event because a response was lost.
Recurring imports add new native plans without overwriting pending local edits.
API calls run outside event/profile database transactions. After API reads,
short serialized compare-and-set transactions verify local fields, identity
links and the complete saved baseline before applying pulls, claiming exports
or acknowledging their readback. A newer edit holds reconciliation; partial
exports keep their pending outcome instead of overwriting that edit.

Existing mapped volunteer names use the People API, its explicit version, the
credential organization, and an independent exact phone match. A numeric
Services ID alone does not establish People identity. Updates touch first/last
name only; ambiguous structured names and conflicts hold. No login, permissions,
qualifications or SMS consent are granted. New Text Monkey signup persistence in
Supabase remains the separate cloud-registration worker.

Generated native BlockoutDate UTC intervals supplement local eligibility.
Removing a native blockout cannot erase local unavailability. A refresh error
holds the mapped person, and application decision clocks enforce a five-minute
freshness limit. These records belong to the private `texty` store, not public
browser-readable tables.

The staffing worker continues to capture local confirmations/cancellations in
the same transaction and reconcile their native C/D status. A verified incoming
decline/removal of an active future assignment now queues an ordinary durable
FillRequest. The existing fill scheduler owns ranking, Gloo composition, consent,
eligibility, review, quiet hours and transport. It excludes the cancelled person.
Repeated polls do not create duplicate requests. Importing historical declines
does not begin outreach, and no SMS receipt is invented for an API transition.
An observed native C/U-to-D transition also saves immutable pre/post native
evidence and the original interval in the source-bound refusal ledger. Central
eligibility excludes that interval across ranking, other roles and later fill
requests. Other dates remain eligible. Rescheduling does not move the recorded
interval, and only the existing exact operator reversal can lift it. Missing or
altered provenance holds for source review. Planner removals do not invent a
volunteer refusal.

## Run against an existing selected store

Review/apply the existing private PCO migration and ensure the store matches the
current ORM schema. The CLI does not perform migrations or enable transport.
Keep credentials, signing keys, process receipts and journals outside Git.

```sh
python tools/planning_center_sync.py \
  --env-file /private/backend.env --env-file /private/planning-center.env \
  --expected-org NUMERIC_ORGANIZATION_ID --expected-project PROJECT_REFERENCE \
  --watch \
  --journal /private/planning-center-sync.json
```

Install verified person and coordinator-reviewed canonical position bindings in
the authoritative scheduling store before activating staffing write/poll flags.
Use the existing authenticated role-binding workflow; preserve role requirements.
Give that runtime the same private signing key used for its role review.
Do not run two authoritative assignment writers against different databases.
Supabase and the connected Mac have different numeric IDs; use native identity
links, never copy local IDs into the cloud.
The current connected deployment uses Mac SQLite as the sole scheduling and
delivery authority, with Supabase as its cloud metadata/profile mirror. Leave
cloud `--event-writes` and `--profile-writes` off. Those switches are reserved for
an explicitly handed-off authoritative store, never a parallel mirror worker.

In the application, `PCO_SYNC_ENABLED=true` adds metadata and native availability
refresh to the existing 60-second PCO job, including when general messaging
automation is disabled. API exports also require `PCO_STAFFING_WRITE_ENABLED`.
Signed schedule webhooks use create-only import while this reconciliation is
enabled, so they cannot overwrite an unsynchronized local edit. Keep both the
application and standalone worker from owning the same profile writes at once.

## Supported boundaries

- New local events without a native link, cancellation/deletion, and conflicting
  metadata require an explicit native mapping/action; they are not blindly
  exported or deleted.
- Services does not publish a new-person creation endpoint. People creation and
  Services enrollment are different operations. This worker cannot promise
  automatic Services enrollment for every signup.
- Native blockout/frequency **write execution** remains held under the existing
  notification, ownership and source-review contracts. General hours, event-relative
  windows and hard monthly limits do not have equivalent native API fields.
  Local restrictions must not be broadened to appear synchronized.
- An active API worker and passing synthetic tests do not prove a real
  decline-to-replacement phone delivery. Verify that separately with an actual
  authorized participant and the connected transport.

References: [Services Plan](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan),
[PlanTime](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/plan_time),
[People Person](https://api.planningcenteronline.com/docs/apps/people/versions/2025-07-17/vertices/person),
[BlockoutDate](https://api.planningcenteronline.com/docs/apps/services/versions/2018-11-01/vertices/blockout_date),
and [staffing activation](PLANNING_CENTER_ACTIVATION.md).
