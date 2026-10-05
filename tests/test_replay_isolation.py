"""Actual kernel enforcement in disposable subprocesses, never mocked as a pass."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from proteinrsi.replay.broker import reader_roots
from proteinrsi.replay.sandbox import probe


def test_worker_filesystem_network_and_legacy_abi_protections(tmp_path):
    if not probe()["available"]:
        pytest.skip("Landlock/libseccomp unavailable")
    private = tmp_path/"hidden-labels.csv"
    private.write_text("PRIVATE TEST LABELS")
    readonly = tmp_path/"readonly-reference.txt"
    readonly.write_text("KEEP THIS CONTENT")
    work = tmp_path/"work"
    work.mkdir()
    script = r'''
import json, os, socket, sys
from pathlib import Path
from proteinrsi.replay.sandbox import restrict
data = json.loads(sys.stdin.readline())
private, readonly, work = map(Path, (data["private"], data["readonly"], data["work"]))
state = restrict(data["roots"], str(work))
assert readonly.read_text() == "KEEP THIS CONTENT"
def denied(name, fn):
    try:
        fn()
    except PermissionError:
        results[name] = True
    else:
        raise AssertionError(name + " was not denied")
results = {}
denied("private_read", private.read_text)
denied("private_truncate", lambda: os.truncate(private, 0))
denied("readonly_truncate", lambda: os.truncate(readonly, 0))
denied("readonly_open_trunc", lambda: os.open(readonly, os.O_RDONLY | os.O_TRUNC))
denied("readonly_write", lambda: os.open(readonly, os.O_WRONLY))
denied("metadata", lambda: os.chmod(readonly, 0o777))
denied("network", socket.socket)
denied("hardlink", lambda: os.link(readonly, work/"link"))
denied("rename", lambda: os.rename(readonly, work/"renamed"))
fd = os.open(work/"scratch", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.write(fd, b"scratch")
denied("ftruncate", lambda: os.ftruncate(fd, 0))
os.close(fd)
assert (work/"scratch").read_text() == "scratch"
print(json.dumps({"denied": results, "abi": state["landlock_abi"]}))
'''
    payload = {"private": str(private), "readonly": str(readonly), "work": str(work),
               "roots": [*reader_roots(), str(readonly)]}
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1]/"src"),
           "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    run = subprocess.run([sys.executable, "-c", script], input=json.dumps(payload)+"\n", text=True,
                         capture_output=True, env=env, cwd=work, timeout=20)
    assert run.returncode == 0, run.stdout+run.stderr
    assert len(json.loads(run.stdout)["denied"]) == 10
    assert private.read_text() == "PRIVATE TEST LABELS"
    assert readonly.read_text() == "KEEP THIS CONTENT"
    assert readonly.stat().st_mode & 0o777 != 0o777


def test_guarded_wrap_preserves_existing_local_tools_without_reregistering(tmp_path):
    from proteinrsi.contracts import TaskSpec
    from proteinrsi.localtools.config import LocalToolsConfig
    from proteinrsi.runtime import Campaign
    from proteinrsi.replay.broker import GuardedTeam
    task = TaskSpec(name='wrap test', reference_sequence='ACDE', mutable_positions=[2])
    campaign = Campaign.initialize(str(tmp_path/'run'), task, local_tools=LocalToolsConfig())
    original = campaign.team
    assert 'protein_sequence_qc' in original.tools._tools
    wrapped = GuardedTeam.from_team(original)
    assert wrapped.tools is original.tools
    assert wrapped.protein_model is original.protein_model
    assert wrapped.guarded


def test_guarded_worker_keeps_task_filtered_tool_catalog(campaign):
    from proteinrsi.replay.sandbox import probe
    from proteinrsi.replay.broker import GuardedTeam
    from proteinrsi.tools import ToolSpec
    if not probe()['available']:
        import pytest
        pytest.skip('Sandbox unavailable')
    spec = ToolSpec(name='binder_only', capability='backbone.generate', implementation_version='test',
        task_kinds=['binder_design'], input_schema={'type': 'object'}, output_schema={'type': 'object'})
    campaign.team.tools.register(spec, lambda args: {})
    view = campaign.view()
    view.workflow.tool_names = ['binder_only']
    guarded = GuardedTeam.from_team(campaign.team)
    assert guarded.run(view)
