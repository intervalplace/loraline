"""Binds the session to the link. Views call pump() and render what comes back."""

from __future__ import annotations

import time

from .crypto import GROUP, Identity, Keyring
from .session import Event, Session, SystemEvent
from .transport import Link


import os, time as _t
_TRACE = os.environ.get("LORALINE_TRACE")
_last_trace = [0.0]


class Client:
    def __init__(self, link: Link, identity: Identity, keyring: Keyring,
                 nick: str, psm: str = "", colour: int = 0) -> None:
        self.link = link
        self.identity = identity
        self.keyring = keyring
        now = time.time()
        self.session = Session(
            nick=nick, identity=identity, keyring=keyring, psm=psm, colour=colour,
            payload_budget=link.payload_budget(), now=now,
        )
        self.error: str | None = None
        self._warned_budget = False
        self.notes: list[str] = []

        lora = link.lora
        if lora is not None:
            beat = self.session.sample_heartbeat()
            cost = lora.config.airtime_of(link.wire_frame(beat).size)
            note = self.session.pace_heartbeat(cost, link.budget.allowance_ms())
            if note:
                self.notes.append(note)
            idle = 3600.0 / self.session.heartbeat_s * cost
            allowance = link.budget.allowance_ms()
            if allowance > 0:
                self.notes.append(
                    f"Just sitting here costs {idle / 1000:.0f} seconds of radio "
                    f"time an hour, out of the {allowance / 1000:.0f} you are "
                    f"allowed. The rest is yours to talk with."
                )
            else:
                self.notes.append(
                    f"Just sitting here costs {idle / 1000:.0f} seconds of radio "
                    f"time an hour. There is no hourly limit on this band."
                )

    def pump(self, now: float | None = None) -> list[Event]:
        now = now if now is not None else time.time()
        events: list[Event] = []

        if self.link.new_bearer_up():
            self.session.announce()

        frames, errors = self.link.pump()
        for exc in errors:
            self.error = f"interface failed: {exc}"
            events.append(SystemEvent(self.error, level="warn"))
        for frame in frames:
            events += self.session.on_frame(frame, now)

        events += self.session.tick(now)

        frames, tx_events = self.session.drain(now, self.link.can_send)
        for frame, dst in frames:
            self.link.send(frame, dst)
        events += tx_events

        held = self.session.queued_count()
        if held and not self._warned_budget:
            wait = self.link.budget.next_free_in_s(500.0, now)
            if wait > 0:
                self._warned_budget = True
                events.append(SystemEvent(
                    f"{held} message(s) held: no airtime for about "
                    f"{wait / 60:.0f} min.", level="warn"))
        elif not held:
            self._warned_budget = False

        if _TRACE and now - _last_trace[0] >= 1.0:
            _last_trace[0] = now
            with open(_TRACE, "a") as f:
                f.write(f"\n[{now:.1f}] me={self.session.address} "
                        f"acks={dict(self.session.acks)}\n")
                for it in self.session.outbox:
                    f.write(f"  seq={it.seq} tgt={it.target} "
                            f"state={it.state.name} att={it.attempts} "
                            f"recip={sorted(it.recipients)} "
                            f"conf={sorted(it.confirmed)} "
                            f"text={it.text[:24]!r}\n")
                for addr, p in self.session.peers.items():
                    f.write(f"  peer {addr} nick={p.nick!r} "
                            f"status={p.status.name} "
                            f"known_key={p.known_key}\n")
        return events

    def shutdown(self) -> None:
        self.session.sign_off()
        frames, _ = self.session.drain(time.time(), lambda f, d: True)
        for frame, dst in frames:
            try:
                self.link.send(frame, dst)
            except Exception:
                pass
        time.sleep(0.4)

    def status_summary(self, now: float, convo: str = GROUP) -> str:
        if not self.keyring.can_encrypt_to(convo):
            lock = "NOT ENCRYPTED"
        elif convo == GROUP:
            lock = "encrypted for the group"
        else:
            lock = "encrypted, just you two"

        bits = [self.link.status()]
        waiting = self.session.unconfirmed_count()
        if waiting:
            bits.append(f"{waiting} waiting")
        if len(self.link.interfaces) > 1:
            bits.append(f"{self.link.forwarded} passed on")
        bits.append(lock)
        return "   ".join(bits)
