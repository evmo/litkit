"""Tests for the parts where being wrong costs real data.

The manifest migration matters most: three buckets already exist, written by
two earlier tools, and litkit has to read them without a re-upload. After that
come the two properties everything else rests on — that nothing from the
bucket lands outside the checkout, and that a pull which fails verification
leaves the previous local copy alone.

    uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import dataclasses
import http.client
import io
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from litkit import cli, config, creds, kinds, paths
from litkit import remote as transport
from litkit.hashing import file_sha256, tree_hash
from litkit.manifest import Malformed, Manifest
from litkit.remote import Conflict, Missing, Oversized, Remote, _url

SYNC_TOML = """\
[remote]
base = "https://example.invalid/mirror"

[artifact.cache]
kind = "archive"
path = "data/cache"
key  = "v1/data-cache.tar.zst"
what = "raw API responses"

[artifact.out]
kind = "mirror"
path = "out"
include = [".csv", ".json"]
"""

# Stand-ins that are the right *shape*: the manifest reader rejects a digest
# that is not 64 hex characters, so fixtures cannot use "aa" any more.
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


class Fake:
    """An in-memory bucket that behaves like litkit.remote.Remote."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.public = True
        self.where = "memory://test"
        self.fail_upload_after = None      # nth upload raises
        self.uploads = 0
        self._seq = 0

    def _stamp(self, key):
        self._seq += 1
        self.etags[key] = f"etag-{self._seq}"

    def get_bytes(self, key, *, limit=None):
        if key not in self.objects:
            raise Missing(key)
        body = self.objects[key]
        if limit is not None and len(body) > limit:
            raise Oversized(key)
        return body, self.etags.get(key)

    def download(self, key, dest, max_bytes=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.objects[key])

    def download_many(self, jobs, workers=8, label=""):
        for job in jobs:
            self.download(*job)

    def upload(self, src, key, content_type):
        self.uploads += 1
        if self.fail_upload_after and self.uploads > self.fail_upload_after:
            raise OSError("connection reset")
        self.objects[key] = Path(src).read_bytes()
        self._stamp(key)

    def put_bytes(self, key, body, content_type, *, if_match=None,
                  if_absent=False):
        if if_match is not None and self.etags.get(key) != if_match:
            raise Conflict(f"{key} moved under us")
        if if_absent and key in self.objects:
            raise Conflict(f"{key} appeared under us")
        self.objects[key] = body
        self._stamp(key)


class Ctx:
    def __init__(self, cfg, remote, manifest):
        self.cfg, self.remote, self.manifest = cfg, remote, manifest
        self.state = {}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="litkit-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "sync.toml").write_text(SYNC_TOML)
        self.cfg = config.load(self.root)

    def write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def reload(self, toml: str):
        (self.root / "sync.toml").write_text(toml)
        return config.load(self.root)


# --- configuration ----------------------------------------------------------

class TestConfig(Base):
    def test_parses_both_kinds(self):
        names = {a.name: a.kind for a in self.cfg.artifacts}
        self.assertEqual(names, {"cache": "archive", "out": "mirror"})
        self.assertEqual(self.cfg.base, "https://example.invalid/mirror")

    def test_select_rejects_unknown(self):
        self.assertEqual([a.name for a in self.cfg.select(["out"])], ["out"])
        with self.assertRaises(SystemExit):
            self.cfg.select(["nope"])

    def test_manual_artifacts_are_skipped_by_a_bare_select(self):
        cfg = self.reload(
            SYNC_TOML +
            '\n[artifact.stages]\nkind = "fetch"\nmanual = true\n'
            'url = "https://example.invalid/stages.psv"\n'
            'path = "sources/stages.psv"\n')
        self.assertNotIn("stages", [a.name for a in cfg.select(None)])
        # ...but named explicitly, or asked for by status, it is there
        self.assertEqual([a.name for a in cfg.select(["stages"])], ["stages"])
        self.assertIn("stages",
                      [a.name for a in cfg.select(None, include_manual=True)])

    def test_archive_without_key_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.reload('[artifact.x]\nkind = "archive"\npath = "data"\n')

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.reload('[artifact.x]\nkind = "rsync"\npath = "data"\n')

    def test_escaping_path_is_rejected(self):
        for bad in ("../elsewhere", "/etc", "out/../..", "."):
            with self.subTest(bad), self.assertRaises(SystemExit):
                self.reload(f'[artifact.x]\nkind = "mirror"\npath = "{bad}"\n')

    def test_escaping_archive_key_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.reload('[artifact.x]\nkind = "archive"\npath = "data"\n'
                        'key = "../../../etc/passwd"\n')

    def test_overlapping_artifacts_are_rejected(self):
        with self.assertRaises(SystemExit) as e:
            self.reload(SYNC_TOML +
                        '\n[artifact.inner]\nkind = "fetch"\n'
                        'url = "https://example.invalid/x.csv"\n'
                        'path = "out/x.csv"\n')
        self.assertIn("overlap", str(e.exception))

    def test_two_archives_may_not_share_a_key(self):
        with self.assertRaises(SystemExit) as e:
            self.reload(SYNC_TOML +
                        '\n[artifact.other]\nkind = "archive"\n'
                        'path = "data/other"\nkey = "v1/data-cache.tar.zst"\n')
        self.assertIn("both publish to key", str(e.exception))

    def test_fetch_url_must_be_http(self):
        with self.assertRaises(SystemExit):
            self.reload('[artifact.x]\nkind = "fetch"\npath = "data/x"\n'
                        'url = "file:///etc/passwd"\n')

    def test_manifest_key_must_be_relative(self):
        with self.assertRaises(SystemExit):
            self.reload('[remote]\nmanifest = "/etc/passwd"\n\n'
                        '[artifact.x]\nkind = "mirror"\npath = "out"\n')


# --- path containment -------------------------------------------------------

class TestPaths(Base):
    def test_relative_rejects_the_usual_suspects(self):
        for bad in ("../x", "/etc/passwd", "", ".", "..", "a/../b", "a//b",
                    "a/", "./a", "C:/x", "a\\b", "x\0y", "x" * 2000):
            with self.subTest(bad), self.assertRaises(paths.Unsafe):
                paths.relative(bad)

    def test_relative_accepts_an_ordinary_name(self):
        self.assertEqual(paths.relative("out/sub/a.csv").as_posix(),
                         "out/sub/a.csv")

    def test_resolve_under_rejects_a_symlinked_parent(self):
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / "out").symlink_to(outside)
        with self.assertRaises(paths.Unsafe):
            paths.resolve_under(self.root, "out/a.csv")

    def test_resolve_under_refuses_to_write_through_a_symlink(self):
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / "out").mkdir()
        (self.root / "out/a.csv").symlink_to(outside / "target.csv")
        with self.assertRaises(paths.Unsafe):
            paths.resolve_under(self.root, "out/a.csv")

    def test_resolve_under_allows_a_path_that_does_not_exist_yet(self):
        got = paths.resolve_under(self.root, "out/deep/a.csv")
        self.assertEqual(got, self.root / "out/deep/a.csv")

    def test_under_artifact_rejects_another_artifacts_path(self):
        art = {a.name: a for a in self.cfg.artifacts}["out"]
        with self.assertRaises(paths.Unsafe):
            paths.under_artifact(self.root, art, "data/cache/x.csv")

    def test_under_artifact_follows_a_symlinked_artifact_directory(self):
        """`out -> /mnt/scratch` is a thing people do, and it is allowed."""
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / "out").symlink_to(outside)
        art = {a.name: a for a in self.cfg.artifacts}["out"]
        self.assertEqual(paths.under_artifact(self.root, art, "out/a.csv"),
                         outside / "a.csv")

    def test_under_artifact_still_bounds_a_symlinked_directory(self):
        """...but the bucket's names may not then wander out of it either."""
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / "out").symlink_to(outside)
        (outside / "sub").symlink_to(outside.parent)
        art = {a.name: a for a in self.cfg.artifacts}["out"]
        with self.assertRaises(paths.Unsafe):
            paths.under_artifact(self.root, art, "out/sub/elsewhere.csv")


