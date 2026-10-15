"""Only owned temporary processes are signalled; no scientific requests occur."""
import os
import signal
import subprocess
import sys

import pytest

from proteinrsi.contracts import digest
from proteinrsi.execution_control import pause_campaign, process_identity, resume_campaign
from proteinrsi.governance import _packaged_source
from proteinrsi.storage import Store


@pytest.mark.core
def test_pause_resume_includes_background_descendants(tmp_path):
    source = ("import subprocess,sys,time; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
              "print(p.pid,flush=True); time.sleep(30)")
    process = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE,
                               text=True, start_new_session=True)
    child = int(process.stdout.readline())
    store = Store(tmp_path)
    store.put('execution_control', 'controller', {**process_identity(process.pid),
        'running': True, 'source_ref': digest(_packaged_source())})
    try:
        paused = pause_campaign(tmp_path)
        assert {p['pid'] for p in paused['processes']} == {process.pid, child}
        assert all(process_identity(pid)['state'] == 'T' for pid in (process.pid, child))
        resumed = resume_campaign(tmp_path)
        assert resumed['resumed_processes'] == 2
    finally:
        for pid in (child, process.pid):
            try:
                os.kill(pid, signal.SIGCONT)
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
