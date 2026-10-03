import pytest

from proteinrsi.contracts import Workflow
from proteinrsi.integrations.gepa import decode_workflow, encode_workflow
from proteinrsi.integrations.mcp import MCPExecutor


def test_gepa_codec_works_without_installing_gepa():
    assert decode_workflow(encode_workflow(Workflow())) == Workflow()


def test_mcp_endpoint_constraints():
    with pytest.raises(ValueError):
        MCPExecutor("http://untrusted.example/mcp", "run", "hash")
    with pytest.raises(ValueError):
        MCPExecutor("https://secret@server.example/mcp", "run", "hash")


def test_gepa_real_adapter_when_installed():
    pytest.importorskip("gepa")
    from proteinrsi.integrations.gepa import ProteinWorkflowAdapter
    adapter = ProteinWorkflowAdapter(lambda case, workflow: (1.0, {"visible": case["case_id"]}))
    result = adapter.evaluate([{"case_id": "test"}], encode_workflow(Workflow()), capture_traces=True)
    assert result.scores == [1.0]
    feedback = adapter.make_reflective_dataset({}, result, ["designer_prompt"])
    assert feedback["designer_prompt"][0]["Feedback"] == {"visible": "test"}


def test_langgraph_real_interrupt_and_resume_when_installed(campaign, oracle):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from proteinrsi.integrations.langgraph import invoke_persistent
    output = invoke_persistent(campaign)
    assert output["__interrupt__"][0].value["type"] == "approval"
    batch = campaign.prepare()
    output = invoke_persistent(campaign, {"approved": True, "operator": "pytest"})
    assert output["__interrupt__"][0].value["type"] == "measurements"
    output = invoke_persistent(campaign, {"observations": [o.model_dump(mode="json") for o in oracle.measure(batch)]})
    assert campaign.state["round_index"] == 1
    assert output["__interrupt__"][0].value["type"] == "approval"


def test_mcp_v1_import_contract_when_installed():
    pytest.importorskip("mcp")
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    assert ClientSession and callable(streamablehttp_client)
