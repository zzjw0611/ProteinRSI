#!/usr/bin/env python3
# ruff: noqa: E402
"""Inert-by-default separate frozen-program benchmark. No prompt-W claims.

Only --execute advances replay. Root must supply and freeze scientific programs
and a case matrix first. Tests use synthetic inputs exclusively.
"""
import argparse
import json
import os
from pathlib import Path

import proteinrsi

SOURCE_ROOT = Path(proteinrsi.__file__).resolve().parent

from proteinrsi.contracts import BudgetSpec, MetaPolicy, MethodProgram, TaskSpec, Workflow, digest
from proteinrsi.frozen_program_benchmark import MODE, adapter_sha256
from proteinrsi.localtools.artifacts import file_sha256

FIELDS = {'case_id', 'landscape', 'arm', 'seed', 'task_file', 'task_sha256',
          'dataset', 'dataset_sha256', 'program_file', 'program_sha256'}


def prepare_matrix(matrix):
    if not isinstance(matrix, list) or not matrix:
        raise ValueError('A nonempty explicitly seeded case matrix is required')
    prepared, ids = [], set()
    for row in matrix:
        if set(row) != FIELDS or not isinstance(row['seed'], int) or isinstance(row['seed'], bool):
            raise ValueError('Invalid matrix fields or seed')
        if row['case_id'] in ids:
            raise ValueError('Duplicate case identity')
        ids.add(row['case_id'])
        for pathkey, hashkey in [('task_file', 'task_sha256'), ('dataset', 'dataset_sha256'),
                                 ('program_file', 'program_sha256')]:
            if file_sha256(Path(row[pathkey])) != row[hashkey]:
                raise ValueError('Frozen input file hash differs')
        program = MethodProgram.model_validate_json(Path(row['program_file']).read_text())
        task = TaskSpec.model_validate_json(Path(row['task_file']).read_text())
        if (task.initial_observation_policy != 'provided_parent' or task.candidate_access != 'open'
                or task.feedback_source not in {'measured_replay', 'synthetic'}):
            raise ValueError('Requires open replay with explicitly provided free parent')
        task = TaskSpec.model_validate({**task.model_dump(mode='json'), 'batch_size': 16,
            'max_rounds': 8, 'controls_per_batch': 0, 'batch_fill_policy': 'full_plate',
            'seed': row['seed'], 'budget': BudgetSpec(experimental_wells=128,
                llm_calls=0, tool_calls=16).model_dump()})
        workflow = Workflow(programs=[program], tool_names=['research_python'])
        prepared.append((row, task, workflow))
    return prepared


def _saved_charges(store):
    with store.connect() as connection:
        return [dict(zip(('key', 'resource', 'amount', 'fingerprint', 'state'), values))
                for values in connection.execute('SELECT key,resource,amount,fingerprint,state '
                                                 'FROM charges ORDER BY rowid')]


