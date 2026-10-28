"""Create an end-to-end encrypted, short-lived test invitation, with no logging."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from deltachat2 import IOTransport, Rpc

# This is only a PUBLIC transfer key; its private half never leaves the
# operator's local temporary machine. A new one is used for each staging run.
PUBLIC_HEX = "834f75700039373f8a7dcb7ccfd6000c96ec8d959116e421bc8b46dbe9ea9247"
MAGIC = b"MICHIFLIN-STAGE-1\0"


def main() -> None:
    destination = Path(os.environ["RUNNER_TEMP"]) / "stage-invite.enc"
    accounts = str(Path(os.environ["CONFIG_DIR"]) / "accounts")
    transport = IOTransport(accounts_dir=accounts, stderr=subprocess.DEVNULL)
    transport.start()
    try:
        rpc = Rpc(transport)
        configured = [account for account in rpc.get_all_account_ids() if rpc.is_configured(account)]
        if len(configured) != 1:
            raise RuntimeError("staging account unavailable")
        invite = rpc.get_chat_securejoin_qr_code(configured[0], None).encode("utf-8")
        if not invite or len(invite) > 8192:
            raise RuntimeError("invalid invite")
        ephemeral = X25519PrivateKey.generate()
        recipient = X25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_HEX))
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=MAGIC).derive(ephemeral.exchange(recipient))
        nonce = os.urandom(12)
        public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ciphertext = AESGCM(key).encrypt(nonce, invite, MAGIC)
        destination.write_bytes(MAGIC + public + nonce + ciphertext)
        destination.chmod(0o600)
    finally:
        transport.close()


if __name__ == "__main__":
    # Errors are generic: neither addresses nor invite contents enter CI logs.
    try:
        main()
    except Exception:
        raise SystemExit("Unable to prepare encrypted staging invitation") from None
