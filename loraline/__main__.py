"""Command line entry point.

    python -m loraline config --port /dev/ttyUSB0 --band us915
    python -m loraline link   --port /dev/ttyUSB0 --band us915 --role ping
    python -m loraline chat   --port /dev/ttyUSB0 --band us915 --nick hank
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import crypto
from . import protocol as proto
from .client import Client
from .crypto import GROUP, Identity, Keyring
from .transport import (
    Link, LoRaInterface, RadioConfig, TCPClientInterface, TCPServerInterface,
    channel_for_mhz,
)

# Regional presets. Getting this wrong means transmitting a fairly strong
# signal into somebody else's licensed spectrum, so the argument is required.
BANDS = {
    "eu868": dict(channel=18, power_dbm=8, duty=1.0, sf=7,
                  note="EU 868 MHz: about 14 dBm ERP and a 1% duty cycle. Power is "
                       "low because a 6 dBi antenna adds gain on top, and the "
                       "spreading factor is low so presence fits the budget."),
    "us915": dict(channel=65, power_dbm=22, duty=0.0, sf=10,
                  note="US/Canada 902-928 MHz ISM: no duty cycle limit."),
    "au915": dict(channel=65, power_dbm=22, duty=0.0, sf=10,
                  note="AU/NZ 915-928 MHz: full power fine."),
}


def add_radio_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", help="serial port, e.g. /dev/ttyUSB0. Omit for a "
                                 "node with no radio, reachable only over TCP")
    p.add_argument("--band", choices=sorted(BANDS),
                   help="regional preset; required whenever --port is given")
    p.add_argument("--mhz", type=int, help="override the preset frequency, 850-930")
    p.add_argument("--sf", type=int, choices=range(7, 13),
                   help="spreading factor: 7 fast and short, 12 slow and long. "
                        "Defaults to what the band's duty cycle can afford")
    p.add_argument("--bw", type=int, default=125, choices=(125, 250, 500))
    p.add_argument("--power", type=int, help="10-22 dBm, overrides the preset")
    p.add_argument("--address", type=int, default=258)
    p.add_argument("--netid", type=int, default=0)
    p.add_argument("--duty", type=float, help="duty limit percent; 0 disables")
    p.add_argument("--key", help="shared group passphrase; or set LORALINE_KEY")
    p.add_argument("--identity", help="path to the keypair file")
    p.add_argument("--tcp-listen", type=int, metavar="PORT",
                   help="accept peers over TCP on this port")
    p.add_argument("--tcp-connect", metavar="HOST:PORT",
                   help="reach a peer over TCP, e.g. bridge.example.com:4242")
    p.add_argument("--no-bridge", action="store_true",
                   help="do not relay traffic between interfaces")
    p.add_argument("--no-config", action="store_true",
                   help="skip AT setup, assume the module is already configured")


def build(args) -> tuple[RadioConfig, float, object, list[str]]:
    # A node with no radio has no band to pick. Asking someone joining over
    # the internet which frequency they are on is a confusing question.
    if args.band is None:
        if args.port:
            raise SystemExit("--band is required when using a radio. "
                             f"Choose one of: {', '.join(sorted(BANDS))}")
        args.band = "eu868"
    preset = BANDS[args.band]
    cfg = RadioConfig(
        channel=channel_for_mhz(args.mhz) if args.mhz else preset["channel"],
        sf=args.sf if args.sf is not None else preset["sf"], bw_khz=args.bw,
        power_dbm=args.power if args.power is not None else preset["power_dbm"],
        address=args.address, netid=args.netid,
    )
    duty = args.duty if args.duty is not None else preset["duty"]
    passphrase = getattr(args, "key", None) or os.environ.get("LORALINE_KEY")
    identity = Identity.load_or_create(getattr(args, "identity", None) or crypto.DEFAULT_PATH)
    keyring = Keyring(identity, passphrase)
    return cfg, duty, identity, keyring, crypto.warnings_for(passphrase)


def open_link(args, cfg, duty, keyring, verbose=False) -> Link:
    """Assemble whichever bearers were asked for.

    A node with both a radio and a socket becomes a bridge: it relays sealed
    envelopes between them, including ones it cannot decrypt.
    """
    interfaces = []
    if args.port:
        lora = LoRaInterface(args.port, cfg, duty_limit_percent=duty)
        lora.open()
        if not args.no_config:
            lora.apply_config(verbose=verbose)
        interfaces.append(lora)
    if getattr(args, "tcp_listen", None):
        interfaces.append(TCPServerInterface(port=args.tcp_listen))
    if getattr(args, "tcp_connect", None):
        host, _, port = args.tcp_connect.rpartition(":")
        interfaces.append(TCPClientInterface(host or "127.0.0.1", int(port)))
    if not interfaces:
        raise SystemExit("Nothing to talk over: give --port, --tcp-listen or --tcp-connect.")
    for iface in interfaces:
        iface.start()
    return Link(interfaces, keyring=keyring,
                bridge=not getattr(args, "no_bridge", False))


def cmd_config(args) -> int:
    cfg, duty, identity, keyring, _ = build(args)
    print(BANDS[args.band]["note"])
    print(f"Applying: {cfg.describe()}\n")

    if not args.port:
        raise SystemExit("config needs --port: there is no module to configure.")
    lora = LoRaInterface(args.port, cfg, duty_limit_percent=duty)
    lora.open()
    try:
        lora.apply_config(verbose=True)
    finally:
        lora.close()

    sample = proto.message(identity.address, GROUP, 1, -1, 0, 1, "x" * 40)
    wire = proto.seal(sample, keyring, GROUP).size
    airtime = cfg.airtime_of(wire)
    print(f"\nYour address is {identity.address}, fingerprint {identity.fingerprint}")
    print("Done. Run this with identical settings on every other module.")
    print(f"A 40-character message is {wire} bytes on the wire, {airtime:.0f} ms on air.")
    if duty > 0:
        print(f"Under a {duty}% duty cycle that is about "
              f"{int((3600_000 * duty / 100) / airtime)} messages per hour.")
    return 0


def cmd_chat(args) -> int:
    from .ui_curses import run

    cfg, duty, identity, keyring, warnings = build(args)
    link = open_link(args, cfg, duty, keyring)
    client = Client(link, identity, keyring,
                    nick=args.nick, psm=args.psm, colour=args.colour)
    try:
        run(client, warnings)
    finally:
        client.shutdown()
        link.close()
    return 0


def cmd_link(args) -> int:
    """Round-trip range tester.

    Antenna placement matters more than any parameter you can set, so measure
    it. Run pong at one end, ping at the other, and walk around.
    """
    cfg, duty, identity, keyring, _ = build(args)
    link = open_link(args, cfg, 0.0, keyring)  # testing ignores the duty cap
    seq, sent, recv = 0, 0, 0
    pending: dict[int, float] = {}

    print(f"{cfg.describe()}\nrole={args.role}. Ctrl+C to stop.\n")
    try:
        last = 0.0
        while True:
            frames, errors = link.pump()
            for exc in errors:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            for item in frames:
                if item.type != "M":
                    continue
                dbm = proto.rssi_dbm(item.rssi_raw)
                bars = proto.signal_bars(dbm)
                n = item.field_int(2, -1)
                recv += 1
                if args.role == "pong":
                    link.send(proto.message(identity.address, GROUP, n, -1, 0, 1, "pong"))
                    print(f"ping #{n}  rssi {dbm} dBm  {bars}  -> replied")
                else:
                    t0 = pending.pop(n, None)
                    rtt = f"{(time.time() - t0) * 1000:.0f} ms" if t0 else "?"
                    loss = 100 * (1 - recv / max(sent, 1))
                    print(f"reply #{n}  rssi {dbm} dBm  {bars}  rtt {rtt}  loss {loss:.0f}%")

            if args.role == "ping" and time.time() - last >= args.interval:
                last = time.time()
                seq += 1
                sent += 1
                pending[seq] = last
                link.send(proto.message(identity.address, GROUP, seq, -1, 0, 1, "ping"))
                print(f"ping #{seq} sent")
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(f"\nsent {sent}, received {recv}")
    finally:
        link.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="loraline")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("config", help="write settings to a module")
    add_radio_args(p)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("chat", help="run the messenger")
    add_radio_args(p)
    p.add_argument("--nick", required=True)
    p.add_argument("--psm", default="", help="personal message")
    p.add_argument("--colour", type=int, default=0, help="0-7")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("link", help="range and signal tester")
    add_radio_args(p)
    p.add_argument("--role", required=True, choices=("ping", "pong"))
    p.add_argument("--interval", type=float, default=5.0)
    p.set_defaults(func=cmd_link)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
