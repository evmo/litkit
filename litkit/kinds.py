"""The three artifact kinds — archive, mirror, fetch.

Each exposes the same three verbs against one artifact:

    status(ctx, art) -> Report      compare local and remote, move nothing
    pull(ctx, art, ...) -> None     remote -> here
    push(ctx, art, ...) -> bool     here -> remote; True if it changed anything

`ctx` carries the config, the transport and the loaded manifest, so the verbs
below read as what they do rather than as plumbing.

Both pulls stage. Nothing arriving from the bucket touches the working tree
until it has been downloaded in full, checked against the manifest's sizes and
digests, and — for an archive — hashed as a whole tree. A failed pull costs a
non-zero exit and leaves the previous local copy exactly as it was.
"""

from __future__ import annotations

import contextlib
import errno
import json
import mimetypes
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import paths
from .config import STATE_DIR
from .hashing import file_sha256, human, tree_hash
from .remote import Oversized, fetch_url, head

ZSTD_LEVEL = 10
SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints", ".git", ".litkit"}

# Belt and braces for the unpack: the bundle's digest is checked first, so
# reaching either of these means the bucket and the manifest already disagree.
MAX_MEMBERS = 2_000_000
MAX_UNPACKED = 256 << 30

# How old a leftover staging tree must be before a later run sweeps it away.
STALE_STAGE = 24 * 3600

IN_SYNC, DIFFERS, LOCAL_ONLY, REMOTE_ONLY, ABSENT = (
    "in sync", "DIFFERS", "local only", "remote only", "absent")


@dataclass
class Report:
    local: str
    remote: str
    verdict: str

    @property
    def ok(self) -> bool:
        return self.verdict in (IN_SYNC, ABSENT)


def _zstd():
    try:
        import zstandard
    except ImportError:
        raise SystemExit(
            "the `archive` kind needs zstandard — install litkit with the "
            "`archive` extra:\n"
            "  uv add 'litkit[archive] @ git+https://github.com/evmo/litkit'"
        ) from None
    return zstandard


# ----------------------------------------------------------- where things go -

def _here(ctx, art) -> Path:
    """The artifact's own directory. `config.load` proved it relative."""
    return ctx.cfg.root / art.path


def _dest(ctx, art, rel: str) -> Path:
    """Where one manifest entry is allowed to land.

    The manifest was already checked for shape when it loaded; this is the
    half that can only be done against the filesystem, at the moment of
    writing — that no parent has become a symlink out of the tree since.
    """
    try:
        return paths.under_artifact(ctx.cfg.root, art, rel,
                                    what=f"artifact {art.name!r}: manifest path")
    except paths.Unsafe as e:
        raise SystemExit(f"  {e}") from None


def _staging(cfg):
    """A scratch directory on the same filesystem as the checkout.

    Verified content is moved into place with os.replace, which will not
    cross a filesystem boundary, so /tmp is not an option: the repository may
    well be on a different mount from it.

    A pull that is killed outright, rather than raising, leaves its staging
    tree behind — and that tree can be the size of the artifact. Anything a
    day old belongs to no live run, so it goes.
    """
    base = cfg.root / STATE_DIR / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - STALE_STAGE
    for old in base.glob("stage-*"):
        with contextlib.suppress(OSError):
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
    return tempfile.TemporaryDirectory(prefix="stage-", dir=base)


def _move(src: Path, dest: Path) -> None:
    """Staged -> installed. A rename where it can be, a copy where it cannot.

    An artifact directory that is a symlink onto another disk puts the staging
    area and the destination on different filesystems, where rename is not
    allowed. The bytes have already been verified by then, so falling back to
    a copy costs the per-file atomicity and nothing else.
    """
    try:
        os.replace(src, dest)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dest))


class Blocked(Exception):
    """Installing would fail partway, because the local layout is in the way.

    Raised before anything has moved, so the caller can refuse while the
    working tree is still exactly as it was.
    """

    def __init__(self, conflicts: list[tuple[Path, str]]):
        super().__init__(f"{len(conflicts)} path(s) in the way")
        self.conflicts = conflicts