# --- credentials on disk ----------------------------------------------------

class TestCreds(Base):
    """`.r2` is a live write credential; the mode it sits at is part of it."""

    FULL = ("R2_ACCOUNT_ID=acct\nR2_ACCESS_KEY_ID=akid\n"
            "R2_SECRET_ACCESS_KEY=" + "b" * 64 + "\nR2_BUCKET_NAME=buck\n")

    def setUp(self):
        super().setUp()
        # The environment wins over the file, so a stray R2_* in the shell
        # running the tests would otherwise decide their outcome. patch.dict
        # puts back whatever was there when each test ends.
        self.enterContext(unittest.mock.patch.dict(os.environ, clear=False))
        for k in (*creds.KEYS, "R2_BUCKET"):
            os.environ.pop(k, None)

    def creds_file(self, text: str, mode: int) -> Path:
        p = self.root / creds.CREDS_NAME
        p.write_text(text)
        p.chmod(mode)
        return p

    def test_loads_a_private_file(self):
        self.creds_file(self.FULL, 0o600)
        self.assertEqual(creds.load(self.root)["R2_BUCKET_NAME"], "buck")

    def test_refuses_a_world_readable_secret(self):
        self.creds_file(self.FULL, 0o644)
        with self.assertRaises(SystemExit) as e:
            creds.load(self.root)
        self.assertIn("chmod 600", str(e.exception))

    def test_refuses_a_group_readable_secret(self):
        self.creds_file(self.FULL, 0o640)
        with self.assertRaises(SystemExit):
            creds.load(self.root)

    def test_refuses_a_group_writable_secret(self):
        """Not just readable: another user substituting the key is worse."""
        self.creds_file(self.FULL, 0o620)
        with self.assertRaises(SystemExit):
            creds.load(self.root)

    def test_a_blank_template_is_not_a_secret(self):
        """`.r2` naming the bucket while CI supplies the key is legitimate."""
        self.creds_file(creds.EXAMPLE + "R2_BUCKET_NAME=buck\n", 0o644)
        os.environ.update(R2_ACCOUNT_ID="acct", R2_ACCESS_KEY_ID="akid",
                          R2_SECRET_ACCESS_KEY="c" * 64)
        self.assertEqual(creds.load(self.root)["R2_BUCKET_NAME"], "buck")

    def test_an_env_supplied_key_does_not_excuse_the_file(self):
        """The exposure is the bytes on disk, not which copy litkit used."""
        self.creds_file(self.FULL, 0o644)
        os.environ["R2_SECRET_ACCESS_KEY"] = "d" * 64
        with self.assertRaises(SystemExit):
            creds.load(self.root)

    def test_no_file_at_all_still_reports_what_is_missing(self):
        with self.assertRaises(SystemExit) as e:
            creds.load(self.root)
        self.assertIn("missing R2 credentials", str(e.exception))


# --- identity ---------------------------------------------------------------

class TestTreeHash(Base):
    def test_same_bytes_same_hash_regardless_of_creation_order(self):
        self.write("a/one.txt", "1")
        self.write("a/two.txt", "2")
        first = tree_hash(self.root / "a")

        shutil.rmtree(self.root / "a")
        self.write("a/two.txt", "2")
        self.write("a/one.txt", "1")
        self.assertEqual(tree_hash(self.root / "a"), first)

    def test_content_change_changes_hash(self):
        self.write("a/one.txt", "1")
        before = tree_hash(self.root / "a")[0]
        self.write("a/one.txt", "2")
        self.assertNotEqual(tree_hash(self.root / "a")[0], before)

    def test_rename_changes_hash(self):
        self.write("a/one.txt", "1")
        before = tree_hash(self.root / "a")[0]
        (self.root / "a/one.txt").rename(self.root / "a/uno.txt")
        self.assertNotEqual(tree_hash(self.root / "a")[0], before)

    def test_absent_tree(self):
        self.assertEqual(tree_hash(self.root / "nothing"), ("", 0, 0))


# --- manifest migration -----------------------------------------------------

class TestManifestMigration(Base):
    def test_reads_new_format_unchanged(self):
        raw = {"artifacts": {"out": {"kind": "mirror", "path": "out",
                                     "files": [{"path": "out/a.csv", "size": 1,
                                                "sha256": H1}]}}}
        m = Manifest._migrate(raw, self.cfg)
        self.assertEqual(m.get("out")["kind"], "mirror")
        self.assertEqual(len(m.mirror_files("out")), 1)

    def test_legacy_archive_entries_get_a_kind(self):
        """An older bucket: artifacts keyed by name, no `kind` field."""
        raw = {"artifacts": {"cache": {
            "key": "v1/data-cache.tar.zst", "path": "data/cache",
            "tree_hash": H1, "archive_sha256": H2,
            "archive_bytes": 10, "raw_bytes": 100, "files": 5}}}
        m = Manifest._migrate(raw, self.cfg)
        self.assertEqual(m.get("cache")["kind"], "archive")
        self.assertEqual(m.get("cache")["tree_hash"], H1)

    def test_legacy_flat_file_list_is_grouped_onto_artifacts(self):
        """An older bucket still: one flat `files` list, no artifacts at all."""
        raw = {"generated": "2026-01-01T00:00Z", "files": [
            {"path": "out/a.csv", "size": 1, "sha256": H1},
            {"path": "out/sub/b.json", "size": 2, "sha256": H2},
            {"path": "data/cache/c.bin", "size": 3, "sha256": H3},
        ]}
        m = Manifest._migrate(raw, self.cfg)
        self.assertEqual({e["path"] for e in m.mirror_files("out")},
                         {"out/a.csv", "out/sub/b.json"})
        self.assertEqual(m.get("out")["kind"], "mirror")
        # data/cache is declared as an archive here, but the legacy list is
        # still grouped onto it by path — the point is that no record is lost.
        self.assertEqual(len(m.get("cache")["files"]), 1)
        self.assertEqual(m.orphans, [])

    def test_files_no_artifact_claims_are_kept_as_orphans(self):
        raw = {"files": [{"path": "elsewhere/x", "size": 1, "sha256": H1}]}
        m = Manifest._migrate(raw, self.cfg)
        self.assertEqual(len(m.orphans), 1)
        self.assertIn(b"elsewhere/x", m.dump())

    def test_orphans_survive_a_round_trip(self):
        """dump() writes them alongside `artifacts`; load must not drop them."""
        raw = {"files": [{"path": "elsewhere/x", "size": 1, "sha256": H1}]}
        once = Manifest._migrate(raw, self.cfg)
        once.set("out", {"kind": "mirror", "path": "out", "files": []})
        import json
        twice = Manifest._migrate(json.loads(once.dump()), self.cfg)
        self.assertEqual(len(twice.orphans), 1)

    def test_prefix_match_does_not_bleed_across_artifacts(self):
        """`out` must not claim `outtakes/`."""
        raw = {"files": [{"path": "outtakes/x", "size": 1, "sha256": H1}]}
        m = Manifest._migrate(raw, self.cfg)
        self.assertEqual(m.mirror_files("out"), [])
        self.assertEqual(len(m.orphans), 1)

    def test_missing_manifest_is_empty_not_an_error(self):
        m = Manifest.load(Fake(), self.cfg)
        self.assertTrue(m.empty)
        self.assertFalse(m.existed)


