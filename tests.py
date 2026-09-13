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

# ---------- a scratch field for whatever rides on loraline ----------
bus4 = Bus()
one4 = Node("one", "k", bus4)
two4 = Node("two", "k", bus4)
t4 = 0.0
for _ in range(3):
    t4 += 1; bus4.step(t4, [one4, two4])

one4.session.set_app_state("@31.14c")
t4 += 1; bus4.step(t4, [one4, two4])
assert two4.session.peers[one4.addr].app == "@31.14c"
ok("an application's state rides on the heartbeat that was going out anyway")

one4.session.set_psm("back in five")
one4.session.set_app_state("@32.14!:pike:94")
t4 += 1; bus4.step(t4, [one4, two4])
seen4 = two4.session.peers[one4.addr]
assert seen4.psm == "back in five" and seen4.app == "@32.14!:pike:94"
ok("and does not clobber what the person typed as their personal message")

plain = p.presence(one4.addr, Status.ONLINE, {})
carried = p.presence(one4.addr, Status.ONLINE, {}, app="@31.14c")
cfg4 = RadioConfig(sf=7)
kr4 = Keyring(Identity(), "k")
cost = (cfg4.airtime_of(p.seal(carried, kr4, GROUP).size)
        - cfg4.airtime_of(p.seal(plain, kr4, GROUP).size))
assert cost < 40, cost
ok(f"which costs {cost:.0f} ms on a frame that was already being sent")


# ---------- finding the radio without being told where it is ----------
import types as _types
from loraline import detect as _detect, settings as _settings

class _Port:
    def __init__(self, device, description="", manufacturer="", product=""):
        self.device, self.description = device, description
        self.manufacturer, self.product = manufacturer, product

_fake = [_Port("/dev/cu.Bluetooth-Incoming-Port", "n/a"),
         _Port("/dev/tty.usbserial-0001", "USB Serial"),
         _Port("/dev/cu.usbserial-0001", "USB Serial", "1a86"),
         _Port("/dev/cu.usbmodem14201", "Some Board")]
_lp = _types.ModuleType("serial.tools.list_ports")
_lp.comports = lambda: _fake
_tools = sys.modules.setdefault("serial.tools", _types.ModuleType("serial.tools"))
_tools.list_ports = _lp
sys.modules["serial.tools.list_ports"] = _lp

import unittest.mock as _mock
with _mock.patch.object(sys, "platform", "darwin"):
    seen = _detect.candidates()
    names = [f.port for f in seen]
assert not any("Bluetooth" in n for n in names)
assert not any("/dev/tty." in n for n in names), "the tty twin blocks for ever on macOS"
assert names[0] == "/dev/cu.usbserial-0001", names
ok("the serial ports are narrowed and sorted without anybody being asked")

_answers = {"/dev/cu.usbserial-0001"}
_real_answers = _detect.answers
_detect.answers = lambda port, wait=0.4: port in _answers
with _mock.patch.object(sys, "platform", "darwin"):
    assert _detect.only_one().port == "/dev/cu.usbserial-0001"
    _answers.add("/dev/cu.usbmodem14201")
    assert _detect.only_one() is None, "two radios means the person chooses"
    _answers.clear()
    _fake[:] = [_Port("/dev/cu.usbserial-0001", "USB Serial")]
    assert _detect.only_one().port == "/dev/cu.usbserial-0001", "one port is worth a try"
    _fake[:] = []
    assert _detect.only_one() is None
_detect.answers = _real_answers
ok("one answer is picked, two are offered, none is admitted to")

# A module that answers nothing is still very probably the radio. These are
# transparent bridges: what you write goes over the air, so there is nothing
# on the far end that owes you an OK. Treating silence as a fault threw away a
# working radio while the terminal client was talking on the same port.
_fake[:] = [_Port("/dev/cu.usbmodem5B610966611", "USB Serial"),
            _Port("/dev/cu.usbmodem5B610966561", "USB Serial")]
_detect.answers = lambda port, wait=0.4: False
with _mock.patch.object(sys, "platform", "darwin"):
    silent = _detect.find()
assert len(silent) == 2 and not any(f.answered for f in silent)
assert all(f.port for f in silent), "both are still offered"
_detect.answers = _real_answers
ok("and two silent ports are both still offered, because silence is normal")

# ---------- what the app remembers ----------
import tempfile as _tmp, os as _os
with _tmp.TemporaryDirectory() as _room:
    where = _os.path.join(_room, "settings.json")
    assert not _settings.load(where).ready
    kept = _settings.Settings(nick="hank", passphrase="the usual", band="eu868")
    assert kept.ready and kept.preset()["channel"] == 18
    _settings.save(kept, where)
    back = _settings.load(where)
    assert back.nick == "hank" and back.configured
    assert oct(_os.stat(where).st_mode)[-3:] == "600", "it has a passphrase in it"
    open(where, "w").write("not json")
    assert not _settings.load(where).ready
