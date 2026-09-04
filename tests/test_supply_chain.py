"""Guards on what CI and a build are allowed to resolve for themselves.

Nothing here imports litkit. These are properties of the repository's own
manifests, and each one is a defect that comes back silently: the pin is a
line of YAML or TOML that a later edit can drop without any test going red,
and the consequence only shows up as a version nobody chose.

    uv run python -m unittest discover -s tests
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = ROOT / "pyproject.toml"


class TestCiUsesTheLock(unittest.TestCase):
    """CI must run the versions `uv.lock` names, not ones it resolves itself."""

    def test_no_step_runs_uvx(self):
        # `uvx <tool>` resolves a release independent of the lockfile, so a
        # tool upgraded upstream changes what CI does before anyone reviews
        # it. `uv run` takes the locked version instead.
        offenders = [
            line.strip()
            for line in CI.read_text().splitlines()
            if re.search(r"(^|[;&|\s])uvx(\s|$)", line)
        ]
        self.assertEqual(offenders, [], "CI runs a tool outside the lockfile")

    def test_ruff_is_linted_through_the_lock(self):
        self.assertRegex(CI.read_text(), r"uv run --locked ruff check\b")


class TestBuildBackendIsPinned(unittest.TestCase):
    """`build-system.requires` is the only thing that picks the backend.

    It is resolved in an isolated environment that `uv.lock` never constrains,
    so an unpinned entry means a build can use a backend nobody chose.
    """

    def test_every_build_requirement_is_exact(self):
        requires = tomllib.loads(PYPROJECT.read_text())["build-system"]["requires"]
        self.assertTrue(requires)
        floating = [r for r in requires if "==" not in r]
        self.assertEqual(floating, [], "build backend resolves itself at build time")


class TestPublishedRangesAreBounded(unittest.TestCase):
    """The lock protects this checkout; these ranges are what a consumer reads.

    Both optional dependencies reach substantive APIs, so an unbounded range
    lets an untested major arrive on a consumer's first resolution.
    """

    def test_every_published_requirement_has_an_upper_bound(self):
        project = tomllib.loads(PYPROJECT.read_text())["project"]
        published = list(project.get("dependencies", []))
        for extra in project.get("optional-dependencies", {}).values():
            published.extend(extra)
        self.assertTrue(published)
        unbounded = [r for r in published if "<" not in r]
        self.assertEqual(unbounded, [], "a future major can arrive unreviewed")


if __name__ == "__main__":
    unittest.main()
