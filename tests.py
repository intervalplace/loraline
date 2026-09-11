"""Three sessions on one simulated broadcast bus. No hardware required."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loraline import protocol as p
from loraline.crypto import GROUP, Identity, Keyring
from loraline.session import Session, MessageEvent, SystemEvent
from loraline.protocol import Delivery, Status, LineReader, decode_line

ok = lambda m: print(f"  ok  {m}")

# ---------- sequence + acks ----------
assert p.seq_newer(5,3) and not p.seq_newer(3,5) and p.seq_newer(1,65530)
assert p.decode_acks(p.encode_acks({'aa':4,'bb':9})) == {'aa':4,'bb':9}
ok("sequence wraparound and per-peer ack encoding")

# ---------- the bus ----------
class Node:
    def __init__(self, name, passphrase, bus):
        self.identity = Identity()
        self.keyring = Keyring(self.identity, passphrase)
        self.session = Session(name, self.identity, self.keyring,
                               payload_budget=p.payload_budget(self.keyring), now=0)
        self.reader = LineReader()
        self.events = []
        self.bus = bus; bus.nodes.append(self)
    @property
    def addr(self): return self.identity.address

class Bus:
    """Every node hears every packet, which is what a radio channel actually is."""
    def __init__(self, drop=0): self.nodes = []; self.drop = drop; self.n = 0
    def step(self, now, senders=None):
        for node in (senders or self.nodes):
            frames, _ = node.session.drain(now, lambda f, d: True)
            for frame, dst in frames:
                wire = p.seal(frame, node.keyring, dst)
                self.n += 1
                if self.drop and self.n % self.drop == 0:
                    continue
                raw = wire.encode() + bytes([0xA8])
                for other in self.nodes:
                    if other is node: continue
                    for line, rssi in other.reader.feed(raw):
                        got = decode_line(line, other.keyring)
                        if got is None: continue
                        got.rssi_raw = rssi
                        other.events += other.session.on_frame(got, now)

bus = Bus()
a = Node("hank", "our-group-key", bus)
b = Node("dave", "our-group-key", bus)
c = Node("mira", "our-group-key", bus)

t = 1000.0
for _ in range(3):            # hello, then presence
    bus.step(t); t += 1
for n in (a, b, c):
    assert len(n.session.peers) == 2, (n.session.nick, len(n.session.peers))
    assert all(pe.known_key for pe in n.session.peers.values())
    assert {pe.nick for pe in n.session.peers.values()} == {"hank","dave","mira"} - {n.session.nick}
ok("three nodes discover each other, exchange keys and learn nicks")

# ---------- group message, per-recipient delivery ----------
t += 1
a.session.compose("evening both", GROUP, t)
item = a.session.outgoing(1)
assert item.recipients == {b.addr, c.addr}
bus.step(t, [a]); t += 1
assert item.resolve() is Delivery.SENT and item.tally() == " 0/2"
assert b.session.conversations[GROUP].entries[-1].text == "evening both"
assert c.session.conversations[GROUP].entries[-1].text == "evening both"
ok("group message reaches both peers")

bus.step(t, [b]); t += 1                     # only dave acks so far
assert item.resolve() is Delivery.PARTIAL and item.tally() == " 1/2", item.tally()
bus.step(t, [c]); t += 1
assert item.resolve() is Delivery.DELIVERED and item.tally() == " 2/2"
ok("delivery goes sent -> partial 1/2 -> delivered 2/2")

# ---------- direct message privacy ----------
t += 1
a.session.compose("just between us", b.addr, t)
frames, _ = a.session.drain(t, lambda f, d: True)
dm = [(f, d) for f, d in frames if f.type == "M"][0]
wire = p.seal(dm[0], a.keyring, dm[1])
assert wire.type == "E", "direct messages must be sealed"
assert b"just between us" not in wire.encode()

# mira's radio receives it and she cannot read it
for line, _ in c.reader.feed(wire.encode()):
    got = decode_line(line, c.keyring)
    if got is not None:
        c.session.on_frame(got, t)
assert not any("just between us" in e.text for e in c.session.conversations[GROUP].entries)
assert all(b.addr not in conv for conv in [])  # no leak into any of mira's conversations
assert not any("just between us" in e.text
               for conv in c.session.conversations.values() for e in conv.entries)
assert c.keyring.failures >= 1
# dave can
for line, _ in b.reader.feed(wire.encode()):
    got = decode_line(line, b.keyring)
    if got is not None:
        b.session.on_frame(got, t)
assert b.session.conversations[a.addr].entries[-1].text == "just between us"
ok("direct message readable by its recipient, opaque to the third person")

# ---------- an outsider with the right radio and no key ----------
outsider = Keyring(Identity(), None)
outside_reader = LineReader()
group_wire = p.seal(p.message(a.addr, GROUP, 9, -1, 0, 1, "group chatter"),
                    a.keyring, GROUP)
seen = outside_reader.feed(wire.encode()) + outside_reader.feed(group_wire.encode())
assert len(seen) == 2, "an outsider still sees that packets exist"
assert all(decode_line(line, outsider) is None for line, _ in seen)
assert outsider.failures == 2
ok("someone in range without the key reads neither group nor direct traffic")

# ---------- separate conversation histories ----------
# a conversation exists per contact from the moment keys are exchanged,
# empty until used -- the MSN contact list, not a list of open windows
assert set(b.session.conversations) == {GROUP, a.addr, c.addr}
assert len(b.session.conversations[a.addr].entries) == 1
assert len(b.session.conversations[c.addr].entries) == 0
assert len(b.session.conversations[GROUP].entries) == 1
ok("group and direct histories stay separate; a conversation per contact")

# ---------- typing is per conversation ----------
t += 1
a.session.on_keystroke(True, b.addr, t)
bus.step(t, [a]); t += 1
assert b.session.typing_in(a.addr, t) == ["hank"]
assert b.session.typing_in(GROUP, t) == []
assert c.session.typing_in(GROUP, t) == []
ok("typing indicator shows only in the conversation it belongs to")

# ---------- store and forward with a peer down ----------
t += 300
for n in (a, b, c): n.session.tick(t)
assert a.session.peers[b.addr].status is Status.OFFLINE
a.session.compose("you around?", b.addr, t)
frames, _ = a.session.drain(t, lambda f, d: True)
assert not [f for f, d in frames if f.type == "M"]
assert a.session.queued_count() >= 1
bus.step(t, [b]); t += 1                    # dave comes back
frames, _ = a.session.drain(t, lambda f, d: True)
assert [f for f, d in frames if f.type == "M"]
ok("direct message waits for its recipient, then flushes")

# ---------- group send with one member absent ----------
t += 400
for n in (a, b, c): n.session.tick(t)
bus.step(t, [b]); t += 1                    # only dave is back
a.session.compose("anyone?", GROUP, t)
item = a.session.outgoing(a.session.group_seq)
assert item.recipients == {b.addr}, "absent members are not counted as recipients"
ok("group delivery counts only who was actually there")

# ---------- fragmentation over a lossy encrypted link ----------
bus2 = Bus(drop=4)
x = Node("x", "k", bus2); y = Node("y", "k", bus2)
tt = 0.0
for _ in range(6): bus2.step(tt); tt += 1
long_text = "the antenna is in the attic now and reception is much better " * 4
x.session.compose(long_text, GROUP, tt)
frames, _ = x.session.drain(tt, lambda f, d: True)
assert len([f for f, d in frames if f.type == "M"]) > 1
for i in range(14):
    tt += 95
    for n in (x, y): n.session.tick(tt)
    bus2.step(tt)
assert any(e.text == long_text for e in y.session.conversations[GROUP].entries), \
    [e.text[:40] for e in y.session.conversations[GROUP].entries]
ok("fragmented encrypted message survives a link dropping 1 in 4")

# ---------- address forgery ----------
victim = Node("victim", "k", Bus())
liar = Identity()
assert not victim.keyring.learn("000000", liar.public_b64, liar.verify_b64), \
    "address must match the keys"
assert victim.keyring.learn(liar.address, liar.public_b64, liar.verify_b64)
ok("a node cannot claim an address that does not match its keys")

# an address covers both halves, so a real encryption key cannot be paired
# with a signing key somebody invented
other = Identity()
assert not victim.keyring.learn(liar.address, liar.public_b64, other.verify_b64)
ok("an invented signing key cannot be attached to a real address")

print()
from loraline.transport import RadioConfig
kr = Keyring(Identity(), "k")
for sf in (7, 10, 12):
    cfg = RadioConfig(sf=sf)
    plain = p.message("a1b2c3", GROUP, 1, -1, 0, 1, "x" * 40)
    enc = p.seal(plain, kr, GROUP)
    print(f"  SF{sf}: 40 chars = {cfg.airtime_of(plain.size):7.0f} ms clear, "
          f"{cfg.airtime_of(enc.size):7.0f} ms encrypted")
print(f"\n  payload budget: {p.payload_budget()} B clear, {p.payload_budget(kr)} B encrypted")
print("\nALL PASS")