def audit_completed_case(store, state, task, workflow):
    """Read-only completion audit; failed attempts stay charged and immutable.

    This can inspect an already-completed pinned campaign without replaying it or
    changing its frozen manifest. It grants no permission to resume changed code.
    """
    from collections import Counter
    from proteinrsi.reporting_metrics import align_observations, charged_metrics
    from proteinrsi.contracts import Observation

    usage, charges = store.usage(), _saved_charges(store)
    expected = {'experimental_wells': (128, 128), 'llm_calls': (0, 0), 'tool_calls': (16, None)}
    for resource, (limit, exact) in expected.items():
        item = usage[resource]
        if (item['limit'] != limit or item['reserved'] != 0
                or item.get('allocated_unspent', 0) != 0
                or (exact is not None and item['committed'] != exact)
                or item['committed'] > limit):
            raise ValueError('Benchmark completion has unresolved or incorrect resource accounting')
    if state['round_index'] != 8 or state['status'] != 'complete' or state['pending_batch']:
        raise ValueError('Benchmark did not finish exactly eight rounds')
    if state['task'] != task.model_dump(mode='json') or state['workflow'] != workflow.model_dump(mode='json'):
        raise ValueError('Completed case task or workflow differs')
    if len(workflow.programs) != 1:
        raise ValueError('Exactly one frozen program is required')
    batches, measurements = store.all('batches'), store.all('measurements')
    if len(batches) != 8 or {b['round_index'] for b in batches.values()} != set(range(8)):
        raise ValueError('Benchmark batches do not cover exactly eight rounds')
    sequences, sample_ids, by_round, qc = set(), set(), {}, Counter()
    for bid, batch in batches.items():
        samples = batch['samples']
        if batch['phase'] != 'research' or len(samples) != 16:
            raise ValueError('Benchmark requires eight complete research plates')
        by_round[batch['round_index']] = {s['candidate']['sequence'] for s in samples}
        for sample, observation in zip(samples, align_observations(samples, measurements.get(bid, []))):
            sequence = sample['candidate']['sequence']
            if (sequence in sequences or sequence == task.reference_sequence
                    or sample['sample_id'] in sample_ids or sample['workflow_version'] != workflow.version):
                raise ValueError('Benchmark queries must be distinct new frozen-workflow identities')
            sequences.add(sequence)
            sample_ids.add(sample['sample_id'])
            if observation is None:
                qc['not_returned'] += 1
            else:
                saved = Observation.model_validate(observation)
                if saved.batch_id != bid:
                    raise ValueError('Returned measurement batch differs')
                qc[saved.qc] += 1
    if set(measurements) - set(batches):
        raise ValueError('Unexpected measurement batch')
    well_charges = [c for c in charges if c['resource'] == 'experimental_wells' and c['state'] == 'committed']
    metrics = charged_metrics(batches, measurements, charges, initial=[], reference=task.reference_sequence)
    if (len(well_charges) != 8 or any(c['amount'] != 16 for c in well_charges)
            or metrics['reconstructed_charged_queries'] != 128 or metrics['unreconstructed_charged_queries']):
        raise ValueError('Benchmark queries cannot be reconciled to charged plates')
    tool_charges = [c for c in charges if c['resource'] == 'tool_calls' and c['state'] == 'committed']
    jobs, outputs = store.all('tool_jobs'), store.all('research_step_outputs')
    if (len(tool_charges) != usage['tool_calls']['committed'] or len(tool_charges) > 16
            or any(c['amount'] != 1 for c in tool_charges)
            or set(jobs) != {c['key'] for c in tool_charges} or len(outputs) != len(jobs)):
        raise ValueError('Unexplained charged program attempt')
    program, success_rounds, failed, used_outputs = workflow.programs[0], [], Counter(), set()
    for charge in tool_charges:
        job = jobs[charge['key']]
        output_id = job.get('context')
        output = outputs.get(output_id, {})
        result = job.get('result', {})
        if (charge['key'] != 'tool-' + charge['fingerprint'] or output_id in used_outputs
                or job.get('state') != 'done' or job.get('call', {}).get('name') != 'research_python'
                or job.get('call', {}).get('arguments') != {'code': program.code, 'inputs': program.inputs}
                or output.get('owner') != 'W' or output.get('method_version') != workflow.version
                or output.get('workflow') != workflow.version or output.get('program') != program.name
                or output.get('program_version') != program.version or output.get('result') != result
                or output.get('round') not in range(8)
                or result.get('code_sha256') != digest(program.code)
                or result.get('execution_backend') != 'bwrap_seccomp_generated_v1'
                or result.get('measurement_authority') is not False
                or result.get('evidence_kind') != 'computed_unvalidated'):
            raise ValueError('Program charge and saved execution receipt differ')
        used_outputs.add(output_id)
        if result.get('status') == 'ok':
            candidates = result.get('output', {}).get('candidates', [])
            selected = [c['sequence'] for c in candidates]
            if len(selected) != 16 or set(selected) != by_round[output['round']]:
                raise ValueError('Successful selection differs from its purchased round')
            success_rounds.append(output['round'])
        elif result.get('status') == 'failed' and result.get('error_type'):
            failed[result['error_type']] += 1
        else:
            raise ValueError('Unaccounted program outcome')
    if sorted(success_rounds) != list(range(8)):
        raise ValueError('Exactly one successful program selection per round is required')
    return {'successful_selection_rounds': 8, 'charged_tool_attempts': len(tool_charges),
            'failed_program_attempts': sum(failed.values()), 'failed_attempt_types': dict(sorted(failed.items())),
            'distinct_charged_queries': len(sequences), 'returned_qc_counts': dict(sorted(qc.items())),
            'note': 'Failed attempts remain charged with saved receipts; missing outcomes are not replaced.'}


def run_matrix(matrix, destination, *, execute=False):
    if not execute:
        raise PermissionError("Execution is disabled; root must freeze programs and matrix first")
    import fcntl
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "controller.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_matrix(matrix, destination, execute=True)


