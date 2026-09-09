"""Wire protocol, framing and airtime maths.

Standard library only, and deliberately so: this is the layer that has to keep
working on a machine that has been offline for a decade.

Frames are newline-terminated, fields separated by 0x1f (ASCII unit
separator, which never appears in typed text). The first character is the
frame type, which lets the reader resynchronise after line noise.

    H  hello      src, nick, colour, public key
    P  presence   src, status, nick, colour, personal message, acks
    M  message    src, dst, seq, ack, idx, cnt, text
    T  typing     src, dst
    X  signing off  src
    E  encrypted envelope, base64 of an inner frame

`src` and `dst` are six-hex-character addresses derived from a public key.
`dst` is "*" for the group. Both live inside the encrypted envelope, so an
outside listener cannot tell who is talking to whom.

There is no separate acknowledgement frame. Every M and P carries the highest
sequence number the sender has seen, so delivery confirmation rides along on
traffic that was going out anyway. On a link where airtime is the binding
constraint, a dedicated ack frame is close to pure waste.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum

US = "\x1f"
TERM = "\n"

# The module fragments above 240 bytes. Staying under keeps one frame equal to
# one transmission, which keeps the airtime accounting honest.
MAX_FRAME_BYTES = 200

SEQ_MODULO = 65536
NO_ACK = -1


class Status(str, Enum):
    ONLINE = "on"
    AWAY = "away"
    BUSY = "busy"
    BRB = "brb"
    OFFLINE = "off"

    @property
    def label(self) -> str:
        return {
            Status.ONLINE: "Online", Status.AWAY: "Away", Status.BUSY: "Busy",
            Status.BRB: "Be Right Back", Status.OFFLINE: "Offline",
        }[self]

    @property
    def glyph(self) -> str:
        return {
            Status.ONLINE: "\u25cf", Status.AWAY: "\u25d0", Status.BUSY: "\u25cf",
            Status.BRB: "\u25d0", Status.OFFLINE: "\u25cb",
        }[self]


class Delivery(str, Enum):
    QUEUED = "queued"        # waiting for airtime or for someone to reappear
    SENT = "sent"            # on the air, nobody has confirmed yet
    PARTIAL = "partial"      # some recipients confirmed, not all
    DELIVERED = "delivered"  # everyone it was addressed to confirmed
    FAILED = "failed"        # given up on

    @property
    def glyph(self) -> str:
        return {
            Delivery.QUEUED: "\u00b7", Delivery.SENT: "\u2713",
            Delivery.PARTIAL: "\u2713\u00b7", Delivery.DELIVERED: "\u2713\u2713",
            Delivery.FAILED: "\u2717",
        }[self]


# --------------------------------------------------------------------------
# Sequence numbers
# --------------------------------------------------------------------------

def seq_newer(a: int, b: int) -> bool:
    """True if a is newer than b, tolerating wraparound at SEQ_MODULO.

    Compares in the half-space: anything within half a modulus ahead counts as
    newer. Standard trick, same one TCP uses.
    """
    if b < 0:
        return a >= 0
    return ((a - b) % SEQ_MODULO) < (SEQ_MODULO // 2) and a != b


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------

@dataclass
class Frame:
    type: str
    fields: list[str] = field(default_factory=list)
    rssi_raw: int | None = None

    def encode(self) -> bytes:
        return (US.join([self.type, *self.fields]) + TERM).encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.encode())

    def field_int(self, index: int, default: int = NO_ACK) -> int:
        try:
            return int(self.fields[index])
        except (IndexError, ValueError):
            return default

    def field_str(self, index: int, default: str = "") -> str:
        try:
            return self.fields[index]
        except IndexError:
            return default


def hello(src: str, nick: str, colour: int, public_b64: str) -> Frame:
    return Frame("H", [src, nick, str(colour), public_b64])


def message(src: str, dst: str, seq: int, ack: int,
            idx: int, cnt: int, text: str) -> Frame:
    return Frame("M", [src, dst, str(seq), str(ack), str(idx), str(cnt), text])


def presence(src: str, status: Status, acks: dict[str, int],
             nick: str | None = None, colour: int | None = None,
             psm: str | None = None) -> Frame:
    """A heartbeat. Identity fields are optional and usually omitted.

    Presence is the most frequent thing on the air, so it is also the most
    expensive. Nick, colour and personal message change rarely, so carrying
    them every minute wastes airtime on a channel that has very little. The
    short form is roughly half the size; receivers keep the last values they
    were told.
    """
    fields = [src, status.value, encode_acks(acks)]
    if nick is not None:
        fields += [nick, str(colour or 0), psm or ""]
    return Frame("P", fields)


def typing(src: str, dst: str) -> Frame:
    return Frame("T", [src, dst])


def signoff(src: str) -> Frame:
    return Frame("X", [src])


def encode_acks(acks: dict[str, int]) -> str:
    """Per-peer high-water marks, e.g. "a1b2c3:14;d4e5f6:9"."""
    return ";".join(f"{addr}:{seq}" for addr, seq in sorted(acks.items()))


def decode_acks(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in text.split(";"):
        addr, _, seq = part.partition(":")
        if addr and seq.lstrip("-").isdigit():
            out[addr] = int(seq)
    return out


def sanitize(text: str) -> str:
    """Remove anything that would break framing or upset the radio.

    '+++' is the module's escape into AT command mode. Sending it mid
    conversation would silently take your own transmitter off the air.
    """
    for bad in (US, "\r", "\n"):
        text = text.replace(bad, " ")
    return text.replace("+++", "+ ++")


TYPE_CHARS = set("HMPTXE")


class LineReader:
    """Byte stream to complete envelope lines, with the RSSI byte peeled off.

    Deliberately stops short of decryption. A bridge node has to forward
    traffic it cannot read, since a direct message between two of its peers
    is opaque to it, so line framing and decoding are separate steps.

    Tolerates the stray RSSI byte the module appends after each received
    packet, which lands outside the newline framing, and resynchronises on
    garbage rather than wedging.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pending_rssi: int | None = None

    def feed(self, data: bytes) -> list[tuple[bytes, int | None]]:
        self._buf.extend(data)
        out: list[tuple[bytes, int | None]] = []

        while True:
            while self._buf and chr(self._buf[0]) not in TYPE_CHARS:
                stray = self._buf.pop(0)
                if stray not in (0x0A, 0x0D):
                    self._pending_rssi = stray

            idx = self._buf.find(b"\n")
            if idx == -1:
                if len(self._buf) > MAX_FRAME_BYTES * 4:
                    self._buf.clear()
                break

            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if line:
                out.append((line, self._pending_rssi))
            self._pending_rssi = None
        return out


