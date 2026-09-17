


# Which build this is.
#
# Two sessions were spent on bugs that were already fixed, because there was
# no way to tell from the window whether the code running was the code just
# downloaded. The app prints this on startup and shows it at the foot of the
# page.
__version__ = "1.1"


def build_id() -> str:
    """A short digest of the modules, so two builds of the same version are
    still distinguishable."""
    import hashlib
    import pathlib
    here = pathlib.Path(__file__).parent
    digest = hashlib.blake2b(digest_size=3)
    for name in sorted(p.name for p in here.glob("*.py")):
        try:
            digest.update((here / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()
