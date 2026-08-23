# litkit

Shared plumbing for literate-analysis repositories, so that they answer to the
same commands, keep their data in the same shape, and move it to and from
object storage with one implementation instead of three.

It exists for a small, specific reason: three repositories had three different
ways to start (a `Makefile`, a `run_*.py`, and a README you copy commands out
of) and three different sync mechanisms, and remembering which was which cost
more than the code was worth.

## The convention

One rule, and the rest follows from wanting it to be checkable:

> **The pipeline writes `out/`. The documents only read it.**

So the layout is:

```
Makefile  common.mk  sync.toml  pyproject.toml  uv.lock  _quarto.yml
reports/     *.qmd sources; *.md tracked, *.html not
scripts/     stage entry points        <pkg>/  importable library
data/        inputs      — git-ignored, synced from a bucket
out/         artifacts   — git-ignored, synced from a bucket
.litkit/     litkit's own state and pull staging — git-ignore it
```

and the commands are:

```
make            sync + render — the "I just cloned this" path
make env        install from the lockfile
make sync       pull data/ and out/ from object storage
make build      run the pipeline: data/ -> out/
make render     reports/*.qmd -> the formats this repo publishes
make preview    live preview of one report
make check      tests and validators
make publish    push data/ and out/ (maintainer only)
make reproduce  everything, from an empty checkout
make clean      remove derived files
```

Because a document computes nothing, `freeze: false` is safe everywhere — a
rendered report cannot be quoting a stale number, since it has no cache to
quote from.

## Using it

```sh
uv add "litkit[all] @ git+https://github.com/evmo/litkit"
uv run litkit mk > common.mk        # then commit it
```

A repository's `Makefile` is then the small part that is genuinely its own:

```make
FORMATS   := gfm html
REPRODUCE := env fetch build render

-include common.mk
common.mk: ; $(UV) litkit mk > $@

build:
	uv run python -m mypkg.pipeline all

check:
	uv run pytest
```

`make help` lists every target; `make doctor` checks the toolchain, the
config and the credentials.

The extras track what a repo actually does: `litkit` alone is stdlib-only and
enough to pull public files, `litkit[s3]` adds publishing, `litkit[archive]`
adds the bundled kind, `litkit[all]` is both.

## Sync

Each repository declares what it publishes in `sync.toml`:

```toml
[remote]
base = "https://artifacts.example.org"   # optional: public reads

[artifact.cache]
kind = "archive"
path = "data/cache"
key  = "v1/data-cache.tar.zst"
what = "raw API responses, keyed by hash of their parameters"
```

Three kinds, chosen by what the files are rather than by taste:

| kind | for | identity | credentials |
|---|---|---|---|
| `archive` | a tree of thousands of small files | hash of the file tree | to read a private bucket; always to write |
| `mirror` | a modest number of individually useful files | sha256 per file | none to read a public one; always to write |
| `fetch` | inputs published by something outside the repo | the server's ETag | none — pull-only |

`archive` identity is the hash of the *tree* — every relative path and the
sha256 of every file — and never of the bundle, because zstd is not
bit-reproducible across versions and hashing the bundle would make an unchanged
corpus look changed on a different machine.

One `manifest.json` per bucket describes everything in it. Two older manifest
shapes are read transparently, so pointing litkit at an existing bucket does
not mean re-uploading it.

Everything a reader trusts comes from that one object, so it is not taken on
faith. Its shape and every digest in it are checked before anything is acted
on, and no path in it can name a file outside the artifact that claims it.
Both pulls stage: a bundle or a set of files is downloaded in full and checked
against the manifest before a single byte of the working tree changes, so a
failed pull is a non-zero exit and an untouched checkout rather than a
half-updated one.

Publishing is not a transaction, and litkit does not claim to be one. Objects
are stored under their own names — that is what makes a public bucket
browsable — so a push that dies partway has already changed it. What litkit
does promise is that the manifest never names an object that was not uploaded,
that it is then written to describe what actually landed, and that it goes up
with an `If-Match` on the copy that push read, so two maintainers publishing at
once get a refusal rather than a silently lost entry.

```
litkit pull [name...]      bucket -> here      (make sync)
litkit push [name...]      here -> bucket      (make publish)
litkit status [name...]    compare; non-zero if they differ
litkit mk                  print common.mk
litkit doctor              check toolchain, config, credentials
```

Credentials come from `.r2` at the repository root (git-ignored) or from the
environment, which wins — a laptop supplies them without an export, CI without
a file.

## License

MIT.