def _blocked(dest: Path, stop: Path) -> tuple[Path, str] | None:
    """Why a verified file could not be installed at `dest`, or None.

    `os.replace` of a file onto a directory raises IsADirectoryError, and
    `mkdir(parents=True)` of a parent that is already a file raises
    FileExistsError. Both happen inside the install step, past the point where
    a pull has promised the working tree is safe to change, and both leave it
    half-installed — so the layout is looked at before that point rather than
    after. It happens whenever a published path changes between a file and a
    directory and a reader still holds the old shape.

    A symlink is not in the way: `os.replace` replaces the link itself, and
    `mkdir(exist_ok=True)` is content with a link to a directory.
    """
    if dest.is_dir() and not dest.is_symlink():
        return (dest, "a directory here, and a file in the bucket")
    for anc in dest.parents:
        if anc == stop or stop not in anc.parents:
            break
        if anc.is_file():
            return (anc, "a file here, and a directory in the bucket")
    return None


def _layout_lines(conflicts: list[tuple[Path, str]], root: Path) -> list[str]:
    """One line per distinct path in the way — several files under the same
    conflicting ancestor are one problem, not many."""
    seen: dict[str, str] = {}
    for p, why in conflicts:
        try:
            shown = p.relative_to(root).as_posix()
        except ValueError:
            shown = str(p)
        seen.setdefault(shown, why)
    return [f"    {k} is {v}" for k, v in seen.items()]


def _refuse_layout(art, conflicts: list[tuple[Path, str]], root: Path):
    lines = _layout_lines(conflicts, root)
    return SystemExit(
        f"  {art.name}: the published layout no longer fits what is here, so "
        f"installing would fail partway — {art.path} was not touched:\n"
        + "\n".join(lines[:10])
        + (f"\n    … and {len(lines) - 10:,} more" if len(lines) > 10 else "")
        + "\n    Remove the path(s) named and pull again.")


