# PyInstaller recipe for the loraline app.
#
#     pyinstaller --clean --noconfirm loraline.spec
#
# One file, so what somebody downloads is a thing they can double click and
# not a folder they have to keep together. It is slower to start by a second
# or two, which is a trade worth making exactly once at the point where
# somebody decides whether this is worth the bother.
#
# Windowed on macOS and Windows, so no terminal appears behind the browser.
# On Linux it stays a console program, because a Linux user who runs a binary
# from a shell wants to see what it says.

import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()
NAME = "loraline"

# Anything riding on loraline has to be inside the bundle, because a frozen
# program cannot import something that was not there when it was frozen. A
# download with an empty switcher would be a download that does nothing.
#
# Siblings are picked up if they are checked out next door. Whatever is
# present is built in; whatever is not simply is not in the switcher.
RIDERS = ("catacomms", "longshore", "hearsay")
riding, extra_paths = [], []
for rider in RIDERS:
    for where in (HERE.parent / rider, HERE / rider):
        if (where / rider / "__init__.py").exists():
            extra_paths.append(str(where))
            riding += [rider, f"{rider}.panel"]
            break
if riding:
    print("building in:", ", ".join(sorted(set(r for r in riding if "." not in r))))
else:
    print("building loraline on its own; no riders found next door")

block_cipher = None

a = Analysis(
    [str(HERE / "loraline-app.py")],
    pathex=[str(HERE)] + extra_paths,
    binaries=[],
    datas=[],
    # PyInstaller follows imports, and these are reached in ways it cannot
    # see: pyserial picks its backend at runtime by platform, and PyNaCl's
    # bindings come in through cffi.
    hiddenimports=[
        "serial", "serial.tools", "serial.tools.list_ports",
        "serial.serialposix", "serial.serialwin32", "serial.serialcli",
        "nacl", "nacl.public", "nacl.signing", "nacl.secret",
        "nacl.bindings", "_cffi_backend",
    ] + riding,
    hookspath=[],
    runtime_hooks=[],
    # Nothing here draws a window of its own; the interface is a browser. Left
    # in, these add tens of megabytes to something that is otherwise small.
    excludes=[
        "tkinter", "test", "unittest", "pydoc_data", "lib2to3",
        "numpy", "matplotlib", "setuptools", "pip",
    ] + ([] if "hearsay" in riding else ["PIL"]),
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=(sys.platform.startswith("linux")),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(HERE / "packaging" / "loraline.icns")
    if (HERE / "packaging" / "loraline.icns").exists() else None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name=f"{NAME}.app",
        icon=str(HERE / "packaging" / "loraline.icns")
        if (HERE / "packaging" / "loraline.icns").exists() else None,
        bundle_identifier="org.loraline.app",
        info_plist={
            "CFBundleName": "loraline",
            "CFBundleDisplayName": "loraline",
            "CFBundleShortVersionString": "1.0",
            "LSMinimumSystemVersion": "11.0",
            # It serves a page to itself and opens a browser at it. No network
            # permission prompt is needed for that, but the entitlement makes
            # the intent plain to anybody who looks.
            "NSHighResolutionCapable": True,
            "LSUIElement": False,
        },
    )
