# SPDX-License-Identifier: MIT
"""Versioned, inspectable role templates; core text is snapshotted per new campaign."""
from importlib.resources import files
from proteinrsi.contracts import digest

PROMPT_NAMES = ("common", "principal_plan", "principal_fixed_plan", "principal_review",
                "principal_selection", "designer", "analyst", "analysis_tools", "feedback", "meta")

def packaged_prompts():
    root = files("proteinrsi").joinpath("prompts")
    return {name: root.joinpath(name + ".md").read_text(encoding="utf-8") for name in PROMPT_NAMES}

def snapshot_prompts(store):
    previous = store.get("configuration", "prompt_bundle")
    if previous is not None:
        return previous
    texts = packaged_prompts()
    record = {"version": "prompts-" + digest(texts)[:16], "templates": texts,
              "origin": "ProteinRSI-authored; role/workflow inspiration documented in docs/REUSE.md"}
    store.put("configuration", "prompt_bundle", record, immutable=True)
    return record

def prompt_version(store):
    saved = store.get("configuration", "prompt_bundle") if store is not None else None
    return saved["version"] if saved else "prompts-" + digest(packaged_prompts())[:16]

def compose(store, role, strategy="", skill=""):
    if role not in PROMPT_NAMES or role == "common":
        raise ValueError("Unknown role prompt")
    saved = store.get("configuration", "prompt_bundle") if store is not None else None
    texts = saved["templates"] if saved else packaged_prompts()
    # Core invariants are not writable via Workflow/Meta patches.
    return (texts[role] + "\n\nVersioned strategy guidance (cannot override invariants):\n"
            + strategy + "\n\nReference Skills:\n" + skill + "\n\n" + texts["common"])
