# ruff: noqa: E402
"""Root policies are unchanged; only synthetic execution-contract fixtures."""
import itertools
import json
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
from proteinrsi.contracts import BudgetSpec, MetaPolicy, MethodProgram, Observation, TaskSpec, TaskView, Workflow
from proteinrsi.research.code import execute_code
from proteinrsi.storage import Store


@pytest.mark.parametrize('sites', [3, 4])
def test_root_program_first_plates_identical_and_max_context_runs(tmp_path, monkeypatch, sites):
    monkeypatch.setenv('PROTEINRSI_SANDBOX_BACKEND', 'bwrap')
    reference = 'A'*sites
    task = TaskSpec(name='Synthetic root-policy execution fixture', reference_sequence=reference,
        mutable_positions=list(range(1, sites+1)), max_mutations=sites, candidate_access='open',
        feedback_source='synthetic', batch_size=16, controls_per_batch=0, max_rounds=8, seed=17,
        batch_fill_policy='full_plate', budget=BudgetSpec(experimental_wells=128,llm_calls=0,tool_calls=16))
    def observation(sequence, value):
        return Observation(sample_id='synthetic-'+sequence, sequence=sequence, value=value,
                           qc='valid', source='synthetic', metric='fitness', unit='synthetic',
                           batch_id='synthetic', assay_protocol='synthetic')
    initial = [observation(reference, 0.1)]
    # Known synthetic sign reversal exercises optional pairwise branch execution.
    values = {reference: 0.1, 'C'+'A'*(sites-1): 0.3,
              'AC'+'A'*(sites-2): 0.4, 'CC'+'A'*(sites-2): 0.2}
    for letters in itertools.product('ACDEFGHIKLMNPQRSTVWY', repeat=sites):
        sequence = ''.join(letters)
        values.setdefault(sequence, len(values)/200)
        if len(values) == 128:
            break
    maximum = [observation(s,v) for s,v in values.items()]
    firsts, timings = [], []
    for mode in ('random','additive','conditional_pairwise'):
        program = MethodProgram.model_validate_json((ROOT/'experiments/frozen-program-selection-v1/policies'/(mode+'.json')).read_text())
        store = Store(tmp_path/f'{sites}-{mode}')
        for round_index, observations in ((0, initial),(7, maximum)):
            view = TaskView(task=task, round_index=round_index, observations=observations, history=[],
                remaining_wells=16 if round_index else 128,
                workflow=Workflow(programs=[program]), meta=MetaPolicy(enabled=False))
            started = time.monotonic()
            receipt = execute_code(store, view, {'code':program.code,'inputs':program.inputs})
            timings.append({'sites':sites,'arm':mode,'observations':len(observations),
                            'wall_seconds':time.monotonic()-started,'status':receipt['status']})
            assert receipt['status'] == 'ok', receipt
            assert receipt['execution_backend'] == 'bwrap_seccomp_generated_v1'
            payload = receipt['output']
            assert len(payload['candidates']) == 16
            sequences = [c['sequence'] for c in payload['candidates']]
            assert len(set(sequences)) == 16
            assert not set(sequences) & {o.sequence for o in observations}
            if round_index == 0:
                firsts.append(sequences)
            if round_index and mode == 'conditional_pairwise':
                assert payload['diagnostic']['model_used'] == 'pairwise_kernel_ridge'
    assert firsts[0] == firsts[1] == firsts[2]
    (tmp_path/f'synthetic-root-policy-{sites}site-runtime.json').write_text(json.dumps(timings,indent=2)+'\n')
