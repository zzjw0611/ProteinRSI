# SPDX-License-Identifier: MIT
"""Budgeted generated analysis code; outputs are always unvalidated computational evidence."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from proteinrsi.contracts import TaskKind, canonical, digest
from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.tools import ToolSpec

CODE_TOOL = 'research_python'


def execute_code(store, view, arguments):
    from proteinrsi.replay.broker import reader_roots
    from proteinrsi.replay.sandbox import probe, SandboxUnavailable
    if not probe()['available']:
        raise SandboxUnavailable('Generated code requires Landlock and seccomp')
    source_hash = digest(arguments['code'])
    programs = store.root/'programs'
    programs.mkdir(exist_ok=True)
    source_path = programs/(source_hash+'.py')
    if source_path.exists():
        if source_path.is_symlink() or source_path.read_text() != arguments['code']:
            raise ValueError('Saved generated source differs from its identity')
    else:
        with source_path.open('x') as source_file:
            source_file.write(arguments['code'])
    store.put('code_programs', source_hash, {'code': arguments['code'],
        'source_file': 'programs/'+source_path.name}, immutable=True)
    registry = ArtifactStore(store)
    with tempfile.TemporaryDirectory(prefix='proteinrsi-code-') as work:
        root = Path(work)
        (root/'inputs').mkdir()
        (root/'outputs').mkdir()
        files = {}
        for ref in arguments.get('artifact_refs', []):
            source = registry.resolve(ref)
            destination = root/'inputs'/source.name
            shutil.copyfile(source, destination)
            files[ref] = str(destination)
        context = view.model_dump(mode='json')
        request = {'code': arguments['code'], 'inputs': arguments.get('inputs', {}),
            'artifacts': files, 'context': context, 'read_roots': reader_roots(), 'work': work}
        payload = canonical(request).encode()+b'\n'
        if len(payload) > 4 * 1024**2:
            raise ValueError('Generated-code context exceeds 4MB')
        env = {'PATH': '/usr/bin:/bin', 'HOME': work, 'TMPDIR': work, 'LANG': 'C.UTF-8',
            'PYTHONPATH': str(Path(__file__).resolve().parents[2]), 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1', 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
            'LD_LIBRARY_PATH': str(Path(sys.base_prefix)/'lib')}
        with (root/'stdout').open('wb') as out, (root/'stderr').open('wb') as err:
            proc = subprocess.Popen([sys.executable, '-m', 'proteinrsi.research.code_worker'],
                stdin=subprocess.PIPE, stdout=out, stderr=err, env=env, cwd=work,
                start_new_session=True, close_fds=True)
            try:
                proc.communicate(payload, timeout=20)
                if proc.returncode != 0:
                    result = {'status': 'failed', 'error_type': 'WorkerExit', 'returncode': proc.returncode}
                else:
                    result = json.loads((root/'stdout').read_bytes()[:1024**2])
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                result = {'status': 'failed', 'error_type': 'Timeout', 'error': '20-second wall limit'}
            except (ValueError, UnicodeError):
                result = {'status': 'failed', 'error_type': 'InvalidWorkerOutput'}
        if not isinstance(result, dict) or result.get('status') not in {'ok', 'failed'}:
            result = {'status': 'failed', 'error_type': 'InvalidWorkerOutput'}
        if result['status'] == 'ok':
            output = result.get('output', {})
            if not isinstance(output, dict):
                raise ValueError('Code output must be an object')
            registered = []
            for item in output.pop('artifacts', []):
                name = item['name']
                if Path(name).name != name or name in {'', '.', '..'}:
                    raise ValueError('Invalid output artifact name')
                path = root/'outputs'/name
                if path.is_symlink() or not path.resolve().is_relative_to(root/'outputs'):
                    raise ValueError('Invalid output artifact path')
                content = item.get('content')
                if not isinstance(content, str) or len(content.encode()) > 400000:
                    raise ValueError('Invalid generated scientific text output')
                with path.open('x') as output_file:
                    output_file.write(content)
                registered.append(registry.put(path, item['kind'], origin='generated-code:'+source_hash))
            result['artifacts'] = registered
            if 'candidates' in output:
                result['candidates'] = output['candidates']
        result.update(evidence_kind='computed_unvalidated', code_sha256=source_hash,
                      measurement_authority=False, source_file='programs/'+source_path.name)
        store.event('generated_code_completed', {'code_sha256': source_hash, 'status': result['status']})
        return result


def register_code_tool(gateway, view):
    if getattr(gateway, 'remote_context_tools', False):
        return
    gateway._tools.pop(CODE_TOOL, None)
    if not gateway.store.get('configuration', 'research', {}).get('enable_generated_code'):
        return
    spec = ToolSpec(name=CODE_TOOL, capability='analysis.program',
        description='Write and run Python to compute metrics, analyze visible evidence, or generate candidates. '
            'context is the revealed TaskView; inputs is your JSON; artifacts maps requested refs to readable paths. '
            'Use Python standard library or numpy. Set result to a JSON object; result.candidates may contain Candidate objects. '
            'Use write_artifact(name,text,kind) and return its descriptors in result.artifacts to save scientific files. '
            'Read status/error/stdout, correct the code, and call again when needed.',
        limitations='No network, subprocesses, pip, API keys, campaign database or hidden measurements. '
            '10 CPU seconds, 20 wall seconds, 2GB memory per call. Metrics are computations, never experiment values. '
            'Protein engines are separate tools; request their outputs then pass relevant results in inputs.',
        when_to_use=['A task-specific metric, custom analysis, filtering or candidate transformation is needed.'],
        when_not_to_use=['A dedicated protein engine is needed; use its deployed tool.'],
        output_semantics='Generated-code metrics are unvalidated computational evidence; errors are returned for repair.',
        implementation_version='isolated-python-v1', task_kinds=list(TaskKind),
        input_schema={'type': 'object', 'properties': {
            'code': {'type': 'string', 'minLength': 1, 'maxLength': 30000},
            'inputs': {'type': 'object'}, 'artifact_refs': {'type': 'array', 'maxItems': 16,
                'uniqueItems': True, 'items': {'type': 'string'}}},
            'required': ['code'], 'additionalProperties': False},
        output_schema={'type': 'object', 'required': ['status', 'evidence_kind', 'code_sha256'],
            'properties': {'status': {'enum': ['ok', 'failed']},
                'evidence_kind': {'const': 'computed_unvalidated'}, 'code_sha256': {'type': 'string'}}})
    gateway.register(spec, lambda arguments: execute_code(gateway.store, view, arguments))
