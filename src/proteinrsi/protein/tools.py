# SPDX-License-Identifier: MIT
"""Operator-registered local ESMC tools. No additional MCP wrapper is required."""
from __future__ import annotations

from proteinrsi.contracts import digest
from proteinrsi.tools import ToolGateway

TOOL_NAMES = ["esmc600m_score_variants", "esmc600m_suggest_mutations", "esmc600m_embed_sequences"]


def register_esmc_tools(gateway: ToolGateway, model) -> None:
    from proteinrsi.localtools.catalog import tool_spec
    config = model.config

    def register(name, function):
        spec = tool_spec(name, "esmc600m-native-v1:" + digest(config)[:16])
        if name == TOOL_NAMES[1]:
            spec.input_schema["properties"]["positions"]["maxItems"] = config.max_masked_positions
        def execute(arguments):
            return {**function(arguments), "model": model.identity, "evidence_kind": "proxy"}
        gateway.register(spec, execute)

    register(TOOL_NAMES[0],
        lambda a: {"scores": [{"sequence": s, "masked_marginal_log_odds": v}
                    for s, v in zip(a["sequences"], model.score_variants(a["reference"], a["sequences"]))],
                   "warning": "Sequence prior, NOT measured fitness, affinity or epistasis."})
    register(TOOL_NAMES[1],
        lambda a: {"candidates": model.suggest(a["reference"], a["positions"], a["top_k"])})

    def embeddings(arguments):
        vectors = model.embed(arguments["sequences"])
        artifact = {"sequences": arguments["sequences"], "embeddings": vectors.tolist(),
                    "model": model.identity, "pooling": "final_layer_residue_mean"}
        key = digest(artifact)
        model.store.put("embedding_artifacts", key, artifact, immutable=True)
        # Large vectors stay in local storage, never automatically enter the LLM prompt.
        return {"artifact_ref": "embedding_artifacts/" + key,
                "shape": list(vectors.shape), "pooling": artifact["pooling"]}

    register(TOOL_NAMES[2], embeddings)
