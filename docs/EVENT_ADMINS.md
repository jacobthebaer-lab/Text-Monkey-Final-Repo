# Event recipients and pre-event updates

In Settings, review an existing consenting contact and choose **Save as an additional event admin**. This records an operator attestation without replacing the saved primary, creating a new volunteer, sending a text, or granting coordinator commands. Existing coordinators retain their existing permissions and ministry staffing subscriptions. Ordinary volunteers remain eligible for volunteer planning and replies.

In Shifts, each upcoming event can inherit the saved primary, select one or more saved event recipients, or select nobody to disable that event's pre-event texts. Choices persist independently per event. Existing and new events inherit the current saved primary until explicitly changed. A missing, paused, opted-out or ambiguous primary holds delivery instead of choosing another coordinator. Multiple account owners require an explicit event choice. Another owner's routing configuration is read-only; shared event staffing remains visible.

The existing three-hour deduplication key is preserved. Updates include all recorded canceled names and current filled spots with role names, plus staffing coverage and openings. A later assignment can establish a filled spot when the same slot was vacated, or an actual saved closed fill request proves it. The text does not infer who replaced whom. Initial assignments and partial coverage remain distinct in the portal.

Gloo must compose every update. Pre-event copy may use up to 1,600 characters; ordinary signup composition keeps its 600-character limit. A complete list that exceeds 1,600 characters is held with an actionable explanation and the full list available in Shifts. Names are never silently dropped. There is no template fallback.

Saving recipients never sends texts. Consent, opt-outs, eligibility, quiet hours and configured exact review remain enforced. Event, route, recipient, consent and assignment history are checked again before review acceptance and native delivery. Changes invalidate known-unsent reviews without changing their original body or hash. Claimed or uncertain delivery is never automatically replayed. Event-only contacts can receive only their current source-bound event update, not arbitrary administrator notifications.

Tests and the isolated browser fixture use fictional data and Mock or fake-Mac queues. These establish behavior, not actual native delivery or live deployment.
