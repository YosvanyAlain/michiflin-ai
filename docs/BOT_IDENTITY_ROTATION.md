# Bot identity rotation

## Status and boundaries

The owner verified a separately created bot account and public keys, and the production cutover now uses that fresh identity. Its address and invitation are not published here. The new account does not forward mail from the old address; an old invitation still refers to the old mailbox, not the active bot. That mailbox may remain reachable at its relay until the relay removes or deactivates it. Neither changing an invitation nor deleting a local reference guarantees immediate deletion on relays, other devices, GitHub caches or unreachable Git objects.

Production remains `OWNER_ONLY=1` with the directly verified owner key, a fresh strict epoch and a private anti-downgrade seal. The seal must remain intact. The owner can later authorize specific guests without reactivating old grants. Old queued jobs remain stored under their old bindings and are not automatically sent to the new chat; local storage inside older WebXDC instances does not automatically migrate.

## Recovery without reviving the retired account

The full old encrypted snapshot was initially retained to validate the cutover. A separate, independently tested archive can preserve owner and ambiguous messages, available attachments, WebXDC updates and app state **without importing the old account, its database or its keys**. See [RECOVER_OWNER_HISTORY.md](RECOVER_OWNER_HISTORY.md). Missing attachments and private on-device WebXDC storage cannot be recreated merely by saving an archive.

Removal of old backups or a force update of private backup history is a separate, exceptional owner-authorized operation, not routine maintenance. It must verify the new account, its profile, its current recoverable backup, the seal, all still-available old sources and the downloaded sanitized owner archive first. Unknown branches, tags, identities or data stop the operation. It does not authorize rewriting the public source repository. After old account copies are removed, do not promise that the old account can be restored.

## For a future, separately authorized rotation

1. Confirm the destination relay and prepare a distinct account in isolation. Never publish a private invitation, credential or key.
2. Verify the owner's and bot's fingerprints through a separate trusted channel and test encrypted messaging. Keep the production bot owner-only.
3. Preserve and independently restore the existing state before making changes; do not run two production instances simultaneously.
4. Cut over only once, retain an encrypted recovery copy until the owner verifies the new identity, and seal against silent downgrade. Preserve queued work and report what cannot migrate.
5. Copy only safe profile text when requested; old addresses and invitations must not be advertised as the new identity.
6. Archive the owner's recoverable data independently of the old identity before any separately authorized retirement of old account backups.

Rollback to an old account, if a usable copy exists, is a separate deliberate decision that would reactivate the old identity; it must never happen automatically or bypass the seal.
