# SPDX-License-Identifier: MIT
"""Original adapter to the MIT-licensed GEPA API (no upstream source copied)."""
from __future__ import annotations

import json
from typing import Callable

from proteinrsi.contracts import Workflow, canonical


def encode_workflow(workflow: Workflow) -> dict[str, str]:
    return {key: canonical(value) for key, value in workflow.model_dump().items()}


def decode_workflow(candidate: dict[str, str]) -> Workflow:
    return Workflow.model_validate({key: json.loads(value) for key, value in candidate.items()})


class ProteinWorkflowAdapter:
    """The callback owns labels/budgets; return only legitimately visible trace information.

    evaluator(case, workflow) -> (numeric_score, public_trace)
    GEPA's internal choice is a proposal, NOT permission to publish a workflow.
    """
    propose_new_texts = None

    def __init__(self, evaluator: Callable):
        self.evaluator = evaluator

    def evaluate(self, batch: list[dict], candidate: dict[str, str], capture_traces: bool = False):
        from gepa.core.adapter import EvaluationBatch
        workflow = decode_workflow(candidate)
        outputs, scores, traces = [], [], []
        for case in batch:
            score, trace = self.evaluator(case, workflow)
            outputs.append({"case_id": case["case_id"]})
            scores.append(float(score))
            traces.append(trace)
        return EvaluationBatch(outputs=outputs, scores=scores,
                               trajectories=traces if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        if eval_batch.trajectories is None:
            raise ValueError("Reflection requires captured public traces")
        return {component: [{"Feedback": trace, "score": score}
                for trace, score in zip(eval_batch.trajectories, eval_batch.scores)]
                for component in components_to_update}


def optimize_workflow(workflow: Workflow, adapter: ProteinWorkflowAdapter,
                      trainset: list[dict], valset: list[dict], *, reflection_lm,
                      max_metric_calls: int = 20) -> Workflow:
    import gepa
    if max_metric_calls < len(trainset) + len(valset) or not trainset or not valset:
        raise ValueError("Provide separate nonempty train/validation cases and an adequate call limit")
    if {c["case_id"] for c in trainset} & {c["case_id"] for c in valset}:
        raise ValueError("Train/validation case IDs overlap")
    result = gepa.optimize(seed_candidate=encode_workflow(workflow), trainset=trainset,
        valset=valset, adapter=adapter, reflection_lm=reflection_lm, max_metric_calls=max_metric_calls)
    return decode_workflow(result.best_candidate)
