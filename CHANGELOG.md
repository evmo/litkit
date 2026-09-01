# Changelog

## Unreleased

Integrity work, prompted by an audit. Nothing in `sync.toml` changes, and
existing buckets are read exactly as before — but a few things that used to be
tolerated are now refused, and two commands exit differently.

### Fixed

- **A push could publish a digest the bucket does not have.** Both kinds
  hashed before they uploaded and never looked again, so an artifact rewritten
  in between — `make build` still running under `make publish` — was described
  by a manifest that no longer matched what went up. Push exited 0 and every
  reader's `pull` failed verification until someone happened to push again.
  An archive re-hashes its tree after packing and uploads nothing if it moved;
  a mirror re-reads each file after its upload and leaves out the ones that
  did, then exits non-zero naming them.

- **A failed `pull --clean` deleted local-only files anyway.** The sweep of
  files the bucket does not have ran *before* the download, so a pull that then
  failed verification had already unlinked the only copy of each — artifact
  directories are git-ignored, and a file absent from the bucket has no other
  copy — while the error it printed said nothing under the artifact had been
  changed. The sweep now runs after verification, where the `archive` kind
  already had it.

- **Manifest paths could escape the checkout.** Entries were joined to the
  repository root and written, so `../../…` in a manifest — or a symlinked
  parent directory — could put a download outside the repo, or delete
  something outside it under `--clean`. Every path from the manifest and from
  `sync.toml` is now checked (`litkit.paths`), and manifest entries are
  contained to the artifact that claims them.
- **A failed mirror pull left corrupt files installed.** Downloads went
  straight to their destinations and were verified afterwards, so a checksum
  failure exited non-zero with the bad bytes already in place. Files are now
  staged, verified against the manifest's size *and* digest, and moved into
  place only once every file in the pull has passed.
- **A failed archive pull modified the tree and reported success.** Bundles
  were extracted into the repository and the tree hash checked afterwards,
  where a mismatch printed a note and exited 0. Extraction is now staged, the
  whole tree is hashed before anything is installed, and a mismatch is a
  non-zero exit with the working tree untouched.
- **Deleting the last file in a mirror could not be published.** `push` treated
  "no matching files" as "nothing to do", so an artifact could never reach an
  empty state. An empty directory now publishes an empty manifest entry; a
  *missing* directory is still a skip.
- **A locally edited `fetch` input read as in sync forever.** Freshness was the
  server's ETag alone. The size and digest recorded at download time are now
  checked too, so an edited file reports `DIFFERS` and is re-fetched.
- **An empty bucket blocked unrelated `fetch` artifacts.** `pull` aborted the
  whole command; it now skips what the bucket owes, still fetches the external
  inputs, and returns non-zero.
- **Orphaned manifest records were dropped on the second push.** A legacy flat
  `files` list was preserved by `dump()` but ignored by the next `load()`.
- **`.part` files survived an interrupted download**, and the local state file
  could be truncated by one. Both are now written to a sibling and renamed.
- **Object keys were concatenated into public URLs**, so a key containing a
  space, `#` or `?` addressed the wrong thing. Keys are percent-encoded.
- **An archive of a symlinked artifact directory published one dangling link
  and nothing else.** `tree_hash` reads through such a directory and `tar.add`
  did not, so the two disagreed about what had been published. Found while
  testing the containment work.

### Added

- Manifests are validated on load: types, digest format, sizes, duplicate
  entries, artifact kinds, required archive fields, and caps on document size
  and entry count. Malformed JSON is a clear error rather than a traceback.
- The manifest is committed with an `If-Match` on the copy the run read, so two
  maintainers publishing at once get a refusal instead of a lost update. Stores
  without conditional writes fall back, with a note.
- A push that dies partway now still commits a manifest describing what
  actually reached the bucket, rather than leaving one that describes a state
  that no longer exists.
- `sync.toml` is checked for overlapping artifact paths, two archives sharing a
  key, non-`http(s)` fetch URLs, and paths that leave the repository.
- `doctor` reports read and write readiness separately: a contributor pulling
  from a public bucket is no longer told their missing credentials are a
  failure.
- Caps on archive member count and unpacked size, and on the number of bytes a
  download will accept beyond what the manifest promised.
- A leftover staging tree from a pull that was killed outright — which can be
  the size of the artifact — is swept away by the next run.
- An unreachable bucket reads as one line rather than as a traceback.
- Tests for all of the above: 95, up from 26.
- CI across Python 3.12–3.14, `ruff`, a lockfile check, and a wheel build that
  proves `common.mk` and `py.typed` are still packaged.
- `py.typed`, project URLs and classifiers.

### Changed

- `common.mk`'s `clean` no longer word-splits `$(shell find …)` into `rm -rf`,
  and refuses a `CLEAN_EXTRA` that is absolute or contains `..`. Repositories
  vendoring it should run `make mk-update`.
- `--workers` must be at least 1; 0 used to fail inside the thread pool.
- An artifact directory that is a symlink is followed, not refused — but the
  bucket's names still cannot wander out of wherever it points.

## 0.1.0

First release: `archive`, `mirror` and `fetch` artifact kinds, the shared
`common.mk`, and transparent reading of the two older manifest shapes.
