# SPDX-License-Identifier: MIT
"""Read-only, offline operator timeline; never opens an assay label table."""
import json
from pathlib import Path
import sqlite3
import sys
import time

from proteinrsi.trace_export import (
    DETAIL_FIELDS, TRACE_NAMESPACES, _atomic_text, _destination, export_compact_html, snapshot,
    export_json as export_json, iter_events as iter_events, write_json as write_json,
    _refs, summary, unwrap_event,
)


def read_trace(directory):
    with snapshot(directory) as (con, path):
        names = sorted(TRACE_NAMESPACES)
        rows = con.execute("SELECT namespace,key,value FROM kv WHERE namespace IN (" +
            ",".join("?" for _ in names) + ") OR (namespace='campaign' AND key='state')", names).fetchall()
        events = [{"id": i, "timestamp": t, "kind": k, "payload": json.loads(p)}
                  for i, t, k, p in con.execute("SELECT * FROM events ORDER BY id")]
        metadata = summary(con, path)
    records = {}
    allowed = TRACE_NAMESPACES
    for ns, key, value in rows:
        if ns in allowed:
            records.setdefault(ns, {})[key] = json.loads(value)
    for event in events:
        _, payload, branches = unwrap_event(event)
        prefix, branch = "validation_" * len(branches), "/".join(branches)
        for field, namespace in DETAIL_FIELDS:
            ref = payload.get(field)
            if not isinstance(ref, str):
                continue
            record = records.get(prefix+namespace, {}).get((branch + "/" if branch else "") + ref)
            if record is not None:
                event.setdefault("details", {})[namespace] = record
    return {**metadata, "events": events, "records": records}


def print_event(event, stream=None):
    stream = stream or sys.stderr
    kind, payload, branches = unwrap_event(event)
    if branches:
        kind = " / ".join(branches) + " · " + kind
    from proteinrsi.roles import role_name
    payload = dict(payload)
    if "role" in payload:
        payload["role"] = role_name(payload["role"]) + " (" + payload["role"] + ")"
    stamp = time.strftime("%H:%M:%S", time.localtime(event["timestamp"]))
    detail = " ".join(f"{k}={payload[k]}" for k in ("round", "role", "phase", "tool", "wells", "batch_id", "decision", "error_type", "http_status", "attempt", "delay_seconds", "context_bytes", "source_bytes", "estimated_input_tokens", "context_actions", "action", "from_version", "to_version") if k in payload)
    try:
        print(f"[{stamp}] {kind} {detail}", file=stream, flush=True)
    except OSError:
        pass  # A closed display must not abort a persisted research operation.


def write_text(directory, stream):
    """Readable flow and actual I/O references, derived from the original audit."""
    with snapshot(directory) as (con, path):
        info = summary(con, path)
        print("ProteinRSI 文字轨迹", file=stream)
        print(f"主研究：{info['rounds']}轮；状态：{info['execution']['state']}；查询："
              f"{info['spent'].get('experimental_wells', 0)}", file=stream)
        row = con.execute("SELECT value FROM kv WHERE namespace='configuration' AND key='research'").fetchone()
        policy = json.loads(row[0]).get("method_policy", "validate") if row else "validate"
        meta_flow = ("轨迹 → Improver → 直接采纳（未经对照）→ 边界发布" if policy == "direct" else
                     "轨迹 → Improver → 冻结对照 → H0 ∥ H1 → Evaluator → 边界发布")
        print("Task：Planner → Executor → Experiment → Analyst → 下一轮\n"
              f"Meta：{meta_flow}\n"
              "输入输出引用指向原始数据库记录；下方文字为实际返回内容的预览。\n", file=stream)
        starts, wait, calls, reads = {}, 0.0, 0, 0
        for event_id, timestamp, kind, payload in con.execute(
                "SELECT id,timestamp,kind,payload FROM events ORDER BY id"):
            event = {"id": event_id, "timestamp": timestamp, "kind": kind, "payload": json.loads(payload)}
            leaf, detail, branches = unwrap_event(event)
            identity = (tuple(branches), detail.get("attempt_key", detail.get("key")))
            if leaf == "llm_started":
                starts[identity] = timestamp
                calls += 1
            elif leaf in {"llm_completed", "llm_failed"}:
                started = starts.pop(identity, None)
                if started is not None:
                    wait += timestamp - started
            elif leaf == "context_read_completed":
                reads += 1
            if branches:
                label = " → ".join("H0" if b.endswith("/baseline") else
                    "H1" if b.endswith("/challenger") else b for b in branches)
            else:
                label = "主研究"
            print(f"[{label}]", end=" ", file=stream)
            print_event({**event, "kind": leaf, "payload": detail}, stream)
            if leaf == "research_state_updated":
                print("  研究状态：" + str(detail.get("research_state_ref")) +
                      "；当前焦点：" + detail.get("working_memory", {}).get("focus", ""), file=stream)
                best = detail.get("best_measured")
                if best:
                    print(f"  最佳已测：{best['sequence']}；{best['value']}", file=stream)
            if leaf == "execution_attempt_failed":
                print("  原步骤执行失败：" + detail.get("error", {}).get("message", "") +
                      "；attempt=" + str(detail.get("attempt")) +
                      "；记录=" + detail.get("call_ref", ""), file=stream)
            elif leaf == "execution_call_repaired":
                print("  Executor局部修复：" + detail.get("reason", "") +
                      "；保留成功上游；记录=" + detail.get("call_ref", ""), file=stream)
            elif leaf == "research_plan_revised":
                print("  当轮计划变化：" + detail.get("reason", "scientific_review") +
                      "（尚未发布W/M方法版本）", file=stream)
            for namespace, stored_ns, key in _refs(con, event_id):
                if namespace not in {"context_frames", "llm_attempts", "tool_jobs", "method_candidates", "research_step_outputs"}:
                    continue
                depth = stored_ns.count("validation_")
                result_path = "$" + ".value" * depth + ".result"
                row = con.execute("SELECT substr(coalesce(json_extract(value,?),"
                    "json_extract(value,'$.result')),1,1000) FROM kv WHERE namespace=? AND key=?",
                    (result_path, stored_ns, key)).fetchone()
                direction = "输入" if namespace == "context_frames" else "输入/输出"
                print(f"  {direction}记录：{stored_ns}/{key}", file=stream)
                if row and row[0] and leaf in {"llm_completed", "tool_completed"}:
                    print("  实际输出预览：" + row[0] + "（完整内容见原记录）", file=stream)
        print(f"\n耗时统计：LLM请求{calls}次；上下文读取动作{reads}次；"
              f"已结束LLM请求累计耗时{wait:.1f}秒（并发请求耗时会重叠）。", file=stream)


