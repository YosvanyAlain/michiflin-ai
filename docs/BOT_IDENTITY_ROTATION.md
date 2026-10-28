# Bot identity rotation

## Purpose and status

The goal of a future rotation would be to **stop operating the old identity**, so an old invitation no longer reaches the active bot. It would not forward mail, redirect messages, or retain the old address as an alias. `OWNER_ADDRESS` is an authorization rule, not the bot's Delta Chat account.

No rotation has been performed as part of this plan. No external account was created, no key changed, and the active identity was not modified. Rotation requires explicit owner authorization because it changes the account that sends and receives messages.

## What a new address changes

- A new account, even with the same provider when a second account is allowed, creates a separate identity and invitation.
- The old invitation still points to the old address; it does not redirect to the new one.
- To keep the old invitation from reaching an operational bot, stop the process using the old identity and, where the provider permits it, deactivate or abandon that account. While the old mailbox remains active, someone may still initiate a chat with it even if no bot answers.
- Keep the new bot owner-only while it is intended for personal use. Code can reject and block unknown contacts, but stopping the old identity is the definitive control over the old invitation.
- Update `OWNER_ADDRESS` separately and only to the legitimate owner's address. Changing it does not invalidate an invitation by itself.

## Reversible preparation

1. Confirm the current account and relay and the exact process for creating another account with the selected provider; do not infer compatibility from a similar name.
2. Make and verify an encrypted backup of the Delta Chat configuration and state. Never publish private keys or tokens in the repository or reports.
3. Prepare the new identity in an isolated test environment without putting it into service. Verify reception, delivery, WebXDC, and backup recovery.
4. Verify owner-only access and the expected handling of other senders.
5. Only after explicit authorization, stop the old bot, activate the new identity, share its invitation privately with the owner, and leave the old invitation without an active bot behind it.
6. Verify that no old instance is still connected. Do not forward or redirect the old account. Retain backups until observation is complete and the new account's recovery is confirmed.

## Rollback

If the new identity cannot send or receive, or the owner loses access, stop it and restore the prior backup and configuration. This may reactivate the old identity, so rollback must be a conscious decision rather than an automatic redirect.

## Pending information

The exact current identity and relay, the provider for the new identity, and authorization to touch the active account must be confirmed before a rotation. Until then, the safe action is to keep the plan only and leave the active account unchanged.
