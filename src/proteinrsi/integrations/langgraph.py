# SPDX-License-Identifier: MIT
"""A real LangGraph execution graph around the same idempotent campaign operations."""
from __future__ import annotations

from typing import TypedDict

from proteinrsi.contracts import Observation


class GraphState(TypedDict, total=False):
    campaign_id: str
    batch_id: str | None


def build_graph(campaign, checkpointer):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def plan(state):
        batch = campaign.prepare()
        return {"batch_id": batch.batch_id if batch else None}

    def approval(state):
        if campaign.state["status"] == "awaiting_approval":
            response = interrupt({"type": "approval", "batch_id": state["batch_id"],
                                  "batch": campaign.store.get("batches", state["batch_id"])})
            if response.get("approved") is not True:
                raise ValueError("Approval was not granted; no experiment submitted")
            campaign.approve(state["batch_id"], operator=response.get("operator", ""))
        return {}

    def measurement(state):
        if campaign.state["pending_batch"] == state["batch_id"]:
            response = interrupt({"type": "measurements", "batch_id": state["batch_id"],
                                  "required": "observations matching exported batch"})
            campaign.ingest([Observation.model_validate(o) for o in response["observations"]])
        return {}

    builder = StateGraph(GraphState)
    builder.add_node("plan", plan)
    builder.add_node("approval", approval)
    builder.add_node("measurement", measurement)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", lambda s: "approval" if s.get("batch_id") else END)
    builder.add_edge("approval", "measurement")
    builder.add_edge("measurement", "plan")
    return builder.compile(checkpointer=checkpointer)


def invoke_persistent(campaign, resume_value: dict | None = None):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command
    config = {"configurable": {"thread_id": campaign.state["campaign_id"]}, "recursion_limit": 1000}
    with SqliteSaver.from_conn_string(str(campaign.store.root / "graph.sqlite3")) as saver:
        graph = build_graph(campaign, saver)
        value = Command(resume=resume_value) if resume_value is not None else {"campaign_id": campaign.state["campaign_id"]}
        return graph.invoke(value, config=config)
