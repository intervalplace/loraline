"""Identity, group key and pairwise encryption.

Two kinds of secrecy, because the medium needs both:

  group   anyone with the shared passphrase can read it. Derived by scrypt
          from the phrase you and your friends agreed on.
  direct  a pairwise box between exactly two identities, using X25519. The
          third person in the group cannot read it, even though their radio
          received every byte.

That second one is the whole reason identities exist. On a broadcast bus, a
private chat implemented by having clients politely ignore what isn't theirs
is not private at all: anyone with a patched client or a serial terminal reads
it.

PyNaCl stays a soft dependency. Without it the client still runs, says so
loudly, and direct messages are directed but not confidential.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

try:
    from nacl.bindings import (
        crypto_aead_chacha20poly1305_ietf_decrypt as _dec,
        crypto_aead_chacha20poly1305_ietf_encrypt as _enc,
    )
    from nacl.public import Box, PrivateKey, PublicKey
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

NONCE_BYTES = 12
TAG_BYTES = 16
GROUP = "*"
DEFAULT_PATH = Path.home() / ".loraline" / "identity"


def address_of(public_bytes: bytes) -> str:
    """Six hex characters derived from a public key. Short enough for a header."""
    return hashlib.blake2b(public_bytes, digest_size=3).hexdigest()


def fingerprint(public_bytes: bytes) -> str:
    """Longer form, for reading aloud to check who you are actually talking to."""
    digest = hashlib.blake2b(public_bytes, digest_size=8).hexdigest()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


class Identity:
    def __init__(self, private_bytes: bytes | None = None) -> None:
        if not AVAILABLE:
            self.key = None
            self.public_bytes = hashlib.blake2b(
                private_bytes or os.urandom(32), digest_size=32
            ).digest()
        elif private_bytes:
            self.key = PrivateKey(private_bytes)
            self.public_bytes = bytes(self.key.public_key)
        else:
            self.key = PrivateKey.generate()
            self.public_bytes = bytes(self.key.public_key)
        self.address = address_of(self.public_bytes)

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_bytes).decode("ascii")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_bytes)

    @classmethod
    def load_or_create(cls, path=DEFAULT_PATH) -> "Identity":
        """Persist the keypair so your address survives a restart."""
        path = Path(path)
        if path.exists():
            raw = base64.b64decode(path.read_text().strip())
            return cls(raw)
        identity = cls()
        secret = bytes(identity.key) if AVAILABLE else identity.public_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.b64encode(secret).decode("ascii"))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return identity


class GroupCipher:
    """Symmetric AEAD shared by everyone who knows the passphrase."""

    overhead = NONCE_BYTES + TAG_BYTES

    def __init__(self, passphrase: str, salt: bytes = b"loraline-group-v2") -> None:
        self.key = hashlib.scrypt(
            passphrase.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32
        )

    def seal(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + _enc(plaintext, b"", nonce, self.key)

    def open(self, blob: bytes) -> bytes | None:
        try:
            if len(blob) <= NONCE_BYTES:
                return None
            return _dec(blob[NONCE_BYTES:], b"", blob[:NONCE_BYTES], self.key)
        except Exception:
            return None


class Keyring:
    """The group key plus one box per known peer.

    Decryption tries every key it holds. With a handful of peers that costs
    nothing, and it means no routing information has to travel in the clear:
    an outside listener cannot tell who a packet is even addressed to.
    """

    def __init__(self, identity: Identity, passphrase: str | None = None) -> None:
        self.identity = identity
        self.group = GroupCipher(passphrase) if (passphrase and AVAILABLE) else None
        self.boxes: dict[str, object] = {}
        self.peer_keys: dict[str, bytes] = {}
        self.failures = 0

    @property
    def overhead(self) -> int:
        return NONCE_BYTES + TAG_BYTES if AVAILABLE else 0

    def learn(self, address: str, public_b64: str) -> bool:
        """Register a peer's public key. Returns True if it was new."""
        if not AVAILABLE:
            return False
        try:
            raw = base64.b64decode(public_b64, validate=True)
        except Exception:
            return False
        # The address is derived from the key, so a mismatch means the sender
        # is claiming an address that is not theirs.
        if len(raw) != 32 or address_of(raw) != address:
            return False
        if self.peer_keys.get(address) == raw:
            return False
        self.peer_keys[address] = raw
        self.boxes[address] = Box(self.identity.key, PublicKey(raw))
        return True

    def knows(self, address: str) -> bool:
        return address in self.boxes

    def seal(self, plaintext: bytes, dst: str) -> bytes | None:
        """Encrypt for `dst`, or None if it has to go out in the clear."""
        if not AVAILABLE:
            return None
        if dst != GROUP and dst in self.boxes:
            return base64.b64encode(self.boxes[dst].encrypt(plaintext))
        if self.group is not None:
            return base64.b64encode(self.group.seal(plaintext))
        return None

    def open(self, envelope: bytes) -> bytes | None:
        try:
            blob = base64.b64decode(envelope, validate=True)
        except Exception:
            self.failures += 1
            return None
        if self.group is not None:
            out = self.group.open(blob)
            if out is not None:
                return out
        for box in self.boxes.values():
            try:
                return box.decrypt(blob)
            except Exception:
                continue
        self.failures += 1
        return None

    def can_encrypt_to(self, dst: str) -> bool:
        if not AVAILABLE:
            return False
        return self.group is not None if dst == GROUP else dst in self.boxes


def warnings_for(passphrase: str | None) -> list[str]:
    if not AVAILABLE:
        return [
            "PyNaCl is not installed: nothing is encrypted, and direct messages "
            "are directed but readable by anyone in range. pip install pynacl"
        ]
    if not passphrase:
        return [
            "No group key: group messages go out in the clear. Direct messages "
            "are still end-to-end encrypted. Set --key or LORALINE_KEY."
        ]
    return []
