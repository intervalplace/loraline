# loraline

A small-group instant messenger over LoRa. It also runs over TCP, or over
both at once. Built for Waveshare USB-TO-LoRa-HF (SX1262) modules. One USB port, one
command. No server, no account, no pairing, no phone, no firmware flash.

Three to five people is the sweet spot. Everyone shares one group
conversation, and any two can also talk privately. It is MSN's contact list
and separate windows, on a radio channel where everybody hears every packet.

The design borrows from MSN Messenger, but the properties aren't decoration.
Presence, sparseness and ephemerality were what a thin medium produced, and
this is a thin medium again. When the heartbeats stop, your friend really is
gone. It is not a server's opinion about them.

## Install

```
pip install -r requirements.txt
```

That is `pyserial`, `pynacl` for encryption, and on Windows `windows-curses`,
because Python does not ship the terminal interface on that platform.

The interface is standard-library `curses`. There is no UI framework
dependency, deliberately: the whole client is Python plus a serial port, so it
should still run in fifteen years on a machine that's been offline the whole
time.

Find your port: `ls /dev/ttyUSB* /dev/ttyACM*` on Linux, `ls /dev/cu.*` on
macOS, or Device Manager under Ports on Windows. If Linux gives a permissions
error, run `sudo usermod -aG dialout $USER` and log out of your session
completely.

If you have not done anything like this before, the site has a full
walkthrough: <https://loraline.org/start.html>

## Set the band first

The modules ship on channel 18 (868 MHz). **In North America that is licensed
spectrum, not ISM.** Configure before transmitting:

```
python -m loraline config --port /dev/ttyUSB0 --band us915
```

| preset  | frequency | power  | duty cycle  |
|---------|-----------|--------|-------------|
| `eu868` | 868 MHz   | 8 dBm  | 1% enforced |
| `us915` | 915 MHz   | 22 dBm | none        |
| `au915` | 915 MHz   | 22 dBm | none        |

EU power is low on purpose: the limit is ~14 dBm ERP and a 6 dBi antenna adds
gain on top of whatever the radio emits. Run the same command on both modules;
every parameter must match.

## Measure the link before trusting it

Antenna placement matters more than spreading factor, power, or anything else
you can change. So measure it.

```
python -m loraline link --port /dev/ttyUSB0 --band us915 --role pong   # their end
python -m loraline link --port /dev/ttyUSB0 --band us915 --role ping   # your end
```

RSSI, round-trip time and packet loss every few seconds. Move the antenna and
watch. Better than about -110 dBm at SF10 is comfortable. If you can't hear
each other at all, try `--sf 12` on both ends, then walk it back down until it
breaks.

## Chat

```
export LORALINE_KEY="something you both agree on"
python -m loraline chat --port /dev/ttyUSB0 --band us915 --nick hank \
    --psm "listening to something"
```

Commands: `/away` `/busy` `/brb` `/back` `/psm <text>` `/nick <name>`
`/colour <0-7>` `/whois <name>` `/status` `/clear` `/quit`. Ctrl+Q quits,
PgUp/PgDn scrolls back, End returns to the bottom.

## Mixing radio and internet

A node can hold several bearers at once, and one holding both becomes a
bridge:

```
# you, in Norway: radio to your local friends, socket to everyone else
python -m loraline chat --port /dev/ttyUSB0 --band eu868 --nick hank \
    --tcp-listen 4242

# your friend next door: radio only, no internet needed
python -m loraline chat --port /dev/ttyUSB0 --band eu868 --nick dave

# your friend in England: no radio at all, so no band to set either
python -m loraline chat --nick will \
    --tcp-connect your-host.example.com:4242
```

Everyone is in one conversation. The radio-only peer never touches the
internet; the internet-only peer never touches a radio.

Relaying happens on sealed envelopes, before decryption, so the bridge
forwards a direct message between two of its peers **without being able to
read it**. `tests_bridge.py` asserts exactly that, over real sockets.

Loops are prevented by a dedup cache of recently seen envelopes plus never
sending a line back out the interface it arrived on. When a bearer connects,
the node re-announces itself rather than waiting out the heartbeat interval.

Two things to know. Relayed traffic costs airtime on the radio side, so a
busy internet peer eats into your local duty budget, so the status bar shows
a `relayed` counter. And a bearer with nobody connected reports itself unable to
send, so frames queue rather than vanishing.

## Conversations

Tab cycles. `/g` jumps to the group, `/w dave` opens a private chat, unread
counts sit next to each contact in the sidebar. The status bar tells you which
protection the current conversation has: `group key`, `e2e`, or `CLEAR`.

## Two kinds of privacy, because the medium needs both

Every module on the channel receives every packet. A private chat implemented
by having clients politely ignore what isn't theirs would not be private at
all. Anyone in the group with a patched client, or a stranger with picocom,
would read it.

So each install generates an X25519 keypair, stored at `~/.loraline/identity`,
and your address is derived from the public key. Addresses are exchanged
automatically on first contact.

- **Group messages** are encrypted with the shared passphrase. Everyone who
  has it can read them; nobody else can.
- **Direct messages** use a pairwise box between exactly those two
  identities. The third person in the group cannot read them, even though
  their radio received every byte.

Nothing about routing travels in the clear, so an outside listener can't even
tell who a packet is addressed to. Verify a contact with `/whois dave` and
compare the fingerprint out of band. Key exchange over the air is open to an
active impersonator, though not a passive listener.

## What it does

- **Contact list** with per-peer presence, personal message, colour and signal.
- **Group and private conversations**, separate histories, unread counts.
- **Presence** by heartbeat every 60s; a peer goes offline after ~2.5 missed.
  Any received packet is treated as proof of presence. Five minutes idle
  flips you to Away.
