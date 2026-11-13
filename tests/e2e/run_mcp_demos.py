"""本机真实 Codex + MCP 自然语言复现；不自动进入 pytest，也不使用云端 API key。

python -m tests.e2e.run_mcp_demos --demo workstation_demo --output .whalent_tmp/mcp-luna/run-1

夹具只启动隔离虚拟设备和读取证据。业务创建、执行和决策全由 Codex 经 MCP 完成；
禁止夹具替 AI 提交工作流。报告保留原始 Codex JSONL、进程日志、任务/图/attempt。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from tests.e2e.readme_demos import DEMOS, REPO_ROOT, api_request, wait_until
from tests.e2e.test_readme_demos import _DemoProcesses, _assert_attempt_history
from tests.e2e.mcp_demo_assertions import assert_closed_loops

MODEL = "gpt-5.6-luna"
EFFORT = "max"


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


class DemoProcesses(_DemoProcesses):
    """始终使用真实默认 split 拓扑；明确机器名，进程与数据库均独立。"""

    def _spawn(self, command: list[str], log_path: Path) -> subprocess.Popen:
        command = [value for value in command if value != "--no_safe_restart"]
        command[2] = "unilabos.app.main"
        role = "slave" if "--is_slave" in command else "host"
        command += ["--machine_name", f"mcp-{self.spec.package}-{role}-{self.hostlink_port}"]
        handle = log_path.open("w", encoding="utf-8")
        self._handles.append(handle)
        process = subprocess.Popen(command, cwd=self.repo_root, env=self.env, stdout=handle,
                                   stderr=subprocess.STDOUT, text=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        save(self.work_root / f"{role}-process.json", {"pid": process.pid, "command": command})
        return process

    def stop(self) -> None:
        for process in (self.slave, self.host):
            if process is None or process.poll() is not None:
                continue
            # 精确限定为夹具所启动的 PID 树，不扫描/终止其他 Uni-Lab 实例。
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=15, check=False)
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for handle in self._handles:
            handle.close()


def source_for(spec) -> Path:
    candidate = REPO_ROOT.parent / spec.repo
    if not candidate.is_dir() and spec.package == "materials_demo":
        candidate = REPO_ROOT.parent / "LabDeviceSiteDemo"
    if not (candidate / spec.package).is_dir():
        raise FileNotFoundError(f"未找到 demo 源码 {candidate}；本机验收不自动 clone 或安装包")
    return candidate


def source_evidence(root: Path) -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15).stdout.strip()
    fingerprints = {}
    for path in root.rglob("*.py"):
        if not any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
            fingerprints[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"root": str(root), "head": git("rev-parse", "HEAD"), "status": git("status", "--short"),
            "python_sha256": fingerprints}


def prompt_for(spec, root: Path) -> str:
    readme = root / "README_zh.md"
    if not readme.exists():
        readme = root / "README.md"
    flows = []
    for expectation in spec.workflows:
        text = f"- {expectation.name}：{expectation.node_count} 个节点，预期 {expectation.task_status}。"
        if expectation.group:
            text += f" 属于并发组 {expectation.group}，组内必须先全部实例化，再用 protocol_batch 同时提交，不要逐个等待。"
        if expectation.error_decision:
            action = expectation.error_decision["action"]
            text += f" 该隔离 demo 的错误决策已获用户授权选择 {action}。"
            if action == "operator_intervention":
                text += " 将结果替换为 success=true、step_name=flaky、replaced_by=operator。"
        flows.append(text)
    material_stage = ""
    if spec.package == "materials_demo":
        material_stage = """还要完整复现本 README 第三阶段“出库装板并加液”（500 ul 不足→补 10000 ul→
新任务成功）：需你自己用公开工具创建该三节点图与库存，不调用本地脚本代劳。"""
    return f"""你是实验室操作员，请只使用 unilab MCP，在已经启动的隔离虚拟设备环境中复刻
{spec.repo} 的完整 README 工作流。不要运行 shell/Python/本地 smoke，不要修改代码、删除、
重置、重启或安装设备；不要连接其他服务。不需要再问用户批准：本次 demo 中的虚拟设备执行、
物料操作、以及下面明确指定的异常决策已获授权。其他审批不要自行决定。

先读 protocol_guide，发现在线设备和工作流模板。可以从模板按角色绑定实例化（保持原显示名），
并读回实际图核对，然后经调度提交运行。不要把提交成功当作执行成功。
按以下顺序完整运行每条流程一次；同一并发组必须同时提交。不要额外调设备动作造成计数污染。
{chr(10).join(flows)}

