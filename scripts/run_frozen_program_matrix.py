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
        usage = store.usage()
        if (usage['experimental_wells']['committed'] != 128
                or usage['llm_calls']['committed'] != 0
                or usage['tool_calls']['committed'] != 8
                or campaign.state['round_index'] != 8):
            raise ValueError('Benchmark did not finish the exact 8x16/8-program-call contract')
        with store.connect() as connection:
            charges = [dict(zip(('key', 'resource', 'amount', 'fingerprint', 'state'), values))
                for values in connection.execute('SELECT key,resource,amount,fingerprint,state '
                                                  'FROM charges ORDER BY rowid')]
        metrics = charged_metrics(store.all('batches'), store.all('measurements'), charges,
            initial=store.get('configuration', 'provided_initial_evidence')['observations'],
            reference=task.reference_sequence, direction=task.direction, top_ns=(5, 10), query_bin_size=16)
        # Only descriptive numbers leave the per-case controller. No sequences,
        # saved observations, hidden-label statistics or global ranks are returned.
        metrics.pop('batch_endpoints', None)
        summaries.append({'case_id': row['case_id'], 'landscape': row['landscape'],
            'arm': row['arm'], 'seed': row['seed'], 'usage': usage, 'metrics': metrics})
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
