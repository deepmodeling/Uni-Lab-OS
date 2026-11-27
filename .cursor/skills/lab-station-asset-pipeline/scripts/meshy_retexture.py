#!/usr/bin/env python3
"""Small, single-submission Meshy retexture client. Python 3.10+, standard library.

plan is offline; submit performs one POST; status performs one GET; download only
uses a recorded SUCCEEDED task. A reserved state is never eligible for re-POST.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

API = "https://api.meshy.ai/openapi/v1/retexture"
TRUSTED_DOWNLOAD_HOSTS = {"assets.meshy.ai", "cdn.meshy.ai"}
MAX_SOURCE_BYTES = 100_000_000
MAX_JSON_BYTES = 4_000_000
MODEL_FORMATS = {"glb", "stl", "fbx", "obj", "usdz"}
TEXTURE_KINDS = {"base_color", "metallic", "roughness", "normal", "emission"}
TASK_STATUSES = {"PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "CANCELED"}


class ClientError(Exception):
    """A public message that does not contain request/response secrets."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ClientError("Redirect refused; no credentials were forwarded.")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def redact(value):
    """Persist only selected fields; additionally remove a configured key anywhere."""
    key = os.environ.get("MESHY_API_KEY", "")
    if isinstance(value, str):
        if value.startswith("data:"):
            return "<data-uri-not-recorded>"
        return value.replace(key, "<redacted>") if key else value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def write_json(path, value, *, exclusive=False):
    path = Path(path)
    data = canonical(redact(value))
    if exclusive:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ClientError("Output/state already exists; refusing overwrite or resubmission.") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    fd, temporary = tempfile.mkstemp(prefix=".meshy-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path):
    try:
        value = json.loads(Path(path).read_bytes())
        if not isinstance(value, dict) or value.get("schema") != "lab.meshy-retexture-state/v1":
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ClientError("State is missing or invalid; use the original submission receipt.") from None


def https_url(value, *, download=False):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and parsed.port in (None, 443) and not parsed.fragment)
        if not valid or any(ord(c) < 32 for c in value):
            raise ValueError
        if download and parsed.hostname not in TRUSTED_DOWNLOAD_HOSTS:
            raise ValueError
        key = os.environ.get("MESHY_API_KEY", "")
        if key and key in value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise ClientError("URL must be HTTPS without credentials; downloads require an allowed Meshy host.") from None


def check_glb(data):
    try:
        magic, version, size = struct.unpack_from("<4sII", data)
        if magic != b"glTF" or version != 2 or size != len(data):
            raise ValueError
        length, kind = struct.unpack_from("<II", data, 12)
        if kind != 0x4E4F534A or 20 + length > len(data):
            raise ValueError
        document = json.loads(data[20:20 + length])
        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "uri" and (not isinstance(child, str) or not child.startswith("data:")):
                        raise ValueError
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(document)
    except (ValueError, struct.error, UnicodeError):
        raise ClientError("Expected a self-contained GLB v2; external resource URIs are unsupported.") from None


def source_bytes(path):
    path = Path(path)
    if path.suffix.lower() not in {".glb", ".stl"}:
        raise ClientError("This client accepts self-contained local GLB/STL only; export other formats first.")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise ClientError("Cannot read the model file.") from None
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ClientError("Model must be nonempty and at most 100,000,000 bytes for data-URI upload.")
    if path.suffix.lower() == ".glb":
        check_glb(data)
    else:
        binary = len(data) >= 84 and 84 + 50 * struct.unpack_from("<I", data, 80)[0] == len(data)
        ascii_stl = data.lstrip().startswith(b"solid") and b"facet" in data and b"endsolid" in data
        if not (binary or ascii_stl):
            raise ClientError("Input does not have a recognized STL structure.")
    return data


def prepare(args):
    data = source_bytes(args.model)
    payload = {"ai_model": args.ai_model, "enable_pbr": args.enable_pbr,
               "enable_original_uv": args.enable_original_uv,
               "texture_resolution": args.texture_resolution}
    if args.ai_model == "meshy-6-lite" and args.texture_resolution != "2k":
        raise ClientError("meshy-6-lite supports only 2k in this client.")
    if args.text_style_prompt is not None:
        if not args.text_style_prompt.strip() or len(args.text_style_prompt) > 800:
            raise ClientError("Text style must contain 1 to 800 characters.")
        payload["text_style_prompt"] = args.text_style_prompt
    elif args.image_style_url:
        payload["image_style_url"] = https_url(args.image_style_url)
    else:
        if not 1 <= len(args.multiview_image_urls) <= 4 or args.ai_model != "meshy-7":
            raise ClientError("Multiview requires 1 to 4 images and explicit meshy-7.")
        payload["multiview_image_urls"] = [https_url(url) for url in args.multiview_image_urls]
    preview = {"schema": "lab.meshy-retexture-plan/v1", "endpoint": API,
               "source": {"path": str(Path(args.model).resolve()), "bytes": len(data), "sha256": digest(data)},
               "request_preview": {**payload, "model_url": "<local-model-data-uri-built-in-memory>"},
               "estimated_credits": args.estimated_credits, "max_credits": args.max_credits,
               "pricing_source": https_url(args.pricing_source) if args.pricing_source else None,
               "estimate_kind": "operator_supplied_current_price_estimate",
               "budget_enforcement": "local_preflight_only_not_a_service_side_spending_cap"}
    for value in (args.estimated_credits, args.max_credits):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ClientError("Credit estimate and maximum must be finite positive numbers.")
    if args.estimated_credits is not None and args.max_credits is not None and args.estimated_credits > args.max_credits:
        raise ClientError("Estimated credits exceed the authorized maximum.")
    return data, payload, preview


