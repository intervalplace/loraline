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
import json
from pathlib import Path

try:
    from nacl.bindings import (
        crypto_aead_chacha20poly1305_ietf_decrypt as _dec,
        crypto_aead_chacha20poly1305_ietf_encrypt as _enc,
    )
    from nacl.public import Box, PrivateKey, PublicKey
    from nacl.signing import SigningKey, VerifyKey
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

NONCE_BYTES = 12
TAG_BYTES = 16
GROUP = "*"
DEFAULT_PATH = Path.home() / ".loraline" / "identity"
DEFAULT_PEERS = Path.home() / ".loraline" / "peers.json"


def address_of(public_bytes: bytes, verify_bytes: bytes = b"") -> str:
    """Six hex characters, derived from BOTH halves of an identity.

    Binding the address to the signing key as well as the encryption key is
    what lets a stranger check a signature: given the two public keys they can
    recompute the address themselves, so nobody can pair a real encryption key
    with a signing key they invented.
    """
    return hashlib.blake2b(public_bytes + verify_bytes, digest_size=3).hexdigest()


def fingerprint(public_bytes: bytes) -> str:
    """Longer form, for reading aloud to check who you are actually talking to."""
    digest = hashlib.blake2b(public_bytes, digest_size=8).hexdigest()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


class Identity:
    """One stored secret, two keypairs.

    X25519 for talking privately, Ed25519 for signing what happened. Derived
    separately rather than reusing one key for both jobs, which is the sort of
    shortcut that looks free and is not.
    """

    def __init__(self, private_bytes: bytes | None = None) -> None:
        if not AVAILABLE:
            self.key = self.signing = None
            self.verify_bytes = b""
            self.public_bytes = hashlib.blake2b(
                private_bytes or os.urandom(32), digest_size=32
            ).digest()
        else:
            self.key = (PrivateKey(private_bytes) if private_bytes
                        else PrivateKey.generate())
            self.public_bytes = bytes(self.key.public_key)
            seed = hashlib.blake2b(bytes(self.key), person=b"loraline-sign",
                                   digest_size=32).digest()
            self.signing = SigningKey(seed)
            self.verify_bytes = bytes(self.signing.verify_key)
        self.address = address_of(self.public_bytes, self.verify_bytes)

    @property
    def verify_b64(self) -> str:
        return base64.b64encode(self.verify_bytes or b"").decode("ascii")

    def sign(self, message: bytes) -> str:
        """A detached signature, base64. Raises without PyNaCl, because an
        unsigned record claiming to be signed is worse than none."""
        if not AVAILABLE:
            raise RuntimeError("signing needs PyNaCl: pip install pynacl")
        return base64.b64encode(self.signing.sign(message).signature).decode("ascii")

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

    def __init__(self, identity: Identity, passphrase: str | None = None,
                 keystore=None) -> None:
        self.identity = identity
        self.group = GroupCipher(passphrase) if (passphrase and AVAILABLE) else None
        self.boxes: dict[str, object] = {}
        self.peer_keys: dict[str, bytes] = {}
        self.verifiers: dict[str, bytes] = {}  # address -> signing key
        self.nicks: dict[str, str] = {}       # address -> last known nick
        self.failures = 0
        self.keystore = Path(keystore) if keystore is not None else None
        if self.keystore is not None:
            self._load_keystore()

    def _load_keystore(self) -> None:
        """Restore peer keys learned in earlier sessions.

        Without this, every restart forgets who everyone is: peers show as raw
        addresses and direct messages are unavailable until a fresh hello
        arrives. Persisting the keys means a known peer is recognised, and
        named, the instant they first speak again.
        """
        if not AVAILABLE or not self.keystore.exists():
            return
        try:
            data = json.loads(self.keystore.read_text())
        except Exception:
            return
        for address, rec in data.items():
            try:
                raw = base64.b64decode(rec["key"], validate=True)
            except Exception:
                continue
            try:
                vraw = (base64.b64decode(rec.get("verify", ""), validate=True)
                        if rec.get("verify") else b"")
            except Exception:
                continue
            if len(raw) != 32 or address_of(raw, vraw) != address:
                continue          # never trust a stored key that fails its own address
            self.peer_keys[address] = raw
            self.boxes[address] = Box(self.identity.key, PublicKey(raw))
            if vraw:
                self.verifiers[address] = vraw
            if rec.get("nick"):
                self.nicks[address] = rec["nick"]

    def _save_keystore(self) -> None:
        if self.keystore is None:
            return
        data = {addr: {"key": base64.b64encode(raw).decode("ascii"),
                       "verify": base64.b64encode(
                           self.verifiers.get(addr, b"")).decode("ascii"),
                       "nick": self.nicks.get(addr, "")}
                for addr, raw in self.peer_keys.items()}
        try:
            self.keystore.parent.mkdir(parents=True, exist_ok=True)
            self.keystore.write_text(json.dumps(data))
        except Exception:
            pass          # persistence is best-effort; never break a session over it

    def remember_nick(self, address: str, nick: str) -> None:
        """Record a peer's nick so it survives a restart, saving if it changed."""
        if not nick or self.nicks.get(address) == nick:
            return
        if address in self.peer_keys:
            self.nicks[address] = nick
            self._save_keystore()

    @property
    def overhead(self) -> int:
        return NONCE_BYTES + TAG_BYTES if AVAILABLE else 0

    def learn(self, address: str, public_b64: str,
              verify_b64: str = "") -> bool:
        """Register a peer's public keys. Returns True if they were new.

        Both halves are checked against the address together, so a real
        encryption key cannot be paired with an invented signing key.
        """
        if not AVAILABLE:
            return False
        try:
            raw = base64.b64decode(public_b64, validate=True)
            vraw = (base64.b64decode(verify_b64, validate=True)
                    if verify_b64 else b"")
        except Exception:
            return False
        if len(raw) != 32 or address_of(raw, vraw) != address:
            return False
        if vraw:
            self.verifiers[address] = vraw
        if self.peer_keys.get(address) == raw:
            self._save_keystore()
            return False
        self.peer_keys[address] = raw
        self.boxes[address] = Box(self.identity.key, PublicKey(raw))
        self._save_keystore()
        return True

    def verify(self, address: str, message: bytes, signature_b64: str) -> bool:
        """True only if this exact address signed these exact bytes."""
        raw = self.verifiers.get(address)
        if not AVAILABLE or raw is None:
            return False
        try:
            VerifyKey(raw).verify(message,
                                  base64.b64decode(signature_b64, validate=True))
            return True
        except Exception:
            return False

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
