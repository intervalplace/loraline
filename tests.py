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
    # It has a passphrase in it, so on anything with POSIX modes it is kept to
    # the one account. Windows has no such mode: chmod there is close to a
    # no-op, the file is protected by the profile directory's ACL instead, and
    # this used to fail the build on that leg alone.
    if _os.name == "posix":
        assert oct(_os.stat(where).st_mode)[-3:] == "600", "it has a passphrase in it"
    open(where, "w", encoding="utf-8").write("not json")
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
assert _face.identicon(me_addr) == default, "the same address, the same one"
# A hashed blotch is unique and says nothing. These are the MSN habit: a small
# set of things, and you are one of them until you decide otherwise.
assert default in _face.CREATURES
assert len(_face.CREATURES) == len(_face.CREATURE_NAMES)
_spread = {_face.creature_of("%016x" % n) for n in range(400)}
assert _spread == set(_face.CREATURE_NAMES), sorted(set(_face.CREATURE_NAMES) - _spread)
ok(f"everybody starts as one of {len(_face.CREATURES)} creatures: "
   f"{', '.join(_face.CREATURE_NAMES[:4])} and so on")

# each one has to be a whole picture, and use the colours it says it does
for _art in _face.CREATURES:
    _px = _face.unpack(_art)
    assert len(_px) == _face.SIDE ** 2
    assert len(set(_px)) >= 3, "a creature with two colours is a blotch"
    assert max(_px) < 8
ok("and each is a full picture in the eight colours that travel with it")

# The mark says which picture, not which person. Two people who are both the
# duck have the same mark and never trade pictures, which is exactly right:
# there is nothing to send.
import random as _rand
_rand.seed(7)
_pictures = {_face.pack([_rand.randrange(8) for _ in range(_face.SIDE ** 2)])
             for _ in range(400)}
_marks = {_face.mark(p) for p in _pictures}
assert len(_marks) > 395, len(_marks)
assert len({_face.mark(a) for a in _face.CREATURES}) == len(_face.CREATURES)
ok(f"six characters say which picture: {len(_marks)} distinct in 400")

_same = {_face.mark(_face.identicon("%016x" % n)) for n in range(400)}
assert len(_same) == len(_face.CREATURES)
ok("and two people who are both the duck never trade pictures, having the same one")

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


# ---------- one band table ----------
from loraline.settings import BANDS as _BANDS
from loraline.__main__ import BANDS as _CLI_BANDS
from loraline.protocol import DutyBudget as _Duty

# There were two of these and they disagreed about what the number meant:
# duty=1.0 was a one percent limit in one and no limit at all in the other.
assert _BANDS is _CLI_BANDS, "the terminal and the app must read the same table"
assert _Duty(_BANDS["eu868"]["duty"]).remaining_ms() == 36000.0, "EU is 1% of the hour"
assert _Duty(_BANDS["us915"]["duty"]).remaining_ms() == float("inf")
assert _Duty(_BANDS["au915"]["duty"]).remaining_ms() == float("inf")
for _name, _b in _BANDS.items():
    assert _b["power_dbm"] == _b["power"], _name
    assert _b.get("note") and _b.get("label"), _name
ok("one band table: EU is 36 s an hour, the 915 bands have no limit")


# ---------- one bad tick must not take the node down ----------
class _Breaks:
    tag, title, route, always = "bad", "Bad", "/bad", True
    def start(self, host): pass
    def heard(self, src, payload): pass
    def tick(self, now): raise RuntimeError("every tick")
    def handle(self, order): pass
    def owns(self, path): return False
    def page(self, path=""): return ""
    def snapshot(self): raise RuntimeError("cannot describe myself")

from loraline.app import App as _App
_broken = _App(port=0)
_broken.panels = [_Breaks()]
# Nothing in the loop was guarded, so one exception anywhere ended it, run()
# returned, the process exited, and the window went to connection refused.
for _ in range(3):
    _broken.one_turn()          # must not raise
# and a panel that cannot describe itself says so rather than handing the page
# an empty object to draw "undefined" from
class _Up:
    pass
_broken.client = _Up()
_broken.client.session = _Up()
_broken.client.session.peers = {}
try:
    shown = _broken.snapshot()
    assert "broken" in shown.get("bad", {}), shown.get("bad")
except AttributeError:
    pass                        # a half-built client is not what this tests
ok("a panel that throws every tick cannot stop the node running")

# and the style element must not share the id of the bar it styles
_bar = _host.nav_html([toy], "/")
import re as _re2
_ids = _re2.findall(r'id="([^"]+)"', _bar)
assert len(_ids) == len(set(_ids)), _ids
ok("the navigation stylesheet has an id of its own, so it stays invisible")


