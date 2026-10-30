# Access control and private invitations

`OWNER_ADDRESS` identifies the owner. An explicitly configured `OWNER_BACKUP_ADDRESS` is also an administrator; no guest becomes an administrator by joining. The safe workflow defaults are `OWNER_ONLY=1` and `INVITE_AUTO_ACCESS=0`. A missing owner fails closed in owner-only, maintenance and invitation modes.

## Optional invitation mode

An operator can set `OWNER_ONLY=0` and `INVITE_AUTO_ACCESS=1` to admit ordinary users automatically after a successful, directly verified SecureJoin in an encrypted one-to-one chat. The address and public-key fingerprint are checked and the grant is bound to the chat and a per-grant token. When encrypted backup is configured as required (as in the supplied Actions workflow), its checkpoint must succeed before local access is activated. The bot sends one WebXDC mini app and a separate welcome in the other language; the newcomer need not type `/app`. Older allow-list entries do not become grants.

**An invitation is a bearer capability.** Anyone who receives it may give it to someone else. Share it only through trusted private channels; do not publish the invitation, account address, QR, confirmation codes or state backups. Successful automatic verification depends on the invitation remaining valid; an expired verification token may require a fresh private invitation. Email and the mail provider can delay delivery.

The owner or configured backup administrator can close access immediately in **Administration → Access control · OWNER_ONLY**. Closing stops new guest work and cooperatively pauses eligible running work without deleting data. Opening requires confirmation; it can be temporary, until restart, or until explicitly closed. The persistent choice requires `OWNER_ONLY=0` and a successful checkpoint. A message or provider request already handed off can still finish. If the checkpoint fails, the change is locally closed but an older remote backup might be restored after a crash; fix and verify the backup before relying on the closure across restart.

A simple revocation invalidates the current grant; the person may join again with a valid invitation. An explicit block prevents rejoining until an administrator unblocks them, after which a new SecureJoin is still required. A guest whose key changes must verify again through SecureJoin: a new grant invalidates the former binding. Existing chat-bound jobs and local WebXDC data are preserved but are not automatically migrated or reactivated under the new grant.

`MAINTENANCE_MODE=1` takes precedence and allows only the configured administrators; it cannot be disabled from the mini app. The separate operational-maintenance notice pauses work while preserving grants, notifies eligible current users, and is included in a new user's welcome. Eligible queues can resume afterward, subject to preserved bytes and any manual pause.

## Individual access mode

With `INVITE_AUTO_ACCESS=0`, opening the legacy controlled session does **not** automatically authorize guests. The owner can use **Administration → Approve one person**, or open a temporary session and approve specific addresses. Those session grants live only in memory; restart restores the configured owner-only policy. Historical grants do not revive automatically.

`/owner_only` reports the effective mode, `/owner_only 1` closes it, and `/owner_only 0 15` requests a 15-minute opening. The confirmation code is single-use and expires in five minutes. These controls are reserved to administrators; never share a confirmation code or owner session.

## Key recovery and limitations

Optional email-key recovery deliberately weakens identity authentication: an allowed email address may bind a new encrypted key without proving the old key. **An impersonator of an administrator address may gain its privileges.** In invitation mode this weaker recovery applies only to the configured administrators; guests must complete a new verified SecureJoin. The policy is separately opted in. Prior key bindings, backups and instance-local data are not silently deleted.

Notes, saved links, settings, jobs and temporary workspaces are scoped by person, but providers receive submitted requests and their own terms and limits still apply. The host is temporary, not a permanent cloud drive: queued partials and large attachments may be missing after a runner change. A successful local queueing or encrypted checkpoint is not proof that a recipient opened a message.

## Resumen en español

El valor seguro por defecto mantiene acceso solo al dueño. Si el operador habilita `INVITE_AUTO_ACCESS=1` junto a `OWNER_ONLY=0`, quien complete la invitación privada y su verificación cifrada entra automáticamente como usuario, sin aprobación individual. Comparte la invitación solo en privado: quien la recibe puede reenviarla. El dueño y el administrador de respaldo conservan los controles sensibles. El cierre de emergencia y los vínculos se respaldan; si falla el respaldo, el cierre local podría no sobrevivir a un reinicio. El mantenimiento prevalece y pausa las tareas sin borrar datos. Un invitado con clave nueva debe repetir SecureJoin; sus proyectos locales y trabajos anteriores no migran automáticamente. La recuperación de clave por correo de administradores es vulnerable a suplantación.
