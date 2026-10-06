"""Artificial audit fixtures; these tests never query scientific landscape labels."""
import gzip
import hashlib
from html.parser import HTMLParser
import io
import json
import sqlite3
import tracemalloc
from urllib.parse import unquote, urlsplit

import pytest

from proteinrsi.cli import main
from proteinrsi.storage import Store
from proteinrsi.trajectory import export_html, export_json, read_trace, write_json
import proteinrsi.trace_export as export


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "campaign")
    store.configure_budget({"llm_calls": 10})
    store.put("campaign", "state", {"status": "running", "round_index": 2,
                                   "task": {"feedback_source": "synthetic"}})
    record = {"request": {"context": {"text": "known evidence 雪 🧬"}}, "result": {"ok": True}}
    store.put("llm", "call", record)
    store.put("llm_attempts", "call/attempt-1", record)
    store.put("validation_llm", "branch/call", record)
    store.put("validation_llm_attempts", "branch/call/attempt-1", record)
    store.put("unselected_private_namespace", "not-exported", {"sentinel": "DO_NOT_EXPORT"})
    store.event("llm_completed", {"key": "call", "attempt_key": "call/attempt-1"})
    store.event("validation_event", {"branch": "branch", "kind": "llm_completed",
                "payload": {"key": "call", "attempt_key": "call/attempt-1"}})
    return store


def bundle(path):
    return next(path.parent.glob(path.name + ".assets-*"))


def entries(assets):
    return [json.loads(line) for line in (assets / "manifest.jsonl").read_text().splitlines()]


def test_streamed_json_matches_legacy_and_compressed_cli(store, tmp_path, capsys):
    expected = read_trace(store.root)
    stream = io.StringIO()
    write_json(store.root, stream)
    assert json.loads(stream.getvalue()) == expected
    for name in ("trace.json", "trace.json.gz"):
        target = tmp_path / name
        main(["trace", "--campaign", str(store.root), "--format", "json", "--out", str(target)])
        raw = gzip.decompress(target.read_bytes()) if name.endswith(".gz") else target.read_bytes()
        assert json.loads(raw) == expected
    main(["trace", "--campaign", str(store.root), "--format", "json"])
    assert json.loads(capsys.readouterr().out) == expected


def test_compact_evidence_reconstructs_original_cells_and_linked_records(store, tmp_path):
    path = tmp_path / "trace.html"
    export_html(store.root, path, page_size=2)
    assets = bundle(path)
    manifest = entries(assets)
    metadata = json.loads((assets / "metadata.json").read_text())
    assert metadata["preview_only"] is True
    assert metadata["manifest_sha256"] == hashlib.sha256((assets / "manifest.jsonl").read_bytes()).hexdigest()
    assert metadata["counts"] == {"records": 4, "events": 2}
    # The identical llm, attempts and validation evidence use one exact shared object.
    records = [e for e in manifest if e["type"] == "record"]
    assert len({e["sha256"] for e in records}) == 1
    assert all("DO_NOT_EXPORT" not in p.read_text() for p in assets.glob("*.html"))
    with sqlite3.connect(store.path) as con:
        for entry in manifest:
            raw = gzip.decompress((assets / entry["file"]).read_bytes())
            assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
            assert len(raw) == entry["bytes"]
            if entry["type"] == "event":
                original = con.execute("SELECT payload FROM events WHERE id=?", (entry["id"],)).fetchone()[0]
                assert set(entry["details"]) == {"llm", "llm_attempts"}
                for ref in entry["details"].values():
                    linked = next(e for e in records if (e["namespace"], e["key"]) ==
                                  (ref["namespace"], ref["key"]))
                    assert json.loads(gzip.decompress((assets / linked["file"]).read_bytes()))["result"] == {"ok": True}
                    page, anchor = ref["page"].split("#")
                    assert f'id="{anchor}"' in (assets / page).read_text()
            else:
                original = con.execute("SELECT value FROM kv WHERE namespace=? AND key=?",
                                       (entry["namespace"], entry["key"])).fetchone()[0]
            assert raw == original.encode("utf-8")


