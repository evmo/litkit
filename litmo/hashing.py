"""Content identity — how litmo decides whether two copies are the same.

Nothing here looks at mtimes, ownership, or the machine. Two checkouts that
hold the same bytes under the same names agree, and a `push` after a no-op
re-run uploads nothing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(root: Path) -> tuple[str, int, int]:
    """(hash, file count, total bytes) for a file or a directory.

    Paths are sorted, so the result does not depend on directory iteration
    order, and only names and contents are hashed. This is deliberately not a
    hash of the packed archive: zstd is not bit-reproducible across versions,
    so hashing the archive would make an unchanged corpus look changed on a
    different machine.
    """
    if not root.exists():
        return ("", 0, 0)
    files = ([root] if root.is_file()
             else sorted(p for p in root.rglob("*") if p.is_file()))
    base = root.parent if root.is_file() else root

    h = hashlib.sha256()
    total = 0
    for p in files:
        h.update(p.relative_to(base).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(file_sha256(p).encode("ascii"))
        h.update(b"\n")
        total += p.stat().st_size
    return (h.hexdigest(), len(files), total)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n} B"