# --- manifest validation ----------------------------------------------------

class TestManifestValidation(Base):
    def mirror(self, *files):
        return {"artifacts": {"out": {"kind": "mirror", "path": "out",
                                      "files": list(files)}}}

    def bad(self, raw):
        with self.assertRaises(Malformed) as e:
            Manifest._migrate(raw, self.cfg)
        return str(e.exception)

    def test_traversal_in_a_file_path_is_refused(self):
        self.assertIn("..", self.bad(self.mirror(
            {"path": "out/../../escape", "size": 1, "sha256": H1})))

    def test_absolute_file_path_is_refused(self):
        self.bad(self.mirror({"path": "/etc/passwd", "size": 1, "sha256": H1}))

    def test_a_file_outside_its_artifact_is_refused(self):
        self.assertIn("not under out/", self.bad(self.mirror(
            {"path": "data/cache/x", "size": 1, "sha256": H1})))

    def test_duplicate_entries_are_refused(self):
        e = {"path": "out/a.csv", "size": 1, "sha256": H1}
        self.assertIn("listed twice", self.bad(self.mirror(e, dict(e))))

    def test_a_digest_that_is_not_a_digest_is_refused(self):
        self.bad(self.mirror({"path": "out/a.csv", "size": 1, "sha256": "aa"}))

    def test_a_negative_size_is_refused(self):
        self.bad(self.mirror({"path": "out/a.csv", "size": -1, "sha256": H1}))

    def test_a_size_that_is_a_string_is_refused(self):
        self.bad(self.mirror({"path": "out/a.csv", "size": "1", "sha256": H1}))

    def test_an_unknown_kind_is_refused(self):
        self.bad({"artifacts": {"out": {"kind": "rsync", "path": "out"}}})

    def test_an_archive_without_a_key_is_refused(self):
        self.bad({"artifacts": {"cache": {"kind": "archive", "path": "data",
                                          "tree_hash": H1,
                                          "archive_sha256": H2}}})

    def test_a_non_object_document_is_refused(self):
        self.bad(["not", "a", "manifest"])

    def test_invalid_json_is_refused(self):
        remote = Fake()
        remote.objects[self.cfg.manifest_key] = b"{not json at all"
        with self.assertRaises(Malformed):
            Manifest.load(remote, self.cfg)

    def test_an_oversized_manifest_is_refused(self):
        remote = Fake()
        remote.objects[self.cfg.manifest_key] = b"{}" + b" " * (33 << 20)
        with self.assertRaises(Oversized):
            Manifest.load(remote, self.cfg)

    def test_setting_an_identical_entry_is_not_a_change(self):
        m = Manifest({})
        entry = {"kind": "mirror", "path": "out", "files": []}
        m.set("out", entry)
        self.assertTrue(m.dirty)
        m.dirty = False
        m.set("out", dict(entry))
        self.assertFalse(m.dirty)


# --- the mirror kind --------------------------------------------------------

