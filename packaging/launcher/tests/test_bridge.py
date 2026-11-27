"""桌面必须使用主线工作区路径及冻结的包内图副本。"""
import json
from unilabos.package_manager.workspace_runtime import prepare_workspace_registry_runtime
from unilabos.workspace_host.discovery import ensure_local_token
from unilabos.workspace_host.launch import resolve_backend_launch
from unilabos.workspace_host.model import WorkspacePaths


def test_mainline_plan_keeps_graph_and_state_inside_workspace(tmp_path):
    package = tmp_path / 'example'
    (package / 'example').mkdir(parents=True)
    (package / 'example/__init__.py').write_text('')
    (package / 'deployment/graphs').mkdir(parents=True)
    (package / 'deployment/local_config.py').write_text('')
    (package / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0"\n')
    (package / 'package.yaml').write_text('package:\n  name: example\nworkflows: []\n')
    graph = package/'deployment/graphs/devices.json'
    graph.write_text(json.dumps({'nodes':[], 'links':[]}))
    paths = WorkspacePaths.resolve(package)
    ensure_local_token(paths)
    plan = resolve_backend_launch(paths, graph_path=str(graph), backend_port=18191)
    copy = plan.metadata['validatedGraphPath']
    assert paths.runtime.is_relative_to(package)
    assert prepare_workspace_registry_runtime({'workspace':str(package),'graph':copy}).graph_path.is_relative_to(package)
    assert plan.command[plan.command.index('--process_role')+1] == 'workspace_backend'