# ---------- the app shows the same ticks as the terminal ----------
import re as _re3
_page = open("loraline/app.py", encoding="utf-8").read()
_start = _page.index("const TICK")
_block = _page[_start:_page.index("\n};", _start)]
_keys = set(_re3.findall(r"^  (\w+):", _block, _re3.M))
# It was keyed on single letters while the session reports whole words, so the
# lookup missed every time and no tick has ever appeared in the app.
assert {d.value for d in Delivery} == _keys, {d.value for d in Delivery} ^ _keys
ok(f"every delivery state has a tick in the app: {', '.join(sorted(_keys))}")


# ---------- people you have met, and people you have checked ----------
with _tmp.TemporaryDirectory() as room:
    shelf = _os.path.join(room, "peers.json")
    mine = Identity()
    ring = Keyring(mine, "x", keystore=shelf)
    one, two = Identity(), Identity()
    ring.learn(one.address, one.public_b64, one.verify_b64)
    ring.learn(two.address, two.public_b64, two.verify_b64)
    ring.remember_nick(one.address, "hank")
    ring.saw(one.address, 2000.0)
    ring.saw(two.address, 1000.0)
    # Keys are trusted on first contact, so a stranger who got there before
    # the real person is indistinguishable from them. This is the one claim
    # that settles it, and only the person at the screen can make it.
    ring.check_off(one.address)

    back = Keyring(mine, "x", keystore=shelf)
    assert back.remembered() == [one.address, two.address], "newest heard first"
    assert back.is_checked(one.address) and not back.is_checked(two.address)
    assert back.nicks.get(one.address) == "hank"
    ok("who you have met, when you heard them and who you checked all survive a restart")

    back.check_off(one.address, False)
    assert not Keyring(mine, "x", keystore=shelf).is_checked(one.address)
    # and it cannot be claimed for somebody never met
    stranger = Identity()
    back.check_off(stranger.address)
    assert not back.is_checked(stranger.address)
    ok("a tick can be taken back, and cannot be put against somebody unknown")

    # Unticking somebody takes them off the list and never forgets their key.
    # Forgetting a key is what lets somebody else take that address later, so
    # the keystore keeps everybody while the list keeps only the checked.
    assert two.address in back.remembered() and one.address in back.remembered()
    assert not back.is_checked(two.address)
    ok("and the keystore keeps everybody's keys whether or not they are listed")


# ---------- a presence must not take the turn down ----------
from loraline.session import PresenceEvent as _Presence
import dataclasses as _dc

# PresenceEvent is an empty marker meaning the roster changed. The app read
# event.text off it, which it has never had, so every presence raised and
# aborted the turn before the snapshot was published: the peer was in the
# session the whole time and the page was never told.
assert not _dc.fields(_Presence) if _dc.is_dataclass(_Presence) else True
_quiet = _App(port=0)
_quiet.absorb(_Presence())          # must not raise
ok("a presence event carries nothing, and absorbing one says nothing")


# ---------- what the radio has heard ----------
from loraline.transport import Link as _Link
_counted = _Link([], kr4)
assert _counted.frames_heard == 0 and _counted.frames_foreign == 0
# Three faults look identical from outside: nothing heard at all is the radios
# not reaching each other, frames heard but none decoding is the wrong
# passphrase, and frames decoding with nobody appearing is a fault in here.
assert hasattr(_counted, "frames_heard") and hasattr(_counted, "frames_foreign")
ok("the link counts what it hears and what would not decode, always")


# ---------- the palette has to survive its own packing ----------
_there_and_back = _face._unpack_palette(_face._pack_palette(_face.DEFAULT_PALETTE))
# Four bits a channel, so only multiples of seventeen come back unchanged. The
# paper was #f4efe4 and returned as #ffeeee, which is pink, so every creature
# sat on a pink square.
assert list(_face.DEFAULT_PALETTE) == list(_there_and_back), _there_and_back
ok("the default palette comes back out of the packing exactly as it went in")


# ---------- which build is running ----------
import loraline as _pkg
assert _pkg.__version__ and _pkg.build_id()
assert len(_pkg.build_id()) == 6
# Two sessions went on bugs that were already fixed, because nothing in the
# window said whether the code running was the code just downloaded.
_marked = _App(port=0)
assert "version" not in _marked.snapshot() or _marked.client is None
ok(f"the build says what it is: {_pkg.__version__}, build {_pkg.build_id()}")


import inspect as _inspect
from loraline import transport as _transport

