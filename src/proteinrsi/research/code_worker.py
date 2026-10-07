# SPDX-License-Identifier: MIT
"""Disposable Python process: generated code has no RPC, keys, oracle or database."""
import contextlib
import ctypes
import io
import json
import os
from pathlib import Path
import resource
import signal
import sys


class BoundedText(io.StringIO):
    def write(self, text):
        if self.tell() + len(text) > 16000:
            raise ValueError("Code log exceeds 16000 characters")
        return super().write(text)


def main():
    request = json.loads(sys.stdin.readline(4 * 1024 * 1024))
    # Kernel-enforced lifetime: a killed controller cannot orphan an idle worker.
    # The later generated-code seccomp profile blocks prctl to prevent disabling it.
    libc = ctypes.CDLL(None, use_errno=True)
    if (sys.platform != "linux" or libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0
            or os.getppid() != request.get("parent_pid")):
        raise RuntimeError("Cannot bind generated worker lifetime to its controller")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    from proteinrsi.replay.sandbox import restrict
    restrict(request['read_roots'], request['work'], generated_code=True)
    logs = BoundedText()
    def write_artifact(name, content, kind='json'):
        if Path(name).name != name or kind not in {'pdb', 'cif', 'a3m', 'fasta', 'json'}:
            raise ValueError('Use a simple output filename and supported scientific kind')
        if not isinstance(content, str) or len(content.encode()) > 400000:
            raise ValueError('Scientific text output exceeds 400KB')
        return {'name': name, 'kind': kind, 'content': content}
    scope = {'__name__': '__generated_research__', 'context': request['context'],
        'inputs': request['inputs'], 'artifacts': request['artifacts'], 'write_artifact': write_artifact}
    try:
        with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
            exec(compile(request['code'], '<research_python>', 'exec'), scope)
        result = scope.get('result')
        if not isinstance(result, dict):
            raise ValueError('Set result to a JSON object')
        response = {'status': 'ok', 'output': result, 'stdout': logs.getvalue()}
        encoded = json.dumps(response, allow_nan=False)
        if len(encoded.encode()) > 512000:
            raise ValueError('Code result exceeds 512KB')
    except BaseException as exc:
        encoded = json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
            'error': str(exc)[:2000], 'stdout': logs.getvalue()}, allow_nan=False)
    # A process exit/timeout or direct writes to stdout are treated as failed protocol,
    # never as proof of a scientific result.
    os.write(1, (encoded+'\n').encode())


if __name__ == '__main__':
    main()