class TestMirror(Base):
    def art(self):
        return {a.name: a for a in self.cfg.artifacts}["out"]

    def ctx(self, manifest=None, remote=None):
        return Ctx(self.cfg, remote or Fake(), manifest or Manifest({}))

    def test_include_filter_and_skips(self):
        self.write("out/keep.csv", "a")
        self.write("out/keep2.json", "b")
        self.write("out/skip.txt", "c")
        self.write("out/__pycache__/skip.csv", "d")
        self.write("out/partial.csv.part", "e")
        got = {p.name for p in kinds._walk(self.root, self.art())}
        self.assertEqual(got, {"keep.csv", "keep2.json"})

    def test_push_then_pull_round_trip(self):
        self.write("out/a.csv", "hello")
        self.write("out/b.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)

        self.assertTrue(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(set(remote.objects), {"out/a.csv", "out/b.json"})

        shutil.rmtree(self.root / "out")
        kinds.mirror_pull(ctx, self.art())
        self.assertEqual((self.root / "out/a.csv").read_text(), "hello")

    def test_push_is_a_no_op_when_nothing_changed(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        man.dirty = False
        self.assertFalse(kinds.mirror_push(ctx, self.art()))
        self.assertFalse(man.dirty)

    def test_push_notices_a_deletion_even_with_nothing_to_upload(self):
        self.write("out/a.csv", "hello")
        self.write("out/b.csv", "there")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        (self.root / "out/b.csv").unlink()
        self.assertTrue(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(len(man.mirror_files("out")), 1)

    def test_deleting_the_last_file_publishes_an_empty_mirror(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        (self.root / "out/a.csv").unlink()
        self.assertTrue(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(man.mirror_files("out"), [])

    def test_an_absent_directory_is_a_skip_not_an_empty_mirror(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        shutil.rmtree(self.root / "out")
        self.assertFalse(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(len(man.mirror_files("out")), 1)

    def test_an_include_filter_matching_nothing_publishes_empty(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        (self.root / "out/a.csv").unlink()
        self.write("out/only.txt", "not included")
        self.assertTrue(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(man.mirror_files("out"), [])

    def test_pull_verifies_checksums(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())

        remote.objects["out/a.csv"] = b"tampered"
        (self.root / "out/a.csv").unlink()
        with self.assertRaises(SystemExit):
            kinds.mirror_pull(ctx, self.art())

    def test_a_failed_pull_leaves_the_local_copy_alone(self):
        self.write("out/a.csv", "good")
        self.write("out/b.csv", "also good")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())

        # Both objects change; only one still matches its manifest digest.
        remote.objects["out/a.csv"] = b"new good"
        man.artifacts["out"]["files"][0]["sha256"] = file_sha256(
            self.write("out/scratch.tmp", "new good"))
        man.artifacts["out"]["files"][0]["size"] = len(b"new good")
        (self.root / "out/scratch.tmp").unlink()
        remote.objects["out/b.csv"] = b"corrupt"

        with self.assertRaises(SystemExit) as e:
            kinds.mirror_pull(ctx, self.art(), force=True)
        self.assertIn("nothing under out was changed", str(e.exception))
        self.assertEqual((self.root / "out/a.csv").read_text(), "good")
        self.assertEqual((self.root / "out/b.csv").read_text(), "also good")

    def test_pull_refuses_a_manifest_path_outside_the_artifact(self):
        remote, man = Fake(), Manifest({})
        man.artifacts["out"] = {
            "kind": "mirror", "path": "out",
            "files": [{"path": "out/../../escape", "size": 1, "sha256": H1}]}
        with self.assertRaises(SystemExit):
            kinds.mirror_pull(self.ctx(man, remote), self.art())
        self.assertFalse((self.root.parent / "escape").exists())

    def test_pull_into_a_symlinked_artifact_directory_stays_inside_it(self):
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())

        shutil.rmtree(self.root / "out")
        (self.root / "out").symlink_to(outside)
        kinds.mirror_pull(ctx, self.art())
        self.assertEqual((outside / "a.csv").read_text(), "hello")

        # An escape through that directory is still an escape.
        (outside / "sub").symlink_to(outside.parent)
        man.artifacts["out"]["files"] = [
            {"path": "out/sub/escape.csv", "size": 1, "sha256": H1}]
        with self.assertRaises(SystemExit):
            kinds.mirror_pull(ctx, self.art())
        self.assertFalse((outside.parent / "escape.csv").exists())

    def test_push_reads_through_a_symlinked_artifact_directory(self):
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "a.csv").write_text("hello")
        (self.root / "out").symlink_to(outside)

        remote, man = Fake(), Manifest({})
        self.assertTrue(kinds.mirror_push(self.ctx(man, remote), self.art()))
        self.assertEqual(set(remote.objects), {"out/a.csv"})
        self.assertEqual(remote.objects["out/a.csv"], b"hello")

    def test_push_refuses_a_name_the_manifest_cannot_carry(self):
        self.write("out/we\\ird.csv", "x")
        with self.assertRaises(SystemExit) as e:
            kinds.mirror_push(self.ctx(), self.art())
        self.assertIn("backslash", str(e.exception))

    def test_an_interrupted_push_records_what_actually_landed(self):
        self.write("out/a.csv", "one")
        self.write("out/b.csv", "two")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())          # both published

        self.write("out/a.csv", "one changed")
        self.write("out/b.csv", "two changed")
        remote.fail_upload_after = remote.uploads + 1
        with self.assertRaises(OSError):
            kinds.mirror_push(ctx, self.art())

        # The manifest now describes the bucket: whichever file was uploaded
        # carries its new digest, the other still carries its old one.
        listed = {e["path"]: e["sha256"] for e in man.mirror_files("out")}
        on_disk = {p: file_sha256(self.root / p) for p in listed}
        in_bucket = {p: __import__("hashlib").sha256(
            remote.objects[p]).hexdigest() for p in listed}
        self.assertEqual(listed, in_bucket)
        self.assertNotEqual(listed, on_disk)        # one upload did not happen

    def test_a_file_rewritten_while_it_uploads_is_not_published(self):
        # The digest is taken before the upload; a file that moves in between
        # would otherwise be listed under a digest the object does not have.
        self.write("out/a.csv", "one")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)

        real_upload = remote.upload

        def racing_upload(src, key, content_type):
            real_upload(src, key, content_type)
            self.write("out/a.csv", "one, rewritten mid-upload")

        remote.upload = racing_upload
        with self.assertRaises(SystemExit) as e:
            kinds.mirror_push(ctx, self.art())
        self.assertIn("rewritten while they were uploading", str(e.exception))
        self.assertEqual(man.mirror_files("out"), [])

        # …and the next push, with nothing moving underneath it, publishes it.
        remote.upload = real_upload
        self.assertTrue(kinds.mirror_push(ctx, self.art()))
        self.assertEqual(man.mirror_files("out")[0]["sha256"],
                         file_sha256(self.root / "out/a.csv"))

    def test_status_reports_missing_stale_and_extra(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        self.assertEqual(kinds.mirror_status(ctx, self.art()).verdict, "in sync")

        self.write("out/a.csv", "changed")
        self.write("out/extra.csv", "x")
        r = kinds.mirror_status(ctx, self.art())
        self.assertIn("stale", r.verdict)
        self.assertIn("extra", r.verdict)
        self.assertFalse(r.ok)

    def test_pull_clean_removes_local_extras(self):
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        self.write("out/extra.csv", "x")
        kinds.mirror_pull(ctx, self.art(), clean=True)
        self.assertFalse((self.root / "out/extra.csv").exists())

    def test_a_failed_clean_pull_deletes_no_local_extras(self):
        # --clean removes the only copy there is: artifact directories are
        # git-ignored and the file is by definition not in the bucket. So it
        # has to wait for the download to verify.
        self.write("out/a.csv", "hello")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())
        self.write("out/extra.csv", "irreplaceable")
        remote.objects["out/a.csv"] = b"tampered"

        with self.assertRaises(SystemExit) as e:
            kinds.mirror_pull(ctx, self.art(), force=True, clean=True)
        self.assertIn("nothing under out was changed", str(e.exception))
        self.assertEqual((self.root / "out/extra.csv").read_text(),
                         "irreplaceable")
        self.assertEqual((self.root / "out/a.csv").read_text(), "hello")

    def test_a_published_file_where_a_directory_sits_here_is_refused(self):
        # The published layout changed shape under a reader who still holds
        # the old one. os.replace of a file onto a directory raises from
        # inside the install loop — past the point where the pull has said
        # the tree is safe to change, so the tree ends up neither copy.
        for rel in ("out/a.csv", "out/m.csv", "out/z.csv"):
            self.write(rel, "published")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())

        (self.root / "out/m.csv").unlink()
        self.write("out/m.csv/inner.csv", "irreplaceable")
        self.write("out/a.csv", "local")

        for clean in (False, True):
            with self.assertRaises(SystemExit) as e:
                kinds.mirror_pull(ctx, self.art(), clean=clean)
            self.assertIn("out was not touched", str(e.exception))
            self.assertIn("out/m.csv", str(e.exception))
            # Nothing installed, and --clean swept nothing on the way out.
            self.assertEqual((self.root / "out/a.csv").read_text(), "local")
            self.assertEqual((self.root / "out/m.csv/inner.csv").read_text(),
                             "irreplaceable")

    def test_a_published_directory_where_a_file_sits_here_is_refused(self):
        # The other direction: mkdir(parents=True) of a parent that is a file
        # raises, from the same place.
        self.write("out/a.csv", "published")
        self.write("out/sub.csv/x.csv", "published")
        remote, man = Fake(), Manifest({})
        ctx = self.ctx(man, remote)
        kinds.mirror_push(ctx, self.art())

        shutil.rmtree(self.root / "out/sub.csv")
        self.write("out/sub.csv", "was a file")
        self.write("out/a.csv", "local")

        with self.assertRaises(SystemExit) as e:
            kinds.mirror_pull(ctx, self.art())
        self.assertIn("out was not touched", str(e.exception))
        self.assertEqual((self.root / "out/a.csv").read_text(), "local")
        self.assertEqual((self.root / "out/sub.csv").read_text(), "was a file")


# --- the archive kind -------------------------------------------------------

class TestArchive(Base):
    def art(self):
        return {a.name: a for a in self.cfg.artifacts}["cache"]

    def test_push_then_pull_round_trip(self):
        for i in range(5):
            self.write(f"data/cache/{i}.json", f'{{"n": {i}}}')
        before = tree_hash(self.root / "data/cache")

        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        self.assertTrue(kinds.archive_push(ctx, self.art()))
        self.assertIn("v1/data-cache.tar.zst", remote.objects)
        self.assertEqual(man.get("cache")["files"], 5)

        shutil.rmtree(self.root / "data/cache")
        kinds.archive_pull(ctx, self.art())
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_pull_clean_removes_local_extras(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        self.write("data/cache/stale.json", "{}")
        kinds.archive_pull(ctx, self.art(), clean=True)
        self.assertFalse((self.root / "data/cache/stale.json").exists())
        self.assertTrue((self.root / "data/cache/1.json").exists())

    def test_pull_without_clean_merges_and_keeps_extras(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        self.write("data/cache/mine.json", "{}")
        kinds.archive_pull(ctx, self.art())
        self.assertTrue((self.root / "data/cache/mine.json").exists())
        self.assertTrue((self.root / "data/cache/1.json").exists())

    def test_push_is_a_no_op_when_the_tree_is_unchanged(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        self.assertFalse(kinds.archive_push(ctx, self.art()))

    def test_pull_rejects_a_corrupt_bundle(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())

        remote.objects["v1/data-cache.tar.zst"] = b"not a zstd stream"
        shutil.rmtree(self.root / "data/cache")
        with self.assertRaises(SystemExit):
            kinds.archive_pull(ctx, self.art())

    def test_a_corrupt_bundle_leaves_the_local_tree_alone(self):
        self.write("data/cache/1.json", '{"local": true}')
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        before = tree_hash(self.root / "data/cache")

        remote.objects["v1/data-cache.tar.zst"] = b"not a zstd stream"
        man.artifacts["cache"]["tree_hash"] = H1      # force a re-pull
        with self.assertRaises(SystemExit) as e:
            kinds.archive_pull(ctx, self.art())
        self.assertIn("was not touched", str(e.exception))
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_a_bundle_that_is_not_the_manifests_tree_is_refused(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        before = tree_hash(self.root / "data/cache")

        # A bundle that verifies against archive_sha256 but holds other bytes.
        self.write("data/cache/1.json", '{"different": true}')
        other = Fake()
        kinds.archive_push(Ctx(self.cfg, other, Manifest({})), self.art())
        bundle = other.objects["v1/data-cache.tar.zst"]
        remote.objects["v1/data-cache.tar.zst"] = bundle
        man.artifacts["cache"]["archive_sha256"] = __import__(
            "hashlib").sha256(bundle).hexdigest()
        self.write("data/cache/1.json", "{}")         # back to the first tree

        with self.assertRaises(SystemExit) as e:
            kinds.archive_pull(ctx, self.art(), force=True)
        self.assertIn("not the tree the manifest describes", str(e.exception))
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_a_bundle_that_will_not_unpack_is_a_clean_failure(self):
        self.write("data/cache/1.json", "{}")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        before = tree_hash(self.root / "data/cache")

        # zstd-valid, tar-nonsense: past the digest check, into the unpack.
        import zstandard
        body = zstandard.ZstdCompressor().compress(b"this is not a tar stream")
        remote.objects["v1/data-cache.tar.zst"] = body
        man.artifacts["cache"]["archive_sha256"] = __import__(
            "hashlib").sha256(body).hexdigest()
        man.artifacts["cache"]["archive_bytes"] = len(body)

        with self.assertRaises(SystemExit) as e:
            kinds.archive_pull(ctx, self.art(), force=True)
        self.assertIn("was not touched", str(e.exception))
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_clean_pull_keeps_a_symlinked_artifact_directory(self):
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.root / "data").mkdir()
        (self.root / "data/cache").symlink_to(outside)
        self.write("data/cache/1.json", "{}")

        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        self.write("data/cache/stale.json", "{}")
        kinds.archive_pull(ctx, self.art(), clean=True)

        self.assertTrue((self.root / "data/cache").is_symlink())
        self.assertEqual({p.name for p in outside.iterdir()}, {"1.json"})

    def test_a_tree_rewritten_while_it_packs_is_not_published(self):
        # tree_hash and _pack read the tree separately, so a build still
        # running under a publish can put different bytes in each — and the
        # manifest would record a digest the uploaded bundle does not have.
        for i in range(3):
            self.write(f"data/cache/{i}.json", f'{{"n": {i}}}')
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)

        real_pack = kinds._pack

        def racing_pack(root, src, dest):
            self.write("data/cache/3.json", "{}")     # the build is still going
            real_pack(root, src, dest)

        with unittest.mock.patch.object(kinds, "_pack", racing_pack):
            with self.assertRaises(SystemExit) as e:
                kinds.archive_push(ctx, self.art())
        self.assertIn("changed while it was being packed", str(e.exception))
        self.assertEqual(remote.objects, {})
        self.assertIsNone(man.get("cache"))

    def test_a_symlink_out_of_the_artifact_is_refused_at_push(self):
        # tar stores the link as a link; tree_hash reads through it. So the
        # bundle would carry a reference to a path only this machine has,
        # while the manifest carried the target's bytes — push exit 0,
        # status "in sync", and every reader's pull failing on the `data`
        # extraction filter.
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "big.bin").write_text("BIG")
        self.write("data/cache/1.json", "{}")
        (self.root / "data/cache/big.bin").symlink_to(outside / "big.bin")

        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        with self.assertRaises(SystemExit) as e:
            kinds.archive_push(ctx, self.art())
        self.assertIn("nothing was uploaded", str(e.exception))
        self.assertIn("data/cache/big.bin", str(e.exception))
        self.assertEqual(remote.objects, {})
        self.assertIsNone(man.get("cache"))

    def test_a_symlinked_directory_out_of_the_artifact_is_refused_too(self):
        # Worse than a file link: rglob does not descend through it, so the
        # tree hash does not even see the contents it would publish.
        outside = Path(tempfile.mkdtemp(prefix="litkit-outside-")).resolve()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "x.json").write_text("{}")
        self.write("data/cache/1.json", "{}")
        (self.root / "data/cache/sub").symlink_to(outside)

        ctx = Ctx(self.cfg, Fake(), Manifest({}))
        with self.assertRaises(SystemExit) as e:
            kinds.archive_push(ctx, self.art())
        self.assertIn("data/cache/sub", str(e.exception))

    def test_a_symlink_inside_the_artifact_still_round_trips(self):
        self.write("data/cache/real.json", "{}")
        (self.root / "data/cache/link.json").symlink_to("real.json")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        self.assertTrue(kinds.archive_push(ctx, self.art()))
        self.assertEqual(kinds.archive_status(ctx, self.art()).verdict,
                         "in sync")

        shutil.rmtree(self.root / "data/cache")
        kinds.archive_pull(ctx, self.art())
        self.assertEqual((self.root / "data/cache/link.json").read_text(), "{}")

    def test_a_merge_that_would_fail_partway_is_refused_first(self):
        # A merge moves file by file, so a path that changed between a file
        # and a directory raises after some of them have already landed. The
        # conflict is looked for before the first move instead.
        self.write("data/cache/a.json", "published")
        self.write("data/cache/foo", "published")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())

        (self.root / "data/cache/foo").unlink()
        self.write("data/cache/foo/x.json", "irreplaceable")
        self.write("data/cache/a.json", "local")
        before = tree_hash(self.root / "data/cache")

        with self.assertRaises(SystemExit) as e:
            kinds.archive_pull(ctx, self.art())
        self.assertIn("data/cache was not touched", str(e.exception))
        self.assertIn("data/cache/foo", str(e.exception))
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_a_merge_onto_a_file_that_became_a_directory_is_refused(self):
        self.write("data/cache/a.json", "published")
        self.write("data/cache/foo/x.json", "published")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())

        shutil.rmtree(self.root / "data/cache/foo")
        self.write("data/cache/foo", "was a file")
        self.write("data/cache/a.json", "local")
        before = tree_hash(self.root / "data/cache")

        with self.assertRaises(SystemExit) as e:
            kinds.archive_pull(ctx, self.art())
        self.assertIn("data/cache was not touched", str(e.exception))
        self.assertEqual(tree_hash(self.root / "data/cache"), before)

    def test_clean_pull_replaces_a_conflicting_shape_outright(self):
        # --clean swaps the whole tree rather than merging into it, so it has
        # no per-file conflict to hit and must keep working.
        self.write("data/cache/a.json", "published")
        self.write("data/cache/foo", "published")
        remote, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, remote, man)
        kinds.archive_push(ctx, self.art())
        after = tree_hash(self.root / "data/cache")

        (self.root / "data/cache/foo").unlink()
        self.write("data/cache/foo/x.json", "local")
        kinds.archive_pull(ctx, self.art(), clean=True)
        self.assertEqual(tree_hash(self.root / "data/cache"), after)

    def test_status_absent_both_sides(self):
        ctx = Ctx(self.cfg, Fake(), Manifest({}))
        self.assertEqual(kinds.archive_status(ctx, self.art()).verdict, "absent")


# --- the fetch kind ---------------------------------------------------------

FETCH_TOML = """\
[artifact.positions]
kind = "fetch"
path = "data/positions.csv"
url  = "https://example.invalid/positions.csv"
"""


class TestFetch(Base):
    def setUp(self):
        super().setUp()
        self.cfg = self.reload(FETCH_TOML)
        self.art = self.cfg.artifacts[0]
        self.ctx = Ctx(self.cfg, Fake(), Manifest({}))
        self.served = b"one,two\n"
        self.headers = {"etag": "v1", "last_modified": "Mon, 01 Jan 2026"}
        self.fetches = 0

        def fake_head(url):
            return dict(self.headers, size=str(len(self.served)))

        def fake_fetch(url, dest):
            self.fetches += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.served)
            return dict(self.headers)

        for name, fn in (("head", fake_head), ("fetch_url", fake_fetch)):
            original = getattr(kinds, name)
            setattr(kinds, name, fn)
            self.addCleanup(setattr, kinds, name, original)

    def test_first_pull_fetches_and_records(self):
        kinds.fetch_pull(self.ctx, self.art)
        self.assertEqual(self.fetches, 1)
        seen = self.ctx.state["fetch"]["positions"]
        self.assertEqual(seen["size"], len(self.served))
        self.assertEqual(kinds.fetch_status(self.ctx, self.art).verdict, "in sync")

    def test_an_unchanged_etag_is_not_refetched(self):
        kinds.fetch_pull(self.ctx, self.art)
        kinds.fetch_pull(self.ctx, self.art)
        self.assertEqual(self.fetches, 1)

    def test_a_locally_modified_file_is_not_in_sync(self):
        kinds.fetch_pull(self.ctx, self.art)
        (self.root / "data/positions.csv").write_bytes(b"edited by hand\n")
        r = kinds.fetch_status(self.ctx, self.art)
        self.assertEqual(r.verdict, "DIFFERS")
        self.assertIn("modified here", r.local)

    def test_a_locally_modified_file_is_refetched(self):
        kinds.fetch_pull(self.ctx, self.art)
        (self.root / "data/positions.csv").write_bytes(b"edited by hand\n")
        kinds.fetch_pull(self.ctx, self.art)
        self.assertEqual(self.fetches, 2)
        self.assertEqual((self.root / "data/positions.csv").read_bytes(),
                         self.served)

    def test_a_same_size_edit_is_still_caught(self):
        kinds.fetch_pull(self.ctx, self.art)
        (self.root / "data/positions.csv").write_bytes(b"XXX,two\n")
        self.assertEqual(kinds.fetch_status(self.ctx, self.art).verdict,
                         "DIFFERS")

    def test_a_state_file_from_before_digests_is_trusted(self):
        kinds.fetch_pull(self.ctx, self.art)
        self.ctx.state["fetch"]["positions"] = {"etag": "v1"}
        self.assertEqual(kinds.fetch_status(self.ctx, self.art).verdict,
                         "in sync")

    def test_a_new_etag_refetches(self):
        kinds.fetch_pull(self.ctx, self.art)
        self.headers = {"etag": "v2", "last_modified": "Tue, 02 Jan 2026"}
        self.served = b"three,four\n"
        kinds.fetch_pull(self.ctx, self.art)
        self.assertEqual(self.fetches, 2)


