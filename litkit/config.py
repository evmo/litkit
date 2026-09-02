"""`sync.toml` — what a repository publishes, and where.

    [remote]
    # Optional. A public HTTPS base for credential-free reads. When it is set,
    # `pull` and `status` go over plain HTTPS and a reader needs no
    # credentials; when it is absent they use the S3 API and .r2.
    base   = "https://artifacts.example.org"
    bucket = "project-artifacts"       # or leave to R2_BUCKET_NAME

    [artifact.cache]
    kind = "archive"                   # one tar.zst bundle, tree-hash identity
    path = "data/cache"
    key  = "v1/data-cache.tar.zst"
    what = "raw API responses, keyed by hash of their parameters"

    [artifact.out]
    kind    = "mirror"                 # file for file, sha256 manifest
    path    = "out"
    include = [".csv", ".json", ".md", ".png"]

    [artifact.positions]
    kind = "fetch"                     # pull-only; someone else publishes it
    url  = "https://data.example.org/positions/all-positions.csv.gz"
    path = "data/all-positions.csv.gz"

Which kind to use is a question about the files, not about taste:

  archive  a tree of many small files, where per-file HTTP would be thousands
           of round trips. Moves as one blob; identity is the hash of the tree.
  mirror   a modest number of individually useful files. Readers can fetch one
           without the rest, and a re-publish only uploads what changed.
  fetch    inputs published by something outside this repository. Pull-only,
           conditional on ETag, with no manifest to compare against.
"""

from __future__ import annotations

import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import paths

KINDS = ("archive", "mirror", "fetch")

CONFIG_NAME = "sync.toml"
STATE_DIR = ".litkit"


@dataclass(frozen=True)
class Artifact:
    name: str
    kind: str
    path: Path            # relative to the repository root
    what: str = ""
    key: str = ""         # archive: object key of the bundle
    url: str = ""         # fetch: where to get it
    include: tuple[str, ...] = ()   # mirror: extension filter, empty = all
    # Excluded from a bare `pull`/`push`; moved only when named. For an input
    # that is tracked in git *and* refetchable, where overwriting it is a
    # deliberate act whose `git diff` is the point — not something setup should
    # do behind you. `status` still reports it, so drift stays visible.
    manual: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind})"


@dataclass(frozen=True)
class Config:
    root: Path
    artifacts: tuple[Artifact, ...]
    base: str = ""        # public HTTPS base, or "" for S3 reads
    bucket: str = ""      # falls back to R2_BUCKET_NAME
    manifest_key: str = "manifest.json"

    def select(self, names: list[str] | None,
               include_manual: bool = False) -> list[Artifact]:
        if not names:
            return [a for a in self.artifacts if include_manual or not a.manual]
        known = {a.name: a for a in self.artifacts}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise SystemExit(f"unknown artifact(s): {', '.join(unknown)}   "
                             f"(this repo has: {', '.join(known)})")
        return [known[n] for n in names]

    @property
    def state_file(self) -> Path:
        return self.root / STATE_DIR / "state.json"


def find_root(start: Path | None = None) -> Path:
    """Walk up for the directory holding sync.toml."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).exists():
            return d
    raise SystemExit(
        f"no {CONFIG_NAME} in {here} or any parent.\n"
        f"litkit runs from inside a repository that declares its artifacts; "
        f"see `litkit mk` for the make targets that call it."
    )


def _rel(raw, name: str, field: str) -> Path:
    """A sync.toml path, checked the same way a manifest path is.

    A config is not remote input, but `path = "../../.."` is a typo away and
    everything downstream — pull, `--clean`, the make `clean` recipe — is
    happy to act on it.
    """
    try:
        return Path(paths.relative(raw, what=f"artifact {name!r}: {field}"))
    except paths.Unsafe as e:
        raise SystemExit(str(e)) from None


def _include(raw, name: str) -> tuple[str, ...]:
    """`include = [".csv", ".json"]` — file suffixes, dots and all.

    Unvalidated this was whatever TOML supplied, wrapped in `tuple`. So
    `include = ".csv"` became the tuple of its four characters, `["csv"]` a
    suffix no file has, and `include = 5` a traceback. The first two match
    nothing, which is not an error anywhere downstream: the local index comes
    back empty, and the next push reports every file as gone and writes an
    empty entry. A typo that unpublishes an artifact should not be a quiet
    one, so the shape is checked where it is read.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise SystemExit(f"artifact {name!r}: include must be a list of file "
                         f'suffixes, e.g. [".csv", ".json"], not {raw!r}')
    bad = [x for x in raw if not x.startswith(".") or x == "." or "/" in x]
    if bad:
        raise SystemExit(f"artifact {name!r}: include holds file suffixes, "
                         f'written with their dot like ".csv" — '
                         f"{', '.join(repr(b) for b in bad)} "
                         f"{'are' if len(bad) > 1 else 'is'} not one")
    return tuple(raw)


