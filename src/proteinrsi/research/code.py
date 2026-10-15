# SPDX-License-Identifier: MIT
"""Budgeted generated analysis code; outputs are always unvalidated computational evidence."""
from functools import partial
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from proteinrsi.contracts import TaskKind, TaskView, canonical, digest
from proteinrsi.localtools.artifacts import ArtifactStore
from proteinrsi.tools import ToolSpec

CODE_TOOL = 'research_python'


def python_input_contract(*, role='task'):
    """Small runtime interface derived from the model actually injected into Python."""
    schema = TaskView.model_json_schema()

    def type_name(field):
        if '$ref' in field:
            return field['$ref'].rsplit('/', 1)[-1]
        if 'anyOf' in field:
            return ' | '.join(type_name(item) for item in field['anyOf'])
        if field.get('type') == 'array':
            return 'list[' + type_name(field.get('items', {})) + ']'
        return field.get('type', 'any')

    def fields(model):
        return {name: type_name(field) for name, field in model['properties'].items()}

    return {'version': 'research-python-input-v2', 'context_scope': role, 'variables': {
        'context': {'type': 'TaskView', 'fields': fields(schema),
            'task_fields': fields(schema['$defs']['TaskSpec']),
            'observation_fields': fields(schema['$defs']['Observation']),
            'research_state_path': "context['research_context']['research_state']",
            'research_state_optional': True,
            'representation': 'Original revealed JSON: observations and history are lists, '
                'observation sequences are full sequences. Prompt tables and evidence_ref '
                'descriptors are presentation only; do not decode them in Python.'},
        'inputs': 'dict: literal inputs plus backend-hydrated resource_refs by input name',
        'artifacts': 'dict: requested artifact refs mapped to readable local paths',
        'write_artifact': "write_artifact(name, content, kind='json') returns a descriptor"},
        'output': 'Set result to a JSON object. Computational results are not experimental measurements.',
        'result': {'payload_port': 'payload', 'payload_pointer': '/output',
            'payload_schema': {'type': 'object'},
            'description': 'The result port is an execution receipt (status, logs, output). '
                'Business fields live only in output. Bind step:<id>.payload to downstream '
                'resource_refs for direct business data; step:<id>.result retains the receipt. '
                'Named output views may validate more specific agent-defined business schemas.'}}


def execute_code(store, view, arguments, *, pure=False, role="task"):
    if role not in {"task", "improver"}:
        raise ValueError('Unknown generated-code role')
    if not pure and role == "task":
        view = view.for_task()
    from proteinrsi.replay.broker import reader_roots
    from proteinrsi.replay.sandbox import probe, SandboxUnavailable
    if not probe()['available']:
        raise SandboxUnavailable('Generated code requires Landlock and seccomp')
    if pure and set(arguments) - {'code', 'inputs'}:
        raise ValueError('Pure metric execution cannot request artifacts or capabilities')
    from proteinrsi.dataflow.resources import ResourceStore, standard_registry, scope_for
    resources = ResourceStore(store, standard_registry(), scope_for(view)) if not pure else None
    inputs = (resources.hydrate(arguments.get('resource_refs', {}), arguments.get('inputs', {}))
              if resources is not None else arguments.get('inputs', {}))
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
        if role == 'task':
            registry.require_task_access(arguments.get('artifact_refs', []))
        for ref in arguments.get('artifact_refs', []):
            source = registry.resolve(ref)
            destination = root/'inputs'/source.name
            shutil.copyfile(source, destination)
            files[ref] = str(destination)
        context = {} if pure else view.model_dump(mode='json')
        request = {'code': arguments['code'], 'inputs': inputs,
            'artifacts': files, 'context': context, 'read_roots': reader_roots(), 'work': work,
            'parent_pid': os.getpid()}
        # Bulk revealed evidence travels as a local sandbox input, never through
        # model text or the small worker command pipe. No oracle files are copied.
        if not pure:
            encoded_context = canonical(context).encode()
            if len(encoded_context) > 64 * 1024**2:
                raise ValueError('Generated-code revealed evidence exceeds 64MB')
            context_file = root/'revealed-context.json'
            context_file.write_bytes(encoded_context)
            request['context'] = {}
            request['context_file'] = str(context_file)
        encoded_inputs = canonical(inputs).encode()
        if len(encoded_inputs) > 64 * 1024**2:
            raise ValueError('Generated-code inputs exceed 64MB')
        if len(encoded_inputs) > 256 * 1024:
            input_file = root/'bound-inputs.json'
            input_file.write_bytes(encoded_inputs)
            request['inputs'] = {}
            request['inputs_file'] = str(input_file)
        payload = canonical(request).encode()+b'\n'
        if len(payload) > 4 * 1024**2:
            raise ValueError('Generated-code context exceeds 4MB')
        env = {'PATH': '/usr/bin:/bin', 'HOME': work, 'TMPDIR': work, 'LANG': 'C.UTF-8',
            'PYTHONPATH': str(Path(__file__).resolve().parents[2]), 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1', 'PYTHONHASHSEED': '0', 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1',
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
                    response = (root/'stdout').read_bytes()
                    if len(response.rstrip(b'\n')) > 512000:
                        raise ValueError('Generated-code response exceeds 512KB')
                    result = json.loads(response)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                result = {'status': 'failed', 'error_type': 'Timeout', 'error': '20-second wall limit'}
            except (ValueError, UnicodeError):
                result = {'status': 'failed', 'error_type': 'InvalidWorkerOutput'}
            finally:
                # Interruptions must not leave a still-running pure-code process
                # alive alongside a restart. Parent death is handled in the child.
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)
        if not isinstance(result, dict) or result.get('status') not in {'ok', 'failed'}:
            result = {'status': 'failed', 'error_type': 'InvalidWorkerOutput'}
        if result['status'] == 'ok':
            output = result.get('output', {})
            if not isinstance(output, dict):
                raise ValueError('Code output must be an object')
            if pure and 'artifacts' in output:
                raise ValueError('Pure metric output cannot create artifacts')
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
                registered.append(registry.put(path, item['kind'], origin='generated-code:'+source_hash,
                                               task_visible=role == 'task'))
            result['artifacts'] = registered
        result.update(execution_backend='landlock_seccomp_generated_v1',
                      evidence_kind='computed_unvalidated', code_sha256=source_hash,
                      measurement_authority=False, source_file='programs/'+source_path.name)
        store.event('generated_code_completed', {'code_sha256': source_hash, 'status': result['status']})
        return result