def api_json(method, endpoint, payload=None):
    key = os.environ.get("MESHY_API_KEY", "")
    if not key:
        raise ClientError("MESHY_API_KEY is not set.")
    request = Request(endpoint, data=canonical(payload) if payload is not None else None,
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response:
            data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ClientError("API response exceeded the permitted size.")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except HTTPError as error:
        raise ClientError(f"API HTTP {error.code}; response body withheld. No automatic retry.") from None
    except (URLError, TimeoutError, OSError):
        raise ClientError("API transport failed; response details withheld. No automatic retry.") from None
    except (ValueError, UnicodeError):
        raise ClientError("API returned an invalid JSON object; response withheld.") from None


def task_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ClientError("Expected a UUID task ID.") from None


def submit(args):
    if Path(args.state).exists():
        raise ClientError("State already exists; submit cannot retry. Use status with the same state.")
    if not args.execute or not args.allow_upload:
        raise ClientError("Submission requires --execute and --allow-upload after explicit authorization.")
    if args.estimated_credits is None or args.max_credits is None or not args.pricing_source:
        raise ClientError("Supply --estimated-credits, --max-credits and --pricing-source from current pricing.")
    if not os.environ.get("MESHY_API_KEY"):
        raise ClientError("MESHY_API_KEY is not set.")
    data, payload, preview = prepare(args)
    payload["model_url"] = "data:application/octet-stream;base64," + base64.b64encode(data).decode("ascii")
    state = {"schema": "lab.meshy-retexture-state/v1", "phase": "submission_reserved",
             "created_at": utc_now(), "plan": preview, "plan_sha256": digest(canonical(preview)),
             "request_sha256": digest(canonical(payload)), "task_id": None,
             "authorization": {"upload_allowed": True, "single_post_allowed": True}}
    write_json(args.state, state, exclusive=True)  # durable reservation before any POST
    try:
        response = api_json("POST", API, payload)
        identifier = task_id(response.get("result"))
    except Exception:
        state.update(phase="submission_outcome_unknown", last_event_at=utc_now())
        write_json(args.state, state)
        raise ClientError("Submission outcome unknown. Reservation retained; do not resubmit. Recover the task ID from Meshy, then use status --task-id.") from None
    state.update(phase="submitted", task_id=identifier, last_event_at=utc_now())
    write_json(args.state, state)
    return {"phase": state["phase"], "task_id": identifier, "state": str(args.state)}


def status(args):
    state = read_state(args.state)
    known_id = state.get("task_id")
    identifier = task_id(args.task_id or known_id)
    if known_id and identifier != known_id:
        raise ClientError("Requested task ID differs from the saved receipt.")
    result = api_json("GET", API + "/" + identifier)
    if task_id(result.get("id")) != identifier or result.get("status") not in TASK_STATUSES:
        raise ClientError("Task response identity or status is invalid; original state retained.")
    snapshot = {"id": identifier, "status": result["status"], "checked_at": utc_now()}
    for field in ("progress", "consumed_credits", "created_at", "started_at", "finished_at", "expires_at"):
        value = result.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            snapshot[field] = value
    if result["status"] == "SUCCEEDED":
        snapshot["model_urls"] = {fmt: https_url(url, download=True) for fmt, url in result.get("model_urls", {}).items()
                                  if fmt in MODEL_FORMATS | {"mtl"} and url}
        snapshot["texture_urls"] = [{kind: https_url(url, download=True) for kind, url in item.items()
                                     if kind in TEXTURE_KINDS and url} for item in result.get("texture_urls", [])]
    state.update(task_id=identifier, phase="task_observed", task=snapshot, last_event_at=utc_now())
    write_json(args.state, state)  # a failed GET never alters the reservation/receipt
    return {"task_id": identifier, "status": snapshot["status"], "progress": snapshot.get("progress"),
            "consumed_credits": snapshot.get("consumed_credits"), "state": str(args.state)}


def download_file(url, destination, max_bytes):
    https_url(url, download=True)
    request = Request(url, method="GET")  # deliberately no Authorization header
    sha = hashlib.sha256()
    size = 0
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        with build_opener(NoRedirect()).open(request, timeout=60) as response, temporary.open("xb") as stream:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ClientError("Download exceeded --max-file-bytes; partial output retained.")
                sha.update(chunk)
                stream.write(chunk)
        if not size:
            raise ClientError("Download was empty; partial output retained.")
        temporary.replace(destination)
    except (HTTPError, URLError, OSError, TimeoutError):
        raise ClientError("Download failed; details withheld and partial output retained. No automatic retry.") from None
    return {"path": destination.name, "bytes": size, "sha256": sha.hexdigest()}


def download(args):
    state = read_state(args.state)
    task = state.get("task", {})
    if task.get("status") != "SUCCEEDED" or task.get("id") != state.get("task_id"):
        raise ClientError("Download requires a saved SUCCEEDED task from status.")
    if args.max_file_bytes <= 0:
        raise ClientError("--max-file-bytes must be positive.")
    models = task.get("model_urls", {})
    if not models.get(args.format):
        raise ClientError("Requested model format is absent from the completed task.")
    selected = [(f"model.{args.format}", models[args.format])]
    if args.format == "obj" and models.get("mtl"):
        selected.append(("model.mtl", models["mtl"]))
    for index, textures in enumerate(task.get("texture_urls", [])):
        for kind in sorted(TEXTURE_KINDS):
            if textures.get(kind):
                extension = Path(urlsplit(textures[kind]).path).suffix.lower()
                if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
                    raise ClientError("Texture URL does not have a supported image extension.")
                selected.append((f"texture_{index:03d}_{kind}{extension}", textures[kind]))
    for _, url in selected:
        https_url(url, download=True)
    output = Path(args.output)
    try:
        output.mkdir(parents=False, exist_ok=False)
    except OSError:
        raise ClientError("Download output must be a new directory under an existing parent.") from None
    receipt = {"schema": "lab.meshy-retexture-download/v1", "task_id": state["task_id"],
               "source_sha256": state["plan"]["source"]["sha256"], "phase": "partial", "files": []}
    write_json(output / "download-receipt.json", receipt)
    for filename, url in selected:
        receipt["files"].append(download_file(url, output / filename, args.max_file_bytes))
        write_json(output / "download-receipt.json", receipt)
    receipt.update(phase="downloaded_unqualified", finished_at=utc_now())
    write_json(output / "download-receipt.json", receipt)
    return {"phase": receipt["phase"], "files": len(receipt["files"]), "output": str(output)}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("plan", "submit"):
        command = commands.add_parser(name)
        command.add_argument("--model", required=True, type=Path, help="Self-contained local GLB or STL (<=100 MB)")
        style = command.add_mutually_exclusive_group(required=True)
        style.add_argument("--text-style-prompt")
        style.add_argument("--image-style-url", help="Public HTTPS image URL; no local/base64 image support")
        style.add_argument("--multiview-image-urls", nargs="+", help="1 to 4 HTTPS images, front first; meshy-7")
        command.add_argument("--ai-model", choices=("meshy-7", "meshy-6", "meshy-6-lite"), default="meshy-7")
        command.add_argument("--enable-pbr", action=argparse.BooleanOptionalAction, default=True)
        command.add_argument("--enable-original-uv", action=argparse.BooleanOptionalAction, default=False)
        command.add_argument("--texture-resolution", choices=("2k", "4k", "8k"), default="2k")
        command.add_argument("--estimated-credits", type=float)
        command.add_argument("--max-credits", type=float, help="Authorized single-task limit, checked locally only")
        command.add_argument("--pricing-source", help="Current pricing page used for the supplied estimate")
        if name == "plan":
            command.add_argument("--output", required=True, type=Path)
        else:
            command.add_argument("--state", required=True, type=Path)
            command.add_argument("--execute", action="store_true")
            command.add_argument("--allow-upload", action="store_true")
    command = commands.add_parser("status")
    command.add_argument("--state", required=True, type=Path)
    command.add_argument("--task-id", help="Recover an unknown POST using the task ID obtained from Meshy")
    command = commands.add_parser("download")
    command.add_argument("--state", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--format", choices=sorted(MODEL_FORMATS), default="glb")
    command.add_argument("--max-file-bytes", type=int, default=1_000_000_000)
    return root


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            _, _, result = prepare(args)
            write_json(args.output, result, exclusive=True)
            result = {"phase": "offline_plan", "output": str(args.output), "source_sha256": result["source"]["sha256"]}
        else:
            result = {"submit": submit, "status": status, "download": download}[args.command](args)
        print(json.dumps(redact(result), ensure_ascii=False))
        return 0
    except ClientError as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print("error: local input, response structure, or file operation failed; details withheld.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