# --- local state ------------------------------------------------------------

class TestState(Base):
    def test_round_trip(self):
        kinds.save_state(self.cfg, {"fetch": {"x": {"etag": "1"}}})
        self.assertEqual(kinds.load_state(self.cfg)["fetch"]["x"]["etag"], "1")

    def test_write_is_atomic_and_leaves_no_scratch(self):
        kinds.save_state(self.cfg, {"a": 1})
        siblings = [p.name for p in self.cfg.state_file.parent.iterdir()]
        self.assertEqual(siblings, ["state.json"])

    def test_a_stale_staging_tree_is_swept_away(self):
        import os as _os
        stage = self.cfg.root / ".litkit/tmp/stage-abandoned"
        stage.mkdir(parents=True)
        (stage / "leftover").write_text("x" * 100)
        old = 1_600_000_000                       # long enough ago
        _os.utime(stage, (old, old))
        with kinds._staging(self.cfg):
            pass
        self.assertFalse(stage.exists())

    def test_a_fresh_staging_tree_is_left_alone(self):
        stage = self.cfg.root / ".litkit/tmp/stage-in-flight"
        stage.mkdir(parents=True)
        with kinds._staging(self.cfg):
            pass
        self.assertTrue(stage.exists())

    def test_a_corrupt_state_file_is_not_fatal(self):
        self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_file.write_text("{ truncated")
        self.assertEqual(kinds.load_state(self.cfg), {})

    def test_a_state_file_that_is_not_an_object_is_not_fatal(self):
        self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.state_file.write_text("[1, 2, 3]")
        self.assertEqual(kinds.load_state(self.cfg), {})


