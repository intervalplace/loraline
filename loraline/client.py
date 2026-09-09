"""Binds the session to the link. Views call pump() and render what comes back."""

from __future__ import annotations

import time

from .crypto import GROUP, Identity, Keyring
from .session import Event, Session, SystemEvent
from .transport import Link


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
            self.notes.append(
                f"Idle cost: {idle / 1000:.0f} s of airtime per hour"
                + (f" ({idle / 36000:.2f}% duty)" if link.budget.limit_percent > 0 else "")
                + "."
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
        if self.keyring.can_encrypt_to(convo):
            lock = "e2e" if convo != GROUP else "group key"
        else:
            lock = "CLEAR"
        bits = [self.link.status(),
                f"unconfirmed {self.session.unconfirmed_count()}"]
        if len(self.link.interfaces) > 1:
            bits.append(f"relayed {self.link.forwarded}")
        bits.append(lock)
        return "   ".join(bits)
