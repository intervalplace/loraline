"""Norway on LoRa, England on TCP, one machine bridging. Real sockets."""
import sys, time, threading
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from loraline import protocol as p
from loraline.crypto import GROUP, Identity, Keyring
from loraline.client import Client
from loraline.protocol import Delivery
from loraline.transport import (Interface, Link, Packet, RadioConfig,
                                TCPClientInterface, TCPServerInterface)

ok = lambda m: print(f"  ok  {m}")
PORT = 47811

class FakeLoRa(Interface):
    """A shared radio channel: everyone tuned to it hears every packet."""
    metered = True
    ether: list = []
    def __init__(self):
        super().__init__(); self.name = "lora"; FakeLoRa.ether.append(self)
        self.config = RadioConfig(sf=10, channel=65)
    def send(self, line):
        for other in FakeLoRa.ether:
            if other is not self:
                other.inbox.put(Packet(line.rstrip(b"\n"), "lora", 0xA8))
        return 0.0
    def status(self): return "lora"

def make(nick, ifaces, passphrase="our-key"):
    ident = Identity(); kr = Keyring(ident, passphrase)
    for i in ifaces: i.start()
    link = Link(ifaces, keyring=kr)
    return Client(link, ident, kr, nick=nick)

# hank in Norway: radio + a listening socket. dave next door: radio only.
# will in England: TCP only, no radio at all.
hank = make("hank", [FakeLoRa(), TCPServerInterface(port=PORT)])
dave = make("dave", [FakeLoRa()])
time.sleep(0.3)
will = make("will", [TCPClientInterface("127.0.0.1", PORT)])

def settle(seconds=2.0):
    end = time.time() + seconds
    while time.time() < end:
        for c in (hank, dave, will): c.pump()
        time.sleep(0.05)

settle(3)
for c, name in ((hank,"hank"), (dave,"dave"), (will,"will")):
    others = {p.nick for p in c.session.peers.values()}
    assert others == {"hank","dave","will"} - {name}, (name, others)
ok("all three discover each other across the radio/TCP boundary")
assert hank.link.forwarded > 0
ok(f"the bridge relayed {hank.link.forwarded} envelopes between bearers")

# dave (radio only) talks to the group; will (TCP only) must receive it
dave.session.compose("evening from the radio side", GROUP, time.time())
settle(2)
texts = [e.text for e in will.session.conversations[GROUP].entries]
assert "evening from the radio side" in texts, texts
ok("radio-only peer reaches TCP-only peer through the bridge")

will.session.compose("and from England", GROUP, time.time())
settle(2)
texts = [e.text for e in dave.session.conversations[GROUP].entries]
assert "and from England" in texts, texts
ok("and back the other way")

# the point: a DM between dave and will passes through hank, unreadable
before = hank.keyring.failures
dave.session.compose("just between us two", will.session.address, time.time())
settle(3)
got = [e.text for e in will.session.conversations[dave.session.address].entries]
assert "just between us two" in got, got
leaked = [e.text for conv in hank.session.conversations.values()
          for e in conv.entries if "just between us" in e.text]
assert not leaked, leaked
assert hank.keyring.failures > before
ok("the bridge forwards a direct message it cannot itself read")

# delivery confirmation crosses the boundary too
item = dave.session.outgoing(dave.session.seq)
settle(3)
assert item.resolve() in (Delivery.DELIVERED, Delivery.PARTIAL, Delivery.SENT)
dave.session._force_heartbeat = True; will.session._force_heartbeat = True
settle(3)
assert item.resolve() is Delivery.DELIVERED, item.resolve()
ok("delivery acknowledgement travels back across both bearers")

for c in (hank, dave, will): c.link.close()
print("\nBRIDGE PASS")
