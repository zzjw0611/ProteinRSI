# SPDX-License-Identifier: MIT
"""Report actual main-line Meta use, separately from candidate adoption and efficacy."""
import json


def summarize_rsi(store):
    adopted, uses, proposals = {}, [], []
    with store.connect() as con:
        rows = con.execute("SELECT id,kind,payload FROM events WHERE kind IN "
                           "('method_version_switched','meta_analysis_started','meta_analysis_completed') "
                           "ORDER BY id")
        for row in rows:
            event_id, kind, raw = row
            p = json.loads(raw)
            if kind == "method_version_switched" and p.get("target") == "meta":
                if p.get("action") == "rollback":
                    continue
                adopted[p["to_version"]] = {"version": p["to_version"],
                    "from_version": p["from_version"], "event_id": event_id,
                    "round": p["round"], "evaluation_ref": p.get("evaluation_ref")}
            elif kind == "meta_analysis_started":
                uses.append({"event_id": event_id, "meta": p["meta"],
                    "workflow": p["workflow"], "round": p["round"], "output_id": p["output_id"]})
            elif kind == "meta_analysis_completed" and p.get("patch_id"):
                proposals.append({"event_id": event_id, "meta": p["meta"],
                    "target": p["target"], "patch_id": p["patch_id"],
                    "proposed_version": p.get("proposed_version"), "output_id": p["output_id"]})
    successors = []
    for version, adoption in adopted.items():
        calls = [u for u in uses if u["meta"] == version and u["event_id"] > adoption["event_id"]]
        children = [p for p in proposals if p["meta"] == version and p["event_id"] > adoption["event_id"]]
        successors.append({**adoption, "subsequent_invocations": calls, "subsequent_proposals": children,
                           "actually_reused": bool(calls)})
    return {"scope": "current_mutation_task", "adopted_meta_versions": successors,
        "meta_invocations": len(uses), "proposals": len(proposals),
        "recursive_use_observed": any(s["actually_reused"] and s["subsequent_proposals"] for s in successors),
        "efficacy_claim": "not_established_by_this_audit",
        "note": "Main-line events only; validation-branch calls are not counted as deployed use. "
                "Adoption/reuse is mechanism evidence, not proof of improved fitness or improvement ability."}
