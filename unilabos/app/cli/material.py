"""物料命令模块

提供 material 子命令：
- material list: 查询实验室物料（GET /lab/material?id=<lab_uuid>&with_children=<bool>）
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict

from unilabos.client import (
    EnvelopeError,
    SessionManager,
    print_error,
    print_output,
)
from unilabos.client.material_layout import MaterialLayoutClient
from unilabos.client.material_renderer import (
    MaterialRendererClient,
    MaterialRendererClientError,
)
from unilabos.client.material_template import MaterialTemplateClient
from unilabos.client.material_visual_regression import (
    MaterialVisualRegressionError,
    approve_material_baseline,
    compare_material_capture,
)
from unilabos.workspace_host.model import WorkspaceHostError

from ._client_factory import make_authenticated_client


def register_material_scene_subcommands(subparsers: Any) -> None:
    """注册只连接已附着 Workbench renderer 的场景命令。"""

    scene_parser = subparsers.add_parser(
        "scene", help="Inspect or capture the attached Workbench Material scene"
    )
    scene_subparsers = scene_parser.add_subparsers(
        title="material scene subcommands", dest="material_scene_command"
    )

    def common(parser: Any) -> None:
        parser.add_argument("--workspace", dest="material_workspace", default=".")
        parser.add_argument(
            "--view", choices=["2d", "2.5d", "3d", "split"], default=None
        )
        sites = parser.add_mutually_exclusive_group()
        sites.add_argument(
            "--show-sites", dest="material_show_sites", action="store_true"
        )
        sites.add_argument(
            "--hide-sites", dest="material_hide_sites", action="store_true"
        )
        transfers = parser.add_mutually_exclusive_group()
        transfers.add_argument(
            "--show-transfers", dest="material_show_transfers", action="store_true"
        )
        transfers.add_argument(
            "--hide-transfers", dest="material_hide_transfers", action="store_true"
        )
        parser.add_argument("--selected", action="append", default=[])
        parser.add_argument("--hidden", action="append", default=[])
        parser.add_argument("--timeout", type=float, default=30.0)
        parser.add_argument("--json", dest="material_json", action="store_true")

    inspect_parser = scene_subparsers.add_parser(
        "inspect", help="Return structured nodes, transforms, bounds and Sites"
    )
    common(inspect_parser)
    inspect_parser.add_argument(
        "--headless",
        action="store_true",
        help="Ask Workspace Host to launch the shared renderer when needed",
    )

    capture_parser = scene_subparsers.add_parser(
        "capture", help="Capture the same Material renderer already open in Workbench"
    )
    common(capture_parser)
    adapter = capture_parser.add_mutually_exclusive_group(required=True)
    adapter.add_argument(
        "--attached",
        action="store_true",
        help="Use only the currently open Workbench renderer",
    )
    adapter.add_argument(
        "--headless",
        action="store_true",
        help="Ask Workspace Host to launch the same renderer headlessly if needed",
    )
    capture_parser.add_argument("--viewport", default="1440x960")
    capture_parser.add_argument(
        "--camera", choices=["default", "top"], default="default"
    )
    capture_parser.add_argument("--pixel-ratio", type=float, default=1.0)
    capture_parser.add_argument("--output", required=True)

    compare_parser = scene_subparsers.add_parser(
        "compare", help="Compare or explicitly approve a captured visual baseline"
    )
    compare_parser.add_argument("--candidate", required=True)
    compare_parser.add_argument("--baseline", required=True)
    compare_parser.add_argument("--threshold", type=float, default=0.01)
    compare_parser.add_argument("--approve", action="store_true")
    compare_parser.add_argument("--json", dest="material_json", action="store_true")

    layout_parser = subparsers.add_parser(
        "layout", help="Preview and CAS-apply workspace Material layout changes"
    )
    layout_subparsers = layout_parser.add_subparsers(
        title="material layout subcommands", dest="material_layout_command"
    )
    layout_inspect = layout_subparsers.add_parser(
        "inspect", help="Return the selected graph layout revision and stable source IDs"
    )
    layout_inspect.add_argument("--workspace", dest="material_workspace", default=".")
    layout_inspect.add_argument("--timeout", type=float, default=120.0)
    layout_inspect.add_argument("--json", dest="material_json", action="store_true")

    layout_preview = layout_subparsers.add_parser(
        "preview", help="Compile a non-mutating layout candidate and structural diff"
    )
    layout_preview.add_argument("--workspace", dest="material_workspace", default=".")
    layout_preview.add_argument("--change-set", required=True)
    layout_preview.add_argument("--expected-revision", required=True)
    layout_preview.add_argument("--output", default=None)
    layout_preview.add_argument(
        "--headless",
        action="store_true",
        help="Launch the shared Workbench renderer when no window is attached",
    )
    layout_preview.add_argument("--timeout", type=float, default=120.0)
    layout_preview.add_argument("--json", dest="material_json", action="store_true")

    layout_apply = layout_subparsers.add_parser(
        "apply", help="Apply an existing preview if its source revision still matches"
    )
    layout_apply.add_argument("--workspace", dest="material_workspace", default=".")
    layout_apply.add_argument("--preview", required=True)
    layout_apply.add_argument("--expected-revision", required=True)
    layout_apply.add_argument("--timeout", type=float, default=120.0)
    layout_apply.add_argument("--json", dest="material_json", action="store_true")

    template_parser = subparsers.add_parser(
        "template", help="Validate Material templates in an isolated process"
    )
    template_subparsers = template_parser.add_subparsers(
        title="material template subcommands", dest="material_template_command"
    )
    template_validate = template_subparsers.add_parser(
        "validate", help="Compile the workspace catalog without publishing it"
    )
    template_validate.add_argument(
        "--workspace", dest="material_workspace", default="."
    )
    template_validate.add_argument("--timeout", type=float, default=120.0)
    template_validate.add_argument(
        "--json", dest="material_json", action="store_true"
    )


def dispatch_material_scene_command(args: Dict[str, Any]) -> bool:
    """在产品组合根之前处理 renderer 命令，不启动任何设备 runtime。"""

    if args.get("command") != "material":
        return False
    if args.get("material_command") == "template":
        return _dispatch_material_template_command(args)
    if args.get("material_command") == "layout":
        return _dispatch_material_layout_command(args)
    if args.get("material_command") != "scene":
        return False
    action = args.get("material_scene_command")
    if action not in {"inspect", "capture", "compare"}:
        print_error("material scene 子命令需要指定: inspect | capture | compare")
        raise SystemExit(1)
    if action == "compare":
        try:
            result = (
                approve_material_baseline(args["candidate"], args["baseline"])
                if args.get("approve")
                else compare_material_capture(
                    args["candidate"],
                    args["baseline"],
                    threshold=float(args.get("threshold") or 0.0),
                )
            )
        except (MaterialVisualRegressionError, OSError) as error:
            failure = (
                error.as_dict()
                if isinstance(error, MaterialVisualRegressionError)
                else {"code": "visual_artifact_invalid", "message": str(error)}
            )
            print(json.dumps({"ok": False, "error": failure}, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1) from error
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if not args.get("approve") and not result.get("passed"):
            raise SystemExit(2)
        return True
    show_sites = _optional_toggle(args, "material_show_sites", "material_hide_sites")
    show_transfers = _optional_toggle(
        args, "material_show_transfers", "material_hide_transfers"
    )
    workspace = args.get("material_workspace") or "."
    timeout = float(args.get("timeout") or 30.0)
    try:
        with MaterialRendererClient.discover(
            workspace,
            headless=bool(args.get("headless")),
            timeout=timeout,
        ) as client:
            common = {
                "view": args.get("view"),
                "show_sites": show_sites,
                "show_material_transfers": show_transfers,
                "selected_material_ids": tuple(args.get("selected") or ()),
                "hidden_material_ids": tuple(args.get("hidden") or ()),
                "timeout": timeout,
            }
            if action == "inspect":
                result = client.inspect_scene(**common)
            else:
                result = client.capture_scene(
                    Path(str(args["output"])),
                    camera_preset=str(args.get("camera") or "default"),
                    viewport=_parse_viewport(str(args.get("viewport") or "1440x960")),
                    pixel_ratio=float(args.get("pixel_ratio") or 1.0),
                    **common,
                )
    except (MaterialRendererClientError, OSError, ValueError) as error:
        failure = (
            error.as_dict()
            if isinstance(error, MaterialRendererClientError)
            else {"code": "invalid_input", "message": str(error)}
        )
        print(
            json.dumps({"ok": False, "error": failure}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    if args.get("material_json"):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        data = result.get("data") if isinstance(result, dict) else None
        if action == "capture" and isinstance(data, dict):
            image = data.get("image")
            path = image.get("path") if isinstance(image, dict) else "?"
            print(f"Material scene captured: {path}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
    return True


def _dispatch_material_layout_command(args: Dict[str, Any]) -> bool:
    action = args.get("material_layout_command")
    if action not in {"inspect", "preview", "apply"}:
        print_error("material layout 子命令需要指定: inspect | preview | apply")
        raise SystemExit(1)
    workspace = Path(str(args.get("material_workspace") or ".")).resolve()
    timeout = float(args.get("timeout") or 120.0)
    try:
        client = MaterialLayoutClient.discover(workspace, timeout=timeout)
        if action == "inspect":
            result = client.inspect()
        elif action == "preview":
            change_set = json.loads(
                Path(str(args["change_set"])).expanduser().read_text(encoding="utf-8")
            )
            if not isinstance(change_set, dict):
                raise ValueError("--change-set 文件根必须是 JSON object")
            result = client.preview(
                change_set,
                expected_revision=str(args["expected_revision"]),
            )
            output = args.get("output")
            if output:
                view = result.get("changeSet", {}).get("view", {})
                layout_overrides = result.get("changeSet", {}).get("nodes", [])
                with MaterialRendererClient.discover(
                    workspace,
                    headless=bool(args.get("headless")),
                    timeout=timeout,
                ) as renderer:
                    capture = renderer.capture_scene(
                        Path(str(output)),
                        view=str(view.get("mode") or "2.5d"),
                        camera_preset=str(view.get("cameraPreset") or "default"),
                        viewport=(
                            int(view.get("viewport", {}).get("width") or 1440),
                            int(view.get("viewport", {}).get("height") or 960),
                        ),
                        layout_overrides=layout_overrides,
                        timeout=timeout,
                    )
                result = {**result, "previewImage": capture.get("data")}
        else:
            result = client.apply(
                str(args["preview"]),
                expected_revision=str(args["expected_revision"]),
            )
    except (
        MaterialRendererClientError,
        WorkspaceHostError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        failure = (
            error.as_dict()
            if isinstance(error, (MaterialRendererClientError, WorkspaceHostError))
            else {"code": "invalid_input", "message": str(error)}
        )
        print(
            json.dumps({"ok": False, "error": failure}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    if args.get("material_json"):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif action == "inspect":
        print(f"Material layout revision: {result.get('revision')}")
    elif action == "preview":
        print(f"Material layout preview: {result.get('previewId')}")
    else:
        print(f"Material layout applied: {result.get('revision')}")
    return True


def _dispatch_material_template_command(args: Dict[str, Any]) -> bool:
    if args.get("material_template_command") != "validate":
        print_error("material template 子命令需要指定: validate")
        raise SystemExit(1)
    workspace = Path(str(args.get("material_workspace") or ".")).resolve()
    try:
        result = MaterialTemplateClient(
            workspace,
            timeout=float(args.get("timeout") or 120.0),
        ).validate()
    except (WorkspaceHostError, OSError, ValueError) as error:
        failure = (
            error.as_dict()
            if isinstance(error, WorkspaceHostError)
            else {"code": "invalid_input", "message": str(error)}
        )
        print(
            json.dumps({"ok": False, "error": failure}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") != "valid":
        raise SystemExit(2)
    return True


def _optional_toggle(args: Dict[str, Any], positive: str, negative: str) -> bool | None:
    if args.get(positive):
        return True
    if args.get(negative):
        return False
    return None


def _parse_viewport(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (TypeError, ValueError) as error:
        raise ValueError("--viewport 必须为 WIDTHxHEIGHT") from error
    if not 320 <= width <= 4096 or not 240 <= height <= 4096:
        raise ValueError("--viewport 必须在 320x240 到 4096x4096 范围内")
    return width, height


def cmd_material_list(args, session_manager: SessionManager):
    """material list 命令处理 — GET /lab/material?id=<lab_uuid>&with_children=<bool>"""
    try:
        with session_manager:
            client, _ = make_authenticated_client(args, session_manager)

            params = {"id": args.lab_uuid, "with_children": str(args.with_children).lower()}

            try:
                data = client.get("/lab/material", params=params)
                print_output(data)
            except EnvelopeError as e:
                print_error(f"获取物料列表失败: {e.error}")
                sys.exit(1)
            except Exception as e:
                print_error(f"获取物料列表失败: {e}")
                sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print_error(f"操作失败: {e}")
        sys.exit(1)
