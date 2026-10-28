# Encrypted state backups and technical maintenance

## Storage and recovery

`state_backup.py` defaults to `STATE_BACKUP_STORAGE=releases`: each encrypted archive is a separate asset of a **private** GitHub release, not a new Git commit. A fresh salt and Fernet key derived from `STATE_BACKUP_PASSPHRASE` encrypt each archive. Never publish the passphrase, the GitHub token, or a decrypted archive. `STATE_GITHUB_TOKEN` must have Contents read/write on the private backup repository. The workflow still saves every 30 minutes and at shutdown.

The backup is downloaded independently, compared to the upload, decrypted, extracted into temporary storage, and its SQLite databases checked before it is marked usable. The first save creates an application-owned, state-free release anchor and a ready marker; neither is the default branch. After that marker exists, a missing release cannot silently fall back to a stale Git snapshot. If the newest snapshot is corrupt, recovery tries older marked snapshots; work after the recovered snapshot may be missing. Network errors fail closed. Existing local state is never overwritten.

Only after **two separately downloaded, verified** snapshots exist does rotation delete application-owned assets. It keeps up to 12 recent snapshots, one from each of the seven most recent days and one from each of four recent ISO weeks (at most 23 total; categories overlap). Failed pending uploads older than 48 hours can also be removed after that safety check. Unknown assets are not touched. A local lock serializes overlapping periodic and shutdown saves. No force push is used. If rotation fails, the new verified backup remains and later maintenance retries.

On the first deployment only, if the release and ready marker do not exist, restore migrates from the existing encrypted Git snapshot. The `main` Git snapshot remains as a frozen legacy recovery point and can be stale. **Do not delete the reserved release or tags or switch to `legacy-git` as a routine rollback.** Before reverting to old software, first use the current release-aware code to restore the newest verified snapshot to a safe local directory. Merely restarting old code can restore stale state.

Backups intentionally retain the previous limitation: attachment blobs above 200 KB are omitted, so this is **not a complete archive of every attachment**. Already missing attachments cannot be recreated by rotation. A backup larger than 90 MB is rejected and prior copies remain. If the final backup fails, automatic session handover is stopped; an ephemeral runner cannot guarantee preservation of work since the previous successful snapshot.

Removing reachable refs or release assets does not guarantee immediate physical erasure from GitHub's caches, retained objects, clones, or external copies. GitHub controls garbage collection. The private repository and release must remain private and the encryption passphrase must be preserved separately.

## Diagnostic log cleanup

The daily `Technical maintenance` workflow removes **only GitHub Actions log files** older than 30 days, after preserving the latest ten completed runs per workflow, with at most 100 log sets per pass. It does not delete workflow runs, check results, artifacts, caches, contacts, messages, keys, or ambiguous user history. If it cannot obtain a complete run listing, it deletes nothing. GitHub's own retention policies still apply and may remove data sooner. Runtime diagnostic buffers are already bounded and reset between sessions.

A future explicitly requested one-time user purge may leave older verified recovery snapshots under the above retention schedule. Successful verification of a sanitized snapshot is **not** proof that all older copies, caches, remote mail, or external copies were immediately erased.

GitHub Actions remains a temporary host with policy and availability risks; this maintenance does not turn it into a guaranteed permanent server or a complete disaster-recovery service.
