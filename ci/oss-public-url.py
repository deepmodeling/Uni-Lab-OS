"""Return the public HTTPS URL for an OSS object without exposing credentials."""

from __future__ import annotations

import argparse
import os
from urllib.parse import quote, urlsplit, urlunsplit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True, help="oss://bucket/key-prefix")
    parser.add_argument("--object-name", required=True)
    args = parser.parse_args()

    bucket, separator, key_prefix = args.prefix.removeprefix("oss://").partition("/")
    if not separator or not bucket or not key_prefix:
        raise SystemExit("OSS prefix must be oss://BUCKET/KEY")
    endpoint = urlsplit(os.environ["OSS_ENDPOINT"].rstrip("/"))
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise SystemExit("OSS_ENDPOINT must be an HTTP(S) URL")
    host = endpoint.hostname or ""
    if host != bucket and not host.startswith(bucket + "."):
        host = bucket + "." + host
    if endpoint.port:
        host += ":" + str(endpoint.port)
    key = key_prefix.rstrip("/") + "/" + args.object_name
    print(urlunsplit((endpoint.scheme, host, quote(key, safe="/"), "", "")))


if __name__ == "__main__":
    main()