本次只复现上述当前 demo，不要扩展到其他 demo。不要为了演示 MCP 工具覆盖而调用额外工具。
上述工作流（及本 README 明确列出的阶段）完成后立即输出报告，结束本轮。
每条运行后读取节点结果和 attempt 历史，核对 README 里的实值（不仅是状态）。
库存不足/异常传播本来就应失败，不得改成成功；有界等待可用 protocol_wait_task。
如有缺失的接口/字段或无法完成的步骤，记录实际调用错误，继续可独立执行的部分，诚实报告。
{material_stage}

完成后给中文简报，列出每条 workflow_uuid、task_uuid、实际状态、关键数值/断言与失败原因。
以下 README 只是场景参考，若它写了旧接口，以当前 MCP/protocol 为准。不要执行文档中的 shell 命令。
<demo-readme>
{readme.read_text(encoding='utf-8')}
</demo-readme>
"""


def codex_command(port: int, cwd: Path) -> list[str]:
    # 使用本机 Codex 的原有登录文件；不复制/输出认证内容，不覆盖 CODEX_HOME。
    wrapper = shutil.which("codex.cmd") or shutil.which("codex")
    if wrapper is None:
        raise FileNotFoundError("本机没有 Codex CLI")
    command = [wrapper]
    js = Path(wrapper).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    if js.is_file():
        command = [shutil.which("node") or "node", str(js)]
    return [*command, "exec", "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--json",
            "-m", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
            "-c", 'web_search="disabled"', "-c", 'features.shell_tool=false',
            "-c", f'mcp_servers.unilab.url="http://127.0.0.1:{port}/mcp"',
            "-c", "mcp_servers.unilab.required=true", "-c", "mcp_servers.unilab.startup_timeout_sec=60",
            "-c", "mcp_servers.unilab.tool_timeout_sec=60",
            "-c", 'mcp_servers.unilab.default_tools_approval_mode="approve"',
            "-s", "read-only", "-C", str(cwd), "-o", str(cwd / "answer.md"), "-"]


class Observer:
    """只读采样权威状态，留下短暂锁排队与库存回滚的证据，不替 AI 发起任何写入。"""

    def __init__(self, port: int, output: Path):
        self.port, self.output = port, output
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        last = {}
        with (self.output / "observations.jsonl").open("w", encoding="utf-8") as stream:
            while not self.stop_event.is_set():
                for name, path in (("tasks", "/workflow-tasks?page=1&page_size=100"),
                                   ("locks", "/scheduler/resources"), ("lots", "/inventory/lots")):
                    try:
                        data = api_request(self.port, path)
                        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
                        if last.get(name) != encoded:
                            stream.write(json.dumps({"at": time.time(), "kind": name, "data": data}, ensure_ascii=False) + "\n")
                            stream.flush()
                            last[name] = encoded
                    except Exception:
                        pass
                self.stop_event.wait(0.2)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=16)


def collect(port: int, output: Path) -> dict:
    workflows = api_request(port, "/workflows?page=1&page_size=100")
    tasks = api_request(port, "/workflow-tasks?page=1&page_size=100")
    evidence = {"workflows": workflows, "tasks": tasks, "runs": {}}
    for task in tasks["items"]:
        task_uuid = task["uuid"]
        evidence["runs"][task_uuid] = {
            "task": api_request(port, f"/workflow-tasks/{task_uuid}"),
            "node_runs": api_request(port, f"/workflow-tasks/{task_uuid}/node-runs"),
            "jobs": api_request(port, f"/workflow-tasks/{task_uuid}/jobs"),
            "graph": api_request(port, f"/workflows/{task['workflow_uuid']}/graph"),
        }
    save(output / "evidence.json", evidence)
    return evidence


def validate(spec, evidence: dict) -> list[dict]:
    checks = []
    for expectation in spec.workflows:
        try:
            workflows = [w for w in evidence["workflows"]["items"] if w["name"] == expectation.name]
            assert len(workflows) == 1, f"期望一个实例化定义，实际 {len(workflows)}"
            matches = [value for value in evidence["runs"].values() if value["task"]["workflow_uuid"] == workflows[0]["uuid"]]
            assert len(matches) == 1, f"期望完整运行一次，实际 {len(matches)}"
            proof = matches[0]
            assert proof["task"]["status"] == expectation.task_status, proof["task"]["status"]
            runs = proof["node_runs"]
            assert [r["status"] for r in runs] == list(expectation.expected_node_statuses())
            assert [r["attempt_count"] for r in runs] == list(expectation.expected_attempt_counts())
            for run in runs:
                _assert_attempt_history(expectation, run)
            if expectation.task_error_code:
                error = proof["task"]["error_info"][0]
                assert error["code"] == expectation.task_error_code
                assert expectation.task_error_contains in error["message"]
            checks.append({"workflow": expectation.name, "passed": True, "task_uuid": proof["task"]["uuid"]})
        except Exception as exc:
            checks.append({"workflow": expectation.name, "passed": False, "error": str(exc) or repr(exc)})
    return checks


def run_demo(spec, output: Path, timeout: float) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / "runtime"
    runtime.mkdir()
    codex_dir = output / "codex"
    codex_dir.mkdir()
    source = source_for(spec)
    metadata = {"model": MODEL, "reasoning_effort": EFFORT, "topology": "split", "backend": "hostlink",
                "demo": spec.repo, "source": source_evidence(source), "started_at": time.time()}
    save(output / "metadata.json", metadata)
    processes = DemoProcesses(spec, source, runtime)
    observer = Observer(processes.management_port, output)
    process = None
    try:
        print(f"START {spec.repo} port={processes.management_port}", flush=True)
        processes.start()
        if spec.proof_env:
            # LAN 的后台订阅证明是阶段一；工作流的 start 会再开轮次并覆盖 proof 文件。
            # 先冻结阶段一证据，不能在整个自然语言回合结束后再要求计数仍然等于 1。
            save(output / "initial-device-proofs.json", processes.wait_proofs())
        wait_until(lambda: api_request(processes.management_port, "/registry/workflow-templates").get("templates"),
                   timeout=60, abort=processes.any_exited, description="模板上报")
        prompt = prompt_for(spec, source)
        (codex_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        command = codex_command(processes.management_port, codex_dir)
        save(codex_dir / "command.json", command)
        observer.thread.start()
        with (codex_dir / "events.jsonl").open("w", encoding="utf-8") as stdout, (codex_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       text=True, encoding="utf-8", env=processes.env,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            save(codex_dir / "process.json", {"pid": process.pid})
            process.communicate(prompt, timeout=timeout)
        metadata["codex_exit_code"] = process.returncode
        evidence = collect(processes.management_port, output)
        metadata["checks"] = validate(spec, evidence)
        metadata["closed_loop_checks"] = assert_closed_loops(spec, source, evidence, output, processes.management_port)
        events = [json.loads(line) for line in (codex_dir / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.startswith("{")]
        calls = [event["item"] for event in events if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "mcp_tool_call"]
        metadata["mcp_calls"] = len(calls)
        metadata["tool_names"] = sorted({call.get("tool", "") for call in calls})
        metadata["passed"] = (bool(calls) and process.returncode == 0
                              and all(check["passed"] for check in metadata["checks"])
                              and all(check["passed"] for check in metadata["closed_loop_checks"]))
        if spec.proof_env:
            metadata["device_proofs"] = processes.wait_proofs()
        print(f"DONE {spec.repo} passed={metadata['passed']} calls={len(calls)}", flush=True)
    except Exception as exc:
        metadata.update(passed=False, error=str(exc), traceback=traceback.format_exc())
        print(f"FAILED {spec.repo}: {exc}", flush=True)
        try:
            collect(processes.management_port, output)
        except Exception:
            pass
    finally:
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
            else:
                process.terminate()
        if observer.thread.is_alive():
            observer.stop()
        processes.stop()
        metadata["finished_at"] = time.time()
        save(output / "metadata.json", metadata)
    return metadata


def recheck_demo(output: Path) -> dict:
    """只读复核留档，不启动设备或 Codex，也不重写原始 metadata/summary。"""
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    spec = next(spec for spec in DEMOS if spec.repo == metadata["demo"])
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    checks = validate(spec, evidence)
    closed = assert_closed_loops(spec, Path(metadata["source"]["root"]), evidence, output)
    passed = (metadata.get("codex_exit_code") == 0 and all(check["passed"] for check in checks)
              and all(check["passed"] for check in closed))
    return {"demo": metadata["demo"], "artifact_directory": str(output), "model": metadata["model"],
            "reasoning_effort": metadata["reasoning_effort"], "mcp_calls": metadata["mcp_calls"],
            "source": metadata["source"], "original_passed": metadata.get("passed"),
            "rechecked_at": time.time(), "checks": checks, "closed_loop_checks": closed, "passed": passed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", choices=[s.package for s in DEMOS], action="append")
    parser.add_argument("--recheck", type=Path, action="append", help="离线复核已有 demo 留档，可重复；不调用模型")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.recheck:
        if args.demo:
            parser.error("--recheck 不与 --demo 混用")
        results = [recheck_demo(path.resolve()) for path in args.recheck]
        save(root / "validated-summary.json", results)
        for result in results:
            print(f"RECHECK {result['demo']} passed={result['passed']}", flush=True)
        return 0 if all(result["passed"] for result in results) else 1
    specs = [spec for spec in DEMOS if not args.demo or spec.package in args.demo]
    results = [run_demo(spec, root / spec.package, args.timeout) for spec in specs]
    save(root / "summary.json", results)
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
