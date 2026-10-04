# SPDX-License-Identifier: MIT
"""Sequence membership/sampling without phenotype values; no global ranking endpoint."""
import numpy as np
from proteinrsi.contracts import TaskSpec, TaskKind, digest
from proteinrsi.tools import ToolSpec

LIBRARY_TOOLS = ["library_check", "library_sample"]

def catalogue_task(store, view):
    state = store.get("campaign", "state")
    return TaskSpec.model_validate(state["task"]) if state else view.task

def register_library_tools(gateway, view):
    if getattr(gateway, "remote_context_tools", False):
        return
    task = catalogue_task(gateway.store, view)
    universe = set(task.candidates)
    measured = {o.sequence for o in view.observations}
    for name in LIBRARY_TOOLS:
        gateway._tools.pop(name, None)
    if not universe:
        return
    specs = {
      "library_check": {"sequences": {"type": "array", "minItems": 1, "maxItems": 384, "items": {"type": "string"}}},
      "library_sample": {"count": {"type": "integer", "minimum": 1, "maximum": 384}, "seed": {"type": "integer", "minimum": 0}}
    }
    def check(a):
        return {"availability": [{"sequence": s, "available": s in universe, "already_observed": s in measured}
                                 for s in a["sequences"]], "evidence_kind": "catalogue_metadata"}
    def sample(a):
        eligible = sorted(universe-measured-{task.reference_sequence})
        rng = np.random.default_rng(a["seed"])
        indices = rng.choice(len(eligible), min(a["count"], len(eligible)), replace=False)
        return {"sequences": [eligible[int(i)] for i in indices], "evidence_kind": "catalogue_metadata"}
    for name, props in specs.items():
        field = "availability" if name == "library_check" else "sequences"
        spec = ToolSpec(name=name, capability="library.query", description="Check or sample the eligible sequence catalogue without fitness labels.",
            limitations="Availability is not biological quality. Sampling is uniform by supplied seed, not fitness.",
            cost_hint="One tool call; no measurement query and no protein model invocation.",
            when_to_use=["Check a newly proposed sequence or obtain more unmeasured identities."],
            when_not_to_use=["You need fitness; use the separately budgeted experiment controller."],
            output_semantics="Sequence identities/membership only; no label, rank or imputed outcome.",
            implementation_version="catalogue-v1:"+digest(sorted(universe))+":"+view.evidence_version,
            task_kinds=[TaskKind.VARIANT, TaskKind.RANKING],
            input_schema={"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
            output_schema={"type": "object", "properties": {field: {"type": "array"}, "evidence_kind": {"const": "catalogue_metadata"}},
                           "required": [field,"evidence_kind"], "additionalProperties": False})
        gateway.register(spec, check if name == "library_check" else sample)
