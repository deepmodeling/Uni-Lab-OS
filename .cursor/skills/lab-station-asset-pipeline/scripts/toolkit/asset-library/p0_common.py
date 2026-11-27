"""Read-only acceptance helpers; contracts are review inputs, not asset schemas."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(base, value):
    return (Path(base) / value).resolve()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def unique(rows, key="id"):
    result = {}
    for row in rows:
        ident = row[key]
        require(isinstance(ident, str) and ident.strip(), "Empty identity")
        require(ident not in result, "Duplicate identity: " + ident)
        result[ident] = row
    return result


def scoped(document, rows):
    expected = document["expected_ids"]
    require(expected and len(set(expected)) == len(expected), "Empty/duplicate expected scope")
    require(set(expected) == set(unique(rows)), "Expected scope differs from supplied rows")


def result(rows, **extra):
    failed = sum(r["status"] == "failed" for r in rows)
    pending = sum(r["status"] == "incomplete" for r in rows)
    return {"status": "failed" if failed else "incomplete" if pending else "passed",
            "counts": {"total": len(rows), "failed": failed, "incomplete": pending,
                       "passed": len(rows) - failed - pending}, "checks": rows, **extra}


def checked(ident, function):
    try:
        return {"id": ident, "status": "passed", **function()}
    except Exception as error:
        return {"id": ident, "status": "failed", "error": type(error).__name__ + ": " + str(error)}


def cli(description, check, behavior=False):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    if behavior:
        parser.add_argument("--execute-behavior", action="store_true",
                            help="Execute explicitly pinned, trusted local behavior Python; not a sandbox")
    args = parser.parse_args()
    try:
        data = read(args.input)
        report = check(data, args.input.resolve().parent,
                       **({"execute": args.execute_behavior} if behavior else {}))
    except Exception as error:
        report = result([{"id": "contract", "status": "failed", "error": str(error)}])
    report.update(input_sha256=digest(args.input), tool_sha256=digest(sys.argv[0]),
                  interpreter=sys.version.split()[0], qualification="only the named checks; not physical/hardware qualification")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "counts": report["counts"], "output": str(args.output)}, ensure_ascii=False))
    return {"passed": 0, "failed": 1, "incomplete": 2}[report["status"]]