# ---------- a link reads from what it was given ----------
from loraline.transport import Link as _L, Interface as _I

class _Counting(_I):
    def __init__(self):
        super().__init__()
        self.name, self.started = "toy", 0
    def start(self):
        self.started += 1

# Opening a port and reading from it were two calls, and the app made only the
# first. Sending writes to the port directly, so everything it said went out
# and was heard, while nothing came back: the reader thread did not exist.
_one, _two = _Counting(), _Counting()
_L([_one, _two], keyring=None)
assert _one.started == 1 and _two.started == 1, (_one.started, _two.started)
ok("a link starts every bearer it is handed, so a reader cannot be forgotten")

# and the terminal client starts them too, so starting must be harmless twice
_src = _inspect.getsource(_transport.LoRaInterface.start)
assert "is_alive" in _src, "starting twice would make a second reader thread"
ok("and starting one that is already reading does nothing")


# ---------- a conversation reads in the order it was said ----------
from loraline.session import MessageEvent as _Msg

_reading = _App(port=0)
def _said(text, seq, when, who="test", mine=False):
    _reading.absorb(_Msg(convo="", who=who, text=text, incoming=not mine,
                         seq=seq, when=when, colour=0, src=who))

_said("hello", 1, 100)
_said("hello2", 2, 110)
_said("how are you", 3, 180)
_said("i am fine", 1, 240, who="mats", mine=True)

# A sender retransmits until it is acknowledged and each attempt is sealed
# afresh, so the link cannot tell two tries apart by their bytes.
_said("how are you", 3, 180)
assert [m["text"] for m in _reading.threads[""]].count("how are you") == 1
ok("a message that arrives twice is only shown once")

# Anything said while you were out of range arrives when somebody comes back,
# long after it was written, and a thread in arrival order does not read.
_said("are you there", 0, 60)
_order = [m["text"] for m in _reading.threads[""]]
assert _order[0] == "are you there", _order
assert [m["when"] for m in _reading.threads[""]] == sorted(
    m["when"] for m in _reading.threads[""])
ok("and one that arrives late sits where it was said, not where it landed")


# ---------- a name is worked out when it is drawn ----------
_naming = _App(port=0)
_stranger = Identity()
_naming.absorb(_Msg(convo="", who=_stranger.address, text="hei", incoming=True,
                    seq=1, when=100, colour=0, src=_stranger.address))
# A message kept whatever name was known the moment it arrived, so anything
# said before their nick turned up stayed addressed to sixteen hex characters.
assert _naming.name_for(_stranger.address, _stranger.address) == _stranger.address
assert len(_App.short(_stranger.address)) == 9, "a long address does not fit a sidebar"
assert _App.short("abcd") == "abcd", "a short one is left alone"
ok("an unknown name shortens to eight characters, and is resolved when drawn")


# ---------- a window of its own ----------
from loraline import window as _win
import os as _os2

_was = _os2.environ.pop("LORALINE_WINDOW", None)
_nb = _os2.environ.pop("LORALINE_NO_BROWSER", None)
try:
    assert _win.wanted(), "a window by default"
    _os2.environ["LORALINE_WINDOW"] = "0"
    assert not _win.wanted(), "and the browser when asked for"
    del _os2.environ["LORALINE_WINDOW"]
    # A build server has neither a web view nor a browser, and trying for
    # either is how a build hangs instead of failing.
    _os2.environ["LORALINE_NO_BROWSER"] = "1"
    assert not _win.wanted()
finally:
    _os2.environ.pop("LORALINE_WINDOW", None)
    _os2.environ.pop("LORALINE_NO_BROWSER", None)
    if _was is not None:
        _os2.environ["LORALINE_WINDOW"] = _was
    if _nb is not None:
        _os2.environ["LORALINE_NO_BROWSER"] = _nb
ok("a window of its own by default, the browser on request, neither on a build server")

# the server and the loop have to be separable, because a web view owns the
# main thread on macOS and the node has to turn beside it
assert hasattr(_App, "prepare") and hasattr(_App, "loop")
_split = _App(port=0)
assert _split.prepare.__doc__ and _split.loop.__doc__
ok("and the node can be started and turned separately, so a window can own the main thread")


# ---------- every panel serves its own page ----------
class _NoPath:
    tag, title, route, always = "old", "Old", "/old", False
    def start(self, host): pass
    def heard(self, src, payload): pass
    def tick(self, now): pass
    def handle(self, order): pass
    def owns(self, path): return path == "/old"
    def page(self): return "<html>the old shape</html>"   # takes no route
    def snapshot(self): return {}

