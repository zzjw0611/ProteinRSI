#!/usr/bin/env python3
"""Display-only TXT export: human round numbers without changing a pinned engine."""
import argparse
import json
from pathlib import Path
import sqlite3
import time

from proteinrsi import trajectory
from proteinrsi.execution_control import process_identity
from proteinrsi.trace_export import _atomic_text, _destination, unwrap_event


class RoundLabels:
    def __init__(self, total):
        self.total = total
        self.scopes = {}

    def label(self, event):
        kind, payload, branches = unwrap_event(event)
        scope = tuple(branches)
        number, phase = self.scopes.get(scope, (0, "初始化"))
        raw = payload.get("round")
        role = str(payload.get("role", ""))
        feedback_role = role == "C-feedback"
        meta_role = role.startswith("M")
        post_round = (kind in {"research_state_updated", "analyst_feedback_completed",
                              "method_candidate_transition", "method_version_switched",
                              "runtime_repaired"} or kind.startswith("meta_")
                      or feedback_role or meta_role)
        if kind == "agent_state":
            state_phase = payload.get("phase")
            post_round = state_phase in {"initialized", "feedback_committed", "improver_input"}
            phase = {"initialized": "初始化", "round_input": "准备候选",
                     "awaiting_approval": "提交实验", "feedback_committed": "测量完成",
                     "improver_input": "方法改进"}.get(state_phase, phase)
        if type(raw) is int:
            number = raw if post_round else raw + 1
        if role:
            if feedback_role:
                phase = "分析实测反馈"
            elif meta_role:
                phase = "方法改进"
            elif role in {"A-resources", "A-plan"}:
                phase = "规划"
            elif role == "A-review":
                phase = "复核"
            elif role == "B":
                phase = "执行／生成候选"
            elif role == "C":
                phase = "分析／排序候选"
        if kind in {"batch_prepared", "batch_approved"}:
            phase = "提交实验"
        elif kind == "feedback_ingested":
            phase = "测量完成"
        elif kind == "task_loop_stopped":
            phase = "暂停" if payload.get("status") == "paused" else "停止／完成"
        elif kind == "run_started":
            phase = "续跑" if payload.get("resume") else "启动"
        elif kind == "campaign_completed":
            number, phase = payload.get("rounds", number), "研究完成"
        self.scopes[scope] = number, phase
        if not number:
            return "初始输入／母本证据"
        total = f"／共{self.total}轮" if not scope and self.total else ""
        prefix = "验证" if scope else ""
        return f"{prefix}第{number}轮{total} · {phase}"


def export(directory, destination):
    root = Path(directory).resolve()
    destination = _destination(root, destination)
    database = (root / "state.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(database, uri=True) as con:
        total, completed = con.execute("SELECT json_extract(value,'$.task.max_rounds'), "
            "json_extract(value,'$.round_index') FROM kv "
            "WHERE namespace='campaign' AND key='state'").fetchone()
        labels = RoundLabels(total)
        original = trajectory.print_event

        def print_labeled(event, stream=None):
            # Read only this immutable event, including its validation branch.
            kind, payload = con.execute("SELECT kind,payload FROM events WHERE id=?",
                                        (event["id"],)).fetchone()
            label = labels.label({**event, "kind": kind, "payload": json.loads(payload)})
            print(f"[{label}]", end=" ", file=stream)
            # Keep zero-based counters in the original database, not in the reader's display.
            detail = dict(event["payload"])
            detail.pop("round", None)
            original({**event, "payload": detail}, stream)

        try:
            trajectory.print_event = print_labeled
            with _atomic_text(destination) as stream:
                print(f"计划：{total or '未指定'}轮；已完成：{completed}轮。", file=stream)
                print("下方轮次从第1轮开始；测量后的反馈分析和方法改进属于刚完成的轮次。\n",
                      file=stream)
                trajectory.write_text(root, stream)
        finally:
            trajectory.print_event = original
    return str(destination)


def progress(directory):
    database = (Path(directory).resolve() / "state.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(database, uri=True) as con:
        last = con.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[0]
        row = con.execute("SELECT value FROM kv WHERE namespace='execution_control' "
                          "AND key='controller'").fetchone()
    controller = json.loads(row[0]) if row else {}
    live = process_identity(controller.get("pid", -1))
    alive = bool(live and live["state"] != "Z"
                 and live["start_ticks"] == controller.get("start_ticks"))
    return last, alive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--out")
    parser.add_argument("--follow", action="store_true", help="Refresh until the controller exits")
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("interval must be between 1 and 60 seconds")
    destination = args.out or str(Path(args.campaign) / "trajectory.txt")
    previous = None
    while True:
        current = progress(args.campaign)
        if current != previous:
            export(args.campaign, destination)
            previous = current
        if not args.follow or not current[1]:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
