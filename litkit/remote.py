"""Transport — reads over public HTTPS when possible, S3 when not.

Reads and writes are deliberately asymmetric. If `remote.base` is set in
sync.toml the bucket is served publicly, so `pull` and `status` are plain
HTTPS and a reader needs no account anywhere. Writes always go over the S3
API with credentials, because publishing is a maintainer's job.

Two things a reader is not allowed to assume: that an object key is safe to
paste into a URL (it is percent-encoded, so a key with a space or a `#` in it
addresses the object rather than something adjacent to it), and that a body
is the size it was promised to be (a download stops at the size the manifest
declared rather than filling the disk).
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import creds as _creds
from .hashing import human

UA = "litkit/0.1 (+https://github.com/evmo/litkit)"
TIMEOUT = 120
CHUNK = 1 << 20


def _request(url: str, headers: dict[str, str] | None = None):
    return urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})


def _url(base: str, key: str) -> str:
    """`base/key`, with the key's path segments percent-encoded."""
    return f"{base}/{urllib.parse.quote(key, safe='/')}"


def _drain(reader, fh, max_bytes: int | None) -> int:
    """Copy a stream to a file, refusing to exceed `max_bytes`."""
    total = 0
    while chunk := reader.read(CHUNK):
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise Oversized(f"longer than the {max_bytes:,} bytes promised")
        fh.write(chunk)
    return total


class Missing(Exception):
    """The object is not in the bucket."""


class Oversized(Exception):
    """The body is larger than the manifest said it would be."""


class Conflict(SystemExit):
    """Someone else wrote the manifest while this push was running."""


