


# Which build this is.
#
# Two sessions were spent on bugs that were already fixed, because there was
# no way to tell from the window whether the code running was the code just
# downloaded. The app prints this on startup and shows it at the foot of the
# page.
__version__ = "1.20"


def build_id() -> str:
    """A short digest of the modules, so two builds of the same version are
    still distinguishable.

    A packaged app has no .py files on disk, so this hashed nothing and every
    release reported the digest of an empty hash: the same six characters
    forever, in the one place the stamp is worth having. Packaging writes the
    answer into _build.py before freezing, and that is preferred when it is
    there.
    """
    try:
        from ._build import STAMP
        return STAMP
    except Exception:
        pass
    return stamp_of_sources()


def stamp_of_sources() -> str:
    """The digest of the modules as they are on disk.

    A packaged app has none, so it falls back to the bundle itself, which is a
    different file for every build and therefore answers the only question
    this is for: is the thing running the thing I just downloaded. Saying
    "unstamped" was honest and no use to anybody.
    """
    import hashlib
    import pathlib
    import sys

    here = pathlib.Path(__file__).parent
    names = sorted(p.name for p in here.glob("*.py") if p.name != "_build.py")
    if names:
        digest = hashlib.blake2b(digest_size=3)
        for name in names:
            try:
                digest.update((here / name).read_bytes())
            except OSError:
                pass
        return digest.hexdigest()

    # Frozen: hash the executable's own size and time, which is cheap and
    # differs for every build. Hashing all of it would read a hundred
    # megabytes to answer a six character question.
    try:
        binary = pathlib.Path(sys.executable)
        info = binary.stat()
        digest = hashlib.blake2b(digest_size=3)
        digest.update(f"{binary.name}:{info.st_size}:{int(info.st_mtime)}"
                      .encode("utf-8"))
        return digest.hexdigest()
    except Exception:
        return "unstamped"