ok("settings are remembered once, kept private, and survive being corrupted")


# ---------- everything on one radio ----------
from loraline import host as _host

class _Toy(_host.Panel):
    tag, title, route, always = "toy", "Toy", "/toy", True
    def __init__(self):
        self.heard_it, self.ticked, self.orders = [], 0, []
    def heard(self, src, payload): self.heard_it.append((src, payload))
    def tick(self, now): self.ticked += 1
    def handle(self, order): self.orders.append(order)
    def snapshot(self): return {"ticked": self.ticked}
    def page(self): return "<html><body>toy</body></html>"

toy = _Toy()
assert toy.snapshot() == {"ticked": 0}
toy.tick(0.0); toy.heard("abc123", "hello"); toy.handle({"do": "x"})
assert toy.ticked == 1 and toy.heard_it == [("abc123", "hello")]
ok("a panel is four methods, none of them required")

bar = _host.nav_html([toy], "/toy")
assert 'aria-current="page">Toy<' in bar
assert ">chat</a>" in bar
assert "Toy running" in bar, "an always-on panel says so"
bar = _host.nav_html([toy], "/")
assert 'aria-current="page">chat<' in bar
ok("the switcher is built once and knows which page it is on")

# a panel that explodes must not take the radio down with it
class _Broken(_host.Panel):
    tag, title, route = "broken", "Broken", "/broken"
    def tick(self, now): raise RuntimeError("no")
    def snapshot(self): raise RuntimeError("still no")
    def page(self): raise RuntimeError("never")

assert _host.nav_html([_Broken()], "/") .count("<a") == 2
ok("a panel that cannot draw itself is still in the list")


# ---------- staying on ----------
from loraline import service as _service

auto = _service.Autostart()
where = auto.where
assert where.name and where.parent.name, where
body = auto.body()
assert body.strip(), "there has to be something to write"
run = _service.command()
assert run and all(isinstance(a, str) for a in run)
# Whatever the platform writes, the command to start again has to be in it.
assert all(part in body for part in run[:1]), (run, body[:120])
ok(f"autostart is one file, {auto.describe()}")

# a second copy stands down rather than fighting for the port
import socket as _socket
held = _socket.socket()
held.bind(("127.0.0.1", 0))
held.listen(1)
taken = held.getsockname()[1]
assert not _service.only_one(taken), "something is listening there"
held.close()
free = _socket.socket(); free.bind(("127.0.0.1", 0))
spare = free.getsockname()[1]; free.close()
assert _service.only_one(spare)
ok("a second copy finds the first rather than fighting it for the radio")


# ---------- a face belongs to an identity ----------
from loraline import face as _face

