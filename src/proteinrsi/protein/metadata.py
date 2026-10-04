# SPDX-License-Identifier: MIT
"""Read deployment identity without loading/resolving any model."""
def declared_identity(store, model=None):
    config = store.get("configuration", "protein_model")
    if config is None and model is not None and hasattr(model, "config"):
        cfg = model.config
        config = cfg.model_dump() if hasattr(cfg, "model_dump") else None
    return {"configuration": config, "resolved_snapshot": store.get("protein_backend", "snapshot")}