def export_text(directory, destination):
    destination = _destination(directory, destination)
    with _atomic_text(destination) as stream:
        write_text(directory, stream)
    return str(destination.resolve())


def _export_full_html(directory, destination):
    destination = _destination(directory, destination)
    trace = read_trace(directory)
    data = json.dumps(trace, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")
    document = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>ProteinRSI 研究轨迹</title>
<style>body{font:16px system-ui;max-width:1150px;margin:40px auto;padding:0 20px;background:#f5f7fa;color:#172b4d}input{width:95%;padding:12px}details{background:white;padding:14px;margin:10px 0;border-radius:8px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}small{color:#53657e}</style>
<h1>ProteinRSI 研究轨迹</h1><p id="status"></p><p>按时间回看状态、已知证据、模型实际返回的文字、工具调用和方法验收。展开查看请求与结果；不推测未返回的内部思考。</p>
<input id="filter" placeholder="搜索角色、轮次、事件、序列或决策说明"><main id="timeline"></main>
<details><summary>完整记录：批次、测量、计划及版本验证</summary><pre id="records"></pre></details>
<script id="data" type="application/json">DATA</script><script>
const d=JSON.parse(document.getElementById('data').textContent);
document.getElementById('status').textContent=`来源：${d.source} · 阶段：${d.status} · 执行：${d.execution?.state ?? "unknown"} · 完成轮数：${d.rounds} · 已用预算：${JSON.stringify(d.spent)}`;
document.getElementById('records').textContent=JSON.stringify(d.records,null,2);
function render(){const q=document.getElementById('filter').value.toLowerCase(),root=document.getElementById('timeline');root.replaceChildren();for(const e of d.events){if(!JSON.stringify(e).toLowerCase().includes(q))continue;const item=document.createElement('details'),title=document.createElement('summary'),body=document.createElement('pre');title.textContent=`#${e.id} ${new Date(e.timestamp*1000).toLocaleString()} · ${e.kind} · ${JSON.stringify(e.payload).slice(0,180)}`;body.textContent=JSON.stringify({payload:e.payload,details:e.details},null,2);item.append(title,body);root.append(item);}}
document.getElementById('filter').addEventListener('input',render);render();</script></html>'''.replace('DATA', data)
    with _atomic_text(destination) as stream:
        stream.write(document)
    return str(destination.resolve())


def export_html(directory, destination, *, full=False, page_size=100, preview_bytes=2048):
    """Default to bounded previews plus exact sidecars; full=True retains legacy HTML."""
    if full:
        return _export_full_html(directory, destination)
    return export_compact_html(directory, destination, page_size=page_size,
                               preview_bytes=preview_bytes)


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
