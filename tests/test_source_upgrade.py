"""Synthetic operator-transition tests, NEVER evidence of real campaign migration.

Only fixture construction substitutes an old packaged-source snapshot and a
synthetic bwrap identity/provider. Production hashes and source reads remain live.
Fault-injection patches below deliberately test refusals and atomic rollback.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from proteinrsi.agents import MetaAgent, Team
from proteinrsi.contracts import Candidate, GatePolicy, MetaPolicy, Patch, TaskSpec
from proteinrsi.improvement import apply_patch
from proteinrsi.lab import CSVOracle
from proteinrsi.llm import JSONLLM
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.synthetic import make_fixture

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upgrade_final_selection_source.py"
spec = importlib.util.spec_from_file_location("source_upgrade", SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def old_source_bundle(new=None):
    """Portable authentic 25312 bundle; no immutable checkout or campaign read."""
    old = deepcopy(new if new is not None else upgrade._runtime_source()[0])
    name = "dataflow/design.py"
    before = '''candidate_refs=[its resource_id] and tool_calls=[]. In typed agent:propose, only
candidates selected in that final no-tool reply are published. Earlier tool
panels and provisional replies are not automatically adopted: explicitly name
all desired candidate_refs or return the complete desired final candidates.
New substitutions must use a list'''
    after = 'candidate_refs=[its resource_id] and tool_calls=[]. New substitutions must use a list'
    assert old["files"][name].count(before) == 1
    old["files"][name] = old["files"][name].replace(before, after)
    name = "dataflow/integration.py"
    pairs = [
        ('        # Explicit tool requests are executed exactly as in the existing design loop.\n',
         '        # Explicit tool requests are executed exactly as in the existing design loop.\n'
         '        candidates = list(response.candidates)\n'),
        ('                tool_results.append(result)\n',
         '                tool_results.append(result)\n'
         '                for raw in _candidate_payload(result) or []:\n'
         '                    candidates.append(Candidate.model_validate(raw))\n'),
        ('''        # Intermediate tool outputs/proposals are evidence, not adoption. The
        # final no-tool response has already resolved explicit candidate_refs,
        # edits, and any within-request format repairs through request_design.
        # In particular, a rejected valid earlier panel must not precede the
        # adopted panel when the outer full-plate adapter takes its first slots.
        if not response.candidates:
            raise ContractError("Final designer decision selected no candidates")
        descriptor = resources.sequences(response.candidates, producer="agent:B")''',
         '''            candidates.extend(response.candidates)
        descriptor = resources.sequences(candidates, producer="agent:B")'''),
        ('"typed-designer-v3-final-selection"', '"typed-designer-v2"'),
    ]
    for before, after in pairs:
        assert old["files"][name].count(before) == 1
        old["files"][name] = old["files"][name].replace(before, after)
    assert upgrade.digest(old["files"]) == upgrade.OLD_FILES
    return old


def _make_legacy(tmp_path, monkeypatch, *, typed=False, with_meta=False, bridge=False,
                 pause_current=False, workflow=None):
    # Fixture-only backend identity. No sandbox process or real model is run.
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap")
    monkeypatch.setattr("proteinrsi.replay.sandbox.backend_identity", lambda: {
        "sandbox_backend": "bwrap", "bwrap_path": "/synthetic/bwrap",
        "bwrap_sha256": "1" * 64, "bwrap_version": "synthetic fixture",
        "bwrap_policy_sha256": "2" * 64})
    task_path, labels = make_fixture(tmp_path / "fixture", rounds=20)
    task = json.loads(task_path.read_text())
    task["budget"] = deepcopy(upgrade.LIMITS)
    task = TaskSpec.model_validate(task)
    gate = GatePolicy(criterion="observed_pareto_v1", min_per_arm=2, top_ns=[2])
    old = old_source_bundle()
    from proteinrsi.research.contracts import ResearchConfig
    config = ResearchConfig(protocol_mode="typed", context_policy="evidence-v1", review_after_step=False) if typed else None
    with monkeypatch.context() as build:
        build.setattr("proteinrsi.governance._packaged_source", lambda: deepcopy(old))
        campaign = Campaign.initialize(tmp_path / "campaign", task,
            gate=gate, workflow=workflow, meta=MetaPolicy(enabled=with_meta), research_config=config)
        campaign.store.put("configuration", "replay_security", upgrade._backend(), immutable=True)
        attach_synthetic_provider(campaign, typed=typed, bridge=bridge, pause_current=pause_current)
        if typed:
            from proteinrsi.dataflow.integration import run_campaign_protocol
            def run_bound_protocol(view):
                campaign.team.bind_tools(view)
                return run_campaign_protocol(campaign.team, view, config)
            campaign.team.run = run_bound_protocol
        batch = campaign.prepare()
        campaign.approve(batch.batch_id, operator="synthetic-test")
        campaign.ingest(CSVOracle(labels, task).measure(batch))
        if with_meta:
            state = campaign.state
            state["method_governance"].update(paused=True, pause_reason="Synthetic stop after current C")
            campaign.store.put("campaign", "state", state)
            batch = campaign.prepare()
            campaign.approve(batch.batch_id, operator="synthetic-test")
            if pause_current:
                from proteinrsi.llm import ProviderPaused
                with pytest.raises(ProviderPaused):
                    campaign.ingest(CSVOracle(labels, task).measure(batch))
            else:
                campaign.ingest(CSVOracle(labels, task).measure(batch))
    return campaign


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    return _make_legacy(tmp_path, monkeypatch)


@pytest.fixture
def typed_legacy(tmp_path, monkeypatch):
    return _make_legacy(tmp_path, monkeypatch, typed=True)


def attach_synthetic_provider(campaign, *, typed=False, bridge=False, pause_current=False):
    feedback_calls = {}
    def reply(instructions, context):
        is_feedback = "# C — experimental feedback" in instructions
        if not is_feedback:
            assert typed or "workflow_schema" in context
            if "workflow_schema" in context:
                result = {"reason": "Synthetic M retained the baseline; no patch"}
            elif "task_contract" in context:
                result = {"hypothesis": "Synthetic authentic typed journal",
                    "steps": [{"step_id": "design", "operation": "agent:propose", "question": "Fixture panel",
                               "arguments": {"question": "Select synthetic candidates"}}],
                    "final_outputs": {"candidates": {"source": "step:design.result",
                                                      "schema_ref": "protein.sequence_set/v1"}}}
            else:
                excluded = {o["sequence"] for o in campaign.state["observations"]}
                result = {"candidates": [{"sequence": s} for s in campaign.state["task"]["candidates"]
                                          if s != campaign.state["task"]["reference_sequence"] and s not in excluded]}
        else:
            round_index = context["view"]["round_index"]
            feedback_calls[round_index] = feedback_calls.get(round_index, 0) + 1
            count = feedback_calls[round_index]
            result = {"summary": "Synthetic completed C feedback; no efficacy claim."}
            if typed and count <= 3:
                ref = context["_context"]["source"]["evidence_ref"]
                if count in {1, 3}:
                    result = {"_context_action": {"kind": "read", "ref": ref, "offset": 0, "limit": 1}}
                else:
                    result = {"_context_action": {"kind": "note", "hypothesis": "Synthetic note",
                        "evidence_refs": [ref], "counterevidence": "unknown", "failures": "none",
                        "open_questions": "fixture only", "next_step": "finish C"}}
        return result
    if bridge:
        from proteinrsi.assistant_bridge import AssistantBridgeLLM
        class FixtureBridge(AssistantBridgeLLM):
            """Synthetic reply transport using the real request/accept receipt code."""
            pause_round = 2 if pause_current else None
            def _complete(self, role, instructions, context, schema):
                request = self._request(role, instructions, context, schema)
                key, record = self._pending(request)
                if record["state"] == "done":
                    return self._cached(key, record, schema)
                if role == "C-feedback" and context["view"]["round_index"] == self.pause_round:
                    from proteinrsi.llm import ProviderPaused
                    raise ProviderPaused("Synthetic current C pause")
                response = {k: request[k] for k in (
                    "protocol_version", "transport", "request_id", "request_hash", "model")}
                response["result"] = reply(instructions, context)
                return self._accept(key, record, request, upgrade.canonical(response).encode())
        llm = FixtureBridge(campaign.store, model="synthetic-bridge-C",
                            mailbox=campaign.store.root.parent / "mailbox")
    else:
        def respond(request):
            body = json.loads(request.content)
            result = reply(body["messages"][0]["content"], json.loads(body["messages"][1]["content"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})
        llm = JSONLLM(campaign.store, model="synthetic-C", base_url="https://example.invalid",
                      api_key="fake", max_attempts=1, transport=httpx.MockTransport(respond))
    campaign.team = Team(campaign.store, llm)
    campaign.meta_agent = MetaAgent(llm, campaign.store)
    campaign.team.run = lambda view: [Candidate(sequence=s) for s in view.task.candidates
        if s not in {o.sequence for o in view.observations}]


def apply_plan(campaign, plan=None, **kwargs):
    plan = plan or upgrade.dry_run(campaign.store.root, **kwargs)
    return upgrade.apply(campaign.store.root, expected_digest=plan["dry_run_digest"],
        operator="synthetic-operator", reason="Test the exact implementation repair",
        authorization_ref="synthetic-approval/123", **kwargs)


def database_image(campaign):
    with campaign.store.connect() as con:
        return {name: [tuple(r) for r in con.execute('SELECT * FROM "' + name + '" ORDER BY 1')]
                for name in upgrade.TABLES}


def test_authentic_old_and_new_source_allowlist():
    new, _ = upgrade._runtime_source()
    upgrade._assets(old_source_bundle(new), new)
    assert upgrade.digest(new["files"]) == upgrade.NEW_FILES


def test_default_dry_run_is_read_only_without_store_constructor(legacy, monkeypatch, capsys):
    before = database_image(legacy)
    def forbidden(*args, **kwargs):
        raise AssertionError("Store constructor must not run")
    monkeypatch.setattr(Store, "__init__", forbidden)
    lock = legacy.store.root / ".campaign.lock"
    lock.unlink()
    assert upgrade.main([str(legacy.store.root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["dry_run_digest"]) == 64
    assert result["scientific_improvement_claimed"] is False
    assert not lock.exists()
    assert database_image(legacy) == before


def test_upgrade_only_changes_exact_pointers_and_appends_records(legacy):
    before, old_state = database_image(legacy), legacy.state
    old_records = {ns: legacy.store.all(ns) for ns in (
        "method_assets", "method_snapshots", "method_activations", "feedback_inputs", "feedback_results")}
    plan = upgrade.dry_run(legacy.store.root)
    result = apply_plan(legacy, plan)
    assert result["status"] == "applied"
    after = database_image(legacy)
    for table in ("limits", "charges"):
        assert after[table] == before[table]
    assert after["events"][:-1] == before["events"]
    assert after["events"][-1][2] == "source_upgrade_applied"
    new_state = legacy.state
    expected = deepcopy(old_state)
    expected["method_governance"]["active"] = plan["new_snapshots"]
    expected["method_governance"]["source_transition_ref"] = result["receipt_ref"]
    assert new_state == expected
    for namespace, records in old_records.items():
        assert all(legacy.store.get(namespace, k) == v for k, v in records.items())
    old_kv = {(ns, k): v for ns, k, v in before["kv"]}
    new_kv = {(ns, k): v for ns, k, v in after["kv"]}
    changed = {key for key in old_kv if old_kv[key] != new_kv.get(key)}
    assert changed == {("campaign", "state")}
    assert {ns for ns, _ in new_kv.keys() - old_kv.keys()} == {
        "method_assets", "method_snapshots", "source_qualified_activations", "source_upgrades"}
    legacy.methods.assert_plannable(new_state)
    receipt = legacy.store.get("source_upgrades", result["receipt_ref"])
    assert receipt["scientific_improvement_claimed"] is False
    assert not legacy.store.all("method_switches")
    assert not legacy.store.all("evaluation_verdicts")
    assert apply_plan(legacy, plan) == {"status": "already_applied", "receipt_ref": result["receipt_ref"]}
    assert database_image(legacy) == after


@pytest.mark.parametrize("mutation", ["event", "configuration", "ledger", "state"])
def test_stale_fingerprint_refuses_apply(legacy, mutation):
    plan = upgrade.dry_run(legacy.store.root)
    if mutation == "event":
        legacy.store.event("synthetic-extra-event", {})
    elif mutation == "configuration":
        legacy.store.put("configuration", "other", {"changed": True})
    elif mutation == "ledger":
        legacy.store.reserve("synthetic-charge", "llm_calls", 1, {})
        legacy.store.settle("synthetic-charge")
    else:
        state = legacy.state
        state["planning_attempt"] += 1
        legacy.store.put("campaign", "state", state)
    before = database_image(legacy)
    with pytest.raises(upgrade.UpgradeRefused, match="Stale"):
        apply_plan(legacy, plan)
    assert database_image(legacy) == before


@pytest.mark.parametrize("part", ["python", "dependencies", "unapproved-file", "allowed-file", "source-ref", "contracts", "definition"])
def test_modified_old_pin_refused(legacy, part):
    state = legacy.state
    ref = state["method_governance"]["active"]["workflow"]
    snapshot = legacy.store.get("method_snapshots", ref)
    old = legacy.store.get("method_assets", snapshot["source_ref"])
    if part == "python":
        old["python"] = "0.0.0"
    elif part == "dependencies":
        old["dependencies"]["numpy"] = "0.0.0"
    elif part == "unapproved-file":
        old["files"]["contracts.py"] += "\n# unauthorized\n"
    elif part == "allowed-file":
        old["files"]["dataflow/design.py"] += "\n# unauthorized\n"
    elif part == "contracts":
        snapshot["contracts"]["method"] = {}
    elif part == "definition":
        snapshot["definition"]["exploration"] = 0.99
    if part == "source-ref":
        legacy.store.put("method_assets", snapshot["source_ref"], {"bad": True})
    else:
        old_ref = upgrade.digest(old)
        legacy.store.put("method_assets", old_ref, old)
        snapshot["source_ref"] = old_ref
        new_ref = "method-" + upgrade.digest(snapshot)
        legacy.store.put("method_snapshots", new_ref, snapshot)
        state["method_governance"]["active"]["workflow"] = new_ref
        legacy.store.put("method_activations", "workflow/" + snapshot["version"],
                         {"snapshot_ref": new_ref, "origin": "initial"})
        legacy.store.put("campaign", "state", state)
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.dry_run(legacy.store.root)


@pytest.mark.parametrize("key", ["prompt_bundle", "research", "acceptance_policy", "replay_security"])
def test_changed_configuration_and_backend_refused(legacy, key):
    legacy.store.put("configuration", key, {"unauthorized": True})
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.dry_run(legacy.store.root)


def test_actual_source_tree_not_supplied_manifest_is_checked(legacy, monkeypatch, tmp_path):
    import proteinrsi
    new, _ = upgrade._runtime_source()
    root = tmp_path / "runtime" / "proteinrsi"
    root.mkdir(parents=True)
    for name, text in new["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "contracts.py").write_text("# unapproved runtime\n")
    # Deliberate negative-test fault injection, not a way to make a migration pass.
    monkeypatch.setattr(proteinrsi.__spec__, "origin", str(root / "__init__.py"))
    with pytest.raises(upgrade.UpgradeRefused, match="Actual runtime files"):
        upgrade.dry_run(legacy.store.root)


@pytest.mark.parametrize("problem", ["pending_batch", "pending_patch", "pending_meta", "not-ready",
    "no-C", "C-history", "C-input", "C-identity", "C-receipt", "reservation", "caps", "draft",
    "metric", "trial", "protocol", "plate", "LLM", "tool"])
def test_boundary_failures(legacy, problem):
    state = legacy.state
    if problem.startswith("pending_"):
        state[problem] = "pending"
    elif problem == "not-ready":
        state["status"] = "awaiting_approval"
    elif problem == "C-history":
        state["history"][-1]["analyst_feedback"]["summary"] = "tampered"
    elif problem in {"no-C", "C-input", "C-identity"}:
        key, record = next(iter(legacy.store.all("feedback_results").items()))
        if problem == "no-C":
            with legacy.store.connect() as con:
                con.execute("DELETE FROM kv WHERE namespace='feedback_results'")
        else:
            if problem == "C-input":
                record["input_digest"] = "0" * 64
            else:
                record["identity"]["round"] = 999
            legacy.store.put("feedback_results", key, record)
    elif problem == "C-receipt":
        with legacy.store.connect() as con:
            con.execute("DELETE FROM kv WHERE namespace='llm'")
    elif problem == "reservation":
        legacy.store.reserve("unsettled", "tool_calls", 1, {})
    elif problem == "caps":
        with legacy.store.connect() as con:
            con.execute("UPDATE limits SET amount=3000 WHERE resource='experimental_wells'")
    else:
        namespace, key, record = {
            "draft": ("protocol_drafts", "new", {}),
            "metric": ("evaluation_metric_attempts", "new", {"state": "completed"}),
            "trial": ("trials", "new", {}),
            "protocol": ("research_step_outputs", "protocol-plan:new", {"attempts": 1}),
            "plate": ("plate_plans", "new", {"state": "planning"}),
            "LLM": ("llm", "new", {"state": "started"}),
            "tool": ("tool_jobs", "new", {"state": "started"}),
        }[problem]
        legacy.store.put(namespace, key, record)
    legacy.store.put("campaign", "state", state)
    before = database_image(legacy)
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.dry_run(legacy.store.root)
    assert database_image(legacy) == before


def test_completed_historical_journals_are_preserved(legacy):
    legacy.store.put("plate_plans", "historical", {"state": "complete", "candidates": []})
    legacy.store.put("research_runs", "past", {"status": "complete", "round": 0,
        "protocol_result": {"run_ref": "protocol-run:past"}, "completed": []})
    legacy.store.put("research_step_outputs", "protocol-run:past", {
        "status": "complete", "protocol": {"synthetic": True}})
    legacy.store.put("research_step_outputs", "protocol-plan:past", {
        "protocol": {"synthetic": True}, "attempts": 1})
    old = legacy.store.all("research_step_outputs")
    apply_plan(legacy)
    assert legacy.store.all("research_step_outputs") == old


def test_crash_rolls_back_every_append_and_pointer(legacy, monkeypatch):
    plan = upgrade.dry_run(legacy.store.root)
    before = database_image(legacy)
    original = Store.event
    def crash(self, kind, payload):
        original(self, kind, payload)
        if kind == "source_upgrade_applied":
            raise RuntimeError("synthetic crash after all mutations")
    monkeypatch.setattr(Store, "event", crash)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        apply_plan(legacy, plan)
    assert database_image(legacy) == before


def test_authorization_and_invalid_path_are_fail_closed(legacy, tmp_path):
    plan = upgrade.dry_run(legacy.store.root)
    with pytest.raises(upgrade.UpgradeRefused, match="authorization_ref"):
        upgrade.apply(legacy.store.root, expected_digest=plan["dry_run_digest"],
                      operator="x", reason="x", authorization_ref=" ")
    missing = tmp_path / "not-a-campaign"
    with pytest.raises(FileNotFoundError):
        upgrade.dry_run(missing)
    assert not missing.exists()


def adopt_later_workflow(campaign, *, target="workflow", with_metrics=False):
    """Synthetic normal governance stage + accepted measured-trial transition."""
    from proteinrsi.contracts import GateResult
    original = getattr(campaign.view(), target)
    patch = Patch(target=target, base_version=original.version,
                  changes={"strategy": "pairwise"} if target == "workflow" else {"cooldown_rounds": 2},
                  task_kind=campaign.view().task.kind,
                  hypothesis="Synthetic rollback contract test")
    candidate = apply_patch(original, patch)
    campaign.stage_patch(patch)
    state = campaign.state
    trial_key = "synthetic-completed-trial"
    # The fixture constructs terminal scientific evidence, not a real verdict.
    result = GateResult(decision="accepted", reason="Synthetic acceptance fixture",
                        n_baseline=2, n_challenger=2)
    trial = {"target": target, "patch": patch.model_dump(mode="json"),
        "challenger" if target == "workflow" else "challenger_meta": candidate.model_dump(mode="json"),
        "gate": state["gate"]}
    if target == "meta":
        trial["evaluation_id"] = "synthetic-meta-trial"
    campaign.store.put("trials", trial_key, trial)
    from proteinrsi.contracts import Batch, Observation, Sample
    task = campaign.view().task
    sequences = task.candidates[:4]
    batch = Batch(batch_id=trial_key, campaign_id=state["campaign_id"], round_index=state["round_index"],
        evidence_version=campaign.view().evidence_version, meta_version=campaign.view().meta.version,
        patch_id=patch.patch_id, samples=[Sample(sample_id=trial_key + str(i),
            arm="baseline" if i < 2 else "challenger",
            workflow_version=(original.version if i < 2 else candidate.version)
                if target == "workflow" else campaign.view().workflow.version,
            candidate=Candidate(sequence=sequence)) for i, sequence in enumerate(sequences)])
    observations = [Observation(sample_id=item.sample_id, batch_id=trial_key,
        sequence=item.candidate.sequence, value=float(i), metric=task.metric, unit=task.unit,
        source=task.feedback_source, assay_protocol=task.assay_protocol).model_dump(mode="json")
        for i, item in enumerate(batch.samples)]
    campaign.store.put("batches", trial_key, batch.model_dump(mode="json"))
    campaign.store.put("measurements", trial_key, observations)
    if with_metrics:
        result = completed_synthetic_metrics(campaign, trial_key, patch)
    campaign.store.put("trial_results", trial_key, result.model_dump(mode="json"))
    if target == "meta":
        campaign.store.put("meta_online_attempts", trial["evaluation_id"],
                           {"state": "completed", "trial": trial})
        campaign.store.put("meta_evaluations", trial["evaluation_id"],
                           {**trial, "batch_id": trial_key, "result": result.model_dump(mode="json")})
    campaign.methods.begin(state, patch)
    campaign.methods.complete(state, patch, result, candidate, evaluation_ref="trial_results/" + trial_key)
    return original, patch


def test_source_qualified_rollback_after_verified_later_adoption(legacy):
    result = apply_plan(legacy)
    activation_index = legacy.store.all("method_activations")
    original, _ = adopt_later_workflow(legacy)
    adopted_index = legacy.store.all("method_activations")
    before = database_image(legacy)
    kwargs = {"action": "rollback", "upgrade_receipt": result["receipt_ref"], "target": "workflow"}
    plan = upgrade.dry_run(legacy.store.root, **kwargs)
    returned = apply_plan(legacy, plan, **kwargs)
    assert returned["status"] == "applied"
    assert legacy.view().workflow == original
    assert legacy.store.all("method_activations") == adopted_index
    assert all(adopted_index[k] == v for k, v in activation_index.items())
    after = database_image(legacy)
    assert after["charges"] == before["charges"]
    assert after["limits"] == before["limits"]
    assert legacy.store.all("trials")
    legacy.methods.assert_plannable(legacy.state)
    assert apply_plan(legacy, plan, **kwargs)["status"] == "already_applied"
    assert database_image(legacy) == after


@pytest.mark.parametrize("forgery", ["receipt", "qualified-activation", "original-index", "snapshot",
                                     "no-adoption", "pending-trial", "metric-running", "wrong-target"])
def test_rollback_forgery_and_unsafe_boundaries_refused(legacy, forgery):
    result = apply_plan(legacy)
    adopt_later_workflow(legacy)
    receipt = result["receipt_ref"]
    target = "workflow"
    if forgery == "receipt":
        record = legacy.store.get("source_upgrades", receipt)
        record["to_commit"] = "buggy-old-executable"
        legacy.store.put("source_upgrades", receipt, record)
    elif forgery == "qualified-activation":
        legacy.store.put("source_qualified_activations", "workflow/" + receipt, {})
    elif forgery == "original-index":
        record = legacy.store.get("source_upgrades", receipt)
        old = legacy.store.get("method_snapshots", record["old_snapshots"]["workflow"])
        legacy.store.put("method_activations", "workflow/" + old["version"],
                         {"snapshot_ref": record["new_snapshots"]["workflow"]})
    elif forgery == "snapshot":
        record = legacy.store.get("source_upgrades", receipt)
        legacy.store.put("method_snapshots", record["new_snapshots"]["workflow"], {})
    elif forgery == "no-adoption":
        with legacy.store.connect() as con:
            con.execute("DELETE FROM kv WHERE namespace='method_switches'")
    elif forgery == "pending-trial":
        legacy.store.put("trials", "unfinished", {})
    elif forgery == "metric-running":
        legacy.store.put("evaluation_metric_attempts", "pending", {"state": "running"})
    else:
        target = "meta"
    before = database_image(legacy)
    with pytest.raises((upgrade.UpgradeRefused, ValueError)):
        upgrade.dry_run(legacy.store.root, action="rollback", upgrade_receipt=receipt, target=target)
    assert database_image(legacy) == before


@pytest.mark.parametrize("event_kind", ["plate_completion_requested", "research_plan_created", "team_completed"])
def test_even_completed_current_round_drafts_are_refused(legacy, event_kind):
    legacy.store.event(event_kind, {"round": legacy.state["round_index"], "key": "current"})
    with pytest.raises(upgrade.UpgradeRefused, match="Current-round"):
        upgrade.dry_run(legacy.store.root)


def test_historical_failed_provider_attempt_with_successful_retry_is_preserved(legacy):
    key, receipt = next(iter(legacy.store.all("llm").items()))
    failed = {**receipt, "state": "failed", "attempt": 1, "request_key": key}
    legacy.store.put("llm_attempts", key + "/attempt-1", failed)
    legacy.store.put("llm_attempts", key + "/attempt-2", {**receipt, "attempt": 2})
    legacy.store.put("llm", key, {**receipt, "attempt": 2})
    apply_plan(legacy)
    assert legacy.store.get("llm_attempts", key + "/attempt-1") == failed


def test_source_qualified_meta_rollback_uses_exact_new_baseline(legacy):
    receipt = apply_plan(legacy)["receipt_ref"]
    original, _ = adopt_later_workflow(legacy, target="meta")
    kwargs = {"action": "rollback", "upgrade_receipt": receipt, "target": "meta"}
    apply_plan(legacy, **kwargs)
    assert legacy.view().meta == original
    legacy.methods.assert_plannable(legacy.state)


@pytest.fixture
def synthetic_upgrade_metric_worker(monkeypatch):
    """Fixture-only metric transport; never evaluates generated code."""
    from test_custom_metric_integration import CODE, _synthetic_output
    from proteinrsi.replay.sandbox import generated_profile
    def worker(store, view, args, *, pure=False):
        assert pure and view is None and args["code"] == CODE
        return {"status": "ok", "execution_backend": generated_profile(),
                "code_sha256": upgrade.digest(CODE), "output": _synthetic_output(args["inputs"])}
    monkeypatch.setattr("proteinrsi.evaluation_metrics.execute_code", worker)
    monkeypatch.setattr("proteinrsi.replay.sandbox.probe", lambda: {"available": True})


def completed_synthetic_metrics(campaign, trial_key, patch):
    from types import SimpleNamespace
    from test_custom_metric_integration import custom_plan
    from proteinrsi.evaluation_metrics import execute_evaluation_metrics
    from proteinrsi.llm_evaluation import ensure_evaluation_plan, adjudicate_evaluation
    class Responses:
        model, base_url, cache_settings = "synthetic-E", "https://fixture.invalid", {}
        def complete(self, role, instructions, context, schema):
            if role == "E-plan":
                return custom_plan()
            assert role == "E-verdict"
            return {"plan_ref": context["plan_ref"], "decision": "accepted",
                    "reason": "Synthetic acceptance fixture, not scientific evidence",
                    "supporting_evidence_refs": context["available_evidence_refs"]}
    team = SimpleNamespace(llm=Responses())
    evaluation_id = "synthetic-metric-trial"
    inputs = {"target": "workflow", "context": {"patch": patch.model_dump(mode="json")}}
    campaign.store.put("evaluation_trial_inputs", evaluation_id, inputs)
    plan = ensure_evaluation_plan(campaign.store, team, evaluation_id=evaluation_id,
        target="workflow", context=inputs["context"], max_top_n=2)
    envelope = {"protocol": "evaluation_inputs/v1", "evaluation_id": evaluation_id,
        "target": "workflow", "task": campaign.state["task"], "top_ns": plan["plan"]["top_ns"],
        "subject_refs": ["baseline", "challenger"],
        "arms": {"baseline": [{"sequence": "ACDE", "value": 1, "qc": "valid"},
                                {"sequence": "ACDF", "value": 2, "qc": "valid"}],
                 "challenger": [{"sequence": "ACDG", "value": 3, "qc": "valid"},
                                 {"sequence": "ACDH", "value": 5, "qc": "valid"}]},
        "cases": [], "denominators": {s: {"submitted": 2, "unique_valid": 2}
                                         for s in ("baseline", "challenger")}}
    metrics = execute_evaluation_metrics(campaign.store, plan, envelope)
    return adjudicate_evaluation(campaign.store, team, evaluation_id=evaluation_id,
        plan_ref=plan["plan_ref"], evidence={metrics["result_ref"]: metrics}, n_baseline=2, n_challenger=2)


def test_rollback_preserves_completed_historical_custom_metrics(legacy, synthetic_upgrade_metric_worker):
    receipt = apply_plan(legacy)["receipt_ref"]
    adopt_later_workflow(legacy, with_metrics=True)
    metrics = legacy.store.all("evaluation_metric_results")
    kwargs = {"action": "rollback", "upgrade_receipt": receipt, "target": "workflow"}
    apply_plan(legacy, **kwargs)
    assert metrics and legacy.store.all("evaluation_metric_results") == metrics


@pytest.mark.parametrize("part", ["evaluation_metric_results", "evaluation_metric_validations",
                                  "evaluation_evidence", "evaluation_verdicts", "trial_results"])
def test_rollback_refuses_tampered_historical_evidence(legacy, synthetic_upgrade_metric_worker, part):
    receipt = apply_plan(legacy)["receipt_ref"]
    adopt_later_workflow(legacy, with_metrics=True)
    key, record = next(iter(legacy.store.all(part).items()))
    record["tampered"] = True
    legacy.store.put(part, key, record)
    with pytest.raises((upgrade.UpgradeRefused, ValueError, RuntimeError)):
        upgrade.dry_run(legacy.store.root, action="rollback", upgrade_receipt=receipt, target="workflow")


@pytest.mark.parametrize("part", ["python", "dependencies"])
def test_changed_actual_runtime_identity_refused(legacy, monkeypatch, part):
    if part == "python":
        monkeypatch.setattr(upgrade.platform, "python_version", lambda: "0.0.0")
    else:
        old = upgrade.metadata.version
        monkeypatch.setattr(upgrade.metadata, "version", lambda name: "0.0.0" if name == "numpy" else old(name))
    with pytest.raises(upgrade.UpgradeRefused, match="differs|Dependencies"):
        upgrade.dry_run(legacy.store.root)



def test_authentic_typed_resources_and_multiread_note_c_upgrade(typed_legacy):
    store = typed_legacy.store
    assert store.all("research_runs")
    resources = {k: v for k, v in store.all("research_step_outputs").items() if k.startswith("resource:")}
    assert resources and all("resource_id" not in r for r in resources.values())
    actions = [r for r in store.all("llm").values() if r["role"] == "C-feedback"
               and "_context_action" in r.get("result", {})]
    assert len(actions) == 3
    assert {r["result"]["_context_action"]["kind"] for r in actions} == {"read", "note"}
    before = store.all("research_step_outputs")
    apply_plan(typed_legacy)
    assert store.all("research_step_outputs") == before
    typed_legacy.methods.assert_plannable(typed_legacy.state)


@pytest.mark.parametrize("malformation", ["missing-final", "invalid-final", "unrecognized-action", "resource-hash"])
def test_typed_c_and_resource_malformations_refused(typed_legacy, malformation):
    store = typed_legacy.store
    if malformation == "resource-hash":
        key, record = next((k, v) for k, v in store.all("research_step_outputs").items()
                           if k.startswith("resource:"))
        record["data"] = {"changed": True}
        store.put("research_step_outputs", key, record)
    else:
        key, record = next((k, v) for k, v in store.all("llm").items()
                           if v["role"] == "C-feedback" and "_context_action" not in v["result"])
        if malformation == "missing-final":
            with store.connect() as con:
                con.execute("DELETE FROM kv WHERE namespace='llm' AND key=?", (key,))
        else:
            record["result"] = {"wrong": True} if malformation == "invalid-final" else {
                "_context_action": {"kind": "invented"}}
            store.put("llm", key, record)
    with pytest.raises(ValueError):
        upgrade.dry_run(store.root)



def test_authentic_typed_context_and_historical_m_request_upgrade(tmp_path, monkeypatch):
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True)
    meta_requests = {k: v for k, v in campaign.store.all("research_step_outputs").items()
                     if k.startswith("meta-request:")}
    assert len(meta_requests) == 1
    assert campaign.state["round_index"] == 2
    apply_plan(campaign)
    assert all(campaign.store.get("research_step_outputs", k) == v for k, v in meta_requests.items())
    campaign.methods.assert_plannable(campaign.state)


@pytest.mark.parametrize("mutation", ["missing-decision", "wrong-history", "wrong-key", "current-M"])
def test_historical_m_requests_fail_closed(tmp_path, monkeypatch, mutation):
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True)
    key, record = next((k, v) for k, v in campaign.store.all("research_step_outputs").items()
                       if k.startswith("meta-request:"))
    if mutation == "missing-decision":
        with campaign.store.connect() as con:
            con.execute("DELETE FROM events WHERE kind='meta_decision'")
    elif mutation == "wrong-key":
        campaign.store.put("research_step_outputs", "meta-request:" + "0" * 64, record)
    else:
        if mutation == "wrong-history":
            record["view"]["history"][0]["analyst_feedback"]["summary"] = "tampered"
        else:
            record["view"]["round_index"] = campaign.state["round_index"]
        campaign.store.put("research_step_outputs", key, record)
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.dry_run(campaign.store.root)



def test_authentic_assistant_bridge_typed_context_and_historical_m(tmp_path, monkeypatch):
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True, bridge=True)
    records = campaign.store.all("llm")
    assert records and all(r["transport"] == "assistant_bridge" for r in records.values())
    assert any(r["role"] == "M" for r in records.values())
    assert any(r["role"] == "C-feedback" and "_context_action" in r["result"] for r in records.values())
    apply_plan(campaign)
    campaign.methods.assert_plannable(campaign.state)


@pytest.mark.parametrize("field", ["response", "request_hash", "raw_output"])
def test_modified_bridge_final_receipt_refused(tmp_path, monkeypatch, field):
    campaign = _make_legacy(tmp_path, monkeypatch, typed=True, with_meta=True, bridge=True)
    key, call = next((k, r) for k, r in campaign.store.all("llm").items()
                     if r["role"] == "C-feedback" and r["round"] == 2 and "_context_action" not in r["result"])
    call[field] = "tampered"
    campaign.store.put("llm", key, call)
    with pytest.raises(upgrade.UpgradeRefused):
        upgrade.dry_run(campaign.store.root)


@pytest.mark.parametrize("field", ["task", "history", "history-length"])
def test_completed_c_binds_current_task_and_full_history(legacy, field):
    state = legacy.state
    if field == "task":
        state["task"]["metric"] = "unapproved-other-assay"
    elif field == "history":
        state["history"][-1]["prediction_mae"] = 123.0
    else:
        state["history"].insert(0, deepcopy(state["history"][-1]))
    legacy.store.put("campaign", "state", state)
    before = database_image(legacy)
    with pytest.raises(upgrade.UpgradeRefused, match="task/history"):
        upgrade.dry_run(legacy.store.root)
    assert database_image(legacy) == before


@pytest.mark.parametrize("field", ["request", "raw_output", "attempt"])
def test_completed_native_c_receipt_integrity_is_checked(legacy, field):
    key, call = next((key, call) for key, call in legacy.store.all("llm").items()
                     if call["role"] == "C-feedback")
    if field == "request":
        call["request"]["messages"][1]["content"] = '{"unrelated":"different evidence"}'
    elif field == "raw_output":
        call["raw_output"] = '{"summary":"different actual provider response"}'
    else:
        call["attempt"] += 1
    legacy.store.put("llm", key, call)
    before = database_image(legacy)
    with pytest.raises(upgrade.UpgradeRefused, match="native|Native"):
        upgrade.dry_run(legacy.store.root)
    assert database_image(legacy) == before


def test_native_c_raw_output_matches_even_if_attempt_copy_matches(legacy):
    key, call = next((key, call) for key, call in legacy.store.all("llm").items()
                     if call["role"] == "C-feedback")
    call["raw_output"] = '{"summary":"different actual provider response"}'
    legacy.store.put("llm", key, call)
    legacy.store.put("llm_attempts", key + "/attempt-" + str(call["attempt"]), call)
    with pytest.raises(upgrade.UpgradeRefused, match="output differs"):
        upgrade.dry_run(legacy.store.root)


def test_completed_c_packed_context_binds_its_original_evidence(typed_legacy):
    store = typed_legacy.store
    key, call = next((key, call) for key, call in store.all("llm").items()
                     if call["role"] == "C-feedback" and "_context_action" not in call["result"])
    context = json.loads(call["request"]["messages"][1]["content"])
    root = context["_context"]["source"]["evidence_ref"]
    evidence = store.get("context_evidence", root)
    evidence["value"]["view"]["task"]["metric"] = "unapproved-other-assay"
    store.put("context_evidence", root, evidence)
    with pytest.raises(upgrade.UpgradeRefused, match="evidence context differs"):
        upgrade.dry_run(store.root)


@pytest.mark.parametrize("repeat_policy", ["allow", "exclude"])
def test_feedback_task_projection_matches_pinned_runtime(legacy, repeat_policy):
    state = legacy.state
    state["task"]["proposal_pool_size"] = 4
    state["task"]["repeat_policy"] = repeat_policy
    assert upgrade._feedback_task(state) == legacy.view(state).task.model_dump(mode="json")
