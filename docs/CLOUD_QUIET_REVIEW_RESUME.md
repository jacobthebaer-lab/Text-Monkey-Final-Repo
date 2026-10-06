# Resume one reviewed quiet-hours hold

A review approved during quiet hours can expire without creating an outgoing
message. A verified superadmin can request one linked successor review at
`POST /api/cloud-texting/demo/quiet-review/{approval_id}` with the original
`content_hash`. During normal quiet hours, an applicable current one-time quiet
exception is required. This action only stages a pending review. Approve its exact
text separately through the existing proposal-review endpoint.

The successor preserves the original payload, body, hash, source input and fixed
expiry. The original approved/blocked audit and Gloo proof remain dependencies at
review and delivery. No Gloo call, input replay, outgoing message or delivery claim
is created by staging. Repeated requests return the same pending successor; a
decided or expired successor cannot be replaced or automatically recomposed.

This narrow path covers sender-bound signup replies and configured admin texts.
It requires a proven `inside quiet hours` hold, no prior outgoing-message link,
current consent/session/source restrictions and no uncertain submission. Initial
invitations, event reviews and genuinely expired, changed or revoked evidence
retain their existing fresh-proposal paths. Normal quiet-hour settings remain
unchanged. Continuous signup cannot authorize this successor automatically.
