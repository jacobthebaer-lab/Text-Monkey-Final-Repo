# Exact pre-send rejection recovery

This operator path applies only to an individually human-approved Google text
rejected during recipient preparation with `recipient_choice_wait_unavailable`.
It cannot recover an initial invitation, uncertain submission, submitted text,
native ledger entry, another failure reason or another recovery successor.

The verified superadmin must inspect the original private `/prepare` rejection
receipt and attest that exact historical reason. Save its SHA-256 privately.
With the existing sender and session unchanged and ordinary delivery requirements
still met, call:

`POST /api/cloud-texting/demo/presend-review/<original message ID>`

```json
{
  "content_hash": "<original approved content hash>",
  "reason_code": "recipient_choice_wait_unavailable",
  "prepare_receipt_sha256": "<private original rejection receipt SHA-256>",
  "confirmed": true
}
```

The backend checks the original rejected message, durable claim, exact human
decision, unchanged Gloo composition and original inbound/conversation evidence.
The private connector serializes a fresh same-key ledger-absence observation,
rejects any pending preparation or unknown submission, verifies the sender and
permanently disables the original key before issuing its durable absence proof.
It does not prepare a message or click Send. The receipt's historical reason is
explicitly an operator attestation, not reconstructed native evidence.

The response supplies one new pending approval and unchanged content hash. Review
and approve it through the ordinary proposal route. Automatic signup cannot
approve or recompose it. The original rejected message, approval, claim, body,
timestamps and conversation reservation remain intact; the one newly reviewed
successor has a distinct reservation and message key. Repeating the staging call
returns the same pending review, never another message or model request.

Then perform the existing fresh-inbox and exact single-message dispatch steps.
This operation does not enable signup, unpause delivery or waive any other gate.
The one-time quiet grant still expires at 2026-10-06T05:00:00Z and caps native
submission; this path cannot extend it or the original approved payload expiry.
If that deadline or any other proof is invalid, hold. No database reset or native
retry of the original key is supported.
