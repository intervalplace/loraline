# Shipping the app

```
pip install pyserial pynacl pyinstaller
pyinstaller --clean --noconfirm loraline.spec
python packaging/smoke.py
```

Whatever rides on loraline is built in, if it is checked out next door:

```
somewhere/
  loraline/     <- build from here
  catacomms/
  longshore/
  hearsay/
```

A frozen program cannot import what was not there when it was frozen, so a
build made on its own produces a chat client and nothing else. That is a
perfectly reasonable thing to build, and the only thing there is to build
before the other repositories exist, so it is allowed.

What the smoke test refuses is a mismatch: something checked out next door
that did not make it into the switcher. That is the failure worth catching,
because it looks exactly like success. A repository present but a panel that
will not import gives a bundle that starts, serves, and quietly carries
nothing.

One file, about seventeen megabytes with all three riding, ten without. `dist/loraline` on Linux, `dist/loraline.exe`
on Windows, `dist/loraline.app` on macOS.

`.github/workflows/build.yml` does all four builds on every push and attaches
them to a release on a tag. macOS is built twice, on `macos-14` for Apple
silicon and `macos-13` for Intel, because a single binary cannot be both and a
person who downloads the wrong one gets a file that will not open.

Linux is built on Ubuntu 22.04 deliberately. A binary built against an old
glibc runs on newer systems; the other direction does not work.

## The smoke test earns its place

A build that produces a file is not a build that produces a program. The check
starts the thing, waits for its page, and then asks it to look for a radio,
which is the part that exercises pyserial inside the bundle. A bundle missing
the serial backend starts perfectly well and falls over at exactly that point,
which is also the first thing a person does.

## What you will have to deal with, and I cannot

**macOS will refuse to open it.** An unsigned app downloaded from the internet
is quarantined, and the message says the app is damaged, which it is not. The
fixes, in ascending order of expense:

- Tell people to right-click and choose Open the first time. It works and it
  looks like exactly the advice malware gives.
- `xattr -d com.apple.quarantine loraline.app`. Same objection.
- Sign and notarise it. This needs an Apple Developer account at ninety-nine
  dollars a year, and then `codesign --deep --options runtime` followed by
  `xcrun notarytool submit`. It is the only version that behaves properly.

**Windows SmartScreen will warn about it**, with a More info link that reveals
a Run anyway button. This goes away with an code signing certificate, which is
a few hundred a year, or slowly on its own as more people download it without
anything bad happening.

**Linux will do nothing at all**, which is the correct amount.

None of this is packaging being difficult. It is two companies charging rent
for the right to distribute software, and the only way around it for something
given away free is to tell people the truth on the download page.

## Drivers

Most of these modules use a CH340 or CP210x serial chip. Recent macOS and
every current Linux have drivers for both. Older Windows may not, and the
symptom is that the app finds no ports at all. The app says so rather than
failing silently, but the driver has to come from the chip maker.

## Icons

Put `loraline.icns` and `loraline.ico` in this folder and the spec picks them
up. Without them the build is fine and the icon is generic.
