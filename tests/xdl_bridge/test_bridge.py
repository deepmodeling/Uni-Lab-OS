from pathlib import Path

from unilabos.xdl_bridge import build_xdl_workflow, load_station_profile


ROOT = Path(__file__).parents[2]
COMPREHENSIVE_PROFILE = (
    ROOT
    / "unilabos"
    / "test"
    / "experiments"
    / "comprehensive_protocol"
    / "xdl_bridge.yaml"
)


def test_comprehensive_profile_uses_shared_protocol_contract():
    profile = load_station_profile(COMPREHENSIVE_PROFILE)

    assert profile.workstation_id == "OrganicSynthesisStation"
    assert profile.operation("Transfer")["template"] == "PumpTransferProtocol"
    assert profile.bind_component("reactor", "reactor") == "main_reactor"
    assert profile.operation("FilterThrough")["overrides"]["filter_through"] == "filter_1"
    assert profile.operation("RunColumn")["overrides"]["column"] == "column_1"


def test_xdl_builds_standard_unilab_workflow_for_selected_station(tmp_path):
    xdl = tmp_path / "transfer.xdl"
    xdl.write_text(
        """<?xdl version="2.0.0" ?>
<XDL><Synthesis>
  <Hardware>
    <Component id="reactor" type="reactor"/>
    <Component id="separator" type="separator"/>
  </Hardware>
  <Reagents />
  <Procedure>
    <Transfer from_vessel="reactor" to_vessel="separator" volume="5 mL"/>
  </Procedure>
</Synthesis></XDL>
""",
        encoding="utf-8",
    )

    workflow = build_xdl_workflow(xdl, COMPREHENSIVE_PROFILE, name="transfer")

    node = workflow["nodes"][0]
    assert node["resource_name"] == "workstation"
    assert node["device_name"] == "OrganicSynthesisStation"
    assert node["template_name"] == "PumpTransferProtocol"
    assert node["param"]["from_vessel"] == "main_reactor"
    assert node["param"]["to_vessel"] == "separator_1"
