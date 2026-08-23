"""The manifest — one object in the bucket describing everything else in it.

    {
      "generated": "2026-08-15T12:00Z",
      "artifacts": {
        "cache": {"kind": "archive", "path": "data/cache", "key": "...",
                  "tree_hash": "...", "archive_sha256": "...", ...},
        "out":   {"kind": "mirror",  "path": "out",
                  "files": [{"path": "out/x.csv", "size": 1, "sha256": "..."}]}
      }
    }

It is the only thing a reader has to trust, so nothing in it is taken on
faith: every field is checked for type and shape on load, every path is
proved to stay under the artifact that claims it (see litkit.paths), and
every download is checked against the size and digest recorded here.

What "written last" does and does not buy you: the manifest is committed once,
after the objects it names, and with an If-Match on the copy this run read — so
it never names an object that was not uploaded, and two maintainers pushing at
once cannot silently lose one another's entries. It is *not* a transaction.
Objects are stored under their own names, which is what makes a public bucket
browsable, so a push that dies partway has already changed the bucket. litkit
closes most of that gap by writing the manifest to describe what actually
landed, including on the failure path. What is left is the window between one
object's PUT and that write, in which the bucket holds bytes the manifest still
describes with the previous digest.

Two older shapes are read transparently, so migrating a bucket does not mean
re-uploading it:
  - `{"artifacts": {name: {tree_hash, ...}}}` with no `kind` (archives)
  - `{"files": [{path, size, sha256}]}` — a flat list, grouped back onto the
    artifacts whose `path` contains each file.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from . import paths
from .remote import Missing

# Caps, so that a bucket that has gone wrong costs a message rather than the
# machine's memory. All are orders of magnitude above any real repository.
MAX_BYTES = 32 << 20
MAX_ARTIFACTS = 1_000
MAX_FILES = 200_000

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
MANIFEST_KINDS = ("archive", "mirror")


class Malformed(SystemExit):
    """The manifest is not something this reader is willing to act on."""


def _bad(where: str, msg: str):
    return Malformed(f"  manifest: {where}: {msg}")


def _int(entry: dict, field: str, where: str, *, default: int = 0) -> int:
    v = entry.get(field, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise _bad(where, f"{field} must be a non-negative integer, not {v!r}")
    return v


def _digest(entry: dict, field: str, where: str) -> str:
    v = entry.get(field)
    if not isinstance(v, str) or not HEX64.match(v):
        raise _bad(where, f"{field} must be a sha-256 hex digest, not {v!r}")
    return v


def _rel(raw, where: str, field: str, *, under: str | None = None) -> str:
    try:
        rel = paths.relative(raw, what=field)
    except paths.Unsafe as e:
        raise _bad(where, str(e)) from None
    if under is not None and not paths.is_under(rel, under):
        raise _bad(where, f"{field} {raw!r} is not under {under}/")
    return rel.as_posix()


def _check_files(entries, where: str, under: str | None) -> None:
    if not isinstance(entries, list):
        raise _bad(where, f"files must be a list, not {type(entries).__name__}")
    if len(entries) > MAX_FILES:
        raise _bad(where, f"{len(entries):,} files, more than the {MAX_FILES:,} "
                          f"this reader will accept")
    seen: set[str] = set()
    for i, e in enumerate(entries):
        w = f"{where}: file {i}"
        if not isinstance(e, dict):
            raise _bad(w, f"must be an object, not {type(e).__name__}")
        rel = _rel(e.get("path"), w, "path", under=under)
        if rel in seen:
            raise _bad(w, f"{rel} is listed twice")
        seen.add(rel)
        _int(e, "size", w)
        _digest(e, "sha256", w)


def _check_artifact(name: str, e: dict, prefix: str | None) -> None:
    where = f"artifact {name!r}"
    if not isinstance(e, dict):
        raise _bad(where, f"must be an object, not {type(e).__name__}")
    if e.get("kind") not in MANIFEST_KINDS:
        raise _bad(where, f"kind must be one of "
                          f"{', '.join(MANIFEST_KINDS)}, not {e.get('kind')!r}")
    if "path" in e:
        _rel(e["path"], where, "path")
    if e["kind"] == "archive":
        _rel(e.get("key"), where, "key")
        _digest(e, "tree_hash", where)
        _digest(e, "archive_sha256", where)
        for f in ("archive_bytes", "raw_bytes", "files"):
            _int(e, f, where)
    else:
        _check_files(e.get("files", []), where, prefix)


class Manifest:
    def __init__(self, artifacts: dict, orphans: list | None = None):
        self.artifacts = artifacts
        # Manifest records for files that no current artifact claims. Kept so
        # that migrating a bucket never drops a checksum a reader might need.
        self.orphans = orphans or []
        # True once an entry has actually changed. `push` writes the manifest
        # if and only if this is set, which is also what makes writing it from
        # a `finally` safe: an aborted push that uploaded nothing writes
        # nothing.
        self.dirty = False
        # Whether the bucket held a manifest at all, and the ETag of the copy
        # this run read if it did — the precondition for writing the new one.
        # A store that returns no ETag leaves `etag` None, which degrades to an
        # unconditional write rather than to a false "it was not there".
        self.existed = False
        self.etag: str | None = None

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(cls, remote, cfg) -> Manifest:
        try:
            body, etag = remote.get_bytes(cfg.manifest_key, limit=MAX_BYTES)
        except Missing:
            return cls({})
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Malformed(f"  {cfg.manifest_key} in {remote.where} is not "
                            f"readable JSON: {e}") from None
        m = cls._migrate(raw, cfg)
        m.existed, m.etag = True, etag
        return m

    @classmethod
    def _migrate(cls, raw: dict, cfg) -> Manifest:
        if not isinstance(raw, dict):
            raise _bad("document", f"must be an object, not "
                                   f"{type(raw).__name__}")
        arts = raw.get("artifacts") or {}
        if not isinstance(arts, dict):
            raise _bad("artifacts", f"must be an object, not "
                                    f"{type(arts).__name__}")
        if len(arts) > MAX_ARTIFACTS:
            raise _bad("artifacts", f"{len(arts):,} entries, more than the "
                                    f"{MAX_ARTIFACTS:,} this reader accepts")
        arts = dict(arts)

        # Old archive entries carry tree_hash but no kind.
        for e in arts.values():
            if isinstance(e, dict) and "kind" not in e:
                e["kind"] = "archive" if "tree_hash" in e else "mirror"

        # Old flat file list -> the artifacts that contain those paths.
        orphans = []
        flat = raw.get("files")
        if flat and not arts:
            _check_files(flat, "files", None)
            by_art: dict[str, list] = {}
            for entry in flat:
                for art in cfg.artifacts:
                    prefix = art.path.as_posix().rstrip("/") + "/"
                    if entry["path"] == art.path.as_posix() or \
                            entry["path"].startswith(prefix):
                        by_art.setdefault(art.name, []).append(entry)
                        break
                else:
                    orphans.append(entry)
            for art in cfg.artifacts:
                if art.name in by_art:
                    arts[art.name] = {"kind": "mirror",
                                      "path": art.path.as_posix(),
                                      "files": by_art[art.name]}
        elif flat:
            _check_files(flat, "files", None)
            orphans = list(flat)

        prefixes = {a.name: a.path.as_posix() for a in cfg.artifacts}
        for name, e in arts.items():
            if not isinstance(name, str) or not name:
                raise _bad("artifacts", f"{name!r} is not a usable name")
            _check_artifact(name, e, prefixes.get(name))
        return cls(arts, orphans)

    # --- reading ------------------------------------------------------------

    def get(self, name: str) -> dict | None:
        return self.artifacts.get(name)

    def mirror_files(self, name: str) -> list[dict]:
        return (self.get(name) or {}).get("files", [])

    @property
    def empty(self) -> bool:
        return not self.artifacts

    # --- writing ------------------------------------------------------------

    def set(self, name: str, entry: dict) -> None:
        """Record an entry, noting whether it actually said anything new."""
        if self.artifacts.get(name) != entry:
            self.artifacts[name] = entry
            self.dirty = True

    def dump(self) -> bytes:
        doc = {
            "generated": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%MZ"),
            "artifacts": self.artifacts,
        }
        if self.orphans:
            doc["files"] = self.orphans
        return json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