def decode_line(line: bytes, keyring=None) -> Frame | None:
    """Turn one envelope line into a Frame, or None if we cannot read it."""
    if not line:
        return None
    if line[0:1] == b"E":
        if keyring is None:
            return None
        body = line[2:] if line[1:2] == US.encode() else line[1:]
        inner = keyring.open(body)
        if inner is None:
            return None
        line = inner

    parts = line.decode("utf-8", errors="replace").split(US)
    if not parts or parts[0] not in TYPE_CHARS or parts[0] == "E":
        return None
    return Frame(parts[0], parts[1:])


def seal(frame: Frame, keyring, dst: str) -> Frame:
    """Wrap a frame in an encrypted envelope, or return it unchanged."""
    if keyring is None:
        return frame
    blob = keyring.seal(frame.encode().rstrip(b"\n"), dst)
    if blob is None:
        return frame
    return Frame("E", [blob.decode("ascii")])


def payload_budget(keyring=None) -> int:
    """How many bytes of inner frame fit in one transmission.

    Sized for the encrypted case whenever encryption is possible at all, so a
    message composed while a peer key is unknown still fits once it arrives.
    """
    if keyring is None or keyring.overhead == 0:
        return MAX_FRAME_BYTES - 1
    b64_chars = MAX_FRAME_BYTES - 3
    ciphertext = (b64_chars // 4) * 3
    return max(32, ciphertext - keyring.overhead - 16)


def fragment(text: str, per_fragment: int) -> list[str]:
    """Split text into UTF-8-safe chunks that fit the payload budget."""
    data = text.encode("utf-8")
    if len(data) <= per_fragment:
        return [text]

    chunks, start = [], 0
    while start < len(data):
        end = min(start + per_fragment, len(data))
        while end > start and (data[end - 1] & 0xC0) == 0x80:
            end -= 1  # do not split a multi-byte character
        if end == start:
            end = min(start + per_fragment, len(data))
        chunks.append(data[start:end].decode("utf-8", errors="ignore"))
        start = end
    return chunks


# --------------------------------------------------------------------------
# Signal strength
# --------------------------------------------------------------------------

def rssi_dbm(raw: int | None) -> int | None:
    """Convert the module's raw RSSI byte to dBm.

    Waveshare do not document the encoding. The usual convention for these
    SX1262 DTUs is a two's-complement byte. Relative readings are reliable
    even if the absolute figure carries a fixed offset.
    """
    if raw is None:
        return None
    return raw - 256 if raw > 127 else -raw


def signal_bars(dbm: int | None) -> str:
    if dbm is None:
        return "----"
    for threshold, bars in ((-80, "||||"), (-100, "|||."), (-115, "||.."), (-125, "|...")):
        if dbm >= threshold:
            return bars
    return "...."


SPARK = " .:-=+*#"


def sparkline(values: list[int | None], width: int = 20) -> str:
    """Render recent RSSI history as a single line of characters."""
    recent = values[-width:]
    if not recent:
        return ""
    real = [v for v in recent if v is not None]
    if not real:
        return " " * len(recent)
    lo, hi = min(real), max(real)
    mid = len(SPARK) // 2
    out = []
    for v in recent:
        if v is None:
            out.append(" ")
        elif hi == lo:
            out.append(SPARK[mid])  # a flat link is steady, not absent
        else:
            out.append(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))])
    return "".join(out)


