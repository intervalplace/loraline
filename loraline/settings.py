"""What the app remembers, so nobody is asked twice.

A nick, a passphrase, a band and a port live in one small file beside the
identity. The point of the file is that the second launch asks nothing at all:
the thing that keeps people off a radio is not difficulty, it is being made to
answer the same four questions every time.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path.home() / ".loraline" / "settings.json"

BANDS = {
    "eu868": dict(label="Europe, UK, Norway (868 MHz)",
                  channel=18, sf=7, power=8, duty=0.01),
    "us915": dict(label="United States, Canada (915 MHz)",
                  channel=65, sf=10, power=22, duty=1.0),
    "au915": dict(label="Australia, New Zealand (915 MHz)",
                  channel=65, sf=10, power=22, duty=1.0),
}


@dataclass
class Settings:
    nick: str = ""
    passphrase: str = ""
    band: str = ""
    port: str = ""            # blank means look for it every time
    configured: bool = False

    @property
    def ready(self) -> bool:
        """Enough to start. A port is not required: if it is blank the app
        goes looking, which is the better default anyway because people move
        the thing between sockets."""
        return bool(self.nick and self.passphrase and self.band in BANDS)

    def preset(self) -> dict:
        return BANDS.get(self.band, BANDS["eu868"])


def load(path=DEFAULT_PATH) -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings()
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return Settings()
    known = {f: raw.get(f) for f in Settings().__dict__ if f in raw}
    settings = Settings(**{k: v for k, v in known.items() if v is not None})
    settings.configured = settings.ready
    return settings


def save(settings: Settings, path=DEFAULT_PATH) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(settings), indent=2))
        os.replace(temporary, path)
        # It has a passphrase in it.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