def _install(staged: Path, dest: Path, attic: Path, *, clean: bool) -> None:
    """Move a verified staging tree onto the destination.

    A destination that is a symlink is followed, not replaced: someone who
    pointed `out/` at a scratch disk meant the artifact to live there, and a
    pull has no business quietly turning it back into an ordinary directory.

    Where the whole tree is being swapped, the old one is renamed into `attic`
    — inside the staging directory, which is about to be deleted anyway —
    rather than removed first, so the destination is never briefly absent.
    """
    if dest.is_symlink():
        dest = Path(os.path.realpath(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if clean or not dest.exists() or dest.is_dir() != staged.is_dir():
        if dest.exists():
            try:
                os.replace(dest, attic)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        _move(staged, dest)
        return
    if staged.is_file():
        _move(staged, dest)
        return
    files = sorted(p for p in staged.rglob("*") if p.is_file())
    blocked = [c for src in files
               if (c := _blocked(dest / src.relative_to(staged), dest))]
    if blocked:
        raise Blocked(blocked)
    for src in files:
        target = dest / src.relative_to(staged)
        target.parent.mkdir(parents=True, exist_ok=True)
        _move(src, target)


# --------------------------------------------------------------- archive ----
# One tar.zst bundle per artifact. For a tree of thousands of small files
# (cached API responses, say) this is the difference between one request and
# thousands. Identity is the hash of the *tree*, never of the bundle — see
# hashing.tree_hash.

def _pack(root: Path, src: Path, dest: Path) -> None:
    # tar.add stores a symlink *as* a symlink and does not recurse into it, so
    # an artifact directory pointed at a scratch disk would otherwise publish
    # as one dangling link and nothing else. tree_hash reads through it; so
    # must this, or the two disagree about what was published.
    arcname = src.relative_to(root).as_posix()
    real = Path(os.path.realpath(src))
    cctx = _zstd().ZstdCompressor(level=ZSTD_LEVEL, threads=-1)
    with dest.open("wb") as raw, cctx.stream_writer(raw) as z:
        with tarfile.open(fileobj=z, mode="w|") as tar:
            tar.add(real, arcname=arcname)


def _escaping_links(src: Path) -> list[tuple[Path, Path]]:
    """Symlinks under `src` whose target is outside it.

    `tar.add` stores a link *as* a link, while `tree_hash` reads *through* it
    — so a link out of the artifact is hashed as its target's bytes but packed
    as a reference to a path only this machine has. The push exits 0 and
    `status` says in sync; every reader's pull then fails, because the `data`
    extraction filter refuses a link that leaves the destination, and nothing
    on the publisher's side ever says so. The publishing end is the only place
    that can see the difference, so it is where this is refused.

    A link that stays inside the artifact is fine, and stays fine: it packs,
    extracts and hashes the same on both sides.
    """
    root = Path(os.path.realpath(src))
    if not root.is_dir():
        return []                      # the artifact is one file; _pack reads
                                       # through it and stores a regular file
    out = []
    for p in sorted(root.rglob("*")):  # rglob does not descend through links
        if not p.is_symlink():
            continue
        target = Path(os.path.realpath(p))
        if target != root and root not in target.parents:
            out.append((p.relative_to(root), target))
    return out


def _unpack(archive: Path, root: Path) -> None:
    """Extract into `root`, one member at a time so the counters can bite.

    `filter="data"` is what refuses absolute names, `..`, devices and links
    out of the tree; the counters are the cheap guard against a bundle whose
    digest matched but whose contents are absurd.
    """
    dctx = _zstd().ZstdDecompressor()
    members = unpacked = 0
    with archive.open("rb") as raw, dctx.stream_reader(raw) as z:
        with tarfile.open(fileobj=z, mode="r|") as tar:
            for member in tar:
                members += 1
                unpacked += max(member.size, 0)
                if members > MAX_MEMBERS:
                    raise SystemExit(f"  archive holds more than "
                                     f"{MAX_MEMBERS:,} members — refusing")
                if unpacked > MAX_UNPACKED:
                    raise SystemExit(f"  archive unpacks to more than "
                                     f"{human(MAX_UNPACKED)} — refusing")
                tar.extract(member, root, filter="data")


def archive_status(ctx, art) -> Report:
    digest, n, size = tree_hash(_here(ctx, art))
    remote = ctx.manifest.get(art.name) or {}
    local_s = f"{n:,} files, {human(size)}" if digest else "absent"
    remote_s = (f"{remote.get('files', 0):,} files, "
                f"{human(remote.get('raw_bytes', 0))}" if remote else "absent")
    if not remote and not digest:
        verdict = ABSENT
    elif not remote:
        verdict = LOCAL_ONLY
    elif not digest:
        verdict = REMOTE_ONLY
    else:
        verdict = IN_SYNC if digest == remote.get("tree_hash") else DIFFERS
    return Report(local_s, remote_s, verdict)


def archive_pull(ctx, art, *, force=False, clean=False) -> None:
    remote = ctx.manifest.get(art.name)
    if not remote:
        print(f"  {art.name:9} not in the bucket")
        return
    dest = _here(ctx, art)
    local_hash, n, size = tree_hash(dest)
    if local_hash == remote["tree_hash"] and not force:
        print(f"  {art.name:9} up to date  ({n:,} files, {human(size)})")
        return

    print(f"  {art.name:9} downloading {human(remote['archive_bytes'])}"
          f" -> {art.path} ({remote['files']:,} files,"
          f" {human(remote['raw_bytes'])} unpacked)", flush=True)
    with _staging(ctx.cfg) as tmp:
        tmp = Path(tmp)
        bundle = tmp / "bundle.tar.zst"
        try:
            ctx.remote.download(remote["key"], bundle,
                                remote.get("archive_bytes") or None)
        except Oversized as e:
            raise SystemExit(f"  {art.name}: {e} — {art.path} was not "
                             f"touched") from None
        got = file_sha256(bundle)
        if got != remote["archive_sha256"]:
            raise SystemExit(f"  {art.name}: archive failed verification — "
                             f"{art.path} was not touched\n"
                             f"    expected {remote['archive_sha256']}\n"
                             f"    got      {got}")

        tree = tmp / "tree"
        tree.mkdir()
        try:
            _unpack(bundle, tree)
        except SystemExit:
            raise
        except Exception as e:                                # noqa: BLE001
            raise SystemExit(f"  {art.name}: the bundle would not unpack "
                             f"({type(e).__name__}: {e}) — {art.path} was "
                             f"not touched") from None
        bundle.unlink()

        staged = tree.joinpath(*art.path.parts)
        stray = [p.relative_to(tree).as_posix() for p in tree.rglob("*")
                 if p.is_file() and not paths.is_under(
                     p.relative_to(tree).as_posix(), art.path.as_posix())]
        after, _, _ = tree_hash(staged)
        if stray or after != remote["tree_hash"]:
            raise SystemExit(
                f"  {art.name}: the bundle is not the tree the manifest "
                f"describes — {art.path} was not touched\n"
                + (f"    it also holds {len(stray):,} file(s) outside "
                   f"{art.path}, e.g. {stray[0]}\n" if stray else "")
                + f"    expected {remote['tree_hash']}\n"
                  f"    got      {after}")

        # Verified: from here the working tree may be changed — and the
        # merge is checked for conflicts first, so `_install` cannot fail
        # halfway through and leave a tree that is neither copy.
        try:
            _install(staged, dest, tmp / "replaced", clean=clean)
        except Blocked as b:
            raise _refuse_layout(art, b.conflicts, ctx.cfg.root) from None

    final, n3, size3 = tree_hash(dest)
    note = "verified" if final == remote["tree_hash"] else (
        "merged — local extras remain, so the tree is a superset of the "
        "bucket's (re-run with --clean for an exact copy)")
    print(f"  {art.name:9} {note}  ({n3:,} files, {human(size3)})")


def archive_push(ctx, art, *, force=False, dry_run=False) -> bool:
    src = _here(ctx, art)
    if not src.exists():
        print(f"  {art.name:9} skipped — {art.path} is not here")
        return False

    # Before the hash, not after: the walk is far cheaper than reading every
    # byte, and an artifact that cannot be published is worth saying so about
    # even on a push that would otherwise have found nothing to do.
    escaping = _escaping_links(src)
    if escaping:
        raise SystemExit(
            f"  {art.name}: {art.path} holds {len(escaping):,} symlink(s) "
            f"pointing outside it, which pack as links no reader can follow "
            f"— nothing was uploaded:\n"
            + "\n".join(f"    {(art.path / rel).as_posix()} -> {t}"
                        for rel, t in escaping[:10])
            + (f"\n    … and {len(escaping) - 10:,} more"
               if len(escaping) > 10 else "")
            + f"\n    Replace them with the files themselves, or move the "
              f"targets inside {art.path}, then re-run `litkit push`.")

    digest, n, size = tree_hash(src)
    remote = ctx.manifest.get(art.name) or {}
    if remote.get("tree_hash") == digest and not force:
        print(f"  {art.name:9} up to date  ({n:,} files, {human(size)})")
        return False

    print(f"  {art.name:9} packing     ({n:,} files, {human(size)}) …", flush=True)
    with _staging(ctx.cfg) as tmp:
        bundle = Path(tmp) / f"{art.name}.tar.zst"
        _pack(ctx.cfg.root, src, bundle)
        # `tree_hash` and `_pack` read the tree independently, and packing a
        # large one takes a while — long enough for a build still running
        # underneath the publish to put different bytes in each. The manifest
        # would then record a digest the bundle does not have, and every
        # reader's pull would fail on it until someone happened to push
        # again. Re-read before anything is uploaded.
        again, _, _ = tree_hash(src)
        if again != digest:
            raise SystemExit(
                f"  {art.name}: {art.path} changed while it was being "
                f"packed — nothing was uploaded\n"
                f"    was {digest}\n"
                f"    now {again}\n"
                f"    wait for whatever is writing it to finish, then "
                f"re-run `litkit push`")
        packed = bundle.stat().st_size
        if dry_run:
            print(f"  {art.name:9} would upload {human(packed)} -> {art.key}")
            return False
        print(f"  {art.name:9} uploading   {human(packed)}"
              f" ({100 * packed / max(size, 1):.0f}% of raw) -> {art.key}",
              flush=True)
        ctx.remote.upload(bundle, art.key, "application/zstd")
        ctx.manifest.set(art.name, {
            "kind": "archive",
            "key": art.key,
            "path": art.path.as_posix(),
            "tree_hash": digest,
            "archive_sha256": file_sha256(bundle),
            "archive_bytes": packed,
            "raw_bytes": size,
            "files": n,
        })
    return True


# ---------------------------------------------------------------- mirror ----
# File for file, with a sha256 for each. Slower to publish than a bundle, but a
# reader can fetch one file without the rest, and a re-publish after a partial
# re-run uploads only what actually moved.

def _walk(root: Path, art) -> list[Path]:
    base = root / art.path
    if base.is_file():
        return [base]
    if not base.exists():
        return []
    out = []
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.name.endswith(".part"):
            continue
        if SKIP_DIRS & set(p.relative_to(base).parts):
            continue
        if art.include and p.suffix not in art.include:
            continue
        out.append(p)
    return out


def _local_index(ctx, art) -> tuple[dict[str, dict], int]:
    idx, total = {}, 0
    for p in _walk(ctx.cfg.root, art):
        rel = p.relative_to(ctx.cfg.root).as_posix()
        # A name that cannot be written into the manifest cannot be published:
        # the next reader would refuse the whole document over one file.
        try:
            paths.relative(rel, what=f"artifact {art.name!r}")
        except paths.Unsafe as e:
            raise SystemExit(f"  {e}\n  rename it, or exclude it with "
                             f"`include`, before publishing") from None
        size = p.stat().st_size
        idx[rel] = {"path": rel, "size": size, "sha256": file_sha256(p)}
        total += size
    return idx, total


def _unchanged(path: Path, e: dict) -> bool:
    """Is the file still the bytes `_local_index` hashed into `e`?

    Size first, because it settles nearly every real case without re-reading
    a large file.
    """
    try:
        return (path.stat().st_size == e["size"]
                and file_sha256(path) == e["sha256"])
    except OSError:
        return False


def _classify(local: dict, remote: list[dict]) -> tuple[list, list, list]:
    """(missing, stale, extra) — what pull would need, from the reader's side."""
    missing, stale = [], []
    for e in remote:
        got = local.get(e["path"])
        if got is None:
            missing.append(e)
        elif got["sha256"] != e["sha256"]:
            stale.append(e)
    known = {e["path"] for e in remote}
    extra = [p for p in local if p not in known]
    return missing, stale, extra


def mirror_status(ctx, art) -> Report:
    local, total = _local_index(ctx, art)
    remote = ctx.manifest.mirror_files(art.name)
    local_s = f"{len(local):,} files, {human(total)}" if local else "absent"
    remote_s = (f"{len(remote):,} files, "
                f"{human(sum(e['size'] for e in remote))}" if remote else "absent")
    if not remote and not local:
        return Report(local_s, remote_s, ABSENT)
    if not remote:
        return Report(local_s, remote_s, LOCAL_ONLY)
    if not local:
        return Report(local_s, remote_s, REMOTE_ONLY)
    missing, stale, extra = _classify(local, remote)
    if not (missing or stale or extra):
        return Report(local_s, remote_s, IN_SYNC)
    detail = ", ".join(f"{len(x)} {n}" for n, x in
                       (("missing", missing), ("stale", stale), ("extra", extra)) if x)
    return Report(local_s, remote_s, f"{DIFFERS} ({detail})")


def mirror_pull(ctx, art, *, force=False, clean=False, workers=8) -> None:
    remote = ctx.manifest.mirror_files(art.name)
    if not remote:
        print(f"  {art.name:9} not in the bucket")
        return
    local, _ = _local_index(ctx, art)
    missing, stale, extra = _classify(local, remote)
    want = remote if force else missing + stale

    def sweep() -> None:
        """Remove the local files the bucket does not have.

        The only irreversible thing a pull does, and artifact directories are
        git-ignored, so the copy being removed is routinely the only one.
        That is why it happens *after* the download has verified and never
        before: a pull that fails deletes nothing, which is what the module
        docstring promises and what the archive kind already does by sweeping
        inside `_install`.
        """
        for rel in extra:
            print(f"  {art.name:9} removing local extra {rel}")
            with contextlib.suppress(FileNotFoundError):
                (ctx.cfg.root / rel).unlink()

    if not want:
        if clean:
            sweep()
        print(f"  {art.name:9} up to date  ({len(local):,} files)")
        return
    print(f"  {art.name:9} downloading {len(want):,} of {len(remote):,} files"
          f"  ({human(sum(e['size'] for e in want))})", flush=True)

    with _staging(ctx.cfg) as tmp:
        tmp = Path(tmp)
        moves = [(e, tmp.joinpath(*PurePosixPath(e["path"]).parts),
                  _dest(ctx, art, e["path"])) for e in want]
        try:
            ctx.remote.download_many(
                [(e["path"], staged, e["size"]) for e, staged, _ in moves],
                workers=workers)
        except Oversized as e:
            raise SystemExit(f"  {art.name}: {e} — nothing under {art.path} "
                             f"was changed") from None

        bad = [e["path"] for e, staged, _ in moves
               if not staged.exists() or staged.stat().st_size != e["size"]
               or file_sha256(staged) != e["sha256"]]
        if bad:
            raise SystemExit(
                f"  {art.name}: {len(bad)} of {len(want)} file(s) failed "
                f"verification — nothing under {art.path} was changed:\n" +
                "\n".join(f"    {p}" for p in bad[:10]) +
                (f"\n    … and {len(bad) - 10:,} more" if len(bad) > 10 else ""))

        here = _here(ctx, art).resolve()
        blocked = [c for _e, _s, d in moves if (c := _blocked(d, here))]
        if blocked:
            raise _refuse_layout(art, blocked, ctx.cfg.root)

        # Verified, and every destination can be written: from here the
        # working tree may be changed. The layout check comes before the
        # sweep, so a refusal costs nothing at all.
        if clean:
            sweep()
        for _e, staged, dest in moves:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _move(staged, dest)
    print(f"  {art.name:9} {len(want):,} fetched and verified -> {art.path}")


def _mirror_entry(art, local: dict, known: dict, uploaded: set) -> dict:
    """What the bucket holds, as best this push knows.

    A file is listed with its local digest if it was just uploaded or was
    already current. One whose upload did not happen keeps the digest the
    bucket had before, because that is still what is there. One with neither
    is not in the bucket at all, and so is not listed. Building the entry this
    way is what lets the manifest be written even when a push dies partway:
    it describes what happened rather than what was intended.
    """
    files = []
    for rel in sorted(local):
        e = local[rel]
        if rel in uploaded or known.get(rel, {}).get("sha256") == e["sha256"]:
            files.append(e)
        elif rel in known:
            files.append(known[rel])
    return {"kind": "mirror", "path": art.path.as_posix(),
            "raw_bytes": sum(f["size"] for f in files), "files": files}


def mirror_push(ctx, art, *, force=False, dry_run=False) -> bool:
    base = _here(ctx, art)
    local, total = _local_index(ctx, art)
    # An empty directory is a publishable state — it is how deleting the last
    # file reaches the bucket. A missing one is not: `out/` not existing on
    # this machine means the pipeline has not run here, not that the artifact
    # is now empty.
    if not local and not base.exists():
        print(f"  {art.name:9} skipped — {art.path} is not here")
        return False
    known = {e["path"]: e for e in ctx.manifest.mirror_files(art.name)}
    todo = [e for e in local.values()
            if force or known.get(e["path"], {}).get("sha256") != e["sha256"]]
    gone = sorted(known.keys() - local.keys())

    print(f"  {art.name:9} {len(local):,} files, {human(total)} — "
          f"{len(todo):,} changed, {len(local) - len(todo):,} already current")
    if gone:
        print(f"  {art.name:9} {len(gone):,} no longer here — dropping from the "
              f"manifest (the objects stay in the bucket)")
    if dry_run:
        for e in todo[:20]:
            print(f"    {e['size']:>12,}  {e['path']}")
        if len(todo) > 20:
            print(f"    … and {len(todo) - 20:,} more")
        for rel in gone[:20]:
            print(f"    {'drop':>12}  {rel}")
        return False

    uploaded: set[str] = set()
    raced: list[str] = []
    try:
        for i, e in enumerate(todo, 1):
            full = ctx.cfg.root / e["path"]
            ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
            ctx.remote.upload(full, e["path"], ctype)
            # The digest was taken before the upload started, so a file
            # rewritten in between would be published under a digest the
            # object does not have — and every reader would then fail to
            # verify it. Re-reading the file is the only way to know that
            # the bytes which went up are the bytes being described.
            if not _unchanged(full, e):
                raced.append(e["path"])
                print(f"    [{i}/{len(todo)}] {'changed':>12}  {e['path']}"
                      f"  — rewritten mid-upload", flush=True)
                continue
            uploaded.add(e["path"])
            print(f"    [{i}/{len(todo)}] {e['size']:>12,}  {e['path']}", flush=True)
    finally:
        # Even an interrupted push leaves the manifest describing the objects
        # that did land; `cli.cmd_push` commits it on the way out. A file that
        # raced is left out of `uploaded`, so it keeps the digest the bucket
        # had before — or, if it is new, is not listed at all. Either way the
        # next push sees it as changed and re-uploads it.
        ctx.manifest.set(art.name, _mirror_entry(art, local, known, uploaded))
    if raced:
        raise SystemExit(
            f"  {art.name}: {len(raced)} file(s) were rewritten while they "
            f"were uploading, so the bucket now holds bytes this push cannot "
            f"describe:\n" +
            "\n".join(f"    {p}" for p in raced[:10]) +
            (f"\n    … and {len(raced) - 10:,} more" if len(raced) > 10 else "") +
            f"\n    They are not published. Wait for whatever is writing "
            f"{art.path} to finish, then re-run `litkit push`.")
    # Even with nothing uploaded the file list may have shrunk, so the manifest
    # is rewritten whenever it no longer matches what is on disk.
    return bool(todo) or bool(gone)


# ----------------------------------------------------------------- fetch ----
# Inputs published by something outside this repository. There is no manifest
# to compare against, so freshness is the server's ETag, remembered locally.

def _intact(dest: Path, seen: dict) -> bool:
    """Is the local copy still the bytes that were downloaded into it?

    Without this an edited file whose upstream ETag has not moved reads as in
    sync forever, and `pull` never repairs it. Size is compared first because
    it settles nearly every real case without hashing a large input.
    """
    if not seen.get("sha256"):
        return True                    # nothing recorded — a pre-0.1 state file
    try:
        if seen.get("size") is not None and dest.stat().st_size != seen["size"]:
            return False
        return file_sha256(dest) == seen["sha256"]
    except OSError:
        return False


def _fresh(now: dict, seen: dict) -> bool:
    return bool((now.get("etag") and now["etag"] == seen.get("etag")) or
                (now.get("last_modified") and
                 now["last_modified"] == seen.get("last_modified")))


def fetch_status(ctx, art) -> Report:
    dest = _here(ctx, art)
    seen = ctx.state.get("fetch", {}).get(art.name, {})
    here = dest.exists()
    intact = _intact(dest, seen) if here else True
    local_s = "absent"
    if here:
        local_s = human(dest.stat().st_size)
        if not intact:
            local_s += ", modified here"
        elif seen.get("last_modified"):
            local_s += ", " + seen["last_modified"]
    try:
        now = head(art.url)
    except Exception as exc:                                  # noqa: BLE001
        return Report(local_s, f"unreachable ({type(exc).__name__})", DIFFERS)
    remote_s = (human(int(now["size"])) if now.get("size") else "?") + \
               (", " + now["last_modified"] if now.get("last_modified") else "")
    if not here:
        return Report(local_s, remote_s, REMOTE_ONLY)
    return Report(local_s, remote_s,
                  IN_SYNC if (intact and _fresh(now, seen)) else DIFFERS)


def fetch_pull(ctx, art, *, force=False, clean=False) -> None:
    dest = _here(ctx, art)
    seen = ctx.state.setdefault("fetch", {}).get(art.name, {})
    if dest.exists() and not force:
        if not _intact(dest, seen):
            print(f"  {art.name:9} local copy is not what was fetched — "
                  f"replacing it")
        else:
            try:
                if _fresh(head(art.url), seen):
                    print(f"  {art.name:9} up to date  "
                          f"({human(dest.stat().st_size)})")
                    return
            except Exception:                                 # noqa: BLE001
                pass      # a server that will not answer HEAD still answers GET

    print(f"  {art.name:9} fetching    {art.url}", flush=True)
    meta = fetch_url(art.url, dest)
    meta |= {"sha256": file_sha256(dest), "size": dest.stat().st_size}
    ctx.state["fetch"][art.name] = meta
    print(f"  {art.name:9} {human(meta['size'])} -> {art.path}")


def fetch_push(ctx, art, *, force=False, dry_run=False) -> bool:
    print(f"  {art.name:9} not published from here — {art.url} is written by "
          f"something else")
    return False


# -------------------------------------------------------------- dispatch ----

VERBS = {
    "archive": (archive_status, archive_pull, archive_push),
    "mirror": (mirror_status, mirror_pull, mirror_push),
    "fetch": (fetch_status, fetch_pull, fetch_push),
}


def status(ctx, art) -> Report:
    return VERBS[art.kind][0](ctx, art)


def pull(ctx, art, **kw) -> None:
    VERBS[art.kind][1](ctx, art, **kw)


def push(ctx, art, **kw) -> bool:
    return VERBS[art.kind][2](ctx, art, **kw)


# ------------------------------------------------------------------ state ---
# Only the `fetch` kind needs local memory; the others compare against the
# manifest, which is the same everywhere.

def load_state(cfg) -> dict:
    p = cfg.state_file
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {}
        return state if isinstance(state, dict) else {}
    return {}


def save_state(cfg, state: dict) -> None:
    """Write via a sibling and rename, so an interrupted write loses the
    update rather than the file."""
    p = cfg.state_file
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".part")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
