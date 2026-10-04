# Audited correction lineage, held previews only

Normal producers still require the saved Mac inbound receipt intent to equal
the profile queue route. One independently audited historical local correction
may instead have a separate signed `Policy` record. Original message, receipt,
profile and queue rows are preserved. This adds no schema, worker or sender.

This evidence class is `audited_local_producer`. It is **not** a vendor-signed
Gloo response. Audit hashes identify reviewed artifacts; they do not establish
that a provider call occurred. The coordinator independently reviews the prior
trusted execution chain and accepts one exact manifest digest operationally.
The authenticated Supabase principal is recorded as the **provisioner**, not as
the person who independently reviewed that historical execution.

## Operator sequence

The sole runtime owner performs these steps after independent code approval.
No record has been issued by implementing or testing this feature.

1. The source owner prepares one private manifest from the independently
   accepted historical execution and fresh exact source/native readback. The
   coordinator explicitly accepts that manifest's SHA256. Evidence-class
   acceptance alone does not authorize provisioning an unspecified manifest.
2. Stop on any source discrepancy. Preserve a backend DB backup and before
   proof of the exact originals. Use the same backend DB file and native Messages
   file that were read back; file path/device/inode hashes bind this store.
3. Set a distinct `PCO_CORRECTION_LINEAGE_KEY_PATH` to an owner-private absolute
   regular file containing at least 32 random bytes. It is separate from the
   frequency review key. Never commit it, the manifest, bearer token, DB, native
   configuration or private evidence. Symlink/public/non-owner key files fail.
4. Run the private tool in check-only mode, using the actual selected Mac config
   and a current Supabase bearer in `PCO_LINEAGE_REVIEW_BEARER`:

   ```sh
   python -m scripts.pco_provision_correction_lineage \
     --manifest "$PRIVATE_MANIFEST" \
     --accepted-manifest-sha256 "$ACCEPTED_SHA256" \
     --mac-config "$PRIVATE_MAC_CONFIG"
   ```

   Manifest and config must also be private absolute regular files owned by the
   backend user. The tool gets the current principal from `/auth/v1/user`,
   checks confirmed coordinator access, locks the committed source, and reads
   exactly the native row selected by the manifest. It checks GUID, body,
   fingerprint, selected sender/receiving line/service and the matching sole
   direct-chat handle. It never polls Messages, starts a session, calls Gloo or
   Planning Center, changes preferences or texts anyone.
   The selected backend must be an existing file SQLite database. Missing,
   memory, non-SQLite and query-parameter targets fail before authentication;
   SQLite `mode=rw` prevents creating a file if it disappears before connection.
   Check-only acquires the source reader's reservation, with no source DML/DDL.
5. Only after the exact digest acceptance and successful check, add `--apply`.
   This inserts one signed Policy in a separate committed transaction. An
   existing record fails instead of being overwritten. Save the returned
   Policy key/document hash/accepted manifest hash and verify originals unchanged
   in after proof. Check-only never signs or writes a record.
6. Set `PCO_CORRECTION_LINEAGE_ENABLED=true` only for the reviewed held-preview
   deployment; its default is false. Existing `PCO_REVIEW_ENABLED`, complete
   explicitly migrated schema, private membership bindings and authenticated
   review requirements still apply. The UI request/response structure stays
   unchanged. The response includes `audited_local_correction_preview_only` in
   release holds. Identical native preference yields a noop, not ownership.

## Exact manifest

Only `{schema:1,binding:{...},audit:{...}}` is accepted. The file is the UTF-8
canonical encoding used by `_json`: sorted keys, compact separators, no trailing
newline, no NaN. Both file SHA256 and canonical SHA256 must match the explicitly
accepted digest. No actor, permission flags, artifact paths, native payloads or
message text are accepted as additional fields.

`binding` contains exactly:

- `database_identity_hash`, `source_id`, `volunteer_id`, `organization_id`,
  `person_id`, `message_id`, `guid`, `fingerprint`, `receipt_result_hash`,
  `original_intent`, `corrected_profile_key`, `profile_hash`,
  `source_snapshot_hash`, `correction_route`, `session_id`;
- `previous_profile_key`, `previous_profile_hash`, `previous_profile_state`;
  the older row must belong to the same source/GUID/phone, have the original
  route and matching canonical key/profile digest, and precede the current row.
  Its exact pending/failed/synced/held state is preserved. A held **current**
  row remains unsupported;
- `native`: `database_identity_hash`, `row_id`, `guid`, `service`, `body_hash`,
  `sender_hash`, `receiving_line_hash`. Value hashes use `_hash` (SHA256 of
  canonical JSON), whereas the original receipt fingerprint retains its
  existing NUL-separated string formula.

`audit` contains exactly `saved_context_hash`, `producer_commit` (40 lowercase
hex characters), `producer_script_sha256`, `audit_sha256`, `gloo_output_hash`,
`completed_at` (timezone-aware ISO time), `publication_proof_sha256`. Hashes are
64 lowercase hex characters. Completion must follow the corrected queue time
and precede provisioning. These are accepted artifact bindings, not signatures
from the provider or automatic proof of publication permission.

The signed document records the fixed action/domain/evidence class, exact
binding/audit/accepted manifest digest, internal acceptance method/basis/reason,
and the independently authenticated provisioner/confirmation/provision time.
Its Policy key is `profile_fix:` plus the binding hash, 76 characters. Signed
flags always say `provider_signature_verified:false` and
`execution_allowed:false`.

## Invalidation and execution boundary

Only explicitly opted-in preview/review readers consume this record. Every
existing same-store, approved-recipient, active-consent, unique sender/session,
exact-current-profile, mapping, receipt and latest-revision guard remains.
Source/audit/binding/signature/key/action/actor-allowlist changes, revocation,
duplicate matching records or uncommitted source writes hold the read. A copied
DB or moved native evidence cannot silently inherit another store's authority.
The record is tied to one immutable historical revision rather than a renewable
session; no expiry/session extension is manufactured. To revoke it, the sole
owner can set that exact Policy envelope state to `revoked` or withdraw its key.

Tagged sources cannot enter the frequency executor, even with low-level forged
ownership/release inputs. Existing execution authorization still refuses live
release. Native notification silence, edit coordination, native writes,
scheduling, delivery and external data permissions receive no authority from
this record.
