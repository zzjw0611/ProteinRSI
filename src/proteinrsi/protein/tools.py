# SPDX-License-Identifier: MIT
"""Operator-registered local ESMC tools. No additional MCP wrapper is required."""
from __future__ import annotations

from proteinrsi.contracts import TaskKind, digest
from proteinrsi.tools import ToolGateway, ToolSpec

TOOL_NAMES = ["esmc600m_score_variants", "esmc600m_suggest_mutations", "esmc600m_embed_sequences"]
SEQ = {"type": "string", "minLength": 1, "pattern": "^[ACDEFGHIKLMNPQRSTVWY]+$"}
SEQS = {"type": "array", "items": SEQ, "minItems": 1, "maxItems": 384}


def register_esmc_tools(gateway: ToolGateway, model) -> None:
    config = model.config
    output = {"type": "object", "required": ["model", "evidence_kind"]}

    def register(name, capability, properties, required, function, *, protected=None, kinds=None):
        spec = ToolSpec(name=name, capability=capability,
            implementation_version="esmc600m-native-v1:" + digest(config)[:16],
            task_kinds=kinds or [TaskKind.VARIANT, TaskKind.RANKING, TaskKind.BINDER],
            input_schema={"type": "object", "properties": properties, "required": required,
                          "additionalProperties": False}, output_schema=output,
            protected_inputs=protected or {}, license_id="SEE_THIRD_PARTY.md", data_egress=False)
        def execute(arguments):
            return {**function(arguments), "model": model.identity, "evidence_kind": "proxy"}
        gateway.register(spec, execute)

    register(TOOL_NAMES[0], "variant.score",
        {"reference": SEQ, "sequences": SEQS}, ["reference", "sequences"],
        lambda a: {"scores": [{"sequence": s, "masked_marginal_log_odds": v}
                    for s, v in zip(a["sequences"], model.score_variants(a["reference"], a["sequences"]))],
                   "warning": "Sequence prior, NOT measured fitness, affinity or epistasis."},
        protected={"reference": "reference_sequence"})
    register(TOOL_NAMES[1], "variant.suggest",
        {"reference": SEQ, "positions": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                          "minItems": 1, "maxItems": config.max_masked_positions},
         "top_k": {"type": "integer", "minimum": 1, "maximum": 384}},
        ["reference", "positions", "top_k"],
        lambda a: {"candidates": model.suggest(a["reference"], a["positions"], a["top_k"])},
        protected={"reference": "reference_sequence", "positions": "mutable_positions"},
        kinds=[TaskKind.VARIANT])

    def embeddings(arguments):
        vectors = model.embed(arguments["sequences"])
        artifact = {"sequences": arguments["sequences"], "embeddings": vectors.tolist(),
                    "model": model.identity, "pooling": "final_layer_residue_mean"}
        key = digest(artifact)
        model.store.put("embedding_artifacts", key, artifact, immutable=True)
        # Large vectors stay in local storage, never automatically enter the LLM prompt.
        return {"artifact_ref": "embedding_artifacts/" + key,
                "shape": list(vectors.shape), "pooling": artifact["pooling"]}

    register(TOOL_NAMES[2], "sequence.embed", {"sequences": SEQS}, ["sequences"], embeddings)
