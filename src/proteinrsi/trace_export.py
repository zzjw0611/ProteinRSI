# SPDX-License-Identifier: MIT
"""Bounded offline trace presentation and lossless streaming audit exports.

Only the same operator namespaces as read_trace are exported. A compact bundle
contains previews, never replacement scientific evidence. Exact UTF-8 SQLite JSON
cells are gzip-compressed and addressed by the SHA-256 of their uncompressed bytes.
"""
from __future__ import annotations

import codecs
import errno
from contextlib import closing, contextmanager
import gzip
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.parse import quote


TRACE_NAMESPACES = frozenset({
    "llm_attempts", "validation_llm_attempts", "llm", "tool_jobs", "agent_snapshots",
    "llm_preflights", "validation_llm_preflights",
    "feedback_inputs", "feedback_results", "validation_feedback_inputs", "validation_feedback_results",
    "validation_llm", "validation_tool_jobs", "validation_agent_snapshots", "patches",
    "trials", "trial_results", "meta_evaluations", "meta_online_attempts", "research_runs",
    "research_step_outputs", "batches", "measurements", "validation_research_runs",
    "validation_research_step_outputs", "computational_iterations", "code_programs",
    "validation_code_programs", "plate_plans", "workflow_validation_outcomes",
    "method_candidates", "method_candidate_states", "method_transitions", "method_switches",
    "method_snapshots", "method_activations", "method_deferrals", "method_proposal_failures",
    "batch_method_bindings", "gepa_attempts", "gepa_results", "gepa_failures",
})
DETAIL_FIELDS = (
    ("attempt_key", "llm_attempts"), ("key", "llm"), ("key", "tool_jobs"),
    ("preflight_key", "llm_preflights"),
    ("feedback_input_ref", "feedback_inputs"), ("feedback_ref", "feedback_results"),
    ("snapshot_ref", "agent_snapshots"), ("run_id", "research_runs"),
    ("output_id", "research_step_outputs"), ("snapshot_ref", "method_snapshots"),
    ("patch_id", "method_candidates"), ("archive_ref", "gepa_results"),
)
TRACE_NOTE = ("Model-returned text and decision summaries only; unavailable hidden "
              "reasoning is not reconstructed.")
CHUNK_BYTES = 64 * 1024
DEFAULT_PAGE_SIZE = 100
DEFAULT_PREVIEW_BYTES = 2048


@contextmanager
def snapshot(directory):
    path = (Path(directory) / "state.sqlite3").resolve(strict=True)
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        yield con, path
    finally:
        con.close()


def summary(con, path):
    # Extract just these fields in SQLite, not the growing state/context into Python.
    row = con.execute("""SELECT json_extract(value,'$.status'),
        json_extract(value,'$.round_index'), json_extract(value,'$.task.execution_mode'),
        json_extract(value,'$.task.feedback_source')
        FROM kv WHERE namespace='campaign' AND key='state'""").fetchone()
    status, rounds, mode, source = row or (None, None, None, None)
    limits = dict(con.execute("SELECT resource,amount FROM limits"))
    totals = dict(con.execute("SELECT resource,SUM(amount) FROM charges "
                              "WHERE state='committed' GROUP BY resource"))
    return {"campaign": str(path.parent), "status": status, "rounds": rounds,
            "source": "computational" if mode == "computational" else source,
            "limits": limits, "spent": {key: totals.get(key, 0) for key in limits},
            "note": TRACE_NOTE}


def _records(con):
    names = sorted(TRACE_NAMESPACES)
    return con.execute("SELECT rowid,namespace,key FROM kv WHERE namespace IN (" +
                       ",".join("?" for _ in names) + ") ORDER BY namespace,key", names)


def _refs(con, event_id):
    # Return only small reference fields; do not deserialize the event's entire payload.
    fields = ["branch"] + list(dict.fromkeys(field for field, _ in DETAIL_FIELDS))
    expr = ",".join("json_extract(payload,?)" for _ in fields)
    kind = con.execute("SELECT kind FROM events WHERE id=?", (event_id,)).fetchone()[0]
    validation = kind == "validation_event"
    paths = ["$.branch"] + [("$.payload." if validation else "$.") + f for f in fields[1:]]
    row = con.execute(f"SELECT {expr} FROM events WHERE id=?", (*paths, event_id)).fetchone()
    values = dict(zip(fields, row))
    prefix = "validation_" if validation else ""
    branch = values["branch"] + "/" if validation else ""
    for field, namespace in DETAIL_FIELDS:
        ref = values[field]
        if isinstance(ref, str):
            yield namespace, prefix + namespace, branch + ref