def test_exact_whitespace_unicode_and_chunk_boundaries(store, tmp_path, monkeypatch):
    # Store.put canonicalizes JSON; older/raw SQLite JSON formatting is still exact evidence.
    original = '{ "notes" : "🧬雪\\n' + 'x' * 400 + '" }\n'
    with sqlite3.connect(store.path) as con:
        con.execute("UPDATE kv SET value=? WHERE namespace='llm'", (original,))
    monkeypatch.setattr(export, "CHUNK_BYTES", 7)
    path = tmp_path / "trace.html"
    export_html(store.root, path, preview_bytes=128)
    assets = bundle(path)
    entry = next(e for e in entries(assets) if e.get("namespace") == "llm")
    assert gzip.decompress((assets / entry["file"]).read_bytes()) == original.encode()
    stream = io.StringIO()
    write_json(store.root, stream)
    assert json.loads(stream.getvalue()) == read_trace(store.root)
    assert "TRUNCATED PREVIEW" in (assets / "records-1.html").read_text()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self.links.extend(value for key, value in attrs if key == "href")


def test_html_escaping_all_pages_and_encoded_local_navigation(store, tmp_path):
    attack = '</script><script>alert(1)</script><img src=x onerror="alert(2)">'
    store.put("llm", "evil-" + attack, {"notes": attack})
    store.event(attack, {"text": attack})
    path = tmp_path / 'trace " # & 雪.html'
    export_html(store.root, path, page_size=2)
    assets = bundle(path)
    for page in [path, *assets.glob("*.html")]:
        text = page.read_text()
        assert attack not in text
        parser = Links()
        parser.feed(text)
        assert not any(tag == "img" for tag, _ in parser.tags)
        assert not any(key.startswith("on") for _, attrs in parser.tags for key in attrs)
        assert not any(tag == "script" and "src" in attrs for tag, attrs in parser.tags)
        for link in parser.links:
            parts = urlsplit(link)
            assert not parts.scheme and not parts.netloc
            assert (page.parent / unquote(parts.path)).is_file()
    assert "&lt;script&gt;" in (assets / "events-2.html").read_text()


def test_large_repeated_records_bound_python_memory_pages_and_deduplicate(store, tmp_path, monkeypatch):
    # A 32 MiB audit with repeated contexts must not be assembled into a Python trace.
    payload = {"request": {"context": "abcdefghijklmnopqrstuvwxyz雪" * 40000}}
    for i in range(28):
        store.put("llm", f"large-{i}", payload)
        store.event("llm_completed", {"key": f"large-{i}"})
    del payload
    monkeypatch.setattr("proteinrsi.trajectory.read_trace", lambda *_: pytest.fail("eager trace read"))
    path = tmp_path / "large.html"
    tracemalloc.start()
    export_html(store.root, path, page_size=5, preview_bytes=1024)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assets = bundle(path)
    assert peak < 5 * 1024 * 1024
    assert path.stat().st_size < 10_000
    assert max(p.stat().st_size for p in assets.glob("*.html")) < 32_000
    large = [e for e in entries(assets) if e.get("key", "").startswith("large-")]
    assert len(large) == 28
    assert len({e["sha256"] for e in large}) == 1
    # Output streaming also must not materialize linked full JSON or call read_trace.
    class CountingSink:
        total = 0
        largest = 0
        def write(self, text):
            self.total += len(text)
            self.largest = max(self.largest, len(text))
    sink = CountingSink()
    tracemalloc.start()
    write_json(store.root, sink)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sink.total > 50_000_000
    assert sink.largest <= export.CHUNK_BYTES
    assert peak < 5 * 1024 * 1024


@pytest.mark.parametrize("failure", ["evidence", "page", "publish"])
def test_failed_export_preserves_previous_bundle_and_cleans_its_temporary_files(store, tmp_path, monkeypatch, failure):
    path = tmp_path / "trace.html"
    export_html(store.root, path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*")
              if p.is_file() and "campaign" not in p.parts}
    def fail(*args, **kwargs):
        raise OSError("artificial interruption or disk full")
    if failure == "evidence":
        monkeypatch.setattr(export, "_evidence", fail)
    elif failure == "page":
        monkeypatch.setattr(export, "_write_page", fail)
    else:
        monkeypatch.setattr(export.os, "replace", fail)
    with pytest.raises(OSError, match="artificial"):
        export_html(store.root, path)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*")
             if p.is_file() and "campaign" not in p.parts}
    assert after == before
    assert not list(tmp_path.glob(".trace-*"))


@pytest.mark.parametrize("suffix", [".json", ".json.gz"])
def test_interrupted_full_json_preserves_previous_output(store, tmp_path, monkeypatch, suffix):
    path = tmp_path / ("trace" + suffix)
    path.write_bytes(b"previous exact output")
    def fail(directory, stream):
        stream.write('{"partial":')
        raise RuntimeError("interrupted")
    monkeypatch.setattr(export, "write_json", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        export_json(store.root, path)
    assert path.read_bytes() == b"previous exact output"
    assert not list(tmp_path.glob("*.tmp"))


def test_read_snapshot_and_export_do_not_write_to_source(store, tmp_path):
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    first = tmp_path / "trace.html"
    export_html(store.root, first)
    export_json(store.root, tmp_path / "trace.json")
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="source database"):
        export_html(store.root, store.path)
    with pytest.raises(ValueError, match="source database"):
        export_json(store.root, store.path)


