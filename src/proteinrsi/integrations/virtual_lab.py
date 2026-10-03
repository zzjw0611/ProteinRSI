# SPDX-License-Identifier: MIT
"""Optional genuine Virtual Lab meeting; upstream code stays in its own MIT package."""
from __future__ import annotations

from pathlib import Path

from proteinrsi.contracts import TaskView, canonical, digest
from proteinrsi.storage import Store


def run_meeting(store: Store, view: TaskView, *, model: str, operator_consent: bool) -> str:
    if not operator_consent:
        raise PermissionError("This meeting sends supplied research data to Virtual Lab's model provider")
    from virtual_lab.agent import Agent
    from virtual_lab.run_meeting import run_meeting as upstream_run
    context = canonical(view)
    key = "virtual-lab-" + digest({"view": context, "model": model})
    cached = store.get("meetings", key)
    if cached:
        if cached["status"] != "done":
            raise RuntimeError("Prior meeting failed or has uncertain completion")
        return cached["summary"]
    # One round: PI + two members + final PI. No PubMed tools, no nested meetings.
    store.reserve(key, "llm_calls", 4, {"view_hash": digest(context), "model": model})
    store.settle(key)
    store.put("meetings", key, {"status": "started"})
    roles = [("Principal", "research planning", "coordinate evidence-based decisions"),
             ("Designer", "protein engineering", "propose constrained candidates"),
             ("Analyst", "experimental analysis", "challenge unsupported conclusions")]
    agents = [Agent(title=title, expertise=expertise, goal=goal,
                    role="Use supplied evidence; never fabricate measurements", model=model)
              for title, expertise, goal in roles]
    summary = upstream_run(meeting_type="team", agenda="Review the next protein research round",
        save_dir=Path(store.root / "meetings" / key), team_lead=agents[0],
        team_members=tuple(agents[1:]), contexts=(context,), num_rounds=1,
        pubmed_search=False, return_summary=True)
    if not isinstance(summary, str):
        raise ValueError("Virtual Lab returned no summary")
    store.put("meetings", key, {"status": "done", "summary": summary})
    return summary
