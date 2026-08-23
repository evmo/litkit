"""Shared plumbing for literate-analysis repositories.

Three things live here so that three repositories do not each have their own:

  litkit.kinds     artifact sync — archive, mirror and fetch
  litkit/data/common.mk   the make targets every repository answers to
  litkit.config    the sync.toml schema that ties them together

The convention those enforce is one line long: **the pipeline writes `out/`,
and the documents only read it.** Everything else — nine make targets, data/
and out/ kept out of git and in a bucket, reports/ holding the .qmd — follows
from wanting that to be checkable.
"""

__version__ = "0.1.0"


def mk_path() -> str:
    """Filesystem path of the packaged common.mk."""
    from pathlib import Path
    return str(Path(__file__).parent / "data" / "common.mk")