class Remote:
    def __init__(self, cfg, *, need_write: bool = False):
        self.cfg = cfg
        self.public = bool(cfg.base) and not need_write
        self._s3 = None
        self._bucket = cfg.bucket
        if not self.public:
            c = _creds.load(cfg.root, cfg.bucket)
            self._s3 = _creds.client(c)
            self._bucket = c["R2_BUCKET_NAME"]

    # --- description, for messages -----------------------------------------

    @property
    def where(self) -> str:
        return self.cfg.base if self.public else f"s3://{self._bucket}"

    # --- reads --------------------------------------------------------------

    def get_bytes(self, key: str, *, limit: int | None = None
                  ) -> tuple[bytes, str | None]:
        """(body, etag). The etag is the precondition for writing it back."""
        if self.public:
            try:
                with urllib.request.urlopen(_request(_url(self.cfg.base, key)),
                                            timeout=TIMEOUT) as r:
                    body = r.read(limit + 1) if limit is not None else r.read()
                    if limit is not None and len(body) > limit:
                        raise Oversized(f"{key} is larger than {limit:,} bytes")
                    return body, (r.headers.get("ETag") or "").strip('"') or None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise Missing(key) from None
                raise
        import botocore.exceptions
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise Missing(key) from None
            raise
        with obj["Body"] as body_stream:
            body = (body_stream.read(limit + 1) if limit is not None
                    else body_stream.read())
        if limit is not None and len(body) > limit:
            raise Oversized(f"{key} is larger than {limit:,} bytes")
        return body, (obj.get("ETag") or "").strip('"') or None

    def download(self, key: str, dest: Path, max_bytes: int | None = None) -> None:
        """Fetch one object to `dest`, leaving nothing behind on failure.

        `max_bytes` is enforced while streaming on the public path. Over the
        S3 API boto3 owns the transfer, so the guard there is the size and
        digest check the caller makes against the manifest afterwards.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            if self.public:
                with urllib.request.urlopen(_request(_url(self.cfg.base, key)),
                                            timeout=TIMEOUT) as r, \
                        tmp.open("wb") as fh:
                    _drain(r, fh, max_bytes)
            else:
                self._s3.download_file(self._bucket, key, str(tmp))
            os.replace(tmp, dest)
        except Oversized as e:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise Oversized(f"{key} is {e}") from None
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def download_many(self, jobs: list[tuple], workers: int = 8,
                      label: str = "") -> None:
        """(key, dest[, max_bytes]) triples, concurrently. Raises on the first
        failure, and drops whatever has not started.

        `with ThreadPoolExecutor` would shut down without `cancel_futures`, so
        every queued job still ran to completion before the exception the
        caller is waiting for surfaced. On a network that has gone away each
        of those spends the full socket timeout, which turns a thousand-file
        pull into hours of waiting for an error that was already decided. The
        in-flight ones still have to finish; the rest are cancelled.
        """
        if not jobs:
            return
        done = 0
        pool = cf.ThreadPoolExecutor(max_workers=max(1, workers))
        try:
            futures = {pool.submit(self.download, *j): j for j in jobs}
            for fut in cf.as_completed(futures):
                key, dest = futures[fut][0], futures[fut][1]
                fut.result()
                done += 1
                print(f"    [{done}/{len(jobs)}] {human(dest.stat().st_size):>10}"
                      f"  {key}", flush=True)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    # --- writes -------------------------------------------------------------

    def upload(self, src: Path, key: str, content_type: str) -> None:
        self._s3.upload_file(str(src), self._bucket, key,
                             ExtraArgs={"ContentType": content_type},
                             Config=_creds.transfer_config())

    def put_bytes(self, key: str, body: bytes, content_type: str, *,
                  if_match: str | None = None,
                  if_absent: bool = False) -> None:
        """Write an object, optionally only if it is still what we read.

        `if_match` is the ETag this run loaded; `if_absent` says the object
        was not there at all. Either way a second maintainer who published
        between the read and this write gets a refusal rather than having
        their entries dropped. A store that does not implement conditional
        writes says so, and is written to unconditionally with a warning —
        losing the precondition is better than losing the push.
        """
        import botocore.exceptions
        extra = {}
        if if_match:
            extra["IfMatch"] = f'"{if_match.strip(chr(34))}"'
        elif if_absent:
            extra["IfNoneMatch"] = "*"
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=body,
                                ContentType=content_type, **extra)
        except botocore.exceptions.ClientError as e:
            if not extra:
                raise
            code = e.response["Error"]["Code"]
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("PreconditionFailed", "ConditionalRequestConflict") \
                    or status in (412, 409):
                raise Conflict(
                    f"  {key} changed in {self.where} while this push was "
                    f"running — someone else published.\n"
                    f"  Nothing further was written. Re-run `litkit push`; it "
                    f"will re-read the manifest and merge."
                ) from None
            if code in ("NotImplemented", "InvalidRequest") or status == 501:
                print(f"  note: {self.where} does not support conditional "
                      f"writes — writing {key} unconditionally")
                self._s3.put_object(Bucket=self._bucket, Key=key, Body=body,
                                    ContentType=content_type)
                return
            raise


def head(url: str) -> dict[str, str]:
    """ETag / Last-Modified / Content-Length for an arbitrary URL.

    Used by the `fetch` kind, whose files are published by something outside
    this repository and so have no manifest to compare against.
    """
    req = _request(url)
    req.get_method = lambda: "HEAD"
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        h = r.headers
        return {k: v for k, v in (
            ("etag", (h.get("ETag") or "").strip('"')),
            ("last_modified", h.get("Last-Modified") or ""),
            ("size", h.get("Content-Length") or ""),
        ) if v}


def fetch_url(url: str, dest: Path) -> dict[str, str]:
    """Download an arbitrary URL, returning its validators."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(_request(url), timeout=TIMEOUT) as r, \
                tmp.open("wb") as fh:
            _drain(r, fh, None)
            h = r.headers
            meta = {"etag": (h.get("ETag") or "").strip('"'),
                    "last_modified": h.get("Last-Modified") or ""}
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return {k: v for k, v in meta.items() if v}