# --------------------------------------------------------------------------
# Airtime and duty cycle
# --------------------------------------------------------------------------

MODULE_OVERHEAD_BYTES = 4


def airtime_ms(
    payload_bytes: int, sf: int, bw_khz: int = 125, cr: int = 1,
    preamble: int = 8, explicit_header: bool = True, crc: bool = True,
) -> float:
    """Semtech's time-on-air formula. `cr` is 1..4 meaning 4/5..4/8."""
    bw = bw_khz * 1000
    t_sym = (2 ** sf) / bw
    t_preamble = (preamble + 4.25) * t_sym

    low_rate = 1 if (sf >= 11 and bw_khz == 125) else 0
    ih = 0 if explicit_header else 1
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16 * (1 if crc else 0) - 20 * ih
    denominator = 4 * (sf - 2 * low_rate)
    n_payload = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)
    return (t_preamble + n_payload * t_sym) * 1000


class DutyBudget:
    """Rolling one-hour airtime accounting.

    EU 868 sub-bands are capped at 1%. US 902-928 has no duty limit, but the
    accounting is still worth keeping: it tells you when you are monopolising
    a half-duplex channel.
    """

    WINDOW_S = 3600.0

    def __init__(self, limit_percent: float = 1.0) -> None:
        self.limit_percent = limit_percent
        self._events: list[tuple[float, float]] = []

    def record(self, ms: float, now: float | None = None) -> None:
        self._events.append((now if now is not None else time.time(), ms))

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_S
        self._events = [(t, ms) for t, ms in self._events if t >= cutoff]

    def used_ms(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        self._prune(now)
        return sum(ms for _, ms in self._events)

    def used_percent(self, now: float | None = None) -> float:
        return self.used_ms(now) / (self.WINDOW_S * 1000.0) * 100.0

    def allowance_ms(self) -> float:
        return self.WINDOW_S * 1000.0 * self.limit_percent / 100.0

    def remaining_ms(self, now: float | None = None) -> float:
        if self.limit_percent <= 0:
            return float("inf")
        return max(0.0, self.allowance_ms() - self.used_ms(now))

    def can_send(self, ms: float, now: float | None = None) -> bool:
        return self.limit_percent <= 0 or self.used_ms(now) + ms <= self.allowance_ms()

    def next_free_in_s(self, ms: float, now: float | None = None) -> float:
        """Seconds until enough airtime frees up for a transmission of `ms`."""
        if self.limit_percent <= 0:
            return 0.0
        now = now if now is not None else time.time()
        self._prune(now)
        need = self.used_ms(now) + ms - self.allowance_ms()
        if need <= 0:
            return 0.0
        freed = 0.0
        for t, spent in sorted(self._events):
            freed += spent
            if freed >= need:
                return max(0.0, t + self.WINDOW_S - now)
        return self.WINDOW_S
