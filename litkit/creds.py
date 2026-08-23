"""Bucket credentials, and the S3 client that uses them.

Credentials live in `.r2` at the repository root — git-ignored, one KEY=value
per line — or in the environment, which wins. That ordering is what lets CI
supply them without a file and a laptop supply them without an export.

Reading a *public* mirror needs none of this; see remote.py.
"""

from __future__ import annotations

import os
from pathlib import Path

CREDS_NAME = ".r2"
KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME")

EXAMPLE = """\
# Cloudflare R2 credentials for this repository's bucket. Copy to `.r2`, which
# is git-ignored, and fill in. Any of these may instead be set in the
# environment, which takes precedence — that is the path for CI.
#
# The token needs Object Read & Write on this one bucket and nothing else.
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
"""


def load(root: Path, bucket_default: str = "") -> dict[str, str]:
    creds: dict[str, str] = {}
    path = root / CREDS_NAME
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in KEYS:
        if os.environ.get(k):
            creds[k] = os.environ[k]
    # `R2_BUCKET` is what one of these repos used before; accept both.
    if not creds.get("R2_BUCKET_NAME"):
        creds["R2_BUCKET_NAME"] = os.environ.get("R2_BUCKET", "") or bucket_default

    missing = [k for k in KEYS if not creds.get(k)]
    if missing:
        raise SystemExit(
            f"missing R2 credentials: {', '.join(missing)}\n"
            f"  put them in {path} (see .r2.example) or the environment"
        )
    return creds


def client(creds: dict[str, str]):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise SystemExit(
            "this needs the S3 client — install litkit with the `s3` extra:\n"
            "  uv add 'litkit[s3] @ git+https://github.com/evmo/litkit'"
        ) from None

    return boto3.client(
        "s3",
        endpoint_url=f"https://{creds['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=creds["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        # Two R2 quirks, both learned the hard way:
        #  - it rejects botocore's default aws-chunked checksum trailers;
        #  - it closes long multipart connections often enough that five
        #    attempts are not enough for a 250 MB upload.
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"},
                      connect_timeout=30, read_timeout=300,
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def transfer_config():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=64 * 1024 * 1024,
                          multipart_chunksize=64 * 1024 * 1024,
                          max_concurrency=4, use_threads=True)
