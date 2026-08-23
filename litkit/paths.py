"""Where a name is allowed to land on disk.

Every path litkit writes to is a string joined to the repository root: the
manifest's, which arrived over the network, and sync.toml's, which arrived
from a file. `../../.ssh/authorized_keys` is a perfectly ordinary-looking
manifest entry, so neither may be joined before it has been proved to stay
underneath where it claimed to be.

The rule is narrow on purpose. A path is a non-empty, already-normalised,
relative POSIX path — no leading `/`, no drive letter, no `.` or `..`
component, no backslash, no NUL — and, once resolved through whatever
symlinks its parents turn out to be, still beneath the directory it named.
The second half is not optional: `out` may itself be a symlink to /tmp, and
then `out/x.csv` escapes the checkout without a single `..` anywhere.
"""

from __future__ import annotations

import ntpath
from pathlib import Path, PurePosixPath

MAX_LEN = 1024


class Unsafe(ValueError):
    """A path that would land somewhere other than where it claimed to."""


def relative(raw, *, what: str = "path") -> PurePosixPath:
    """Validate `raw` as a relative POSIX path, or raise Unsafe."""
    if not isinstance(raw, str):
        raise Unsafe(f"{what}: must be a string, not {type(raw).__name__}")
    if not raw:
        raise Unsafe(f"{what}: must not be empty")
    if len(raw) > MAX_LEN:
        raise Unsafe(f"{what}: longer than {MAX_LEN} characters")
    if "\0" in raw:
        raise Unsafe(f"{what}: contains a NUL byte")
    if "\\" in raw:
        raise Unsafe(f"{what}: contains a backslash — separate with / ({raw!r})")
    # ntpath catches `C:foo` and `\\host\share`, which posixpath happily calls
    # relative and Windows does not.
    if raw.startswith("/") or ntpath.splitdrive(raw)[0]:
        raise Unsafe(f"{what}: must be relative, not {raw!r}")
    p = PurePosixPath(raw)
    # PurePosixPath(".").parts is (), so this has to be its own check.
    if not p.parts:
        raise Unsafe(f"{what}: names no file ({raw!r})")
    if any(part in (".", "..") for part in p.parts):
        raise Unsafe(f"{what}: must not contain `.` or `..` ({raw!r})")
    # Reject rather than silently rewrite: for a mirror the same string is
    # both the object key and the destination, so normalising here would
    # quietly ask the bucket for something other than what it was given.
    if p.as_posix() != raw:
        raise Unsafe(f"{what}: not normalised — {raw!r} means {p.as_posix()!r}")
    return p


def is_under(child, parent) -> bool:
    """Lexically: is `child` at or below `parent`? Both relative POSIX."""
    c, p = PurePosixPath(child).parts, PurePosixPath(parent).parts
    return c[:len(p)] == p


def resolve_under(base: Path, raw, *, what: str = "path") -> Path:
    """`base / raw`, proved to stay inside `base`.

    The deepest ancestor that actually exists is resolved and compared with
    the resolved base, so a symlinked parent cannot smuggle the destination
    out of the tree. The destination itself may not be a symlink either: a
    dangling one pointing outside would otherwise be written *through*.
    """
    rel = relative(raw, what=what)
    root = base.resolve()
    dest = root.joinpath(*rel.parts)

    probe = dest
    while probe != root and not probe.is_symlink() and not probe.exists():
        probe = probe.parent
    real = probe.resolve()
    if real != root and root not in real.parents:
        raise Unsafe(f"{what}: {raw!r} resolves to {real}, outside {root}")
    if dest.is_symlink():
        raise Unsafe(f"{what}: {raw!r} is a symlink — refusing to write through it")
    return dest


def under_artifact(root: Path, art, raw, *, what: str = "path") -> Path:
    """A manifest path: inside the artifact that filed it.

    An entry filed under `out` has no business naming `data/cache/x`, however
    contained that would be — the manifest says which artifact owns it.

    Containment is measured from the artifact's own directory as it resolves
    on this machine, not from the repository root. Someone who symlinks `out/`
    onto a scratch disk has said where that artifact lives; the job here is to
    stop the bucket's names wandering out of it, not to overrule them.
    """
    rel = relative(raw, what=what)
    prefix = PurePosixPath(art.path.as_posix())
    if not is_under(rel, prefix):
        raise Unsafe(f"{what}: {raw!r} is not under {prefix}/, "
                     f"which is what artifact {art.name!r} covers")
    inside = rel.parts[len(prefix.parts):]
    if not inside:
        return root / art.path          # the artifact *is* the one file
    return resolve_under((root / art.path).resolve(),
                         PurePosixPath(*inside).as_posix(), what=what)