def _run_matrix(matrix, destination, *, execute=False):
    if not execute:
        raise PermissionError('Execution is disabled; root must freeze programs and matrix first')
    os.environ['PROTEINRSI_SANDBOX_BACKEND'] = 'bwrap'
    from proteinrsi.replay.sandbox import probe
    security = probe()
    if not security['available'] or security['backend'] != 'bwrap':
        raise RuntimeError('Mandatory bwrap enforcement unavailable')
    prepared = prepare_matrix(matrix)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {'mode': MODE, 'claim': 'Separate root-authored deterministic mechanism diagnostic; '
        'not evaluation of existing prompt-W', 'matrix': matrix, 'adapter_sha256': adapter_sha256(),
        'runner_sha256': file_sha256(Path(__file__)), 'sandbox': security,
        'source_bundle_sha256': digest({p.relative_to(SOURCE_ROOT).as_posix():file_sha256(p)
            for p in sorted(SOURCE_ROOT.rglob('*'))
            if p.is_file() and p.suffix in {'.py','.md','.json'}}),
        'initial_evidence': 'one provided free parent; 128 additional paid slots',
        'outputs': 'aggregate purchased-observation metrics only; no oracle summaries'}
    manifest_path = destination / 'frozen-manifest.json'
    if manifest_path.exists():
        if json.loads(manifest_path.read_text()) != manifest:
            raise ValueError('Cannot resume a different frozen matrix or runtime')
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    from proteinrsi.runtime import Campaign
    from proteinrsi.storage import Store
    from proteinrsi.replay.controller import run_replay
    from proteinrsi.research.contracts import ResearchConfig
    from proteinrsi.reporting_metrics import charged_metrics
    summaries = []
    for row, task, workflow in prepared:
        store = Store(destination / ('case-' + digest(row)[:20]))
        if store.get('campaign', 'state') is None:
            campaign = Campaign.initialize(str(store.root), task, workflow=workflow,
                meta=MetaPolicy(enabled=False), auto_meta_evaluation=False,
                governance_config={'allowed_patch_targets': []},
                research_config=ResearchConfig(enabled=False, enable_generated_code=True), _store=store)
            binding = {'mode': MODE, 'workflow': workflow.version,
                'program': workflow.programs[0].version,
                'program_sha256': digest(workflow.programs[0].code), 'adapter_sha256': adapter_sha256()}
            store.put('configuration', 'frozen_program_benchmark', binding, immutable=True)
            store.put('configuration', 'frozen_program_case', row, immutable=True)
        else:
            campaign = Campaign(store)
            if (campaign.state['task'] != task.model_dump(mode='json')
                    or campaign.state['workflow'] != workflow.model_dump(mode='json')
                    or store.get('configuration', 'frozen_program_case') != row):
                raise ValueError('Resume case identity differs')
        run_replay(campaign, row['dataset'], guarded=True)
        audit = audit_completed_case(store, campaign.state, task, workflow)
        usage, charges = store.usage(), _saved_charges(store)
        metrics = charged_metrics(store.all('batches'), store.all('measurements'), charges,
            initial=store.get('configuration', 'provided_initial_evidence')['observations'],
            reference=task.reference_sequence, direction=task.direction, top_ns=(5, 10), query_bin_size=16)
        # Only descriptive numbers leave the per-case controller. No sequences,
        # saved observations, hidden-label statistics or global ranks are returned.
        metrics.pop('batch_endpoints', None)
        summaries.append({'case_id': row['case_id'], 'landscape': row['landscape'],
            'arm': row['arm'], 'seed': row['seed'], 'usage': usage, 'execution_audit': audit, 'metrics': metrics})
        progress = {'completed_cases':len(summaries),'total_cases':len(prepared),'status':'running'}
        (destination/'progress.json').write_text(json.dumps(progress)+'\n')
    result = {'mode': MODE, 'matrix_sha256': digest(matrix), 'case_aggregates': summaries,
              'llm_calls': 0, 'prompt_workflow_evaluated': False}
    path = destination / 'aggregate-results.json'
    if path.exists() and json.loads(path.read_text()) != result:
        raise ValueError('Saved aggregate differs on resume')
    path.write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matrix', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('No execution without --execute and a frozen root-authored matrix')
    result = run_matrix(json.loads(Path(args.matrix).read_text()), args.out, execute=True)
    print(json.dumps({'status':'completed','completed_cases':len(result['case_aggregates']),
        'results_file':str(Path(args.out).resolve()/'aggregate-results.json'),
        'results_sha256':digest(result)}))
