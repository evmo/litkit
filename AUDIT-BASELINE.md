# AUDIT-BASELINE.md

Findings we have seen and consciously accepted. Audit prompts read this file
first and skip anything listed here. Keep it short; if an entry stops being
true, delete it.

## Not a defect

- **`litkit/data/common.mk:110` — the `clean` guard does *not* miss `~`.**
  Reported by audit-failure-modes on 2026-09-01 as a bypass: a `CLEAN_EXTRA`
  of `~/scratch` supposedly passing the `case "$p" in /*|*..*)` pattern and
  then being tilde-expanded by the `rm -rf` recipe. It is not. Both lines
  expand `$(CLEAN_EXTRA)` into shell text, and the guard's own `for p in
  $(CLEAN_EXTRA)` performs tilde expansion on the word list first — so `p` is
  already `/home/<user>/scratch` when `case` sees it, and `/*` catches it.
  Verified from a real `make clean`, both with `CLEAN_EXTRA` set in a Makefile
  and passed on the command line:

      $ make clean                       # CLEAN_EXTRA ?= ~/scratch
      CLEAN_EXTRA must be repo-relative: /home/evmo/scratch
      make: *** [common.mk:110: clean] Error 1

  The forms where the tilde does survive the `for` — `"~/scratch"` quoted in
  the Makefile, or `~nosuchuser/x` — survive the `rm` identically, so they
  address a literal `./~…` inside the checkout and never leave it. Checking
  the `case` pattern against a quoted `p` in isolation is what makes this look
  real; it is not how the recipe runs. Accepted 2026-09-02.

## Real, and deliberately not changed

- **`litkit/hashing.py:24` — `tree_hash` re-reads every byte on every call,
  and there is no digest cache.**
  Reported by audit-perf on 2026-09-02 as medium, suggesting an index under
  `.litkit/` mapping relative path to `(size, mtime_ns, digest)`, git-style.
  The mechanism is exactly as described, counted live by wrapping
  `kinds.tree_hash`: one full tree read for `status`, for a no-op `pull` and
  for a no-op `push`; three for a real pull (pre-compare, staged
  verification, final message); two for a real push (pre-hash and the
  post-pack race re-read).

  What does not hold up is the scale. The report's own threshold is "visible
  around 50,000 files or 5 GB". The largest real artifact any consumer
  publishes today is owphack's `data/cache` at **2,965 files / 59 MB, which
  `tree_hash` reads in 0.102 s**; its `out` is 10 files / 20 MB in 0.012 s.
  Synthetic trees confirm the curve — 100,000 × 1 KiB is 3.10 s warm against
  a 0.67 s walk-and-stat floor, and 20 × 105 MB is 1.21 s — but nothing is
  within 16× of the knee.

  Against that 0.1 s, the index moves identity's trust from bytes to
  `(size, mtime)` in the one module whose docstring says it looks at neither,
  and its failure mode is silent and data-shaped: a stale entry makes `push`
  say "up to date" and never upload, in repos whose own `sync.toml` says the
  bucket is the only other copy.

  There is also a trap in the suggested shape. The cache must be excluded
  from `archive_pull`'s staged-tree hash at `kinds.py:421` — that hash is
  what proves the bundle is the tree the manifest describes, and `_unpack`
  restores the *publisher's* mtime onto the staged files. Measured: a file
  pulled and left alone has a staged `st_mtime` byte-identical to the local
  one (1788317538.6402206 both sides), so a cache keyed on seconds or on the
  float serves a hit and verifies nothing. Keyed on `st_mtime_ns` it misses
  by 94 ns — but only as a float-rounding artifact of the pax header, not a
  guarantee. "The index only decides which files must be re-read" is not true
  of that call site, and the report does not say so.

  What would change this: a real artifact past roughly 20,000 files or 2 GB.
  Then an index scoped to the *local* tree reads only — `archive_status`, the
  pre-push digest, the pre-pull compare — and explicitly refused at
  `kinds.py:421`, is worth writing. Accepted 2026-09-02.
