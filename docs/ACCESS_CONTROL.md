# Session access control

`OWNER_ADDRESS` identifies the sole administrator. The production environment starts with `OWNER_ONLY=1`, so only the owner is admitted initially. `MAINTENANCE_MODE=1` always admits only the owner, takes precedence, and cannot be disabled from the mini app. Neither an old allow-list entry nor an old job grants access.

The owner can use **Administration → Access control · OWNER_ONLY** to request an in-memory opening for 15 minutes, 1 hour, 4 hours, 24 hours, or until restart. Opening requires confirmation and **does not authorize anyone automatically**: approve each address explicitly in the same screen. Closing or revoking blocks new work and pauses pending guest work without deleting historical data. A message already handed to the mail core just before closure may still complete.

`/owner_only` reports the effective mode, `/owner_only 1` closes it, and `/owner_only 0 15` requests a 15-minute opening. The confirmation code is single-use and expires in five minutes. Use `/admin_allow` and `/admin_remove` for specific addresses. Never share the code or the owner's session. These owner controls remain available during the kill switch; maintenance cannot be opened.

Overrides and guests exist **only in memory**. On restart, the configured `OWNER_ONLY` value and its prior boot policy apply; a configured `1` returns to owner-only. The app refreshes after expiry, subject to asynchronous email delays. Old guest jobs and WebXDC instances cannot resume on reauthorization; the guest needs a new app. If the mail core cannot unblock a contact, authorization fails closed.
