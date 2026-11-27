import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import oss2


def public_url(bucket_name: str, key: str) -> str:
    endpoint = urlsplit(os.environ["OSS_ENDPOINT"].rstrip("/"))
    host = endpoint.hostname or ""
    if host != bucket_name and not host.startswith(bucket_name + "."):
        host = bucket_name + "." + host
    if endpoint.port:
        host += ":" + str(endpoint.port)
    return urlunsplit((endpoint.scheme, host, quote(key, safe="/"), "", ""))


prefix = os.environ["OSS_UPLOAD_PREFIX"]
bucket_name, _, key_prefix = prefix.removeprefix("oss://").partition("/")
if not bucket_name:
    raise SystemExit("OSS upload prefix is missing a bucket name")

bucket = oss2.Bucket(
    oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]),
    os.environ["OSS_ENDPOINT"].rstrip("/"),
    bucket_name,
)

for name in os.environ["OSS_UPLOAD_FILES"].split(";"):
    path = Path(name)
    key = key_prefix.rstrip("/") + "/" + path.name
    size = path.stat().st_size
    print("Starting OSS upload: oss://" + bucket_name + "/" + key + " (" + str(size) + " bytes)", flush=True)
    last_report = [0.0]

    def progress(consumed, total):
        now = time.monotonic()
        if consumed == total or now - last_report[0] >= 15:
            percent = 100 if not total else consumed * 100 / total
            print(
                "OSS upload progress: "
                + path.name
                + " "
                + str(consumed)
                + "/"
                + str(total)
                + " bytes ("
                + format(percent, ".1f")
                + "%)",
                flush=True,
            )
            last_report[0] = now

    for attempt in range(1, 4):
        try:
            oss2.resumable_upload(
                bucket,
                key,
                str(path),
                store=oss2.ResumableStore(root=".oss-upload"),
                progress_callback=progress,
            )
            break
        except Exception as error:
            if attempt == 3:
                raise
            wait_seconds = attempt * 15
            print(
                "OSS upload attempt "
                + str(attempt)
                + " failed ("
                + type(error).__name__
                + ": "
                + str(error)
                + "); retrying in "
                + str(wait_seconds)
                + " seconds",
                flush=True,
            )
            time.sleep(wait_seconds)

    if bucket.get_object_meta(key).content_length != size:
        raise SystemExit("OSS upload verification failed: " + key)
    print("OSS upload complete: oss://" + bucket_name + "/" + key, flush=True)
    print("Download URL: " + public_url(bucket_name, key), flush=True)
