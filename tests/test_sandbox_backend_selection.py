"""Backend-selection/failure plumbing; mocks here are not enforcement evidence."""
from pathlib import Path
import subprocess

import pytest

from proteinrsi.replay import bwrap_backend, sandbox


def test_default_is_landlock_without_automatic_fallback(monkeypatch):
    monkeypatch.delenv('PROTEINRSI_SANDBOX_BACKEND', raising=False)
    monkeypatch.setattr(sandbox, '_probe_landlock', lambda: {'available': False})
    monkeypatch.setattr(bwrap_backend, 'probe_bwrap', lambda: pytest.fail('Implicit fallback'))
    assert sandbox.selected_backend() == 'landlock'
    assert sandbox.generated_profile() == 'landlock_seccomp_generated_v1'
    assert sandbox.backend_identity() == {}
    assert sandbox.probe() == {'available': False}


@pytest.mark.parametrize('choice', ['', 'auto', 'inprocess', 'BWRAP', 'none'])
def test_unknown_explicit_backend_fails_closed(monkeypatch, choice):
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', choice)
    with pytest.raises(sandbox.SandboxUnavailable, match='Unknown'):
        sandbox.probe()


def test_missing_bwrap_is_unavailable_without_fallback(monkeypatch):
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
    monkeypatch.setattr(bwrap_backend.shutil, 'which', lambda name: None)
    monkeypatch.setattr(sandbox, '_probe_landlock', lambda: pytest.fail('Implicit fallback'))
    state = sandbox.probe()
    assert not state['available'] and 'requires bubblewrap' in state['reason']


def test_file_capability_binary_is_rejected_without_execution(monkeypatch):
    if not bwrap_backend.shutil.which('bwrap'):
        pytest.skip('Unit fixture requires an existing bwrap path, not enforcement')
    monkeypatch.setattr(bwrap_backend.os, 'getxattr', lambda *args: b'capability-canary')
    monkeypatch.setattr(bwrap_backend.subprocess, 'run', lambda *a, **k: pytest.fail('Privileged binary executed'))
    with pytest.raises(sandbox.SandboxUnavailable, match='file capabilities'):
        bwrap_backend.identity()


def test_failed_mandatory_setup_is_unavailable_without_fallback(monkeypatch):
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
    monkeypatch.setattr(bwrap_backend, 'identity', lambda: {'bwrap_path': '/usr/bin/bwrap'})
    calls = []
    def failed_launch(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', 'synthetic mandatory mount failure')
    monkeypatch.setattr(bwrap_backend.subprocess, 'run', failed_launch)
    monkeypatch.setattr(sandbox, '_probe_landlock', lambda: pytest.fail('Implicit fallback'))
    state = sandbox.probe()
    assert not state['available'] and 'mandatory mount failure' in state['reason']
    assert len(calls) == 1 and '--remount-ro' in calls[0]
    assert '--unshare-net' not in calls[0] and '--unshare-user-try' not in calls[0]


def test_direct_host_restrict_cannot_use_an_environment_marker(monkeypatch, tmp_path):
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
    with pytest.raises(sandbox.SandboxUnavailable, match='mandatory bwrap'):
        sandbox.restrict([], tmp_path, generated_code=True, backend='bwrap')


def test_command_refuses_runtime_writable_overlap(monkeypatch, tmp_path):
    monkeypatch.setattr(bwrap_backend, 'identity', lambda: {'bwrap_path': '/usr/bin/bwrap'})
    for roots in (['/'], [str(tmp_path)]):
        with pytest.raises(sandbox.SandboxUnavailable, match='outside'):
            bwrap_backend.command(roots, tmp_path, 'unused')


def test_private_campaign_and_labels_rejected_in_allowlist(tmp_path):
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    for private in (runtime/'labels.csv', runtime/'campaign'):
        with pytest.raises(ValueError, match='outside runtime'):
            bwrap_backend.assert_private_paths([private], [runtime])
    bwrap_backend.assert_private_paths([tmp_path/'private'], [runtime])


def test_replay_policy_identity_covers_syscall_policy(monkeypatch):
    if not bwrap_backend.shutil.which('bwrap'):
        pytest.skip('Unit fixture requires existing bwrap identity')
    before = bwrap_backend.identity()
    original = Path.read_bytes
    def changed(path):
        content = original(path)
        return content + b'\n# synthetic policy change' if path.name == 'sandbox.py' else content
    monkeypatch.setattr(Path, 'read_bytes', changed)
    after = bwrap_backend.identity()
    assert after['bwrap_policy_sha256'] != before['bwrap_policy_sha256']
    assert after['bwrap_sha256'] == before['bwrap_sha256']