# loraline passes the route it matched. A panel that would not accept it
# raised a TypeError the host swallowed, and the chat page was served in its
# place: a switcher that looked dead rather than broken.
_hosting = _App(port=0)
_hosting.panels = [_NoPath()]
_served = _hosting.page_for("/old")
assert "could not draw its page" in _served, _served[:200]
assert "TypeError" in _served
ok("a panel whose page will not take the route says so, instead of serving another")


# ---------- everything the app accepts can be reached from its page ----------
import re as _re4
_source = open("loraline/app.py", encoding="utf-8").read()
_handled = set(_re4.findall(r'what == "(\w+)"', _source))
# The page is a Python string, so an order inside a template literal has its
# quotes escaped: do:\'unface\' rather than do:'unface'.
_sendable = set(_re4.findall(r"do:\s*\\?'(\w+)", _source))
_sendable |= set(_re4.findall(r'do:\s*\\?"(\w+)', _source))
# The app grew handlers for a status message and for away and busy that
# nothing on the page could ever send: the terminal had them and the window
# did not, and nobody would guess that from looking.
_only_by_hand = _handled - _sendable - {"forget"}
assert not _only_by_hand, f"no way to reach: {sorted(_only_by_hand)}"
ok(f"every order the app handles has something on the page that sends it")


# ---------- every file is read as utf-8, wherever this runs ----------
import ast as _ast
import pathlib as _pl

# Python's open() uses the locale encoding on Windows, not utf-8, so a file
# with "æ" in it came back as "Ã¦" and a test comparing source text failed on
# the build machine and nowhere else.
#
# Checked against the parse tree rather than the text, because a line-by-line
# search called a wrapped call unencoded and missed one where the arguments
# had brackets of their own.
_TEXTY = {"read_text", "write_text", "open"}
_unencoded = []
for _py in sorted(_pl.Path("loraline").rglob("*.py")) + [_pl.Path("tests.py")]:
    _tree = _ast.parse(_py.read_text(encoding="utf-8"))
    for _node in _ast.walk(_tree):
        if not isinstance(_node, _ast.Call):
            continue
        _name = getattr(_node.func, "attr", getattr(_node.func, "id", ""))
        if _name not in _TEXTY:
            continue
        if any(k.arg == "encoding" for k in _node.keywords):
            continue
        # binary opens carry their own answer, and a socket is not a file
        _modes = [a.value for a in _node.args
                  if isinstance(a, _ast.Constant) and isinstance(a.value, str)]
        if any("b" in m for m in _modes):
            continue
        if _name == "open" and not isinstance(_node.func, _ast.Name):
            continue          # somebody else's open(), not the builtin
        _unencoded.append(f"{_py}:{_node.lineno}")
assert not _unencoded, f"reads without an encoding: {_unencoded}"
ok("every file this reads or writes says utf-8, so Windows agrees with everywhere else")


# ---------- a packaged build has to know which build it is ----------
import loraline as _pkg2
# A frozen app has no .py files on disk, so the digest was of an empty hash:
# the same six characters for every release, in the one place it matters.
assert _pkg2.stamp_of_sources() != "unstamped", "there are sources here"
_empty = __import__("hashlib").blake2b(digest_size=3).hexdigest()
assert _pkg2.build_id() != _empty, "that is the digest of nothing at all"
assert (_pl.Path("packaging") / "stamp.py").exists(), \
    "packaging writes the stamp in before freezing"
ok(f"the build says which build it is: {_pkg2.build_id()}, not the digest of nothing")


# ---------- the bundle has to contain the window ----------
_spec = open("loraline.spec", encoding="utf-8").read()
_hidden = _spec[_spec.index("hiddenimports"):_spec.index("hookspath")]
_excluded = _spec[_spec.index("excludes="):_spec.index("win_no_prefer")]
# webview belongs in hiddenimports and was listed in excludes instead, which
# told the bundle to leave out the very thing that gives it a window, so every
# build quietly opened a browser and nothing anywhere said why.
assert "webview" in _hidden, "the web view has to be bundled"
assert "webview" not in _excluded, "and must not be excluded from it"
for _backend in ("cocoa", "edgechromium", "gtk"):
    assert _backend in _hidden, _backend
ok("the packaged app bundles the web view, and every platform's backend")

# and a build that cannot name itself is no use, so it falls back to the
# bundle rather than saying "unstamped" and leaving you none the wiser
assert "sys.executable" in open("loraline/__init__.py", encoding="utf-8").read()
ok("a frozen build works its id out from the bundle, having no sources to hash")


