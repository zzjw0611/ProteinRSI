"""Portable synthetic contracts for the frozen W; no empirical inputs or model APIs."""

import hashlib
import os
from pathlib import Path

import pytest

from proteinrsi.contracts import TaskSpec, TaskView, Workflow, MetaPolicy, Observation, digest
from proteinrsi.research.code import execute_code
from proteinrsi.replay.sandbox import probe
from proteinrsi.storage import Store

CONFIG = Path(__file__).resolve().parents[1] / "configs/experiments/observed_neighborhood_w1.json"
WORKFLOW = Workflow.model_validate_json(CONFIG.read_text())
CODE = WORKFLOW.programs[0].code


@pytest.mark.core
def test_frozen_observed_neighborhood_identity():
    assert hashlib.sha256(CONFIG.read_bytes()).hexdigest() == (
        "e55b84bde21603795b934fcb49779853c149f74044f79c2e2c5b5cfa85fab98b"
    )
    assert WORKFLOW.version == "w-d2d2ae7a057dad0f"
    assert hashlib.sha256(CODE.encode()).hexdigest() == (
        "af5163419e8e562d95ec4cd208e19f472c3c3a7228805138eb5726b47b8f6ab6"
    )
    initial = Workflow.model_validate_json(
        CONFIG.with_name("planner_delegation_w0.json").read_text()
    )
    assert initial.version == "w-6ccf8d89b4d80098"
    before, after = initial.model_dump(mode="json"), WORKFLOW.model_dump(mode="json")
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    assert changed == {"designer_prompt", "principal_prompt", "programs"}
    assert after["principal_prompt"].startswith(before["principal_prompt"])
    assert len(after["programs"]) == 1
    assert after["programs"][0]["name"] == "observed_neighborhood_coverage"


def task(n=3, **kw):
    fields = dict(
        name="Synthetic coverage validation",
        reference_sequence="A" * n,
        mutable_positions=list(range(1, n + 1)),
        candidate_access="open",
        max_mutations=n,
        controls_per_batch=0,
    )
    fields.update(kw)
    return TaskSpec(**fields)


def obs(seq, value=None, qc="valid"):
    # Every row below is invented. measured_replay is only a required schema label
    # for unavailable status, never a loaded or queried historical record.
    return Observation(
        sample_id="synthetic-" + seq + "-" + str(value) + "-" + qc,
        sequence=seq,
        value=value,
        qc=qc,
        metric="fitness",
        unit="a.u.",
        source="measured_replay" if qc == "unavailable" else "synthetic",
        batch_id="synthetic-fixture",
        assay_protocol="synthetic-only-v1",
    )


def view(t, observations):
    return TaskView(
        task=t,
        observations=observations,
        history=[],
        round_index=0,
        remaining_wells=0,
        workflow=Workflow(),
        meta=MetaPolicy(enabled=False),
    )


def counts(result, eligible, queried, valid, nonvalid, unqueried, key="parent_neighborhood"):
    got = result[key]
    assert {
        k: got[k]
        for k in (
            "eligible_count",
            "queried_count",
            "valid_count",
            "nonvalid_only_count",
            "unqueried_count",
        )
    } == dict(
        eligible_count=eligible,
        queried_count=queried,
        valid_count=valid,
        nonvalid_only_count=nonvalid,
        unqueried_count=unqueried,
    ), got
    assert len(got["unqueried_site_residues"]) == unqueried
    assert len(got["nonvalid_site_residues"]) == nonvalid
    assert got["all_neighbor_outcomes_valid"] == (eligible == valid)


def empty3(r):
    counts(r, 57, 0, 0, 0, 57)
    assert r["incumbent_site_residues"] is None and r["incumbent_neighborhood"] is None


def empty4(r):
    counts(r, 76, 0, 0, 0, 76)
    assert r["positions_1based"] == [1, 2, 3, 4]


def mixed(r):
    counts(r, 57, 4, 1, 3, 53)
    assert r["incumbent_site_residues"] == "CAA"
    assert r["parent_neighborhood"]["nonvalid_site_residues"] == ["ADA", "AEA", "FAA"]


def zero(r):
    counts(r, 57, 2, 0, 2, 55)
    assert r["incumbent_site_residues"] is None and r["incumbent_neighborhood"] is None


def minimize(r):
    counts(r, 57, 2, 2, 0, 55)
    assert r["incumbent_site_residues"] == "DAA"


def restricted(r):
    counts(r, 57, 1, 1, 0, 56)
    counts(r, 19, 1, 1, 0, 18, key="incumbent_neighborhood")
    # Only reversal to parent plus 18 alternatives at the same changed site.
    assert r["incumbent_site_residues"] == "CAA"
    assert all(s[1:] == "AA" for s in r["incumbent_neighborhood"]["unqueried_site_residues"])


