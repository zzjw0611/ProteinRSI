"""No-data checks for operator-approved, assistant-authored initial guidance."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from proteinrsi.contracts import Workflow

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/experiments/planner_delegation_w0.json"
BASE_PRINCIPAL = (
    "Pursue the task objective within budget: generate proposals for design tasks "
    "and assess supplied sequences for ranking tasks."
)


@pytest.mark.core
def test_planner_delegation_config_identity_and_only_principal_delta():
    raw = CONFIG.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "9855c00c37e64f247102bd2e4c1d0a4882c20b2c45dff2f47e1a148ee9a0fcd8"
    )
    revised = json.loads(raw)
    # Reconstruct the original frozen W from this single-field change. Its
    # content-derived identity pins every remaining original field as well.
    original = {**revised, "principal_prompt": BASE_PRINCIPAL}
    assert Workflow.model_validate(original).version == "w-cfe9141564fc1ec9"
    assert Workflow.model_validate(revised).version == "w-6ccf8d89b4d80098"
    assert [k for k in original if original[k] != revised[k]] == ["principal_prompt"]
    assert revised["designer_prompt"] == original["designer_prompt"]
    assert "delegate exact candidate choice to Designer" in revised["principal_prompt"]


@pytest.mark.core
def test_planner_delegation_prompt_reaches_a_plan(tmp_path):
    from proteinrsi.research.contracts import ResearchConfig, default_plan
    from proteinrsi.research.runner import ResearchRunner
    from proteinrsi.storage import Store

    workflow = Workflow.model_validate_json(CONFIG.read_text())
    seen = []

    class FakeLLM:
        def complete(self, role, instructions, context, schema):
            seen.append((role, instructions))
            return default_plan().model_dump(mode="json")

    team = SimpleNamespace(store=Store(tmp_path / "capture"), llm=FakeLLM())
    runner = ResearchRunner(team, ResearchConfig())
    runner._request_plan(
        SimpleNamespace(workflow=workflow, model_dump=lambda **kwargs: {}), {}
    )
    assert seen[0][0] == "A-plan"
    assert workflow.principal_prompt in seen[0][1]
