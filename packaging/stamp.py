#!/usr/bin/env python3
"""Write the build id into the package, before freezing it.

A packaged app has no .py files on disk to hash, so loraline reported the
digest of an empty hash: identical for every release, in the one place a build
stamp is worth having. Run this before PyInstaller and it bakes the real
answer in.

    python3 packaging/stamp.py
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from loraline import __version__, stamp_of_sources   # noqa: E402

stamp = stamp_of_sources()
out = HERE.parent / "loraline" / "_build.py"
out.write_text(
    '"""Written by packaging/stamp.py. Not the source of anything."""\n'
    f'STAMP = "{stamp}"\n',
    encoding="utf-8")
print(f"loraline {__version__}, build {stamp} (written to {out.name})")