# ---------- every module a file uses is one it imports ----------
import ast as _ast2

# `sys.stderr` was reached only when somebody asked for a window and could not
# have one, so it was a NameError nobody hit until a packaged build did.
for _mod in sorted(_pl.Path("loraline").glob("*.py")):
    _tree = _ast2.parse(_mod.read_text(encoding="utf-8"))
    _have = {a.asname or a.name.split(".")[0]
             for n in _ast2.walk(_tree) if isinstance(n, _ast2.Import)
             for a in n.names}
    _have |= {a.asname or a.name for n in _ast2.walk(_tree)
              if isinstance(n, _ast2.ImportFrom) for a in n.names}
    _have |= {n.name for n in _ast2.walk(_tree)
              if isinstance(n, (_ast2.FunctionDef, _ast2.ClassDef))}
    _have |= {t.id for n in _ast2.walk(_tree)
              if isinstance(n, _ast2.Assign) for t in n.targets
              if isinstance(t, _ast2.Name)}
    # only the standard modules this project actually leans on
    for _name in ("sys", "os", "json", "time", "queue", "threading", "base64",
                  "hashlib", "math", "socket", "webbrowser"):
        _used = any(isinstance(n, _ast2.Attribute) and isinstance(n.value, _ast2.Name)
                    and n.value.id == _name for n in _ast2.walk(_tree))
        assert not (_used and _name not in _have), f"{_mod.name} uses {_name} without importing it"
ok("every file imports the modules it uses, including on paths nobody walks often")


# ---------- the build says which shell it wants ----------
_flow = _pl.Path(".github/workflows/build.yml")
if _flow.exists():
    _yaml = None
    try:
        import yaml as _yaml
    except ImportError:
        pass
    if _yaml is not None:
        _plan = _yaml.safe_load(_flow.read_text(encoding="utf-8"))
        # Windows runners default to PowerShell, where `if [ ... ]; then` and a
        # heredoc are both syntax errors. Some steps said bash and some did
        # not, so this only ever broke on the Windows leg.
        _quiet = [(j, s.get("name")) for j, spec in _plan["jobs"].items()
                  for s in spec.get("steps", [])
                  if s.get("run") and s.get("shell") != "bash"]
        assert not _quiet, f"steps without an explicit shell: {_quiet}"
        ok("every step that runs anything asks for bash, so Windows agrees")


# ---------- and nothing checks a POSIX mode without saying so ----------
_suite = _pl.Path("tests.py").read_text(encoding="utf-8")
# Windows has no POSIX file mode, so a bare st_mode check fails there and
# nowhere else, which is a build that passes twice and fails once.
_lines = _suite.splitlines()
for _n, _line in enumerate(_lines, 1):
    # An assertion about a mode, not this check talking about itself.
    if "st_mode" not in _line or not _line.strip().startswith("assert"):
        continue
    _before = "\n".join(_lines[max(0, _n - 6):_n])
    assert 'os.name == "posix"' in _before, \
        f"line {_n} checks a POSIX mode without asking whether this is POSIX"
ok("a POSIX file mode is only checked where there are POSIX file modes")


# ---------- the app wears its own face ----------
_icns = _pl.Path("packaging/loraline.icns")
_ico = _pl.Path("packaging/loraline.ico")
# Without these the app wears PyInstaller's default, which is a Python logo on
# a floppy disk.
assert _icns.exists() and _ico.exists(), "the icons have to be in the repo"

import struct as _st
_raw = _icns.read_bytes()
assert _raw[:4] == b"icns"
assert _st.unpack(">I", _raw[4:8])[0] == len(_raw), "the icns says its own length"
_at, _kinds = 8, []
while _at < len(_raw):
    _kinds.append(_raw[_at:_at + 4])
    _at += _st.unpack(">I", _raw[_at + 4:_at + 8])[0]
assert b"ic10" in _kinds and b"ic07" in _kinds, _kinds
ok(f"the macOS icon carries {len(_kinds)} sizes, drawn at each")

# PIL's ICO writer scales one source down and will not go above it, so a
# small source quietly gives a one-entry file, as it did for the favicon.
_raw = _ico.read_bytes()
_, _kind, _count = _st.unpack("<HHH", _raw[:6])
assert _kind == 1 and _count >= 5, f"the ico has only {_count} sizes"
ok(f"and the Windows icon carries {_count}, from 16 to 256")

_spec = open("loraline.spec", encoding="utf-8").read()
assert "icon=ICON" in _spec and "loraline.ico" in _spec
ok("and the packaging picks the right one for the platform")

print("\nALL PASS")
