"""Fail before a build when a target platform was already published."""

from __future__ import annotations

import argparse
import os

import oss2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()

    prefix = args.prefix.rstrip("/")
    bucket_name, separator, key_prefix = prefix.removeprefix("oss://").partition("/")
    if not separator or not bucket_name or not key_prefix:
        raise SystemExit("OSS release prefix must be oss://BUCKET/KEY")
    bucket = oss2.Bucket(
        oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]),
        os.environ["OSS_ENDPOINT"].rstrip("/"),
        bucket_name,
    )
    result = bucket.list_objects(prefix=key_prefix + "/", max_keys=1)
    if result.object_list:
        raise SystemExit("Release already exists in OSS: " + prefix)
    print("OSS release prefix is available: " + prefix, flush=True)


if __name__ == "__main__":
    main()
