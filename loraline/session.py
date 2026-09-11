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
from typing import Callable, Union

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
# After this many newer messages arrive past a gap, treat the missing
# sequence number as permanently lost and advance the acknowledgement mark
# over it. Without this a single dropped radio frame jams the mark forever
# and every later message retransmits until it hits MAX_ATTEMPTS. The sender
# reuses a seq on retransmit, so a briefly-lost frame still has several
# chances to arrive and fill the gap before it is skipped.
SKIP_AFTER = 5
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
class AppEvent:
    """Something arrived for an application other than chat."""
    app: str
    src: str
    payload: str
    convo: str


@dataclass
class PresenceEvent:
    pass


# Written as a typing.Union rather than `A | B`, which is a runtime
# expression and would need Python 3.10. macOS still ships 3.9, and needing a
# Homebrew install to plug in a radio is exactly the accidental friction this
# project is supposed to be free of.
Event = Union[MessageEvent, SystemEvent, DeliveryEvent, AppEvent, PresenceEvent]


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
    lost: set[str] = field(default_factory=set)     # recipients who reported this dropped
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
        outstanding = self.recipients - self.confirmed - self.lost
        if not outstanding:
            # No one left to hear from: either some got it (partial, final)
            # or nobody did (failed).
            return Delivery.PARTIAL if self.confirmed else Delivery.FAILED
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
    heard_on: set = field(default_factory=set)   # which bearers carried them
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

        self.group_seq = 0
        self.dm_seq = 0
        self.outbox: list[Outgoing] = []
        self.acks: dict[str, int] = {}          # contiguous high-water mark per peer
        self._received: dict[str, set[int]] = {}
        self._lost: dict[str, set[int]] = {}   # seqs abandoned by gap recovery, to report back
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
        self._hello_reply_requested = False   # ask peers to hello back

    # -- roster ------------------------------------------------------------

    def on_air(self, address: str) -> bool:
        """Did this person's own transmissions reach us through the air?

        A frame relayed over a socket by somebody else proves they exist, not
        that they were near enough to hear.
        """
        person = self.peers.get(address)
        return bool(person and "lora" in person.heard_on)

    def peer(self, address: str) -> Peer:
        if address not in self.peers:
            p = Peer(address)
            # A peer whose key we persisted from an earlier session is known
            # straight away, and wears the nick we last saw, so they appear as
            # "mats" rather than a raw address the moment they first speak.
            if self.keyring is not None and self.keyring.knows(address):
                p.known_key = True
                nick = getattr(self.keyring, "nicks", {}).get(address, "")
                if nick:
                    p.nick = nick
            self.peers[address] = p
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

        if getattr(frame, "via", ""):
            peer.heard_on.add(frame.via)
        was_offline = peer.status is Status.OFFLINE
        peer.last_seen = now
        events: list[Event] = []

        # Someone we have never met, or whose key we lack: introduce ourselves
        # and ask them to hello back so we get their key even if an earlier
        # exchange was half-lost.
        if (not known or not peer.known_key) and now - self.last_hello_sent > HELLO_COOLDOWN_S:
            self._send_hello = True
            if not self.keyring.knows(src):
                self._hello_reply_requested = True

        if frame.type == "H":
            events += self._on_hello(frame, peer, now)
        elif frame.type == "P":
            events += self._on_presence(frame, peer, was_offline, now)
        elif frame.type == "M":
            events += self._on_message(frame, peer, now)
        elif frame.type == "T":
            peer.typing[self._convo_for(frame.field_str(1), src)] = now + TYPING_EXPIRY_S
            events.append(PresenceEvent())
        elif frame.type == "D":
            dst = frame.field_str(1)
            if dst in (GROUP, self.address):
                events.append(AppEvent(frame.field_str(2), src,
                                       frame.field_str(3),
                                       self._convo_for(dst, src)))
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
        if self.keyring is not None and peer.nick:
            self.keyring.remember_nick(peer.address, peer.nick)
        peer.colour = frame.field_int(2, 1) % len(PALETTE)
        reply_requested = frame.field_str(5) == "1"
        learned = self.keyring.learn(peer.address, frame.field_str(3),
                                     frame.field_str(4))
        # If the peer asked us to hello back (their view of us is missing our
        # key), oblige on the next drain. Send it plainly, without re-requesting,
        # to avoid a request ping-pong.
        if reply_requested:
            self._send_hello = True
        events: list[Event] = []
        if learned:
            peer.known_key = True
            self.conversation(peer.address).title = peer.label
            events.append(SystemEvent(
                f"{peer.label}'s key arrived. Private messages with "
                f"{peer.label} can now be read by nobody else."
            ))
        else:
            peer.known_key = peer.known_key or self.keyring.knows(peer.address)
            events.append(PresenceEvent())
        return events

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

        acks_in = proto.decode_acks(frame.field_str(2))
        lost_in = proto.decode_lost(frame.field_str(2))
        events = self._absorb_acks(
            peer.address, acks_in.get(self.address), "group",
            lost_in.get(self.address),
        )
        events += self._absorb_acks(
            peer.address, acks_in.get(self.address + "#dm"), "dm",
            lost_in.get(self.address + "#dm"),
        )
        events += self._absorb_losses(
            peer.address, lost_in.get(self.address), "group"
        )
        events += self._absorb_losses(
            peer.address, lost_in.get(self.address + "#dm"), "dm"
        )
        if status != peer.status and not was_offline:
            events.append(SystemEvent(f"{peer.label} is now {status.label}."))
        peer.status = status
        events.append(PresenceEvent())
        return events

    def _absorb_acks(self, from_addr: str, ack: int | None,
                     stream: str = "group",
                     holes: set[int] | None = None) -> list[Event]:
        if ack is None or ack < 0:
            return []
        holes = holes or set()
        events: list[Event] = []
        for item in self.outbox:
            item_stream = "group" if item.target == GROUP else "dm"
            if item_stream != stream:
                continue
            if from_addr not in item.recipients or from_addr in item.confirmed:
                continue
            if item.seq in holes:
                continue     # the mark jumped OVER this one; it was not received
            if not seq_newer(item.seq, ack):     # item.seq <= ack
                item.confirmed.add(from_addr)
                # A late-arriving frame can confirm a message we had marked
                # lost for this peer: the ack now covers it and it is no longer
                # in the peer's hole list. Clear the stale loss and let the
                # state recompute, so a cross can turn back into a tick.
                if from_addr in item.lost:
                    item.lost.discard(from_addr)
                    if item.state is Delivery.FAILED:
                        item.state = Delivery.SENT   # let resolve() lift it
                item.state = item.resolve()
                events.append(DeliveryEvent(item.seq))
        return events

    def _absorb_losses(self, from_addr: str, holes, stream: str = "group") -> list[Event]:
        """Mark our own messages the peer has explicitly given up on as failed.

        Gap recovery on the receiver reports the exact sequence numbers it
        abandoned. For a group message a single peer's loss is a partial
        failure; for a direct message that one peer is the only recipient, so
        its loss fails the message outright. Either way the sender learns the
        message did not land, instead of a dropped frame silently reading as
        delivered.
        """
        if not holes:
            return []
        events: list[Event] = []
        for item in self.outbox:
            item_stream = "group" if item.target == GROUP else "dm"
            if item_stream != stream or item.seq not in holes:
                continue
            if from_addr in item.confirmed:
                continue          # somehow both lost and confirmed; trust the ack
            item.lost.add(from_addr)
            # Let resolve() decide the state now that a recipient is known to
            # have lost it: PARTIAL if others still got it or may yet, FAILED
            # only when nobody received it. Don't hard-set FAILED here or a
            # message half the room received would wrongly read as lost.
            if item.state not in (Delivery.QUEUED, Delivery.DELIVERED):
                new_state = item.resolve()
                if new_state != item.state:
                    item.state = new_state
                    events.append(DeliveryEvent(item.seq))
        return events

    def _on_message(self, frame: Frame, peer: Peer, now: float) -> list[Event]:
        dst = frame.field_str(1)
        if dst != GROUP and dst != self.address:
            return []      # a direct message between two other people

        seq = frame.field_int(2)
        inline_stream = "group" if dst == GROUP else "dm"
        events = self._absorb_acks(peer.address, frame.field_int(3), inline_stream)
        idx, cnt = frame.field_int(4, 0), frame.field_int(5, 1)
        text = frame.field_str(6)
        convo = self._convo_for(dst, peer.address)
        peer.typing.pop(convo, None)

        ack_key = self._ack_key(peer.address, dst)
        if cnt <= 1:
            self._note_received(ack_key, seq)
            return events + [self._deliver(convo, peer, seq, text, now)]

        slot = self._reassembly.setdefault((ack_key, seq), {"count": cnt, "parts": {}})
        slot["parts"][idx] = text
        if len(slot["parts"]) >= slot["count"]:
            whole = "".join(slot["parts"][i] for i in sorted(slot["parts"]))
            del self._reassembly[(ack_key, seq)]
            self._note_received(ack_key, seq)
            events.append(self._deliver(convo, peer, seq, whole, now))
        return events

    def _ack_key(self, address: str, dst: str) -> str:
        """Namespace the per-peer ack mark by message stream.

        Group and direct messages have independent sequence counters on the
        sender, so their seq numbers overlap. Keeping one contiguous mark per
        (peer, stream) stops a direct message's seq from punching an
        unfillable hole in the group mark, which used to jam acknowledgement
        permanently and cause endless retransmission.
        """
        return address if dst == GROUP else address + "#dm"

    def _note_received(self, key: str, seq: int) -> None:
        """Advance the acknowledgement high-water mark, contiguously.

        Two things matter here. Acknowledge only once a message is fully
        reassembled, or the sender is told a half-arrived message landed and
        stops retrying the missing fragments. And advance only over an
        unbroken run, because the mark is inclusive: jumping to 5 while 4 is
        still incomplete would silently confirm 4 as well.
        """
        seen = self._received.setdefault(key, set())
        seen.add(seq)
        # A frame we had already given up on has arrived late (radio reorder or
        # a retransmit that finally made it). Retract the loss so we stop
        # reporting it as dropped; the sender can then clear its cross too.
        lost = self._lost.get(key)
        if lost and seq in lost:
            lost.discard(seq)
            if not lost:
                self._lost.pop(key, None)
            self._force_heartbeat = True   # tell the sender to un-cross it
        mark = self.acks.get(key, 0)
        while ((mark + 1) % proto.SEQ_MODULO) in seen:
            mark = (mark + 1) % proto.SEQ_MODULO
            seen.discard(mark)
        # Gap recovery: if messages well beyond the mark have arrived, the
        # frame at mark+1 was dropped by the radio and is not coming, so step
        # over it rather than jam acknowledgement permanently.
        if seen:
            highest = max(seen)
            ahead = (highest - mark) % proto.SEQ_MODULO
            while ahead > SKIP_AFTER and ahead < proto.SEQ_MODULO // 2:
                mark = (mark + 1) % proto.SEQ_MODULO      # abandon the lost seq
                # remember what we gave up on, but only for the group stream:
                # a "#dm" key means a private message, whose loss is reported
                # on that pairwise channel, not broadcast to everyone.
                self._lost.setdefault(key, set()).add(mark)
                while ((mark + 1) % proto.SEQ_MODULO) in seen:
                    mark = (mark + 1) % proto.SEQ_MODULO
                    seen.discard(mark)
                highest = max(seen) if seen else mark
                ahead = (highest - mark) % proto.SEQ_MODULO
        if mark != self.acks.get(key, 0):
            self.acks[key] = mark
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
                quiet = self.peer_timeout_s
                spell = (f"{quiet / 60:.0f} minutes" if quiet >= 90
                         else f"{quiet:.0f} seconds")
                events.append(SystemEvent(
                    f"{peer.label} has gone offline. Their radio has said "
                    f"nothing for {spell}."
                ))

        # Key exchange self-heals. If a peer is reachable but we still lack
        # its key, our earlier hello or its reply was dropped by the radio.
        # Re-introduce ourselves once the cooldown has elapsed, rather than
        # waiting for a frame to arrive at just the right moment. Without this
        # a single lost hello leaves one side unable to name or privately
        # message the other for the whole session.
        if (self.keyring is not None
                and any(pe.status is not Status.OFFLINE
                        and not self.keyring.knows(pe.address)
                        for pe in self.peers.values())
                and now - self.last_hello_sent > HELLO_COOLDOWN_S):
            self._send_hello = True
            self._hello_reply_requested = True

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
        if target == GROUP:
            self.group_seq = (self.group_seq + 1) % proto.SEQ_MODULO
            seq = self.group_seq
        else:
            self.dm_seq = (self.dm_seq + 1) % proto.SEQ_MODULO
            seq = self.dm_seq
        overhead = proto.message(self.address, target, seq, 0, 0, 9, "").size
        fragments = proto.fragment(text, max(16, self.payload_budget - overhead))

        if target == GROUP:
            recipients = {p.address for p in self.online_peers()}
        else:
            recipients = {target}

        item = Outgoing(seq, target, text, fragments,
                        recipients=recipients, created=now)
        self.outbox.append(item)
        self.last_typing_sent[target] = 0.0

        convo = self.conversation(target)
        event = MessageEvent(target, self.nick, text, False, seq, now,
                             self.colour, self.address)
        convo.entries.append(event)

        warn: list[Event] = []
        if not self.keyring.can_encrypt_to(target):
            label = "the group" if target == GROUP else self.conversation(target).title
            warn.append(SystemEvent(
                f"That went out unencrypted, because there is no key for "
                f"{label} yet. Anyone in range could read it.",
                level="warn", convo=target,
            ))
        return [event, DeliveryEvent(seq), *warn]

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

    def _lost_wire(self) -> dict[str, set[int]]:
        """Abandoned seqs in wire form, keyed as the ack marks are.

        Group losses are reported under the source peer's address; direct
        losses under that address plus the "#dm" suffix, mirroring the ack
        namespaces so the original sender matches them to the right outbox
        items. Empty keys are dropped so a quiet channel adds nothing.
        """
        return {k: v for k, v in self._lost.items() if v}

    def sample_heartbeat(self) -> Frame:
        """A representative heartbeat, for costing before anything is sent."""
        return proto.presence(self.address, self.status, self.acks,
                              self.nick, self.colour, self.psm,
                              lost=self._lost_wire())

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
            f"This setup only has room to check in every {needed / 60:.1f} "
            f"minutes, so someone leaving may take up to "
            f"{self.peer_timeout_s / 60:.0f} minutes to show as offline. "
            f"A shorter range setting would make that quicker."
        )

    def announce(self) -> None:
        """Re-send identity and presence, e.g. when a new bearer comes up."""
        self._send_hello = True
        self._force_heartbeat = True
        self._identity_dirty = True
        self.last_hello_sent = 0.0

    def send_app(self, app: str, payload: str, target: str = GROUP) -> None:
        """Queue an application frame. Rides the same link as everything else."""
        self._pending.append(proto.data(self.address, target, app,
                                        proto.sanitize(payload)))

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
                                 self.identity.public_b64,
                                 self.identity.verify_b64,
                                 reply_requested=self._hello_reply_requested)
            if offer(beacon, GROUP):
                self._send_hello = False
                self._hello_reply_requested = False
                self.last_hello_sent = now

        if self._force_heartbeat or (now - self.last_heartbeat >= self.heartbeat_s):
            # Carry identity when it has changed, and periodically anyway so a
            # peer who joined late catches up without having to ask.
            full = self._identity_dirty or self.beats_sent % 10 == 0
            beat = proto.presence(
                self.address, self.status, self.acks,
                *( (self.nick, self.colour, self.psm) if full else () ),
                lost=self._lost_wire(),
            )
            if offer(beat, GROUP):
                self.last_heartbeat = now
                self._force_heartbeat = False
                self._identity_dirty = False
                self.beats_sent += 1

        for frame in list(self._pending):
            dst = frame.field_str(1, GROUP) if frame.type in ("T", "D") else GROUP
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

            ack = (self.acks.get(item.target + "#dm", proto.NO_ACK)
                   if item.target != GROUP else proto.NO_ACK)
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
