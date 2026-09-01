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