def iter_events(directory):
    """Iterate event payloads only, without reading or expanding linked record contexts."""
    with snapshot(directory) as (con, _):
        for i, t, k, p in con.execute("SELECT id,timestamp,kind,payload FROM events ORDER BY id"):
            yield {"id": i, "timestamp": t, "kind": k, "payload": json.loads(p)}


def _copy_json(con, table, column, rowid, stream):
    decoder = codecs.getincrementaldecoder("utf-8")()
    with con.blobopen(table, column, rowid, readonly=True) as blob:
        while chunk := blob.read(CHUNK_BYTES):
            stream.write(decoder.decode(chunk))
        stream.write(decoder.decode(b"", final=True))


def write_json(directory, stream):
    """Stream the legacy full read_trace schema, including repeated event details.

    Memory is bounded by a chunk and small metadata, not the total trace or any
    individual record. The output can still be very large; use compact HTML for review.
    A caller-owned stream can be partial on error. export_json publishes atomically.
    """
    with snapshot(directory) as (con, path):
        head = json.dumps(summary(con, path), ensure_ascii=False)
        stream.write(head[:-1] + ',"events":[')
        sep = ""
        for i, t, k in con.execute("SELECT id,timestamp,kind FROM events ORDER BY id"):
            stream.write(sep + json.dumps({"id": i, "timestamp": t, "kind": k},
                                         ensure_ascii=False)[:-1] + ',"payload":')
            _copy_json(con, "events", "payload", i, stream)
            detail_sep = ',"details":{'
            found = False
            for name, namespace, key in _refs(con, i):
                row = con.execute("SELECT rowid FROM kv WHERE namespace=? AND key=?",
                                  (namespace, key)).fetchone()
                if row is not None:
                    stream.write(detail_sep + json.dumps(name) + ":")
                    _copy_json(con, "kv", "value", row[0], stream)
                    detail_sep, found = ",", True
            if found:
                stream.write("}")
            stream.write("}")
            sep = ","
        stream.write('],"records":{')
        namespace = None
        for rowid, ns, key in _records(con):
            if ns != namespace:
                stream.write(("}," if namespace is not None else "") + json.dumps(ns) + ":{")
                namespace, sep = ns, ""
            stream.write(sep + json.dumps(key, ensure_ascii=False) + ":")
            _copy_json(con, "kv", "value", rowid, stream)
            sep = ","
        stream.write(("}" if namespace is not None else "") + "}}\n")


def _destination(directory, destination):
    destination = Path(destination).absolute()
    source = (Path(directory) / "state.sqlite3").resolve(strict=True)
    if destination.resolve() in {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}:
        raise ValueError("A trace export must not replace its source database")
    return destination


@contextmanager
def _atomic_text(destination):
    fd, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp",
                                dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def export_json(directory, destination):
    """Write legacy full JSON (or .json.gz) without holding the trace in memory."""
    destination = _destination(directory, destination)
    if destination.suffix == ".gz":
        fd, name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp",
                                    dir=destination.parent)
        os.close(fd)
        try:
            with gzip.open(name, "wt", encoding="utf-8") as stream:
                write_json(directory, stream)
            os.replace(name, destination)
        finally:
            Path(name).unlink(missing_ok=True)
    else:
        with _atomic_text(destination) as stream:
            write_json(directory, stream)
    return str(destination.resolve())