- **Per-recipient delivery**: queued `·`, sent `✓`, partial `✓· 1/2`,
  delivered `✓✓ 2/2`, failed `✗`. In a group you can see who has it.
- **Acknowledgements ride on the heartbeat.** There is no dedicated ack
  frame. Presence carries a per-peer high-water mark, so confirmation is
  free. The mark only advances over an unbroken run, and only once a message
  is fully reassembled.
- **Store and forward.** Message someone offline and it queues, then flushes
  when they reappear. Same for airtime exhaustion: the duty cycle becomes a
  delay, not an error.
- **Fragmentation** with reassembly across retries.
- **Retry** three times 90s apart, then a visible failure.
- **Typing indicator**, per conversation, one packet per 5-second window. On a
  half-duplex radio that doubles as collision avoidance.
- **Airtime budget** in the status bar, and a signal history strip in the
  sidebar: the last twenty readings as a sparkline, so you can tell a steady
  link from a drifting one. Block characters, falling back to ASCII on a
  terminal that is not on a UTF-8 locale.

There is deliberately no nudge. Every other feature here reports something
true about the other person: presence, typing, delivery, signal. A nudge
carries no information at all; it only demands attention, and it cannot be
declined. It existed on MSN because by then the online dot had stopped meaning
anything, so you needed a way to check whether someone was really there.
Heartbeats over radio answer that honestly, so the feature has nothing left to
do except cost 200 ms of airtime.
- No logging. Nothing touches disk except the keypair.

## How many people

Airtime is the limit, not the code. Everyone heartbeats once a minute, and any
two transmitting at once lose both packets. At SF10 with no duty limit, five
or six before it degrades. Under the EU 1% cap, three is realistic and four is
optimistic. It is a small-group tool by physics, not by choice.

## Signing, and why an address is two keys

Every identity carries an X25519 keypair for talking privately and an Ed25519
keypair for signing what happened, derived separately from the same stored
secret. An address is the hash of **both** public halves, which is what lets a
stranger check a signature: given the two keys they can recompute the address
themselves, so nobody can pair a real encryption key with a signing key they
invented.

Hello carries both halves, and the peer keystore stores both, so a restart
keeps the ability to verify as well as the ability to talk.

Frames also record which bearer carried them. Being heard on air is a
different claim from being reachable over a socket, and `Session.on_air` is
what applications use to tell the two apart.

## Architecture

```
protocol.py   framing, fragmentation, sequence maths, airtime, duty budget.
              Pure functions, standard library only. Line framing and
              decryption are separate steps so a bridge can relay what it
              cannot read.
crypto.py     identity keypair, group cipher, pairwise boxes, keyring
session.py    roster, conversations, presence, delivery, store-and-forward.
              Frames and clock ticks in, events out. No I/O. Time is
              passed in, never read.
transport.py  pluggable bearers (LoRa serial, TCP server, TCP client) and
              the Link that seals, spreads and bridges between them
client.py     binds them: one pump() per loop iteration
ui_curses.py  a view. Reads events, draws, sends keys back.
```

The split is what makes the rest possible: a second view, a headless relay, or
a test that exercises three minutes of presence timeout instantly, all attach
at `session.py` without touching anything else. `tests.py` runs two sessions
against a simulated lossy radio and needs no hardware.

## The cost of encryption

Real numbers, from `tests.py`, for a 40-character message:

| SF  | clear   | encrypted |
|-----|---------|-----------|
| 7   | 108 ms  | 190 ms    |
| 10  | 657 ms  | 1108 ms   |
| 12  | 2466 ms | 4432 ms   |

The AEAD adds 28 bytes and base64 adds a third on top, which roughly doubles
airtime. Under a 1% duty cycle at SF10 that's about 32 messages an hour
instead of 55. Worth it, but it is not free, and the payload budget shrinks
from 199 to 119 bytes per fragment. Fragmentation accounts for this
automatically.

## Notes and caveats

- Without a key everything is broadcast in the clear and anyone in range with a
  matching module can read it. The client says so at startup and keeps `CLEAR`
  in the status bar.
- The module's own 16-bit "key" setting is not encryption and isn't used here.
- RSSI byte encoding is undocumented by Waveshare; `rssi_dbm()` assumes the
  usual two's-complement convention. Relative readings are reliable even if the
  absolute figure carries a fixed offset.
- `+++` is stripped from outgoing text. It is the module's escape into AT
  command mode, and sending it mid-conversation would take your own transmitter
  off the air.
- Sequence numbers wrap at 65536 and comparisons handle it.

## Testing it on one machine

Both modules in the same laptop, two terminals. This proves the radios, the
configuration and the client all work before anyone has to walk anywhere. The
two clients need separate keypairs, or they share an address and ignore each
other:

```
export LORALINE_KEY="whatever you agree on"

python -m loraline chat --port /dev/ttyUSB0 --band eu868 --nick hank
python -m loraline chat --port /dev/ttyUSB1 --band eu868 --nick test \
    --identity ~/.loraline/second
```

## Tests

```
python tests.py          # three sessions on a simulated broadcast bus
python tests_bridge.py   # LoRa and TCP bridged, over real sockets
```

## Source

<https://github.com/intervalplace/loraline>

Three sessions on a simulated broadcast bus that can drop packets, no hardware
required. Covers key exchange, group delivery going sent to partial to
delivered, a direct message being opaque to the third person, an outsider with
the right radio and no key reading nothing, per-conversation typing, store and
forward, address forgery, and a fragmented encrypted message surviving a link
dropping one packet in four.

## Licence

MIT. Do what you like with it.
