"""夹具自身的快速测试，不启动 Codex、不消耗账号额度。"""

from pathlib import Path

import pytest

from tests.e2e.readme_demos import DEMOS
from tests.e2e.run_mcp_demos import codex_command, prompt_for


@pytest.mark.parametrize("spec", DEMOS, ids=lambda spec: spec.package)
def test_prompt_limits_mutations_to_current_demo(tmp_path, spec):
    (tmp_path / "README.md").write_text("当前 demo 的 README", encoding="utf-8")
    prompt = prompt_for(spec, tmp_path)
    assert "不要扩展到其他 demo" in prompt
    assert "不要运行 shell/Python/本地 smoke" in prompt
    assert ("出库装板并加液" in prompt) == (spec.package == "materials_demo")
    for workflow in spec.workflows:
        assert workflow.name in prompt
        if workflow.group:
            assert "protocol_batch 同时提交" in prompt


def test_codex_uses_exact_model_and_existing_login(monkeypatch, tmp_path):
    monkeypatch.setattr("tests.e2e.run_mcp_demos.shutil.which", lambda name: str(tmp_path / name))
    command = codex_command(12345, Path(tmp_path))
    assert command[command.index("-m") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="max"' in command
    assert 'mcp_servers.unilab.url="http://127.0.0.1:12345/mcp"' in command
    assert "features.shell_tool=false" in command
    assert 'web_search="disabled"' in command
    assert "--ignore-user-config" in command
    assert not any("api_key" in part or "CODEX_HOME" in part for part in command)