def _evidence(con, table, column, rowid, objects, preview_bytes):
    """Read a SQLite cell in fixed chunks, deduplicating exact byte-identical cells."""
    digest = hashlib.sha256()
    first = bytearray()
    last = b""
    size = 0
    temporary = objects / "pending.gz"
    with con.blobopen(table, column, rowid, readonly=True) as blob, \
            temporary.open("wb") as raw, \
            gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0, compresslevel=6) as compressed:
        while chunk := blob.read(CHUNK_BYTES):
            digest.update(chunk)
            compressed.write(chunk)
            size += len(chunk)
            first.extend(chunk[:max(0, preview_bytes - len(first))])
            last = (last + chunk)[-preview_bytes:]
    sha = digest.hexdigest()
    target = objects / (sha + ".json.gz")
    if target.exists():
        temporary.unlink()
    else:
        temporary.rename(target)
    # Previews may split a UTF-8 codepoint; the exact evidence never does.
    truncated = size > preview_bytes
    preview = first.decode("utf-8", errors="replace")
    if truncated:
        half = preview_bytes // 2
        preview = (bytes(first[:half]).decode("utf-8", errors="replace") +
                   "\n[… preview omitted bytes; use exact evidence …]\n" +
                   last[-half:].decode("utf-8", errors="replace"))
    return {"sha256": sha, "bytes": size, "file": "objects/" + target.name}, preview, truncated


def _escape(value):
    return html.escape(str(value), quote=True)


def _link(url, title, *, download=False):
    return ('<a href="' + _escape(url) + '"' + (' download' if download else '') +
            '>' + _escape(title) + '</a>')


_STYLE = """body{font:16px system-ui;max-width:1100px;margin:36px auto;padding:0 20px;
background:#f5f7fa;color:#172b4d}a{color:#174b95}details{background:white;padding:14px;
margin:10px 0;border-radius:8px}summary{cursor:pointer;overflow-wrap:anywhere}pre{white-space:
pre-wrap;overflow-wrap:anywhere;font-size:13px}small{color:#42546a}nav{display:flex;gap:20px;
flex-wrap:wrap;margin:20px 0}input{padding:8px}label{display:block;margin:12px 0}
.notice{background:#fff4cf;padding:14px;border-radius:8px}p{overflow-wrap:anywhere}"""


def _document(title, body, script=""):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; '
            'form-action \'none\'">'
            '<title>' + _escape(title) + '</title><style>' + _STYLE + '</style></head><body>' +
            '<h1>' + _escape(title) + '</h1>' + body +
            ('<script>' + script + '</script>' if script else '') + '</body></html>')


def _page_navigation(section, number, count, home):
    links = [_link(home, "Overview"), _link("events-1.html", "Events"),
             _link("records-1.html", "Records")]
    if number > 1:
        links.append(_link(f"{section}-{number - 1}.html", "Previous"))
    if number < count:
        links.append(_link(f"{section}-{number + 1}.html", "Next"))
    return ('<nav>' + " ".join(links) + '</nav><p>Page ' + str(number) + ' of ' + str(count) +
            '</p><label>Jump to page <input id="page" type="number" min="1" max="' +
            str(count) + '" value="' + str(number) + '"> <button id="go">Go</button></label>')


def _write_page(path, section, number, count, home, cards):
    body = (_page_navigation(section, number, count, home) +
            '<p class="notice">Bounded previews only. Long JSON is explicitly abbreviated. '
            'Exact uncompressed byte counts and SHA-256 hashes identify the full .json.gz evidence. '
            'Decompress a linked file to inspect its complete original JSON.</p>'
            '<label>Filter this page’s previews only <input id="filter" type="search"></label>'
            '<p id="matches" role="status"></p><main>' + "".join(cards) + '</main>' +
            _page_navigation(section, number, count, home).split('<label>')[0])
    # No data is inserted into executable JavaScript. section/count are controlled constants.
    script = """const input=document.getElementById('filter');
input.addEventListener('input',()=>{let count=0;const q=input.value.toLowerCase();
for(const card of document.querySelectorAll('main>details')){
card.hidden=!card.textContent.toLowerCase().includes(q);if(!card.hidden)count++;}
document.getElementById('matches').textContent=`${count} matches on this page only`;});
document.getElementById('go').addEventListener('click',()=>{
const n=Number(document.getElementById('page').value);
if(Number.isInteger(n)&&n>=1&&n<=COUNT)location.href='SECTION-'+n+'.html';});"""
    script = script.replace("COUNT", str(count)).replace("SECTION", section)
    path.write_text(_document("ProteinRSI " + section, body, script), encoding="utf-8")


