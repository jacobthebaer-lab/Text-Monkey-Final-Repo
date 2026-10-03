# Fictional contact import demo

Use `data/demo-import/contacts-clean.csv` and `data/demo-import/contacts-needs-fixes.csv`. Both contain the same three fictional contact identities, reserved example emails and fictional 202-555-01xx phones. Nothing in this package sends a text or calls Gloo. Do not substitute personal contact exports for this demo.

## What is actually imported

| CSV heading | Map to | Stored meaning |
| --- | --- | --- |
| Full name | Name | Contact display name |
| Mobile | Phone | Normalized phone text; ownership/delivery unverified |
| Email | Email | Contact email text |
| Team | Ministry | Unverified free-text note |

The Team cells include example role interests and availability **only as ministry notes**. The importer does not parse these into interests, scheduling availability, qualifications, role assignments or coordinator clearance. It imports no consent, permission to text, active volunteer status or schedule. Staged contacts remain separate from the live roster. A separate reviewed workflow is required to connect them to that roster.

## Presenter walkthrough

1. Open the existing live demo at <https://text-monkey-demo.pages.dev/>. Open **Set up your church**, save a fictional church draft, then choose **Import contacts**. The public preview stores this exercise only in that browser; it cannot prove server account isolation or real delivery.
2. Choose `contacts-needs-fixes.csv`. Match the four columns above, choose United States and enter source `Fictional Text Monkey demo package`. Select **Preview contacts**. Expect **0 ready, 0 duplicates, 3 rows to fix**. Saving is disabled. Download the row report to show the actionable errors below. No contacts were staged and no texting permission was granted.
3. Choose `contacts-clean.csv`, use the same mapping/source and preview. In an empty staging area expect **3 ready, 0 duplicates, 0 invalid**. Select **Save 3 staged contacts**. Show **Awaiting consent**, **Staged only · cannot text**, and **Outreach blocked**. The receipt reports zero texts sent.
4. Preview the clean file again. Expect **0 ready, 3 duplicates, 0 invalid**. Saving is disabled; existing names, notes and consent are preserved. Remove only these three fictional staged contacts if resetting the exercise.

| Invalid CSV row | Error | Correction already demonstrated by clean CSV |
| --- | --- | --- |
| 2 | Provide a name of 1–160 characters. | Add Alex Sample. |
| 3 | Check the email address or leave it blank. | Use casey@example.test, or clear the email cell. |
| 4 | Replace formulas/errors with plain text values. | Use Morgan's plain ministry note. |

On a nonempty account, existing fixture numbers count as duplicates; remove only this package's staged records or use a fresh fictional workspace before expecting the clean-file counts above. Validation precedes duplicate detection, so the invalid file still reports three invalid rows.

## Connected account-isolation check

Use two existing authorized test admin accounts in separate browser profiles against the connected portal. This is an optional connected demonstration; creating accounts, changing allowlists, enabling scheduling or texting is not part of the package.

1. Account A saves its church draft and stages the clean file. Record the three fictional staged contact IDs. Account A sees exactly its own staging records, not necessarily an empty live scheduling dashboard: that dashboard remains the existing single church.
2. Account B opens setup before saving anything. Its staged contact list is empty and its unsaved setup revision is zero. Account B cannot remove an Account A contact: the authenticated API returns 404 for that ID. Neither frontend-supplied owner nor workspace IDs determine ownership.
3. Account B saves its own fictional church draft. Preview the same clean file: **3 ready**, because duplicate checks are workspace-scoped. Account B may independently stage it; Account A's records stay unchanged.
4. Return to Account A and verify its saved church and three contacts remain intact. All imported records on both accounts remain `consent: not_recorded`, `status: staged`, `can_text: false`; the import and contact-list receipts report `texts_sent: 0`.

Without verified login, setup parsing/preview/import endpoints return 401. With no saved workspace, preview returns 409 asking to save setup. A zero-ready import (all invalid or all duplicates) is rejected with 422, **There are no valid new contacts to save**; it does not return a successful zero-contact import receipt. The public browser preview has no account identity and cannot establish any of these server authorization outcomes.

## Repeatable verification

From the repository root, run:

```sh
python -m pytest tests/test_demo_import_package.py
node --test web/texty/tests/demo-import-package.test.js
```

These checks parse the actual CSV bytes, compare backend/public-preview results, stage with synthetic verified identities in an in-memory test database, verify duplicate and retry behavior, reject all-invalid imports, and check empty Account B staging and cross-account removal denial. Live volunteer/message/outreach/assignment/approval tables remain empty. They use no real accounts, external API, Gloo call or Messages transport; they do not establish real delivery.