# --- transport --------------------------------------------------------------

class Response:
    """Enough of an HTTP response for `_drain` and `get_bytes`."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body, self.headers = body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._body = self._body, b""
            return out
        out, self._body = self._body[:n], self._body[n:]
        return out


class TestRemote(Base):
    def served(self):
        """A `file://` base, so the public read path runs with no server."""
        d = Path(tempfile.mkdtemp(prefix="litkit-served-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d, Remote(dataclasses.replace(self.cfg, base=d.as_uri()))

    def test_object_keys_are_percent_encoded(self):
        self.assertEqual(_url("https://x.invalid/m", "out/a b#c.csv"),
                         "https://x.invalid/m/out/a%20b%23c.csv")
        self.assertEqual(_url("https://x.invalid/m", "v1/x.tar.zst"),
                         "https://x.invalid/m/v1/x.tar.zst")

    def test_download(self):
        d, remote = self.served()
        (d / "a.csv").write_bytes(b"hello")
        remote.download("a.csv", self.root / "out/a.csv")
        self.assertEqual((self.root / "out/a.csv").read_bytes(), b"hello")

    def test_a_failed_download_leaves_no_part_file(self):
        _, remote = self.served()
        with self.assertRaises(urllib.error.URLError):
            remote.download("nope.csv", self.root / "out/a.csv")
        self.assertEqual(list((self.root / "out").iterdir()), [])

    def test_a_failed_download_cancels_the_queue(self):
        # The first failure decides the pull. Everything still queued behind
        # it would otherwise run anyway — at a socket timeout each, once the
        # network is what failed.
        _, remote = self.served()
        started = []

        def counting(key, dest, max_bytes=None):
            started.append(key)
            if key == "boom.csv":
                raise urllib.error.URLError("gone")
            time.sleep(0.02)

        remote.download = counting
        jobs = [("boom.csv", self.root / "out/boom.csv")]
        jobs += [(f"{i}.csv", self.root / f"out/{i}.csv") for i in range(40)]
        with self.assertRaises(urllib.error.URLError):
            remote.download_many(jobs, workers=1)
        self.assertLess(len(started), 10, started)

    def test_a_dropped_connection_is_retried(self):
        # The write path has ten botocore attempts; the read path most people
        # exercise used to have none, and one blip discarded a whole pull.
        _, remote = self.served()
        calls = []

        def flaky(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.URLError(ConnectionResetError("reset"))
            return Response(b"hello")

        with unittest.mock.patch.object(transport.urllib.request, "urlopen",
                                        flaky), \
                unittest.mock.patch.object(transport, "BACKOFF", 0):
            remote.download("a.csv", self.root / "out/a.csv")
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.root / "out/a.csv").read_bytes(), b"hello")

    def test_a_retry_does_not_append_to_the_previous_attempt(self):
        _, remote = self.served()
        calls = []

        def truncating(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                return Response(b"half")     # closed before the body finished
            return Response(b"the whole thing")

        real_drain = transport._drain

        def drain(reader, fh, max_bytes):
            n = real_drain(reader, fh, max_bytes)
            if n < len(b"the whole thing"):
                raise http.client.IncompleteRead(b"")
            return n

        with unittest.mock.patch.object(transport.urllib.request, "urlopen",
                                        truncating), \
                unittest.mock.patch.object(transport, "_drain", drain), \
                unittest.mock.patch.object(transport, "BACKOFF", 0):
            remote.download("a.csv", self.root / "out/a.csv")
        self.assertEqual((self.root / "out/a.csv").read_bytes(),
                         b"the whole thing")

    def test_a_missing_object_is_not_retried(self):
        _, remote = self.served()
        calls = []

        def gone(req, timeout=None):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {},
                                         None)

        with unittest.mock.patch.object(transport.urllib.request, "urlopen",
                                        gone):
            with self.assertRaises(Missing):
                remote.get_bytes("manifest.json")
        self.assertEqual(len(calls), 1)

    def test_an_oversized_body_is_not_retried(self):
        _, remote = self.served()
        calls = []

        def big(req, timeout=None):
            calls.append(req.full_url)
            return Response(b"x" * 100)

        with unittest.mock.patch.object(transport.urllib.request, "urlopen",
                                        big):
            with self.assertRaises(Oversized):
                remote.download("a.csv", self.root / "out/a.csv", 10)
        self.assertEqual(len(calls), 1)

    def test_a_body_longer_than_promised_is_refused(self):
        d, remote = self.served()
        (d / "a.csv").write_bytes(b"x" * 100)
        with self.assertRaises(Oversized):
            remote.download("a.csv", self.root / "out/a.csv", 10)
        self.assertEqual(list((self.root / "out").iterdir()), [])


class StubS3:
    """A conditional write that is always refused, and the read-back after."""

    def __init__(self, stored: bytes | None, readable: bool = True):
        self.stored, self.readable, self.puts = stored, readable, 0

    def put_object(self, **kw):
        import botocore.exceptions
        self.puts += 1
        raise botocore.exceptions.ClientError(
            {"Error": {"Code": "PreconditionFailed"},
             "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")

    def get_object(self, Bucket, Key):
        if not self.readable:
            raise OSError("the connection is still down")
        return {"Body": Response(self.stored), "ETag": '"e"'}


class TestConditionalWrite(Base):
    """The real `put_bytes`. The fake bucket answers conflicts itself, so the
    412 handling has never been exercised through it."""

    BODY = b'{"artifacts": {}}'

    def remote(self, stub):
        with unittest.mock.patch.object(
                transport._creds, "load",
                lambda root, bucket: {"R2_BUCKET_NAME": "b"}), \
                unittest.mock.patch.object(transport._creds, "client",
                                           lambda c: stub):
            return Remote(self.cfg, need_write=True)

    def test_a_412_over_our_own_bytes_is_not_a_conflict(self):
        # The write landed and the response was lost; botocore's retry then
        # tripped over the first attempt's own object. Nobody else published.
        stub = StubS3(self.BODY)
        self.remote(stub).put_bytes("m.json", self.BODY, "application/json",
                                    if_match="v1")
        self.assertEqual(stub.puts, 1)

    def test_a_412_over_someone_elses_bytes_is_still_a_conflict(self):
        stub = StubS3(b'{"artifacts": {"theirs": {}}}')
        with self.assertRaises(Conflict) as e:
            self.remote(stub).put_bytes("m.json", self.BODY,
                                        "application/json", if_match="v1")
        self.assertIn("someone else published", str(e.exception))

    def test_an_unreadable_manifest_after_a_412_is_still_a_conflict(self):
        stub = StubS3(None, readable=False)
        with self.assertRaises(Conflict):
            self.remote(stub).put_bytes("m.json", self.BODY,
                                        "application/json", if_absent=True)


# --- end to end, through the real transport ---------------------------------

class TestRoundTripOverHttpLikeReads(Base):
    """Push with the fake bucket, then pull with the real `Remote`.

    A `file://` base drives the same code path a public HTTPS mirror does —
    the URL builder, the streaming download, the thread pool, the staging and
    the verification — without a server or a network.
    """

    def setUp(self):
        super().setUp()
        self.bucket = Path(tempfile.mkdtemp(prefix="litkit-bucket-"))
        self.addCleanup(shutil.rmtree, self.bucket, ignore_errors=True)

    def publish(self):
        """Run a push into the fake bucket, then lay it out as files."""
        fake, man = Fake(), Manifest({})
        ctx = Ctx(self.cfg, fake, man)
        for art in self.cfg.artifacts:
            kinds.push(ctx, art)
        fake.put_bytes(self.cfg.manifest_key, man.dump(), "application/json")
        for key, body in fake.objects.items():
            dest = self.bucket / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)

    def reader(self):
        cfg = dataclasses.replace(self.cfg, base=self.bucket.as_uri())
        remote = Remote(cfg)
        return Ctx(cfg, remote, Manifest.load(remote, cfg))

    def test_a_whole_repository_round_trips(self):
        for i in range(4):
            self.write(f"data/cache/{i}.json", f'{{"n": {i}}}')
        self.write("out/summary.csv", "a,b\n1,2\n")
        self.write("out/sub/detail.json", '{"detail": true}')
        before = tree_hash(self.root / "data/cache"), tree_hash(self.root / "out")

        self.publish()
        shutil.rmtree(self.root / "data/cache")
        shutil.rmtree(self.root / "out")

        ctx = self.reader()
        for art in ctx.cfg.artifacts:
            kw = {"workers": 4} if art.kind == "mirror" else {}
            kinds.pull(ctx, art, **kw)

        self.assertEqual(
            (tree_hash(self.root / "data/cache"), tree_hash(self.root / "out")),
            before)
        for art in ctx.cfg.artifacts:
            self.assertEqual(kinds.status(ctx, art).verdict, "in sync")

    def test_a_key_with_awkward_characters_survives_the_url(self):
        self.write("out/a file #1.csv", "x,y\n")
        self.publish()
        (self.root / "out/a file #1.csv").unlink()
        ctx = self.reader()
        kinds.mirror_pull(ctx, {a.name: a for a in ctx.cfg.artifacts}["out"])
        self.assertEqual((self.root / "out/a file #1.csv").read_text(), "x,y\n")

    def test_a_tampered_object_is_caught_and_nothing_is_installed(self):
        self.write("out/a.csv", "trustworthy\n")
        self.publish()
        (self.bucket / "out/a.csv").write_text("tampered with\n")
        (self.root / "out/a.csv").write_text("stale but mine\n")

        ctx = self.reader()
        with self.assertRaises(SystemExit):
            kinds.mirror_pull(ctx, {a.name: a for a in ctx.cfg.artifacts}["out"])
        self.assertEqual((self.root / "out/a.csv").read_text(), "stale but mine\n")


# --- the command line -------------------------------------------------------

class TestCli(Base):
    def run_cli(self, argv, remote=None, cfg=None):
        remote = remote or Fake()
        cfg = cfg or self.cfg

        class _Ctx(cli.Ctx):
            @property
            def remote(_self):
                _self._remote = remote
                return remote

        original = cli.config.load, cli.Ctx
        cli.config.load, cli.Ctx = (lambda *a, **k: cfg), _Ctx
        try:
            return cli.main(argv), remote
        finally:
            cli.config.load, cli.Ctx = original

    def test_doctor_comes_back_from_a_quarto_that_never_answers(self):
        # The one command whose whole job is diagnosing a broken toolchain,
        # on exactly the machine it exists for.
        def hangs(cmd, **kw):
            raise cli.subprocess.TimeoutExpired(cmd, kw["timeout"])

        out = io.StringIO()
        with unittest.mock.patch.object(cli.shutil, "which",
                                        lambda name: f"/usr/bin/{name}"), \
                unittest.mock.patch.object(cli.subprocess, "run", hangs), \
                contextlib.redirect_stdout(out):
            rc, _ = self.run_cli(["doctor"])
        self.assertIn("did not answer", out.getvalue())
        self.assertEqual(rc, 1)

    def test_workers_must_be_at_least_one(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli(["pull", "-w", "0"])
        self.assertEqual(e.exception.code, 2)

    def test_push_writes_the_manifest_once_at_the_end(self):
        self.write("out/a.csv", "hello")
        rc, remote = self.run_cli(["push", "out"])
        self.assertEqual(rc, 0)
        self.assertIn("manifest.json", remote.objects)
        self.assertIn(b"out/a.csv", remote.objects["manifest.json"])

    def test_push_with_nothing_to_say_writes_no_manifest(self):
        self.write("out/a.csv", "hello")
        _, remote = self.run_cli(["push", "out"])
        before = remote.objects["manifest.json"]
        self.run_cli(["push", "out"], remote=remote)
        self.assertEqual(remote.objects["manifest.json"], before)

    def test_a_concurrent_publisher_is_refused_not_overwritten(self):
        class Racing(Fake):
            """A bucket where someone else publishes mid-push."""

            def upload(self, src, key, content_type):
                super().upload(src, key, content_type)
                self._stamp("manifest.json")

        self.write("out/a.csv", "hello")
        _, remote = self.run_cli(["push", "out"], remote=Racing())
        was = remote.objects["manifest.json"]

        self.write("out/a.csv", "changed")
        with self.assertRaises(Conflict):
            self.run_cli(["push", "out"], remote=remote)
        self.assertEqual(remote.objects["manifest.json"], was)

    def test_an_interrupted_push_still_commits_the_manifest(self):
        self.write("out/a.csv", "one")
        self.write("out/b.csv", "two")
        remote = Fake()
        remote.fail_upload_after = 1
        with self.assertRaises(OSError):
            self.run_cli(["push", "out"], remote=remote)
        self.assertIn("manifest.json", remote.objects)
        published = set(remote.objects) - {"manifest.json"}
        for key in published:
            self.assertIn(key.encode(), remote.objects["manifest.json"])

    def test_an_empty_bucket_does_not_block_a_fetch_artifact(self):
        cfg = self.reload(SYNC_TOML + FETCH_TOML)
        calls = []
        original = kinds.VERBS["fetch"]
        kinds.VERBS["fetch"] = (original[0],
                                lambda ctx, art, **kw: calls.append(art.name),
                                original[2])
        self.addCleanup(kinds.VERBS.__setitem__, "fetch", original)

        rc, _ = self.run_cli(["pull"], cfg=cfg)
        self.assertEqual(rc, 1)                 # the bucket really is empty
        self.assertEqual(calls, ["positions"])  # and the input still arrived


if __name__ == "__main__":
    unittest.main()
