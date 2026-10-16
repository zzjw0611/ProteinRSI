"""Selection plumbing for a separate root-authored deterministic diagnostic.

This is not an interpreter or evaluation of a prompt-defined workflow.
"""
from proteinrsi.contracts import Candidate, canonical, digest
from proteinrsi.tasks import validate_candidate

MODE = 'root-frozen-program-selection-v1'


def validate_binding(view, binding):
    if set(binding) != {'mode', 'workflow', 'program', 'program_sha256', 'adapter_sha256'}:
        raise ValueError('Invalid frozen program binding')
    if binding['mode'] != MODE or len(view.workflow.programs) != 1:
        raise ValueError('Exactly one frozen selection program is required')
    program = view.workflow.programs[0]
    if (binding['workflow'] != view.workflow.version or binding['program'] != program.version
            or binding['program_sha256'] != digest(program.code)):
        raise ValueError('Frozen program identity differs')
    if binding['adapter_sha256'] != adapter_sha256():
        raise ValueError('Frozen adapter source differs')


def adapter_sha256():
    from pathlib import Path
    root = Path(__file__).parent
    return digest({name: (root / name).read_text() for name in
                   ('frozen_program_benchmark.py', 'agents.py', 'replay/broker.py')})


def select_candidates(team, view, binding):
    validate_binding(view, binding)
    if team.llm is not None or team.protein_model is not None or team.tools.allow_egress:
        raise PermissionError('Frozen program benchmark forbids LLM, models and egress')
    from proteinrsi.research.methods import run_method_programs
    outputs = run_method_programs(team.store, team.tools, view, view.workflow, owner='W')
    receipt = outputs[0]['result']
    if receipt.get('status') != 'ok':
        raise ValueError('Frozen selection program failed; no fallback')
    payload = receipt.get('output')
    if (not isinstance(payload, dict) or 'candidates' not in payload
            or set(payload) - {'candidates', 'diagnostic'}
            or ('diagnostic' in payload and not isinstance(payload['diagnostic'], dict))):
        raise ValueError('Selection output requires candidates and optional diagnostic metadata')
    diagnostic = payload.get('diagnostic', {})
    if (len(diagnostic) > 16 or len(canonical(diagnostic).encode()) > 4096
            or any(not isinstance(k, str) or len(k) > 128
                   or not isinstance(v, (str, int, float, bool, type(None)))
                   or (isinstance(v, str) and len(v) > 512)
                   for k, v in diagnostic.items())):
        raise ValueError('Diagnostic metadata exceeds the bounded scalar-object contract')
    raw = payload['candidates']
    if not isinstance(raw, list):
        raise ValueError('Selection candidates must be a list')
    candidates = [Candidate.model_validate(item) for item in raw]
    completion = view.research_context.get('plate_completion', {})
    required = completion.get('required_new_candidates', view.task.batch_size)
    excluded = ({o.sequence for o in view.observations} | {view.task.reference_sequence}
                | set(completion.get('exclude_sequences', [])))
    if len(candidates) != required or len({c.sequence for c in candidates}) != required:
        raise ValueError('Selection must fill the exact distinct plate remainder')
    for candidate in candidates:
        validate_candidate(view.task, candidate, enforce_universe=view.task.candidate_access == 'pool')
        if candidate.sequence in excluded:
            raise ValueError('Selection repeats an excluded sequence')
        if (candidate.predicted_value is not None or candidate.prediction_ref is not None
                or candidate.uncertainty is not None or candidate.evidence_kind != 'none'):
            raise ValueError('Selection diagnostic accepts identities only, no prediction claims')
    return candidates
