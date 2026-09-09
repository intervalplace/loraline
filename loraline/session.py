"""Session state: roster, conversations, presence, delivery, store-and-forward.

Still pure. Frames and clock ticks go in, events come out; time is passed in
rather than read. That is what lets the tests exercise a three-minute presence
timeout instantly and run a four-way conversation with no hardware.

Two conversation kinds share one radio channel:

  group   addressed to "*", encrypted with the shared passphrase
  direct  addressed to one peer, encrypted in a pairwise box

Everyone's radio receives every packet either way. What separates a direct
message from a group one is that nobody else holds the key, not that other
clients are being polite about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import protocol as proto
from .crypto import GROUP, Identity, Keyring
from .protocol import Delivery, Frame, Status, seq_newer

HEARTBEAT_S = 60.0
PEER_TIMEOUT_S = 155.0
IDLE_TO_AWAY_S = 300.0
TYPING_LOCKOUT_S = 5.0
TYPING_EXPIRY_S = 12.0
RETRY_AFTER_S = 90.0
MAX_ATTEMPTS = 3
HELLO_COOLDOWN_S = 20.0

PALETTE = ["green", "cyan", "magenta", "yellow", "blue", "red", "white", "teal"]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

@dataclass
class MessageEvent:
    convo: str
    who: str
    text: str
    incoming: bool
    seq: int
    when: float
    colour: int = 0
    src: str = ""


@dataclass
class SystemEvent:
    text: str
    level: str = "info"
    convo: str | None = None    # None means show it wherever the user is


@dataclass
class DeliveryEvent:
    seq: int


@dataclass
class PresenceEvent:
    pass


Event = MessageEvent | SystemEvent | DeliveryEvent | PresenceEvent


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

@dataclass
class Outgoing:
    seq: int
    target: str
    text: str
    fragments: list[str]
    recipients: set[str] = field(default_factory=set)
    confirmed: set[str] = field(default_factory=set)
    state: Delivery = Delivery.QUEUED
    created: float = 0.0
    last_attempt: float = 0.0
    attempts: int = 0

    def resolve(self) -> Delivery:
        if self.state in (Delivery.QUEUED, Delivery.FAILED):
            return self.state
        if not self.recipients:
            return Delivery.SENT
        if self.confirmed >= self.recipients:
            return Delivery.DELIVERED
        return Delivery.PARTIAL if self.confirmed else Delivery.SENT

    def tally(self) -> str:
        if len(self.recipients) <= 1:
            return ""
        return f" {len(self.confirmed)}/{len(self.recipients)}"


@dataclass
class Peer:
    address: str
    nick: str = ""
    colour: int = 1
    status: Status = Status.OFFLINE
    psm: str = ""
    last_seen: float | None = None
    rssi_dbm: int | None = None
    rssi_history: list[int | None] = field(default_factory=list)
    typing: dict[str, float] = field(default_factory=dict)
    known_key: bool = False

    @property
    def label(self) -> str:
        return self.nick or self.address


@dataclass
class Conversation:
    key: str                      # "*" or a peer address
    title: str
    entries: list = field(default_factory=list)
    unread: int = 0

    @property
    def is_group(self) -> bool:
        return self.key == GROUP


class Session:
    def __init__(
        self,
        nick: str,
        identity: Identity,
        keyring: Keyring,
        psm: str = "",
        colour: int = 0,
        payload_budget: int = proto.MAX_FRAME_BYTES - 1,
        now: float = 0.0,
    ) -> None:
        self.nick = nick
        self.identity = identity
        self.keyring = keyring
        self.psm = psm
        self.colour = colour
        self.payload_budget = payload_budget
        self.address = identity.address

        self.status = Status.ONLINE
        self.peers: dict[str, Peer] = {}
        self.conversations: dict[str, Conversation] = {
            GROUP: Conversation(GROUP, "everyone")
        }

        self.seq = 0
        self.outbox: list[Outgoing] = []
        self.acks: dict[str, int] = {}          # contiguous high-water mark per peer
        self._received: dict[str, set[int]] = {}
        self._reassembly: dict[tuple[str, int], dict] = {}

        self.last_keystroke = now
        self.last_heartbeat = 0.0
        self.heartbeat_s = HEARTBEAT_S
        self.peer_timeout_s = PEER_TIMEOUT_S
        self.beats_sent = 0
        self._identity_dirty = True
        self.last_typing_sent: dict[str, float] = {}
        self.last_hello_sent = 0.0
        self.auto_away = False

        self._pending: list[Frame] = []
        self._force_heartbeat = True
        self._send_hello = True

    # -- roster ------------------------------------------------------------

    def peer(self, address: str) -> Peer:
        if address not in self.peers:
            self.peers[address] = Peer(address)
        return self.peers[address]

    def conversation(self, key: str) -> Conversation:
        if key not in self.conversations:
            title = self.peers[key].label if key in self.peers else key
            self.conversations[key] = Conversation(key, title)
        return self.conversations[key]

    def online_peers(self) -> list[Peer]:
        return [p for p in self.peers.values() if p.status is not Status.OFFLINE]

    # -- inbound -----------------------------------------------------------

    def on_frame(self, frame: Frame, now: float) -> list[Event]:
        src = frame.field_str(0)
        if not src or src == self.address:
            return []

        known = src in self.peers
        peer = self.peer(src)
        if frame.rssi_raw is not None:
            peer.rssi_dbm = proto.rssi_dbm(frame.rssi_raw)
        peer.rssi_history.append(peer.rssi_dbm)
        del peer.rssi_history[:-120]

        was_offline = peer.status is Status.OFFLINE
        peer.last_seen = now
        events: list[Event] = []

        # Someone we have never met, or whose key we lack: introduce ourselves.
        if (not known or not peer.known_key) and now - self.last_hello_sent > HELLO_COOLDOWN_S:
            self._send_hello = True

        if frame.type == "H":
            events += self._on_hello(frame, peer, now)
        elif frame.type == "P":
            events += self._on_presence(frame, peer, was_offline, now)
        elif frame.type == "M":
            events += self._on_message(frame, peer, now)
        elif frame.type == "T":
            peer.typing[self._convo_for(frame.field_str(1), src)] = now + TYPING_EXPIRY_S
            events.append(PresenceEvent())
        elif frame.type == "X":
            peer.status = Status.OFFLINE
            peer.last_seen = None
            events.append(SystemEvent(f"{peer.label} has signed out."))

        # Receiving anything at all is proof they are reachable right now.
        # Presence frames refine that into away/busy; everything else just
        # means "on the air". Without this, a peer discovered by hello alone
        # stays offline until their next heartbeat, and store-and-forward
        # refuses to send to them for up to a minute.
        if frame.type != "X" and peer.status is Status.OFFLINE:
            peer.status = Status.ONLINE
        if was_offline and frame.type != "X":
            events.insert(0, SystemEvent(f"{peer.label} has just signed in."))
        return events

    def _convo_for(self, dst: str, src: str) -> str:
        """Which conversation a frame belongs to, from the receiver's side."""
        return GROUP if dst == GROUP or not dst else src

    def _on_hello(self, frame: Frame, peer: Peer, now: float) -> list[Event]:
        peer.nick = frame.field_str(1) or peer.nick
        peer.colour = frame.field_int(2, 1) % len(PALETTE)
        if self.keyring.learn(peer.address, frame.field_str(3)):
            peer.known_key = True
            self.conversation(peer.address).title = peer.label
            return [SystemEvent(
                f"Key exchanged with {peer.label} ({peer.address}). "
                f"Direct messages to them are now end-to-end encrypted."
            )]
        peer.known_key = peer.known_key or self.keyring.knows(peer.address)
        return [PresenceEvent()]

    def _on_presence(self, frame: Frame, peer: Peer, was_offline: bool,
                     now: float) -> list[Event]:
        try:
            status = Status(frame.field_str(1, "on"))
        except ValueError:
            status = Status.ONLINE

        # The short form carries no identity fields; keep what we were told
        # last time rather than blanking them.
        if len(frame.fields) > 3:
            peer.nick = frame.field_str(3) or peer.nick
            peer.colour = frame.field_int(4, 1) % len(PALETTE)
            peer.psm = frame.field_str(5)
        if peer.address in self.conversations:
            self.conversations[peer.address].title = peer.label

        events = self._absorb_acks(
            peer.address, proto.decode_acks(frame.field_str(2)).get(self.address)
        )
        if status != peer.status and not was_offline:
            events.append(SystemEvent(f"{peer.label} is now {status.label}."))
        peer.status = status
        events.append(PresenceEvent())
        return events

    def _absorb_acks(self, from_addr: str, ack: int | None) -> list[Event]:
        if ack is None or ack < 0:
            return []
        events: list[Event] = []
        for item in self.outbox:
            if from_addr not in item.recipients or from_addr in item.confirmed:
                continue
            if not seq_newer(item.seq, ack):     # item.seq <= ack
                item.confirmed.add(from_addr)
                item.state = item.resolve()
                events.append(DeliveryEvent(item.seq))
        return events

    def _on_message(self, frame: Frame, peer: Peer, now: float) -> list[Event]:
        dst = frame.field_str(1)
        if dst != GROUP and dst != self.address:
            return []      # a direct message between two other people

        seq = frame.field_int(2)
        events = self._absorb_acks(peer.address, frame.field_int(3))
        idx, cnt = frame.field_int(4, 0), frame.field_int(5, 1)
        text = frame.field_str(6)
        convo = self._convo_for(dst, peer.address)
        peer.typing.pop(convo, None)

        if cnt <= 1:
            self._note_received(peer.address, seq)
            return events + [self._deliver(convo, peer, seq, text, now)]

        slot = self._reassembly.setdefault((peer.address, seq), {"count": cnt, "parts": {}})
        slot["parts"][idx] = text
        if len(slot["parts"]) >= slot["count"]:
            whole = "".join(slot["parts"][i] for i in sorted(slot["parts"]))
            del self._reassembly[(peer.address, seq)]
            self._note_received(peer.address, seq)
            events.append(self._deliver(convo, peer, seq, whole, now))
        return events

    def _note_received(self, address: str, seq: int) -> None:
        """Advance the acknowledgement high-water mark, contiguously.

        Two things matter here. Acknowledge only once a message is fully
        reassembled, or the sender is told a half-arrived message landed and
        stops retrying the missing fragments. And advance only over an
        unbroken run, because the mark is inclusive: jumping to 5 while 4 is
        still incomplete would silently confirm 4 as well.
        """
        seen = self._received.setdefault(address, set())
        seen.add(seq)
        mark = self.acks.get(address, 0)
        while ((mark + 1) % proto.SEQ_MODULO) in seen:
            mark = (mark + 1) % proto.SEQ_MODULO
            seen.discard(mark)
        if mark != self.acks.get(address, 0):
            self.acks[address] = mark
            self._force_heartbeat = True     # get the acknowledgement moving

    def _deliver(self, convo_key: str, peer: Peer, seq: int, text: str,
                 now: float) -> MessageEvent:
        convo = self.conversation(convo_key)
        event = MessageEvent(convo_key, peer.label, text, True, seq, now,
                             peer.colour, peer.address)
        convo.entries.append(event)
        return event

    # -- clock -------------------------------------------------------------

    def tick(self, now: float) -> list[Event]:
        events: list[Event] = []

        for peer in self.peers.values():
            if (
                peer.last_seen is not None
                and now - peer.last_seen > self.peer_timeout_s
                and peer.status is not Status.OFFLINE
            ):
                peer.status = Status.OFFLINE
                peer.last_seen = None
                peer.rssi_dbm = None
                events.append(SystemEvent(
                    f"{peer.label} appears to have gone offline "
                    f"(no heartbeat for {int(self.peer_timeout_s)}s)."
                ))

        if self.status is Status.ONLINE and now - self.last_keystroke > IDLE_TO_AWAY_S:
            self.auto_away = True
            self.status = Status.AWAY
            self._force_heartbeat = True
            events.append(PresenceEvent())

        for item in self.outbox:
            if item.state in (Delivery.SENT, Delivery.PARTIAL) and \
                    now - item.last_attempt > RETRY_AFTER_S:
                if item.attempts >= MAX_ATTEMPTS:
                    item.state = Delivery.FAILED
                else:
                    item.state = Delivery.QUEUED
                events.append(DeliveryEvent(item.seq))
        return events

    # -- local actions -----------------------------------------------------

    def compose(self, text: str, target: str, now: float) -> list[Event]:
        text = proto.sanitize(text)
        if not text:
            return []
        self.seq = (self.seq + 1) % proto.SEQ_MODULO
        overhead = proto.message(self.address, target, self.seq, 0, 0, 9, "").size
        fragments = proto.fragment(text, max(16, self.payload_budget - overhead))

        if target == GROUP:
            recipients = {p.address for p in self.online_peers()}
        else:
            recipients = {target}

        item = Outgoing(self.seq, target, text, fragments,
                        recipients=recipients, created=now)
        self.outbox.append(item)
        self.last_typing_sent[target] = 0.0

        convo = self.conversation(target)
        event = MessageEvent(target, self.nick, text, False, self.seq, now,
                             self.colour, self.address)
        convo.entries.append(event)

        warn: list[Event] = []
        if not self.keyring.can_encrypt_to(target):
            label = "the group" if target == GROUP else self.conversation(target).title
            warn.append(SystemEvent(
                f"Sent to {label} in the clear: no key available.",
                level="warn", convo=target,
            ))
        return [event, DeliveryEvent(self.seq), *warn]

    def on_keystroke(self, buffer_nonempty: bool, target: str, now: float) -> list[Event]:
        self.last_keystroke = now
        events: list[Event] = []
        if self.auto_away:
            self.auto_away = False
            self.status = Status.ONLINE
            self._force_heartbeat = True
            events.append(PresenceEvent())
        # One typing packet per lockout window, not one per keystroke. On a
        # half-duplex radio that doubles as collision avoidance.
        if buffer_nonempty and now - self.last_typing_sent.get(target, 0.0) >= TYPING_LOCKOUT_S:
            self.last_typing_sent[target] = now
            self._pending.append(proto.typing(self.address, target))
        return events

    def set_status(self, status: Status, now: float) -> list[Event]:
        self.auto_away = False
        self.status = status
        self._force_heartbeat = True
        return [SystemEvent(f"You are now {status.label}."), PresenceEvent()]

    def set_psm(self, psm: str) -> list[Event]:
        self.psm = proto.sanitize(psm)
        self._identity_dirty = True
        self._force_heartbeat = True
        return [SystemEvent(f"Personal message set to: {self.psm or '(none)'}")]

    def sample_heartbeat(self) -> Frame:
        """A representative heartbeat, for costing before anything is sent."""
        return proto.presence(self.address, self.status, self.acks,
                              self.nick, self.colour, self.psm)

    def pace_heartbeat(self, cost_ms: float, allowance_ms_per_hour: float,
                       share: float = 0.4) -> str | None:
        """Slow presence down until it fits inside the airtime budget.

        Presence is the only thing that transmits when nobody is talking, so
        it sets the floor on what an idle client costs. At a high spreading
        factor under a duty cycle cap, a heartbeat every minute can exceed the
        entire hourly allowance on its own, leaving nothing for messages and
        breaking the limit besides.

        Returns a note to show the user if the rate had to change.
        """
        if allowance_ms_per_hour <= 0:
            return None                      # no cap, keep the lively default
        budget = allowance_ms_per_hour * share
        needed = 3600.0 * cost_ms / budget
        if needed <= HEARTBEAT_S:
            return None
        self.heartbeat_s = needed
        self.peer_timeout_s = needed * 2.6
        return (
            f"Presence slowed to one heartbeat every {needed / 60:.1f} min so "
            f"it fits the duty cycle. Someone leaving will take up to "
            f"{self.peer_timeout_s / 60:.0f} min to show as offline. "
            f"A lower spreading factor would fix this."
        )

    def announce(self) -> None:
        """Re-send identity and presence, e.g. when a new bearer comes up."""
        self._send_hello = True
        self._force_heartbeat = True
        self._identity_dirty = True
        self.last_hello_sent = 0.0

    def sign_off(self) -> None:
        self._pending.append(proto.signoff(self.address))

    # -- outbound ----------------------------------------------------------

    def drain(self, now: float,
              can_send: Callable[[Frame, str], bool]) -> tuple[list[tuple[Frame, str]], list[Event]]:
        """Everything that should go on the air now, each with its destination.

        The destination travels alongside the frame rather than inside it so
        the link layer knows which key to seal with. `can_send` is the
        caller's airtime veto; anything vetoed stays queued rather than being
        dropped, which turns the duty cycle into a delay instead of an error.
        """
        out: list[tuple[Frame, str]] = []
        events: list[Event] = []

        def offer(frame: Frame, dst: str) -> bool:
            if not can_send(frame, dst):
                return False
            out.append((frame, dst))
            return True

        if self._send_hello:
            beacon = proto.hello(self.address, self.nick, self.colour,
                                 self.identity.public_b64)
            if offer(beacon, GROUP):
                self._send_hello = False
                self.last_hello_sent = now

        if self._force_heartbeat or (now - self.last_heartbeat >= self.heartbeat_s):
            # Carry identity when it has changed, and periodically anyway so a
            # peer who joined late catches up without having to ask.
            full = self._identity_dirty or self.beats_sent % 10 == 0
            beat = proto.presence(
                self.address, self.status, self.acks,
                *( (self.nick, self.colour, self.psm) if full else () )
            )
            if offer(beat, GROUP):
                self.last_heartbeat = now
                self._force_heartbeat = False
                self._identity_dirty = False
                self.beats_sent += 1

        for frame in list(self._pending):
            dst = frame.field_str(1, GROUP) if frame.type == "T" else GROUP
            if offer(frame, dst):
                self._pending.remove(frame)

        for item in self.outbox:
            if item.state is not Delivery.QUEUED:
                continue
            # Store and forward: hold until somebody is there to hear it.
            reachable = (
                bool(self.online_peers()) if item.target == GROUP
                else self.peer(item.target).status is not Status.OFFLINE
            )
            if not reachable:
                continue

            ack = self.acks.get(item.target, proto.NO_ACK) if item.target != GROUP else proto.NO_ACK
            total = len(item.fragments)
            complete = True
            for idx, part in enumerate(item.fragments):
                frame = proto.message(self.address, item.target, item.seq,
                                      ack, idx, total, part)
                if not offer(frame, item.target):
                    complete = False
                    break
            if not complete:
                break
            if item.target == GROUP:
                item.recipients |= {p.address for p in self.online_peers()}
            item.attempts += 1
            item.last_attempt = now
            item.state = item.resolve() if item.confirmed else Delivery.SENT
            events.append(DeliveryEvent(item.seq))
        return out, events

    # -- views -------------------------------------------------------------

    def outgoing(self, seq: int) -> Outgoing | None:
        for item in self.outbox:
            if item.seq == seq:
                return item
        return None

    def queued_count(self) -> int:
        return sum(1 for i in self.outbox if i.state is Delivery.QUEUED)

    def unconfirmed_count(self) -> int:
        return sum(1 for i in self.outbox
                   if i.state in (Delivery.QUEUED, Delivery.SENT, Delivery.PARTIAL))

    def typing_in(self, convo: str, now: float) -> list[str]:
        return [p.label for p in self.peers.values() if p.typing.get(convo, 0.0) > now]

    def last_seen_text(self, peer: Peer, now: float) -> str:
        if peer.last_seen is None:
            return "never"
        delta = int(now - peer.last_seen)
        return "just now" if delta < 5 else f"{delta}s ago"
