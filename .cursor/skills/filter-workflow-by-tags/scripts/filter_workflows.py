#!/usr/bin/env python3
"""分页拉取 Uni-Lab 工作流列表，汇总 tags 并按 tag 筛选。

使用示例:
    python filter_workflows.py \
        --auth <base64token> \
        --base https://leap-lab.test.bohrium.com \
        --lab-uuid a9059772-... \
        --tags synthesis organic --mode any

仅依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter


def fetch_all_workflows(base: str, auth_token: str, lab_uuid: str, page_size: int = 1000) -> list[dict]:
    """分页拉取所有 owner 工作流，直到 has_more=false。"""
    workflows: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"page": page, "page_size": page_size, "lab_uuid": lab_uuid}
        )
        url = f"{base.rstrip('/')}/api/v1/lab/workflow/owner/list?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Lab {auth_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            sys.exit(f"[ERROR] HTTP {e.code} on page {page}: {e.read().decode('utf-8', 'ignore')}")
        except urllib.error.URLError as e:
            sys.exit(f"[ERROR] URL error on page {page}: {e.reason}")

        if payload.get("code") != 0:
            sys.exit(f"[ERROR] API returned non-zero code: {payload}")

        data = payload.get("data") or {}
        page_items = data.get("data") or []
        workflows.extend(page_items)

        if not data.get("has_more"):
            break
        page += 1
        # 防御性兜底，避免接口异常导致无限循环
        if page > 1000:
            print(f"[WARN] page count exceeded 1000, stopping early", file=sys.stderr)
            break

    return workflows


def aggregate_tags(workflows: list[dict]) -> tuple[list[str], dict[str, int], int]:
    """返回 (sorted_tags, tag_counts, untagged_count)。"""
    counter: Counter[str] = Counter()
    untagged = 0
    for wf in workflows:
        tags = wf.get("tags")
        if not tags:
            untagged += 1
            continue
        for t in tags:
            if isinstance(t, str) and t.strip():
                counter[t.strip()] += 1
    return sorted(counter.keys()), dict(counter), untagged


def filter_workflows(
    workflows: list[dict],
    want_tags: list[str],
    mode: str,
    published_only: bool,
) -> list[dict]:
    """按 tag 筛选。mode 取值 any / all。"""
    want_set = {t.strip() for t in want_tags if t.strip()}
    out: list[dict] = []
    for wf in workflows:
        if published_only and not wf.get("published"):
            continue
        if not want_set:
            out.append(wf)
            continue
        tags = wf.get("tags") or []
        tag_set = {t for t in tags if isinstance(t, str)}
        if mode == "all":
            if want_set.issubset(tag_set):
                out.append(wf)
        else:  # any
            if want_set & tag_set:
                out.append(wf)
    return out


def project_workflow(wf: dict) -> dict:
    """精简输出字段。"""
    return {
        "uuid": wf.get("uuid"),
        "name": wf.get("name"),
        "description": wf.get("description", ""),
        "tags": wf.get("tags") or [],
        "published": bool(wf.get("published")),
        "user_id": wf.get("user_id"),
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch & filter Uni-Lab workflows by tags.")
    p.add_argument("--auth", required=True, help="Base64 token (the part after `Lab `).")
    p.add_argument("--base", required=True, help="Base URL, e.g. https://leap-lab.test.bohrium.com")
    p.add_argument("--lab-uuid", required=True, help="Lab UUID.")
    p.add_argument("--tags", nargs="*", default=[], help="Tags to filter by (space separated).")
    p.add_argument(
        "--mode",
        choices=["any", "all"],
        default="any",
        help="any: workflow contains at least one tag; all: workflow contains every tag.",
    )
    p.add_argument("--published-only", action="store_true", help="Only include published workflows.")
    p.add_argument("--page-size", type=int, default=1000, help="Page size, default 1000.")
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Print tag summary without applying filter (still fetches everything).",
    )
    p.add_argument("--output", help="Write JSON result to this path. If omitted, print to stdout.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    workflows = fetch_all_workflows(
        base=args.base,
        auth_token=args.auth,
        lab_uuid=args.lab_uuid,
        page_size=args.page_size,
    )
    sorted_tags, tag_counts, untagged = aggregate_tags(workflows)

    if args.summary_only:
        result = {
            "total_workflows": len(workflows),
            "untagged_count": untagged,
            "tag_counts": tag_counts,
            "all_tags": sorted_tags,
        }
    else:
        filtered = filter_workflows(
            workflows,
            want_tags=args.tags,
            mode=args.mode,
            published_only=args.published_only,
        )
        result = {
            "total_workflows": len(workflows),
            "untagged_count": untagged,
            "tag_counts": tag_counts,
            "all_tags": sorted_tags,
            "filter": {
                "tags": args.tags,
                "mode": args.mode,
                "published_only": args.published_only,
            },
            "matched_count": len(filtered),
            "filtered_workflows": [project_workflow(wf) for wf in filtered],
        }

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"Wrote {len(workflows)} workflows summary → {args.output}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
