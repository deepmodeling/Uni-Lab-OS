"""构建链只采用实际构建的源码凭证，不把等待 job 的绿色状态当作产物。"""

import json
import os
from pathlib import Path
import subprocess
import unittest

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"
SHA = "1" * 40
OTHER = "2" * 40


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def script_result(filename, job, *, event="workflow_run", conclusion="success",
                  upstream_event="push", artifacts=(), requested_ref="dev"):
    script = workflow(filename)["jobs"][job]["steps"][0]["with"]["script"]
    harness = """
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(0, "utf8"));
const result = {outputs: {}, errors: [], notices: []};
const core = {
  setOutput: (key, value) => result.outputs[key] = value,
  setFailed: message => result.errors.push(message),
  notice: message => result.notices.push(message)
};
const context = {
  eventName: data.event, repo: {owner: "deepmodeling", repo: "Uni-Lab-OS"},
  payload: {workflow_run: {id: 123, conclusion: data.conclusion, event: data.upstream_event,
                          head_branch: "dev", head_sha: data.sha}}
};
const github = {
  rest: {actions: {listWorkflowRunArtifacts: "list"}, repos: {
    getCommit: async args => {
      if (args.ref !== "dev") throw new Error("manual source ref lost");
      return {data: {sha: data.sha}};
    }
  }},
  paginate: async (_method, args) => {
    if (args.run_id !== 123) throw new Error("wrong upstream run");
    return data.artifacts;
  }
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
new AsyncFunction("core", "context", "github", data.script)(core, context, github)
  .then(() => process.stdout.write(JSON.stringify(result)))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
    process = subprocess.run(
        ["node", "-e", harness], input=json.dumps({
            "script": script, "event": event, "conclusion": conclusion,
            "upstream_event": upstream_event, "artifacts": list(artifacts), "sha": SHA,
        }), text=True, capture_output=True, check=True,
        env={**os.environ, "REQUESTED_REF": requested_ref},
    )
    return json.loads(process.stdout)


def artifact(prefix, sha=SHA, state="published", expired=False):
    return {"name": f"{prefix}-source-{state}-{sha}-hostlink-linux-64", "expired": expired}


class CondaWorkflowContractTests(unittest.TestCase):
    def test_companion_wheels_are_release_assets_not_pypi_uploads(self):
        definition = workflow("wheel-release.yml")
        entries = definition["jobs"]["build"]["strategy"]["matrix"]["include"]
        self.assertEqual({x["platform"] for x in entries}, {"linux-64", "osx-64", "osx-arm64", "win-64"})
        upload = definition["jobs"]["attach-release"]
        self.assertEqual(upload["needs"], "build")
        self.assertIn("github.event_name == 'release'", upload["if"])
        self.assertIn("gh release upload", upload["steps"][-1]["run"])
        source = (WORKFLOWS / "wheel-release.yml").read_text(encoding="utf-8")
        self.assertNotIn("pypa/gh-action-pypi-publish", source)
        self.assertNotIn("twine upload", source)
        self.assertNotIn("--clobber", source)

    def test_default_build_is_independent_of_ros_messages(self):
        # BaseLoader 避免 YAML 1.1 把 GitHub 的 on 键当成布尔值。
        definition = yaml.load(
            (WORKFLOWS / "unilabos-conda-build.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(definition["on"]["workflow_run"]["branches"], ["dev"])
        self.assertEqual(definition["on"]["workflow_run"]["workflows"], ["CI Check"])
        self.assertEqual(definition["on"]["workflow_dispatch"]["inputs"]["ros_distros"]["default"], "")
        ros = yaml.load((WORKFLOWS / "multi-platform-build.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertNotIn("workflow_run", ros["on"])
        self.assertNotIn("release", ros["on"])
        self.assertEqual(ros["on"]["push"]["tags"], ["msgs-v*"])

    def test_ros_matrix_channels_and_recipes(self):
        for name in ("multi-platform-build.yml",):
            definition = workflow(name)
            entries = definition["jobs"]["build"]["strategy"]["matrix"]["include"]
            self.assertEqual(len(entries), 8)
            self.assertEqual(
                {(x["platform"], x["ros_distro"]) for x in entries},
                {(p, r) for p in ("linux-64", "osx-64", "osx-arm64", "win-64")
                 for r in ("jazzy", "humble")},
            )
            for entry in entries:
                self.assertEqual(entry["ros_channel"], f"robostack-{entry['ros_distro']}")
        core = workflow("unilabos-conda-build.yml")["jobs"]["build"]["strategy"]["matrix"]["include"]
        self.assertEqual(len(core), 4)
        self.assertTrue(all("ros_distro" not in item for item in core))

    def test_provenance_is_after_builds_and_uploads(self):
        for name in ("multi-platform-build.yml", "unilabos-conda-build.yml"):
            steps = workflow(name)["jobs"]["build"]["steps"]
            record = next(i for i, step in enumerate(steps) if step.get("id") == "source")
            self.assertIn("git rev-parse HEAD", steps[record]["run"])
            self.assertEqual(steps[-1]["name"], "Upload successful source provenance")
            self.assertIn("should_build", steps[-1]["if"])
            for i, step in enumerate(steps):
                if "anaconda -t" in step.get("run", "") or "rattler-build build" in step.get("run", ""):
                    self.assertLess(i, record)
                if step.get("uses", "").startswith("actions/upload-artifact"):
                    self.assertEqual(step["with"]["if-no-files-found"], "error")

    def test_upstream_ci_builds_exact_source_without_publishing(self):
        result = script_result("unilabos-conda-build.yml", "resolve-source")
        self.assertEqual(result["outputs"], {"should_continue": "true", "source_sha": SHA})
        steps = workflow("unilabos-conda-build.yml")["jobs"]["build"]["steps"]
        upload = next(x for x in steps if x.get("name") == "Upload packages after successful tests")
        self.assertNotIn("workflow_run", upload["if"])

    def test_release_resolves_requested_source(self):
        result = script_result("unilabos-conda-build.yml", "resolve-source", event="release")
        self.assertEqual(result["outputs"], {"should_continue": "true", "source_sha": SHA})

    def test_message_release_does_not_republish_core(self):
        result = script_result("unilabos-conda-build.yml", "resolve-source", event="release",
                               requested_ref="msgs-v0.12.1")
        self.assertEqual(result["outputs"], {"should_continue": "false"})

    def test_conda_pack_skips_wait_only_or_unpublished_build(self):
        for items in ([], [artifact("unilabos", state="tested")],
                      [artifact("unilabos", expired=True)]):
            with self.subTest(items=items):
                result = script_result("conda-pack-build.yml", "resolve-source", artifacts=items)
                self.assertEqual(result["outputs"]["should_continue"], "false")
                self.assertFalse(result["errors"])

    def test_conda_pack_rejects_conflicting_sources(self):
        result = script_result("conda-pack-build.yml", "resolve-source",
                               artifacts=[artifact("unilabos"), artifact("unilabos", OTHER)])
        self.assertTrue(result["errors"])
        self.assertEqual(result["outputs"]["should_continue"], "false")

    def test_conda_pack_uses_built_source(self):
        result = script_result("conda-pack-build.yml", "resolve-source",
                               artifacts=[artifact("unilabos")])
        self.assertEqual(result["outputs"], {"should_continue": "true", "source_sha": SHA, "platforms": "linux-64"})
        steps = workflow("conda-pack-build.yml")["jobs"]["build-conda-pack"]["steps"]
        download = next(x for x in steps if x.get("uses", "").startswith("actions/download-artifact"))
        self.assertIn("github.event.workflow_run.id", download["with"]["run-id"])

    def test_conda_pack_manual_branch_is_resolved_once(self):
        result = script_result("conda-pack-build.yml", "resolve-source", event="workflow_dispatch")
        self.assertEqual(result["outputs"]["source_sha"], SHA)
        self.assertEqual(result["outputs"]["should_continue"], "true")
        build = workflow("conda-pack-build.yml")["jobs"]["build-conda-pack"]
        self.assertIn("resolve-source.outputs.source_sha", build["env"]["PACKAGE_REF"])
        checkout = next(step for step in build["steps"]
                        if step.get("uses", "").startswith("actions/checkout"))
        self.assertIn("resolve-source.outputs.source_sha", checkout["with"]["ref"])

    def test_failed_upstream_never_proceeds(self):
        for filename, job, prefix in (
            ("unilabos-conda-build.yml", "resolve-source", "msgs"),
            ("conda-pack-build.yml", "resolve-source", "unilabos"),
        ):
            result = script_result(filename, job, conclusion="failure",
                                   upstream_event="release", artifacts=[artifact(prefix)])
            self.assertEqual(result["outputs"]["should_continue"], "false")


if __name__ == "__main__":
    unittest.main()