me_addr = "a1b2c3"
default = _face.identicon(me_addr)
assert len(_face.unpack(default)) == _face.SIDE ** 2
assert len(_face.colours_of(default)) == 8
assert _face.identicon(me_addr) == default
assert _face.identicon("d4e5f6") != default
# mirrored, because symmetry is what makes a blotch read as a face
px = _face.unpack(default)
S = _face.SIDE
assert all(px[y*S + x] == px[y*S + S-1-x] for y in range(S) for x in range(S // 2))
ok(f"everybody has a face before they set one, {len(default)} characters of it")

marks = {_face.mark(_face.identicon(f"{n:06x}")) for n in range(400)}
assert len(marks) > 395, len(marks)
ok(f"six characters say which picture somebody has: {len(marks)} distinct in 400")

pieces = _face.offer(default)
assert all(len(p) < 110 for p in pieces), max(len(p) for p in pieces)
import random as _random
for shuffled in (pieces, list(reversed(pieces)), _random.sample(pieces, len(pieces))):
    coming = _face.Arriving()
    whole = None
    for piece in shuffled:
        whole = coming.take(piece) or whole
    assert whole == default
ok(f"and the picture itself arrives in {len(pieces)} frames, in any order")

broken = list(pieces)
broken[1] = broken[1][:-4] + "XXXX"
coming = _face.Arriving()
assert not any(coming.take(p) for p in broken), "a damaged piece must not be kept"
assert coming.take("nonsense") is None and coming.take("=bad") is None
ok("a damaged or nonsense piece is thrown away rather than half-kept")

# two faces arriving at once cannot be spliced into a third
other = _face.identicon("d4e5f6")
coming = _face.Arriving()
mixed = []
for a, b in zip(_face.offer(default), _face.offer(other)):
    mixed += [a, b]
result = [coming.take(p) for p in mixed]
assert all(r is None or r in (default, other) for r in result)
ok("and two arriving at once cannot be spliced into one that belongs to nobody")

# it is kept beside the keypair
with _tmp.TemporaryDirectory() as room:
    where = _os.path.join(room, "id.faces.json")
    book = _face.Faces().load(where)
    assert book.own(me_addr) == default, "no picture set means the default"
    book.set_mine(_face.identicon("something else"))
    assert book.learn("d4e5f6", other)
    assert not book.learn("d4e5f6", other), "the same picture twice is not news"
    back = _face.Faces().load(where)
    assert back.mine == book.mine and back.of("d4e5f6") == other
    assert back.of("999999") == _face.identicon("999999")
    ok("a face is kept beside the keypair and read back, theirs and yours")

# the mark rides on a heartbeat; the picture does not
worn = p.presence("a1b2c3", Status.ONLINE, {}, app="@31.14c", face="b5c9b9")
assert worn.field_str(6) == "@31.14c" and worn.field_str(7) == "b5c9b9"
alone = p.presence("a1b2c3", Status.ONLINE, {}, face="b5c9b9")
assert alone.field_str(7) == "b5c9b9", "a face with no app still lands right"
cost = (cfg4.airtime_of(p.seal(worn, kr4, GROUP).size)
        - cfg4.airtime_of(p.seal(p.presence("a1b2c3", Status.ONLINE, {}), kr4, GROUP).size))
assert cost < 60, cost
ok(f"the mark rides on the heartbeat for {cost:.0f} ms; the picture is asked for")


# ---------- an address has to be too long to grind ----------
from loraline.crypto import ADDRESS_BYTES as _BYTES

# Everything here checks a signature against an address, so a keypair whose
# address matches somebody else's is a licence to write in their name. Being
# on a radio is no defence at all: the grinding happens offline with nothing
# transmitted.
#
# It was three bytes. Twenty-four bits is sixteen million addresses and an
# ordinary laptop makes twenty thousand keypairs a second, so a collision was
# about six minutes of work.
assert _BYTES * 8 >= 64, f"{_BYTES * 8} bits is grindable"
made = Identity()
assert len(made.address) == _BYTES * 2
space = 16 ** len(made.address)
a_second = 20000                       # keypairs, measured on an ordinary laptop
years = space / 2 / a_second / 86400 / 365
assert years > 1e6, years
ok(f"an address is {_BYTES*8} bits: {years:.0e} years to grind one, at 20k keys a second")

# and it is still both halves, so a signing key cannot be invented
import base64 as _b64
from loraline.crypto import address_of as _address_of
other = Identity()
mine_public = _b64.b64decode(made.public_b64)
their_verify = _b64.b64decode(other.verify_b64)
assert _address_of(mine_public, their_verify) != made.address
ok("and it is still the hash of both keys, so neither half can be swapped")


# ---------- the app has what it imports ----------
# A missing import inside a rarely-taken branch is invisible until somebody
# takes that branch, and on a build server that is the first thing that
# happens. Compiling every module catches it in a second.
import py_compile as _pyc
import pathlib as _path
for _module in sorted(_path.Path("loraline").glob("*.py")):
    _pyc.compile(str(_module), doraise=True)
import importlib as _imp
for _name in ("app", "service", "host", "face", "detect", "settings"):
    _imp.import_module(f"loraline.{_name}")
ok("every module compiles and imports on its own")


# ---------- the open channel ----------
from loraline import settings as _open

# You cannot agree a phrase with somebody you have not met, so there is one
# everybody knows. It is not private and must never be arrived at by accident.
assert not _open.Settings().open_channel, "a fresh install is not on it"
assert not _open.Settings(passphrase="the usual").open_channel
assert _open.Settings(passphrase=_open.OPEN_PHRASE).open_channel
assert _open.is_open("  " + _open.OPEN_PHRASE + "  "), "stray spaces should not matter"
ok("the open channel is a phrase anybody knows, and never the default")

plain = _host.nav_html([toy], "/", False)
loud = _host.nav_html([toy], "/", True)
assert '<div id="loraline-open">' not in plain
assert '<div id="loraline-open">' in loud
assert "read this" in loud
ok("and every page says so while you are on it, not a setting you chose once")


# ---------- a slower beat in a crowd does not slow anything down ----------
from loraline.session import CROWD_S as _CROWD

crowd = Session("hank", Identity(), kr4)
class _Here:
    status = Status.ONLINE
crowd.peers = {n: _Here() for n in range(19)}
assert crowd.beat_gap() >= 300, crowd.beat_gap()
crowd.last_heartbeat = 1000.0
crowd._force_heartbeat = False
assert not (1001 - crowd.last_heartbeat >= crowd.beat_gap()), "sitting still waits"
# Anything that changes goes out at once. A position nobody has heard about is
# not a position, so the crowd gap must not delay one.
crowd.set_app_state("@12.7c")
assert crowd._force_heartbeat, "a change has to send immediately"
ok(f"a crowd of twenty beats every {crowd.beat_gap():.0f} s, and a change still goes at once")

print("\nALL PASS")
