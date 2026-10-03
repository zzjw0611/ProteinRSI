import math

import pytest
from pydantic import ValidationError

from proteinrsi.contracts import Candidate, MetaPolicy, Observation, Patch, TaskKind, TaskSpec, Workflow
from proteinrsi.improvement import apply_patch
from proteinrsi.tasks import apply_mutations, validate_candidate


@pytest.mark.parametrize("sequence", ["", "acde", "ACDX", "ACD*", "A C"])
def test_sequence_validation(sequence):
    with pytest.raises(ValidationError):
        Candidate(sequence=sequence)


def test_mutation_reference_and_numbering():
    assert apply_mutations("ACDE", [{"position": 2, "from": "C", "to": "F"}]) == "AFDE"
    for edits in [[{"position": 0, "from": "A", "to": "F"}],
                  [{"position": 2, "from": "D", "to": "F"}],
                  [{"position": True, "from": "A", "to": "F"}],
                  [{"position": 2, "from": "C", "to": "F"}] * 2]:
        with pytest.raises(ValueError):
            apply_mutations("ACDE", edits)


def test_fixed_positions_and_mutation_limit():
    task = TaskSpec(name="test", reference_sequence="ACDE", mutable_positions=[2], max_mutations=1)
    validate_candidate(task, Candidate(sequence="AFDE"))
    for sequence in ["FCDE", "AFDF", "ACDEE"]:
        with pytest.raises(ValueError):
            validate_candidate(task, Candidate(sequence=sequence))


def test_affinity_inputs_are_immutable():
    task = TaskSpec(name="affinity", kind="affinity_prediction", reference_sequence="ACDE",
                    target_sequence="FGHI", controls_per_batch=0)
    validate_candidate(task, Candidate(sequence="ACDE"))
    with pytest.raises(ValueError):
        validate_candidate(task, Candidate(sequence="ACDF"))


def test_binder_target_and_scaffold_constraints():
    task = TaskSpec(name="binder", kind="binder_design", reference_sequence="ACDE",
                    target_sequence="FGHI", mutable_positions=[2], max_mutations=1)
    validate_candidate(task, Candidate(sequence="AFDE"))
    with pytest.raises(ValueError):
        validate_candidate(task, Candidate(sequence="FCDE"))
    assert task.target_sequence == "FGHI"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_measurements_rejected(value):
    with pytest.raises(ValidationError):
        Observation(sample_id="s", sequence="ACD", value=value, metric="x", unit="a.u.",
                    source="synthetic", batch_id="b", assay_protocol="v1")


def test_qc_failure_not_zero():
    with pytest.raises(ValidationError):
        Observation(sample_id="s", sequence="ACD", value=0, qc="failed", metric="x", unit="a.u.",
                    source="synthetic", batch_id="b", assay_protocol="v1")


@pytest.mark.parametrize("changes", [{"budget": 100000}, {"eval": "always_accept"},
                                       {"__class__": "arbitrary"}, {}, {"strategy": "exec_code"}])
def test_patch_trusted_boundary(changes):
    base = Workflow()
    patch = Patch(target="workflow", base_version=base.version, changes=changes,
                  hypothesis="A testable hypothesis", task_kind=TaskKind.VARIANT)
    with pytest.raises((ValueError, PermissionError)):
        apply_patch(base, patch)


def test_versioned_patch_and_noop():
    base = Workflow()
    patch = Patch(target="workflow", base_version=base.version, changes={"strategy": "pairwise"},
                  hypothesis="Use combination feedback", task_kind=TaskKind.VARIANT)
    child = apply_patch(base, patch)
    assert child.version != base.version
    assert base.strategy == "additive"
    with pytest.raises(ValueError):
        apply_patch(child, patch)


def test_meta_cannot_self_enable():
    base = MetaPolicy(enabled=False)
    patch = Patch(target="meta", base_version=base.version, changes={"enabled": True},
                  hypothesis="Enable without permission", task_kind=TaskKind.VARIANT)
    with pytest.raises(PermissionError):
        apply_patch(base, patch)