def _card(title, entry, preview, truncated, links="", anchor=""):
    return ('<details' + (' id="' + _escape(anchor) + '"' if anchor else '') +
            '><summary>' + _escape(title[:512]) + '</summary><p>' +
            ('TRUNCATED PREVIEW' if truncated else 'Complete JSON preview') + ' · ' +
            str(entry["bytes"]) + ' source bytes · ' +
            _link(entry["file"], "Exact JSON (.gz)", download=True) + '</p><small>SHA-256: ' +
            entry["sha256"] + '</small>' + links + '<pre>' + _escape(preview) + '</pre></details>')


def _verify_existing_bundle(staging, existing):
    """Never reuse partial/corrupt evidence or replace a previously published bundle."""
    if existing.is_symlink() or not existing.is_dir():
        raise ValueError("Existing trace bundle is unsafe; export to a new output name")
    for source in staging.rglob("*"):
        target = existing / source.relative_to(staging)
        if target.is_symlink() or (source.is_dir() and not target.is_dir()):
            raise ValueError("Existing trace bundle is unsafe; export to a new output name")
        if source.is_file():
            if not target.is_file() or source.stat().st_size != target.stat().st_size:
                raise ValueError("Existing trace bundle is incomplete; export to a new output name")
            with source.open("rb") as left, target.open("rb") as right:
                while chunk := left.read(CHUNK_BYTES):
                    if chunk != right.read(CHUNK_BYTES):
                        raise ValueError("Existing trace bundle differs; export to a new output name")


