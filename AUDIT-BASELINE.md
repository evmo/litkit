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

- **`litkit/kinds.py:285` — `_install`'s merge loop is not worth restructuring,
  and the reason the audit gave for it is wrong.**
  audit-perf on 2026-09-02 raised this inside its "pull walks and hashes more
  times than verification needs" finding: "at 50,000 files the split was
  0.94 s in layout checks, 0.18 s in renames, and 2.46 s in the per-file
  `mkdir` and path arithmetic, over only 2,048 distinct parent directories".
  The two passes that finding was mostly about — the stray walk and the
  closing hash — are fixed. This third part is not.

  Measured here at 50,000 files over the same 2,048 parents: the `mkdir` is
  not the cost. Remembering which parents already exist cuts the calls from
  50,000 to 2,048 and the move loop from 1.29 s to 1.24 s — 0.05 s, because
  `mkdir(exist_ok=True)` on a directory that is already there is about a
  microsecond. What actually costs is the per-file layout check: `_blocked`
  is 1.72–1.89 s, and cProfile puts that in `pathlib` object construction
  (850,000 `PurePath.__init__`, 700,000 `_PathParents.__getitem__`) from
  walking `.parents` once per file rather than once per directory. Memoising
  the ancestor walk per distinct parent gives 1.23 s for a byte-identical
  answer.

  That is 0.66 s at 50,000 files. The largest artifact any consumer merges
  today is 2,965 files, where it is under 0.05 s, and unlike the two passes
  that were removed this one is not a deletion — it splits `_blocked` in two
  and puts a cache in its caller, on the path that decides whether a pull is
  allowed to touch the working tree at all.

  What would change this: a real artifact past roughly 50,000 files that is
  pulled without `--clean`, which is the only path this loop runs on.
  Accepted 2026-09-02.

- **`litkit/remote.py:180` — the public read path really does open a
  connection per object, and it is staying that way for now.**
  Reported by audit-perf on 2026-09-02 as low. Reproduced: a `download_many`
  of 40 objects at 8 workers against a local HTTP/1.1 server that offers
  keep-alive accepted **40 TCP connections from 40 distinct client ports**.
  `urllib.request.urlopen` does not pool, exactly as reported.

  The half the audit said it could not measure — "that part could not be
  measured here and is inferred" — is measured now, against the real bucket
  a reader pulls from. Ten GETs of `nyad-currents.ultraswimming.org/
  manifest.json` (157,817 B), DNS warmed, best of three rounds:

      urlopen per object (today)   210.2 ms   rounds [213, 210, 211]
      one reused connection         85.7 ms   rounds [ 88,  86,  96]
      handshake per object         124.5 ms

  So the inference was right, and slightly conservative. The largest real
  mirror is nyad-currents at 840 files across two artifacts, where that is
  **13.1 s of a fresh `make sync`** at 8 workers — a sync that also moves
  about 1 GB of NetCDF.

  Not fixed because of what the fix costs. `urlopen` is carrying redirects,
  the `HTTPError` codes that `_transient` reads to decide what is worth
  retrying, the 404 that `get_bytes` turns into `Missing`, chunked bodies and
  proxy configuration. Hand-managed `http.client` connections in a
  `threading.local` re-derive all of that, plus discard-and-reconnect on a
  socket that died between requests — new machinery on the one path every
  credential-free reader depends on, and the path where the last two audits
  already found two real bugs (no retries at all, and a conditional PUT
  misreading its own success). Thirteen seconds once per clone does not buy
  that.

  What would change this: a mirror of a few thousand small files, where the
  handshakes cost more than the bytes do — at 5,000 files it is 78 s — or a
  profile of a real `make sync` where connect time beats transfer time.
  Accepted 2026-09-02.