def _check_layout(arts: list[Artifact]) -> None:
    """No artifact may contain another, and no two archives may share a key.

    Overlap is never what someone meant: a mirror over `out` would sweep up a
    `fetch` landing in `out/`, publish it, and delete it again on the next
    `pull --clean`. Two archives sharing a key would each overwrite the other
    on every push.
    """
    for i, a in enumerate(arts):
        for b in arts[i + 1:]:
            ap, bp = a.path.as_posix(), b.path.as_posix()
            if paths.is_under(ap, bp) or paths.is_under(bp, ap):
                raise SystemExit(
                    f"artifacts {a.name!r} ({ap}) and {b.name!r} ({bp}) "
                    f"overlap — one contains the other, so a push or a "
                    f"`pull --clean` of either would act on the other's files")
    keys: dict[str, str] = {}
    for a in arts:
        if a.kind == "archive":
            if a.key in keys:
                raise SystemExit(f"artifacts {keys[a.key]!r} and {a.name!r} "
                                 f"both publish to key {a.key!r}")
            keys[a.key] = a.name


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    raw = tomllib.loads((root / CONFIG_NAME).read_text(encoding="utf-8"))

    remote = raw.get("remote", {})
    arts = []
    for name, spec in raw.get("artifact", {}).items():
        kind = spec.get("kind")
        if kind not in KINDS:
            raise SystemExit(f"artifact {name!r}: kind must be one of "
                             f"{', '.join(KINDS)}, not {kind!r}")
        if "path" not in spec:
            raise SystemExit(f"artifact {name!r}: needs a `path`")
        if kind == "archive" and not spec.get("key"):
            raise SystemExit(f"artifact {name!r}: an archive needs a `key` "
                             f"(the object name of the bundle in the bucket)")
        if kind == "fetch":
            url = spec.get("url", "")
            if not url:
                raise SystemExit(f"artifact {name!r}: a fetch needs a `url`")
            # urllib will happily open file:// and ftp://; a fetch is an input
            # published on the web, and anything else is a typo or a trick.
            if urllib.parse.urlparse(url).scheme not in ("http", "https"):
                raise SystemExit(f"artifact {name!r}: url must be http(s), "
                                 f"not {url!r}")
        if kind != "mirror" and "include" in spec:
            # Only `_walk` reads it, and only a mirror walks. Accepting it
            # elsewhere reads as a filter that is quietly doing nothing.
            raise SystemExit(f"artifact {name!r}: `include` chooses which "
                             f"files a mirror publishes, and nothing reads it "
                             f"on {'an' if kind[:1] in 'aeiou' else 'a'} "
                             f"{kind}. Remove it, or make this a mirror.")
        if kind == "archive":
            _rel(spec["key"], name, "key")
        arts.append(Artifact(
            name=name,
            kind=kind,
            path=_rel(spec["path"], name, "path"),
            what=spec.get("what", ""),
            key=spec.get("key", ""),
            url=spec.get("url", ""),
            include=_include(spec.get("include"), name),
            manual=bool(spec.get("manual", False)),
        ))

    if not arts:
        raise SystemExit(f"{root / CONFIG_NAME} declares no [artifact.*]")
    _check_layout(arts)

    manifest_key = remote.get("manifest", "manifest.json")
    try:
        paths.relative(manifest_key, what="[remote] manifest")
    except paths.Unsafe as e:
        raise SystemExit(str(e)) from None

    return Config(
        root=root,
        artifacts=tuple(arts),
        base=remote.get("base", "").rstrip("/"),
        bucket=remote.get("bucket", ""),
        manifest_key=manifest_key,
    )