def export_compact_html(directory, destination, *, page_size=DEFAULT_PAGE_SIZE,
                        preview_bytes=DEFAULT_PREVIEW_BYTES):
    """Publish a small index last, after all bounded pages and exact evidence exist.

    Export uses a consistent read-only transaction. Staging is removed on failure;
    a prior index and bundle survive failed/repeated exports. A completed bundle
    survives a failed overview publication, so a retry can reuse it safely.
    Identical snapshots reuse a verified content-addressed bundle. Older changed
    snapshots are intentionally retained so existing links stay valid.
    """
    if type(page_size) is not int or not 1 <= page_size <= 200:
        raise ValueError("page_size must be between 1 and 200")
    if type(preview_bytes) is not int or not 128 <= preview_bytes <= 8192:
        raise ValueError("preview_bytes must be between 128 and 8192")
    destination = _destination(directory, destination)
    assets = None
    staging = Path(tempfile.mkdtemp(prefix=".trace-", dir=destination.parent))
    try:
        objects = staging / "objects"
        objects.mkdir()
        with snapshot(directory) as (con, path), \
                closing(sqlite3.connect(staging / ".index.sqlite3")) as index, \
                (staging / "manifest.jsonl").open("w", encoding="utf-8") as manifest:
            index.execute("CREATE TABLE refs(namespace,key,page,anchor,PRIMARY KEY(namespace,key))")
            metadata = summary(con, path)
            record_count = con.execute("SELECT COUNT(*) FROM kv WHERE namespace IN (" +
                ",".join("?" for _ in TRACE_NAMESPACES) + ")", sorted(TRACE_NAMESPACES)).fetchone()[0]
            event_count = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            counts = {"records": record_count, "events": event_count}
            metadata.update(format="proteinrsi-compact-trace-v1", preview_only=True,
                            page_size=page_size, preview_bytes=preview_bytes, counts=counts,
                            evidence="manifest.jsonl", snapshot_max_event_id=con.execute(
                                "SELECT MAX(id) FROM events").fetchone()[0])
            home = "../" + quote(destination.name, safe="")
            def save_entry(entry):
                manifest.write(json.dumps(entry, ensure_ascii=False) + "\n")
            state = con.execute("SELECT rowid FROM kv WHERE namespace='campaign' AND key='state'").fetchone()
            if state:
                evidence, _, _ = _evidence(con, "kv", "value", state[0], objects, preview_bytes)
                save_entry({"type": "state", "namespace": "campaign", "key": "state", **evidence})
            for section in ("records", "events"):
                page_count = max(1, (counts[section] + page_size - 1) // page_size)
                rows = (_records(con) if section == "records" else con.execute(
                    "SELECT id,timestamp,kind FROM events ORDER BY id"))
                cards = []
                number = 1
                for ordinal, (rowid, a, b) in enumerate(rows):
                    table, column = ("kv", "value") if section == "records" else ("events", "payload")
                    evidence, preview, truncated = _evidence(con, table, column, rowid, objects, preview_bytes)
                    anchor, links = "entry-" + str(ordinal), ""
                    if section == "records":
                        entry = {"type": "record", "namespace": a, "key": b, **evidence}
                        title = a + " / " + b
                        index.execute("INSERT INTO refs VALUES (?,?,?,?)", (a, b, number, anchor))
                    else:
                        entry = {"type": "event", "id": rowid, "timestamp": a, "kind": b, **evidence}
                        title = f"#{rowid} · {b} · timestamp {a}"
                        details = {}
                        for name, ns, key in _refs(con, rowid):
                            ref = index.execute("SELECT page,anchor FROM refs WHERE namespace=? AND key=?",
                                                (ns, key)).fetchone()
                            if ref:
                                target = f"records-{ref[0]}.html#{ref[1]}"
                                details[name] = {"namespace": ns, "key": key, "page": target}
                                links += _link(target, name) + " "
                        if details:
                            entry["details"] = details
                            links = "<p>Linked full records: " + links + "</p>"
                    save_entry(entry)
                    cards.append(_card(title, evidence, preview, truncated, links, anchor))
                    if len(cards) == page_size:
                        _write_page(staging / f"{section}-{number}.html", section, number,
                                    page_count, home, cards)
                        cards = []
                        number += 1
                if cards or counts[section] == 0:
                    _write_page(staging / f"{section}-{number}.html", section, number,
                                page_count, home, cards)
        (staging / ".index.sqlite3").unlink()
        manifest_hash = hashlib.sha256()
        with (staging / "manifest.jsonl").open("rb") as stream:
            while chunk := stream.read(CHUNK_BYTES):
                manifest_hash.update(chunk)
        metadata["manifest_sha256"] = manifest_hash.hexdigest()
        metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        (staging / "metadata.json").write_text(metadata_json, encoding="utf-8")
        bundle_hash = hashlib.sha256(metadata_json.encode("utf-8")).hexdigest()
        assets = destination.with_name(destination.name + ".assets-" + bundle_hash)
        prefix = quote(assets.name, safe="") + "/"
        body = ('<p>' + _escape(f"Source: {metadata['source']} · Status: {metadata['status']} · "
                f"Completed rounds: {metadata['rounds']} · Spent: {metadata['spent']}") + '</p>'
                '<p class="notice">Compact preview, not the full record. All ' + str(event_count) +
                ' events and ' + str(record_count) + ' selected operator records are indexed. '
                'Pages show at most ' + str(page_size) + ' entries and ' + str(preview_bytes) +
                ' source bytes per preview. Linked gzip files preserve exact original JSON bytes; '
                'SHA-256 is over the decompressed bytes. No hidden reasoning is reconstructed.</p>'
                '<nav>' + _link(prefix + "events-1.html", "Browse events") +
                _link(prefix + "records-1.html", "Browse records") + '</nav><p>' +
                _link(prefix + "manifest.jsonl", "Complete evidence index (JSONL)", download=True) +
                ' · ' + _link(prefix + "metadata.json", "Snapshot metadata", download=True) + '</p>'
                '<p>Keep this HTML and its adjacent assets folder together. The viewer is offline '
                'and uses no external scripts. Page filters search previews on that page only, '
                'not omitted full evidence. Decompress .json.gz files to inspect full records.</p>'
                '<p>For a single complete legacy JSON export: proteinrsi trace --campaign RUN '
                '--format json --out trace.json.gz. For the unbounded legacy inline HTML: '
                '--format html --full. The source database is unchanged.</p>'
                '<p>Manifest SHA-256: ' + metadata["manifest_sha256"] + '</p>')
        if assets.exists():
            _verify_existing_bundle(staging, assets)
        else:
            try:
                staging.rename(assets)
            except OSError as exc:
                # Another exporter may have published the identical immutable bundle.
                if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                _verify_existing_bundle(staging, assets)
        with _atomic_text(destination) as stream:
            stream.write(_document("ProteinRSI research trajectory", body))
        return str(destination.resolve())
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        # A finalized immutable bundle may already be in use by another exporter.
        # If overview publication fails, retain it for the next identical retry.
