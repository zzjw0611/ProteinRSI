# SPDX-License-Identifier: MIT
"""Read-only, offline operator timeline; never opens an assay label table."""
import json
from pathlib import Path
import sqlite3
import sys
import time


def read_trace(directory):
    path = (Path(directory)/"state.sqlite3").resolve(strict=True)
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as con:
        rows = con.execute("SELECT namespace,key,value FROM kv").fetchall()
        events = [{"id": i, "timestamp": t, "kind": k, "payload": json.loads(p)}
                  for i, t, k, p in con.execute("SELECT * FROM events ORDER BY id")]
        limits = dict(con.execute("SELECT resource,amount FROM limits"))
        charges = con.execute("SELECT resource,amount,state FROM charges").fetchall()
    records = {}
    allowed = {"llm_attempts", "validation_llm_attempts", "llm", "tool_jobs", "agent_snapshots", "validation_llm", "validation_tool_jobs",
        "validation_agent_snapshots", "patches", "trials", "trial_results", "meta_evaluations",
        "meta_online_attempts", "research_runs", "research_step_outputs", "batches", "measurements",
        "validation_research_runs", "validation_research_step_outputs", "computational_iterations",
        "code_programs", "validation_code_programs", "plate_plans", "workflow_validation_outcomes",
        "method_candidates", "method_candidate_states", "method_transitions", "method_switches",
        "method_snapshots", "method_activations", "method_deferrals", "method_proposal_failures",
        "batch_method_bindings", "gepa_attempts", "gepa_results", "gepa_failures"}
    state = {}
    for ns, key, value in rows:
        if ns == "campaign" and key == "state":
            state = json.loads(value)
        elif ns in allowed:
            records.setdefault(ns, {})[key] = json.loads(value)
    for event in events:
        payload = event["payload"]
        prefix, branch = "", ""
        if event["kind"] == "validation_event":
            prefix, branch = "validation_", payload["branch"]+"/"
            payload = payload["payload"]
        for field, namespace in (("attempt_key", "llm_attempts"), ("key", "llm"), ("key", "tool_jobs"), ("snapshot_ref", "agent_snapshots"),
                                 ("run_id", "research_runs"), ("output_id", "research_step_outputs"),
                                 ("snapshot_ref", "method_snapshots"), ("patch_id", "method_candidates"),
                                 ("archive_ref", "gepa_results")):
            ref = payload.get(field)
            if not isinstance(ref, str):
                continue
            record = records.get(prefix+namespace, {}).get(branch+ref)
            if record is not None:
                event.setdefault("details", {})[namespace] = record
    spent = {k: sum(n for r, n, s in charges if r == k and s == "committed") for k in limits}
    return {"campaign": str(path.parent), "status": state.get("status"),
        "rounds": state.get("round_index"), "source": ("computational" if state.get("task", {}).get("execution_mode") == "computational"
                   else state.get("task", {}).get("feedback_source")),
        "limits": limits, "spent": spent, "events": events, "records": records,
        "note": "Model-returned text and decision summaries only; unavailable hidden reasoning is not reconstructed."}


def print_event(event, stream=None):
    stream = stream or sys.stderr
    payload = event["payload"]
    kind = event["kind"]
    if kind == "validation_event":
        kind = payload["branch"]+" · "+payload["kind"]
        payload = payload["payload"]
    stamp = time.strftime("%H:%M:%S", time.localtime(event["timestamp"]))
    detail = " ".join(f"{k}={payload[k]}" for k in ("round", "role", "phase", "tool", "wells", "batch_id", "decision", "error_type", "http_status", "attempt", "delay_seconds") if k in payload)
    try:
        print(f"[{stamp}] {kind} {detail}", file=stream, flush=True)
    except OSError:
        pass  # A closed display must not abort a persisted research operation.


def export_html(directory, destination):
    trace = read_trace(directory)
    data = json.dumps(trace, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>ProteinRSI 研究轨迹</title>
<style>body{font:16px system-ui;max-width:1150px;margin:40px auto;padding:0 20px;background:#f5f7fa;color:#172b4d}input{width:95%;padding:12px}details{background:white;padding:14px;margin:10px 0;border-radius:8px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}small{color:#53657e}</style>
<h1>ProteinRSI 研究轨迹</h1><p id="status"></p><p>按时间回看状态、已知证据、模型实际返回的文字、工具调用和方法验收。展开查看请求与结果；不推测未返回的内部思考。</p>
<input id="filter" placeholder="搜索角色、轮次、事件、序列或决策说明"><main id="timeline"></main>
<details><summary>完整记录：批次、测量、计划及版本验证</summary><pre id="records"></pre></details>
<script id="data" type="application/json">DATA</script><script>
const d=JSON.parse(document.getElementById('data').textContent);
document.getElementById('status').textContent=`来源：${d.source} · 状态：${d.status} · 完成轮数：${d.rounds} · 已用预算：${JSON.stringify(d.spent)}`;
document.getElementById('records').textContent=JSON.stringify(d.records,null,2);
function render(){const q=document.getElementById('filter').value.toLowerCase(),root=document.getElementById('timeline');root.replaceChildren();for(const e of d.events){if(!JSON.stringify(e).toLowerCase().includes(q))continue;const item=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('pre');title.textContent=`#${e.id} ${new Date(e.timestamp*1000).toLocaleString()} · ${e.kind} · ${JSON.stringify(e.payload).slice(0,180)}`;body.textContent=JSON.stringify({payload:e.payload,details:e.details},null,2);item.append(title,body);root.append(item);}}
document.getElementById('filter').addEventListener('input',render);render();</script></html>'''.replace('DATA', data)
    destination = Path(destination)
    destination.write_text(document, encoding="utf-8")
    return str(destination.resolve())


def follow(directory):
    path = (Path(directory)/"state.sqlite3").resolve(strict=True)
    cursor = 0
    while True:
        with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as con:
            events = [{"id": i, "timestamp": t, "kind": k, "payload": json.loads(p)}
                for i, t, k, p in con.execute("SELECT * FROM events WHERE id>? ORDER BY id", (cursor,))]
            row = con.execute("SELECT value FROM kv WHERE namespace='campaign' AND key='state'").fetchone()
        for event in events:
            print_event(event, sys.stdout)
            cursor = event["id"]
        lifecycle = [e for e in events if e['kind'] in {'run_started', 'run_failed', 'campaign_completed'}]
        if (row and json.loads(row[0]).get("status") == "complete") or (lifecycle and lifecycle[-1]['kind'] == 'run_failed'):
            return
        time.sleep(0.5)