def test_empty_trace_and_small_legacy_inline_html(tmp_path):
    store = Store(tmp_path / "empty")
    path = tmp_path / "empty.html"
    export_html(store.root, path)
    assets = bundle(path)
    assert json.loads((assets / "metadata.json").read_text())["counts"] == {"events": 0, "records": 0}
    assert (assets / "events-1.html").is_file()
    assert (assets / "records-1.html").is_file()
    stream = io.StringIO()
    write_json(store.root, stream)
    assert json.loads(stream.getvalue()) == read_trace(store.root)
    legacy = tmp_path / "legacy.html"
    export_html(store.root, legacy, full=True)
    assert '<script id="data"' in legacy.read_text()
    assert not list(tmp_path.glob("legacy.html.assets-*"))


def test_snapshot_consistency_during_export(store, tmp_path, monkeypatch):
    original = export._evidence
    written = False
    def during_export(*args, **kwargs):
        nonlocal written
        if not written:
            written = True
            store.event("arrived_after_snapshot", {})
            store.put("llm", "arrived_after_snapshot", {"fresh": True})
        return original(*args, **kwargs)
    monkeypatch.setattr(export, "_evidence", during_export)
    path = tmp_path / "snapshot.html"
    export_html(store.root, path)
    manifest = entries(bundle(path))
    assert not any(e.get("kind") == "arrived_after_snapshot" or e.get("key") == "arrived_after_snapshot"
                   for e in manifest)
    assert len(store.events()) == 3


def test_cli_text_does_not_read_linked_contexts(store, monkeypatch, capsys):
    monkeypatch.setattr("proteinrsi.trajectory.read_trace", lambda *_: pytest.fail("eager trace read"))
    main(["trace", "--campaign", str(store.root)])
    text = capsys.readouterr().out
    assert "llm_completed" in text
    assert "known evidence" not in text


def test_identical_snapshot_reuses_bundle_without_disk_growth(store, tmp_path):
    path = tmp_path / "trace.html"
    export_html(store.root, path)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*")
              if p.is_file() and "campaign" not in p.parts}
    export_html(store.root, path)
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*")
             if p.is_file() and "campaign" not in p.parts}
    assert after == before
    assert len(list(tmp_path.glob("trace.html.assets-*"))) == 1
    store.event("new_snapshot", {})
    export_html(store.root, path)
    assert len(list(tmp_path.glob("trace.html.assets-*"))) == 2
    assert all((tmp_path / name).read_bytes() == raw for name, raw in before.items()
               if name != "trace.html")


def test_corrupted_existing_bundle_is_not_reused_or_overwritten(store, tmp_path):
    path = tmp_path / "trace.html"
    export_html(store.root, path)
    original = path.read_bytes()
    evidence = next((bundle(path) / "objects").glob("*.gz"))
    evidence.write_bytes(b"corruption")
    with pytest.raises(ValueError, match="Existing trace bundle"):
        export_html(store.root, path)
    assert evidence.read_bytes() == b"corruption"
    assert path.read_bytes() == original
    assert len(list(tmp_path.glob("trace.html.assets-*"))) == 1
    assert not list(tmp_path.glob(".trace-*"))


def test_full_html_cannot_replace_database_and_text_rejects_full(store):
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="source database"):
        export_html(store.root, store.path, full=True)
    assert store.path.read_bytes() == before
    with pytest.raises(SystemExit) as exc:
        main(["trace", "--campaign", str(store.root), "--format", "text", "--full"])
    assert exc.value.code == 2


def test_failed_initial_overview_publication_retries_complete_bundle(store, tmp_path, monkeypatch):
    path = tmp_path / "trace.html"
    replace = export.os.replace
    def fail(*args, **kwargs):
        raise OSError("overview unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(export.os, "replace", fail)
        with pytest.raises(OSError, match="overview unavailable"):
            export_html(store.root, path)
    assert export.os.replace is replace
    assert not path.exists()
    assets = bundle(path)
    assert (assets / "metadata.json").is_file()
    export_html(store.root, path)
    assert path.is_file()
    assert list(tmp_path.glob("trace.html.assets-*")) == [assets]
    assert not list(tmp_path.glob(".trace-*"))
