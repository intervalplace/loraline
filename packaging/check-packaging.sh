# Run this in your loraline checkout to see which packaging files are there.
python3 - <<'PY'
import pathlib, re
ok = True
spec = pathlib.Path("loraline.spec")
if not spec.exists():
    print("no loraline.spec here"); raise SystemExit(1)
text = spec.read_text(encoding="utf-8")
hidden = text[text.index("hiddenimports"):text.index("hookspath")]
excluded = text[text.index("excludes="):text.index("win_no_prefer")]
print("loraline.spec")
print("   webview in hiddenimports:", "webview" in hidden)
print("   webview in excludes     :", "webview" in excluded)
ok &= "webview" in hidden and "webview" not in excluded

flow = pathlib.Path(".github/workflows/build.yml")
if flow.exists():
    f = flow.read_text(encoding="utf-8")
    print("build.yml")
    print("   installs pywebview      :", "pip install pywebview" in f)
    print("   fails if it is missing  :", "the bundle has no web view" in f)
    ok &= "the bundle has no web view" in f
else:
    print("build.yml: not here"); ok = False

init = pathlib.Path("loraline/__init__.py")
if init.exists():
    print("version                    :",
          re.search(r'__version__ = "([^"]+)"', init.read_text(encoding="utf-8")).group(1))
print()
print("everything up to date" if ok else
      "the packaging files are older than the package; copy loraline.spec and "
      ".github/workflows/build.yml across too")
PY