def register_code_tool(gateway, view, *, role="task"):
    if role not in {"task", "improver"}:
        raise ValueError('Unknown generated-code role')
    if role == "task":
        view = view.for_task()
    if getattr(gateway, 'remote_context_tools', False):
        return
    gateway._tools.pop(CODE_TOOL, None)
    if not gateway.store.get('configuration', 'research', {}).get('enable_generated_code'):
        return
    spec = ToolSpec(name=CODE_TOOL, capability='analysis.program',
        description='Write and run Python to compute metrics, analyze visible evidence, or generate candidates. '
            'context is the complete revealed TaskView; inputs contains literal JSON and hydrated resource_refs. '
            'Declare resource_refs={input_name: resource_id}; the backend resolves them into inputs[input_name]. '
            'Do not read and retranscribe existing candidate/history resources into model output. '
            'artifacts maps requested refs to readable paths. '
            'Use Python standard library or numpy. Set result to a JSON object; result.candidates may contain Candidate objects. '
            'Use write_artifact(name,text,kind) and return its descriptors in result.artifacts to save scientific files. '
            'Read status/error/stdout, correct the code, and call again when needed.',
        limitations='No network, subprocesses, pip, API keys, campaign database or hidden measurements. '
            '10 CPU seconds, 20 wall seconds, 2GB memory per call. Metrics are computations, never experiment values. '
            'Protein engines are separate tools; request their outputs then pass relevant results in inputs.',
        when_to_use=['A task-specific metric, custom analysis, filtering or candidate transformation is needed.'],
        when_not_to_use=['A dedicated protein engine is needed; use its deployed tool.'],
        output_semantics='Execution receipt: status/logs plus output containing all generated business fields. '
            'The payload port exposes output directly; generated-code metrics are unvalidated computational evidence.',
        implementation_version='isolated-python-role-v5:'+role, task_kinds=list(TaskKind),
        execution_contract=python_input_contract(role=role),
        examples=[{'code': "result = {'observations_seen': len(context['observations'])}"},
                  {'inputs': {'values': [1, 2]},
                   'code': "result = {'total': sum(inputs['values'])}"}],
        input_schema={'type': 'object', 'properties': {
            'code': {'type': 'string', 'minLength': 1, 'maxLength': 30000},
            'inputs': {'type': 'object'},
            'resource_refs': {'type': 'object', 'maxProperties': 16, 'additionalProperties': {
                'type': 'string', 'pattern': r'^resource:[0-9a-f]{64}$'}},
            'artifact_refs': {'type': 'array', 'maxItems': 16,
                'uniqueItems': True, 'items': {'type': 'string'}}},
            'required': ['code'], 'additionalProperties': False},
        output_schema={'type': 'object', 'required': ['status', 'evidence_kind', 'code_sha256'],
            'properties': {'status': {'enum': ['ok', 'failed']},
                'output': {'type': 'object', 'description': 'Generated business data; not receipt metadata.'},
                'stdout': {'type': 'string'}, 'error_type': {'type': 'string'}, 'error': {'type': 'string'},
                'evidence_kind': {'const': 'computed_unvalidated'}, 'code_sha256': {'type': 'string'}},
            'allOf': [{'if': {'properties': {'status': {'const': 'ok'}}},
                       'then': {'required': ['output']}}]})
    gateway.register(spec, partial(execute_code, gateway.store, view, role=role))
