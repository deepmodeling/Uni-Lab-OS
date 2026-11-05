"""七个 demo 的业务验收：任务成功不等于实验正确，必须核对返回值和独立权威账目。"""

from __future__ import annotations

import importlib
import json
import sys
import time
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

from tests.e2e.readme_demos import E2E_CONTROL_TOKEN, DemoSpec, api_request


@contextmanager
def demo_smoke(spec: DemoSpec, source: Path):
    """只载入本次选定版本，退出时还原模块缓存，不让相邻 checkout 污染 pinned 验收。"""

    def belongs(name):
        return name == spec.package or name.startswith(spec.package + ".")

    saved = {name: module for name, module in sys.modules.copy().items() if belongs(name)}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(source))
    try:
        importlib.invalidate_caches()
        smoke = importlib.import_module(f"{spec.package}.smoke")
        assert Path(smoke.__file__).resolve() == (source / spec.package / "smoke.py").resolve()
        # demo 的 smoke 自己定控制面 token；这里的运行时由 harness 拉起，用 harness 那份
        if hasattr(smoke, "CONTROL_TOKEN"):
            smoke.CONTROL_TOKEN = E2E_CONTROL_TOKEN
        yield smoke
    finally:
        sys.path.remove(str(source))
        for name in list(sys.modules):
            if belongs(name):
                del sys.modules[name]
        sys.modules.update(saved)