def repeats(r):
    counts(r, 57, 2, 2, 0, 55)
    assert r["incumbent_site_residues"] == "DAA"
    assert (
        r["repeat_aggregation"]
        == "Mean of supplied valid rows per sequence; deterministic repeats are not independent evidence."
    )


def complete(r):
    counts(r, 57, 57, 57, 0, 0)


def unsupported(r):
    assert r == {
        "applicable": False,
        "reason": "Requires open fixed-site substitution design with one to eight mutable sites.",
    }


def noncontiguous(r):
    counts(r, 57, 1, 1, 0, 56)
    assert r["positions_1based"] == [2, 4, 6] and r["incumbent_site_residues"] == "CAA"


neighbors = []
for i in range(3):
    for aa in "CDEFGHIKLMNPQRSTVWY":
        s = list("AAA")
        s[i] = aa
        neighbors.append(obs("".join(s), 1))
CASES = [
    ("three_sites_empty", task(), [], empty3),
    ("four_sites_empty", task(4), [], empty4),
    (
        "attempted_valid_unavailable",
        task(),
        [
            obs("CAA", 3),
            obs("ADA", qc="unavailable"),
            obs("AEA", qc="failed"),
            obs("FAA", qc="inconclusive"),
        ],
        mixed,
    ),
    (
        "zero_valid_observations",
        task(),
        [obs("CAA", qc="failed"), obs("ADA", qc="unavailable")],
        zero,
    ),
    ("minimize_direction", task(direction="minimize"), [obs("CAA", 5), obs("DAA", -2)], minimize),
    (
        "restricted_incumbent_neighbors",
        task(max_mutations=1),
        [obs("CAA", 3), obs("AAA", 1)],
        restricted,
    ),
    (
        "valid_repeat_mean_and_nonvalid_repeat",
        task(repeat_policy="allow"),
        [obs("CAA", 0), obs("CAA", 10), obs("CAA", qc="failed"), obs("DAA", 6)],
        repeats,
    ),
    ("all_parent_neighbors_valid", task(), neighbors, complete),
    ("unsupported_pool", task(candidate_access="pool"), [], unsupported),
    (
        "unsupported_ranking",
        task(candidate_access="pool", kind="variant_ranking", candidates=["AAA", "CAA"]),
        [],
        unsupported,
    ),
    (
        "unsupported_indels",
        task(
            allow_indels=True, mutable_positions=[], max_mutations=None, min_length=1, max_length=6
        ),
        [],
        unsupported,
    ),
    ("unsupported_nine_sites", task(9), [], unsupported),
    (
        "noncontiguous_sorted_positions",
        task(reference_sequence="AAAAAAA", mutable_positions=[6, 2, 4]),
        [obs("ACAAAAA", 3)],
        noncontiguous,
    ),
]


def nullable_incumbent3(r):
    counts(r, 57, 1, 1, 0, 56)
    counts(r, 57, 1, 1, 0, 56, key="incumbent_neighborhood")
    assert r["incumbent_site_residues"] == "CAA"


def nullable_incumbent4(r):
    counts(r, 76, 1, 1, 0, 75)
    counts(r, 76, 1, 1, 0, 75, key="incumbent_neighborhood")
    assert r["incumbent_site_residues"] == "CAAA"


CASES += [
    ("nullable_cap_three_empty", task(max_mutations=None), [], empty3),
    ("nullable_cap_four_empty", task(4, max_mutations=None), [], empty4),
    (
        "nullable_cap_three_incumbent",
        task(max_mutations=None),
        [obs("AAA", 1), obs("CAA", 3)],
        nullable_incumbent3,
    ),
    (
        "nullable_cap_four_incumbent",
        task(4, max_mutations=None),
        [obs("AAAA", 1), obs("CAAA", 3)],
        nullable_incumbent4,
    ),
]


@pytest.mark.parametrize("name,t,observations,check", CASES, ids=[case[0] for case in CASES])
def test_frozen_observed_neighborhood_sandbox(tmp_path, monkeypatch, name, t, observations, check):
    monkeypatch.setenv("PROTEINRSI_SANDBOX_BACKEND", "bwrap")
    security = probe()
    if not security["available"]:
        if os.environ.get("PROTEINRSI_REQUIRE_BWRAP") == "1":
            pytest.fail(str(security))
        pytest.skip("Actual bwrap is required for the frozen program contract")
    receipt = execute_code(Store(tmp_path / name), view(t, observations), {"code": CODE})
    assert receipt["status"] == "ok", receipt
    assert receipt["execution_backend"] == "bwrap_seccomp_generated_v1"
    assert receipt["measurement_authority"] is False
    assert receipt["code_sha256"] == digest(CODE)
    check(receipt["output"])
    # Returned identities are sorted, bounded and derived from supplied observations.
    for key in ("parent_neighborhood", "incumbent_neighborhood"):
        if receipt["output"].get(key):
            for field in ("unqueried_site_residues", "nonvalid_site_residues"):
                items = receipt["output"][key][field]
                assert items == sorted(set(items))