class DemoContracts:
    def __init__(self, spec, smoke, port, backend):
        self.spec, self.smoke, self.port, self.backend = spec, smoke, port, backend
        self.checked = []

    def assert_initial(self, proofs):
        if self.spec.package == "lan_demo":
            self.smoke.assert_smoke_proof(proofs["LAN_DEMO_PROOF_FILE"], self.backend)

    def assert_batch(self, expectations, results):
        smoke = self.smoke
        package = self.spec.package
        assert len(expectations) == len(results)
        for expectation, proof in zip(expectations, results, strict=True):
            assert proof["workflow_name"] == expectation.name
            self.checked.append(expectation.name)
            index = list(self.spec.workflows).index(expectation)
            if package == "workstation_demo":
                smoke.assert_workflow_proof(proof, self.backend)
            elif package == "lan_demo":
                smoke.assert_workflow_proof(proof)
            elif package == "complex_workflow_demo":
                functions = [
                    smoke.assert_for_workflow,
                    smoke.assert_wait_workflow,
                    smoke.assert_until_workflow,
                    smoke.assert_nested_workflow,
                    smoke.assert_complex_workflow,
                ]
                functions[index](proof)
            elif package == "exception_demo":
                decision = proof["decision"]
                assert all(isinstance(option, dict) and option.get("action") for option in decision["options"])
                # demo 的旧断言接收 action 字符串列表；仅做断言输入投影，不修改原始决策证据。
                projected = {
                    **proof,
                    "decision": {
                        **decision,
                        "selected_action": decision["resolved_action"],
                        "options": [option["action"] for option in decision["options"]],
                    },
                }
                [smoke.assert_failure_workflow, smoke.assert_recovery_workflow, smoke.assert_retry_workflow][index](
                    projected
                )
            elif package == "materials_demo":
                functions = [
                    lambda p: smoke.assert_site_loop_workflow(p, self.backend),
                    smoke.assert_material_loop_workflow,
                    smoke.assert_site_tour_workflow,
                    smoke.assert_material_flow_workflow,
                ]
                projected = proof
                if index in {1, 3}:
                    # API 导入已经把 SiteSlot 绑定成 uuid，驱动会原样回报该 uuid。
                    # 按权威核对 UUID 和实际占位，保留原始返回值并提供 label -> UUID 绑定。
                    deck = api_request(self.port, f"/materials/instances/{smoke.DECK_UUID}")
                    label = "T3" if index == 1 else "T4"
                    site = next(site for site in deck["sites"] if site["label"] == label)
                    projected = deepcopy(proof)
                    moved = projected["jobs"][3 if index == 1 else 2]["return_info"]["return_value"]
                    assert moved["to_site"] == site["site_uuid"], (moved, site)
                    assert moved["plate_uuid"] == site["occupied_material_uuid"], (moved, site)
                    projected["site_bindings"] = {item["label"]: item["site_uuid"] for item in deck["sites"]}
                functions[index](projected)
            elif package == "inventory_demo":
                # 每步完成后、下一步提交前直查；不能用整个运行期间出现过的值充当这一刻的证据。
                proof["lot"] = api_request(self.port, f"/materials/lots/{smoke.WATER_LOT_UUID}")
                [smoke.assert_restock, smoke.assert_dispense_ok, smoke.assert_dispense_short, smoke.assert_audit][
                    index
                ](proof)
            elif package == "lock_demo":
                if expectation.name == smoke.AUDIT_WORKFLOW_NAME:
                    smoke.assert_audit(proof)
            else:
                raise AssertionError(f"demo 缺少业务验收：{package}")

        if package == "lock_demo" and expectations[0].group:
            group = {"tasks": results, "queued": {p["workflow_name"]: p["queued"] for p in results if p["queued"]}}
            if expectations[0].group == "action-lock":
                smoke.assert_action_lock_group(group)
            else:
                smoke.assert_material_lock_group(group)
            # blockers 必须确实指向本组的持锁 attempt，不能只是碰巧观察到其他任务在排队。
            job_ids = {job["uuid"] for proof in results for job in proof["jobs"]}
            for request in group["queued"].values():
                assert request["blockers"]
                assert set(request["blockers"]) <= job_ids, request

    def assert_final(self, *, timeout: float, output: Path):
        assert self.checked == [expectation.name for expectation in self.spec.workflows], self.checked
        if self.spec.package != "materials_demo":
            return
        smoke = self.smoke
        tree = api_request(self.port, f"/materials/instances/{smoke.DECK_UUID}/tree")
        nodes = {node["material"]["material_uuid"]: node for node in tree["nodes"]}
        deck = nodes[smoke.DECK_UUID]
        # 当前 PLR 协议只有 config.type，不能读取已删除的外层 class_name。
        final = {
            "site_occupancy": {
                site["label"]: (
                    nodes[site["occupied_material_uuid"]]["material"]["name"]
                    if site.get("occupied_material_uuid")
                    else ""
                )
                for site in deck["sites"]
            },
            "class_names": sorted({node["material"]["config"]["type"] for node in tree["nodes"]}),
            "root_children": sorted(
                node["material"]["name"]
                for node in tree["nodes"]
                if node["material"]["parent_material_uuid"] == smoke.DECK_UUID
            ),
        }
        smoke.assert_authority_final_state(final)
        # 复用 demo 的 HTTP 场景（不运行它的进程启动器）：创建两块板、缺料拒绝、补料后同图重跑。
        stage = smoke.run_material_flow_stage(self.port, time.monotonic() + timeout)
        (output / "material-flow-proof.json").write_text(
            json.dumps(stage, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        succeeded = stage["succeeded_run"]
        fill = succeeded["node_runs"][1]["return_info"]["return_value"]
        selected_uuid = succeeded["plates"][fill["plate_name"]]["material_uuid"]
        selected = api_request(self.port, f"/materials/instances/{selected_uuid}/tree")
        root = next(n for n in selected["nodes"] if n["material"]["material_uuid"] == selected_uuid)
        well_name = root["material"]["config"]["ordering"]["A1"]
        well = next(n for n in selected["nodes"] if n["material"]["name"] == well_name)
        assert well["material"]["parent_material_uuid"] == selected_uuid
        assert well["data"]["data"]["volume"] == 1200
        substances = well["data"]["substances"]
        assert [(s["name"], s["quantity"], s["quantity_unit"]) for s in substances] == [("Water", 1200, "ul")]
