# SPDX-License-Identifier: MIT
"""Task-neutral natural-language intake and explicit feedback-route selection."""
import csv
import json
from pathlib import Path
from typing import Literal

from pydantic import Field
from proteinrsi.contracts import Model, TaskSpec, Workflow
from proteinrsi.llm import JSONLLM
from proteinrsi.localtools.artifacts import ArtifactStore, file_sha256
from proteinrsi.localtools.config import LocalToolsConfig, load_config
from proteinrsi.localtools.registry import configured_names, doctor
from proteinrsi.runtime import Campaign
from proteinrsi.storage import Store
from proteinrsi.tasks import validate_task


class ResearchIntent(Model):
    task_kind: Literal['variant_design', 'variant_ranking', 'binder_design', 'affinity_prediction'] | None = None
    route: Literal['measured_replay', 'wetlab', 'computational'] | None = None
    rationale: str
    landscape: str | None = None
    reference_sequence: str = ''
    target_sequence: str | None = None
    candidates: list[str] = Field(default_factory=list, max_length=384)
    mutable_positions: list[int] = Field(default_factory=list)
    max_mutations: int | None = Field(default=None, ge=0)
    allow_indels: bool = False
    min_length: int | None = Field(default=None, ge=1, le=10000)
    max_length: int | None = Field(default=None, ge=1, le=10000)
    repeat_policy: Literal['exclude', 'allow'] = 'exclude'
    metric: str = ''
    unit: str = ''
    direction: Literal['maximize', 'minimize'] = 'maximize'
    assay_protocol: str = ''
    rounds: int | None = Field(default=None, ge=1)
    queries: int | None = Field(default=None, ge=0)
    batch_fill_policy: Literal["flexible", "full_plate"] = "flexible"
    batch_size: int | None = Field(default=None, ge=2, le=384)
    questions: list[str] = Field(default_factory=list)


def available_landscapes(root):
    result = {}
    for path in sorted((Path(root)/'processed').glob('*/task.json')):
        try:
            task = json.loads(path.read_text())
            provenance = json.loads((path.parent/'provenance.json').read_text())
            if provenance.get('status') != 'ready_strict_measured_replay':
                continue
            # No fitness distribution, global maximum, ranking or hidden labels.
            result[path.parent.name] = {k: task.get(k) for k in (
                'reference_sequence', 'mutable_positions', 'max_mutations', 'metric',
                'unit', 'direction', 'assay_protocol')}
        except (OSError, ValueError):
            continue
    return result


def load_replay(root, name, *, candidates=None):
    if name not in available_landscapes(root):
        raise ValueError('Selected landscape is unavailable or quarantined')
    directory = Path(root)/'processed'/name
    provenance = json.loads((directory/'provenance.json').read_text())
    raw = json.loads((directory/'task.json').read_text())
    dataset = directory/'measurements.csv'
    if file_sha256(dataset) != provenance.get('prepared_assets', {}).get('measurements_csv_sha256'):
        raise ValueError('Measurement digest differs from verified preparation')
    if provenance.get('landscape') != name or provenance.get('parent', {}).get('sequence') != raw['reference_sequence']:
        raise ValueError('Landscape/parent provenance mismatch')
    seen, parent = set(), None
    with dataset.open(newline='') as file:
        for row in csv.DictReader(file):
            if (row.get('source') != 'measured_replay' or row.get('qc') != 'valid'
                    or row.get('label_kind') != 'reported_experimental_assay_score'):
                raise ValueError('Only confirmed reported experimental scores are eligible')
            sequence = row['sequence']
            if sequence in seen:
                raise ValueError('Duplicate sequence in prepared measurements')
            seen.add(sequence)
            if sequence == raw['reference_sequence']:
                parent = float(row['value'])
    if parent is None:
        raise ValueError('Verified parent observation is missing')
    if candidates is not None and not set(candidates) <= seen:
        raise ValueError('Supplied ranking candidates lack historical records')
    raw.update(candidates=list(candidates or []), controls_per_batch=0,
        candidate_access='pool' if candidates is not None else 'open',
        initial_observation_policy='provided_parent', initial_parent_measurement={
            'value': parent, 'source_ref': f'{name}; reported experimental parent; SHA256 {file_sha256(dataset)}'})
    return raw, dataset


def input_catalog(store, paths):
    from proteinrsi.localtools.pdbio import read_fasta, read_pdb
    registry = ArtifactStore(store)
    entries = []
    for supplied in paths:
        path = Path(supplied).resolve(strict=True)
        kind = {'.fa': 'fasta', '.faa': 'fasta'}.get(path.suffix.lower(), path.suffix.lower().lstrip('.'))
        item = {**registry.put(path, kind), 'name': path.name}
        if kind == 'fasta':
            records = read_fasta(path)
            if sum(len(seq) for _, seq in records) > 100000:
                raise ValueError('FASTA intake exceeds 100000 residues; select a smaller input set')
            item['sequences'] = [{'name': name, 'sequence': seq} for name, seq in records]
        elif kind == 'pdb':
            item['chains'] = {chain: {'sequence': data['sequence']} for chain, data in read_pdb(path).items()}
        entries.append(item)
    return entries


def prepare_research_goal(goal, *, out, data_root, local_tools=None, inputs=(), llm_calls=200,
                          tool_calls=100, llm_factory=None, event_sink=None, continue_from=False, full_plate=False, protocol_mode=None):
    if not goal.strip() or len(goal) > 12000 or llm_calls < 1 or tool_calls < 0:
        raise ValueError('Provide a nonempty goal and valid compute limits')
    out = Path(out).resolve()
    if not continue_from:
        out.mkdir(parents=True, exist_ok=False)
    elif not out.is_dir():
        raise ValueError('No pending goal at the continuation path')
    store = Store(out)
    if store.get('campaign', 'state') is not None:
        raise ValueError('A campaign already exists; continuation is only for unanswered intake questions')
    previous = store.get('intake', 'pending')
    if continue_from and previous is None:
        raise ValueError('No pending clarification')
    if previous:
        full_plate = full_plate or previous.get("full_plate", False)
        goal = previous['goal']+'\n用户补充：'+goal
    protocol_mode = protocol_mode or (previous.get('protocol_mode', 'legacy') if previous else 'typed')
    if protocol_mode not in {'typed', 'legacy'}:
        raise ValueError('Unknown protocol mode')
    if previous and protocol_mode != previous.get('protocol_mode', 'legacy'):
        raise ValueError('Cannot change protocol mode during an existing intake')
    local = load_config(str(local_tools)) if local_tools else LocalToolsConfig()
    if previous:
        local = LocalToolsConfig.model_validate(previous['local_tools'])
        data_root = Path(previous['data_root'])
        llm_calls = previous['llm_calls']
        tool_calls = previous['tool_calls']
    entries = [*(previous.get('inputs', []) if previous else []), *input_catalog(store, inputs)]
    landscape_catalog = available_landscapes(data_root)
    from proteinrsi.localtools.catalog import descriptions
    names = configured_names(local)
    tools = [d for d in descriptions() if d['name'] in names]
    if local.esmc is not None:
        tools.append({'name': 'ESMC', 'description': 'Optional sequence priors and embeddings; not measured fitness'})
    tools.append({'name': 'research_python', 'description': 'Generate isolated Python for metrics, analysis and candidate transformations; no hidden labels or experiment authority'})
    intake = Store(out/'goal-intake')
    intake.event_sink = event_sink
    intake.configure_budget({'llm_calls': llm_calls})
    llm = (llm_factory or JSONLLM.from_env)(intake)
    raw_intent = llm.complete('goal-intake',
        'Interpret the research goal and select a task kind and feedback route using supplied inputs and tools. '
        'Do not assume GB1, four mutation sites, a fixed protein length, or an experimental objective. '
        'Ranking tasks use the supplied candidate set without requiring a design stage or a reference sequence; absent a reference, leave reference_sequence empty and mutable_positions empty. De novo binders have '
        'an empty reference_sequence and explicit length bounds, with a supplied target_sequence. '
        'For variable-length redesign set allow_indels=true, explicit length bounds, mutable_positions=[], max_mutations=null. '
        'For open-ended design leave candidates empty; do not turn input/reference sequences into a closed library unless explicitly requested. '
        'Infer constraints only from the goal and supplied metadata. Never invent starting/target sequences, measured '
        'values, budgets or dataset availability. Use questions for missing essential inputs, ambiguous task type, '
        'feedback route, explicit round limit, or experimental query budget. Extract budget limits, do not invent them. '
        'Set batch_fill_policy=full_plate when each experimental plate must be filled or the user forbids unused wells; batch_size is then exact, not a suggestion. '
        'For computational tasks queries=0 and no assay is needed; describe the requested proxy metric. '
        'For wetlab specify the requested assay/metric/unit or ask. For measured_replay select a listed landscape '
        'and copy its sequence constraints and assay metadata exactly; do not switch its fitness scale. '
        'All tools are optional. The research agents can write isolated Python, calculate task-specific metrics, '
        'repair code and revise plans; they cannot manufacture experimental feedback. Return the intent schema.',
        {'goal': goal, 'inputs': entries, 'available_landscapes': landscape_catalog, 'available_tools': tools},
        ResearchIntent.model_json_schema())
    try:
        intent = ResearchIntent.model_validate(raw_intent)
    except ValueError:
        intent = ResearchIntent(rationale='The intake response was incomplete or invalid',
            questions=['目标解析未形成有效任务。请补充任务目标、输入序列、反馈方式和迭代上限。'])
    if full_plate:
        intent.batch_fill_policy = "full_plate"
    questions = list(intent.questions)
    if intent.task_kind is None or intent.route is None:
        questions.append('请说明研究对象和希望获得的结果，以及使用计算评估、历史实测回放还是新的湿实验。')
    if intent.rounds is None:
        questions.append('最多进行多少轮迭代？')
    if intent.route in {'measured_replay', 'wetlab'} and intent.queries is None and intent.batch_size is None:
        questions.append('请给出总实验查询数，或每轮查询数。')
    if intent.route == 'wetlab' and not all((intent.metric, intent.unit, intent.assay_protocol)):
        questions.append('湿实验反馈使用什么测量指标、单位和实验协议？')
    raw, dataset = None, None
    if not questions:
        try:
            if intent.route == 'measured_replay':
                raw, dataset = load_replay(data_root, intent.landscape,
                    candidates=intent.candidates if intent.task_kind == 'variant_ranking' else None)
                for key in ('reference_sequence', 'mutable_positions', 'max_mutations', 'metric', 'unit', 'direction', 'assay_protocol'):
                    if getattr(intent, key) != raw[key]:
                        raise ValueError('回放任务的 '+key+' 与数据集定义不一致；请明确是否使用原始实测协议。')
                if intent.allow_indels or intent.task_kind not in {'variant_design', 'variant_ranking'}:
                    raise ValueError('所选回放数据仅支持其定义的变体空间。')
                raw['kind'] = intent.task_kind
                if intent.task_kind == 'variant_ranking':
                    if not intent.candidates or not set(intent.candidates) <= set(raw['candidates']):
                        raise ValueError('排序任务需要给出待排序序列，且回放中必须有相应实测记录。')
                    raw['candidates'] = intent.candidates
            else:
                # Prevent a guessed protein from silently becoming the user's research input.
                known = {x['sequence'] for entry in entries for x in entry.get('sequences', [])}
                known |= {x['sequence'] for entry in entries for x in entry.get('chains', {}).values()}
                if intent.landscape in landscape_catalog:
                    known.add(landscape_catalog[intent.landscape]['reference_sequence'])
                for seq in (intent.reference_sequence, intent.target_sequence, *intent.candidates):
                    if seq and seq not in known and seq not in ''.join(goal.split()):
                        raise ValueError('缺少已提供的母本、靶标或候选序列；请在目标中给出序列，或使用 --input FASTA/PDB。')
                if intent.task_kind == 'variant_ranking' and not intent.candidates:
                    raise ValueError('请提供待排序的候选序列。')
                raw = {k: getattr(intent, k) for k in ('reference_sequence', 'target_sequence', 'candidates',
                    'mutable_positions', 'max_mutations', 'allow_indels', 'min_length', 'max_length',
                    'repeat_policy', 'metric', 'unit', 'direction', 'assay_protocol')}
                raw.update(candidate_access='open' if intent.task_kind != 'variant_ranking' and not intent.candidates else 'pool',
                    kind=intent.task_kind, feedback_source='computational' if intent.route == 'computational' else 'wetlab', controls_per_batch=0)
            rounds = intent.rounds
            queries = 0 if intent.route == 'computational' else (intent.queries if intent.queries is not None else rounds*intent.batch_size)
            batch = intent.batch_size or (max(2, (queries+rounds-1)//rounds) if queries else 12)
            if queries > rounds*batch:
                raise ValueError('总查询预算超过轮数乘以单轮上限。')
            raw.update(name='Natural-language protein research', objective_description=goal,
                execution_mode='computational' if intent.route == 'computational' else 'experimental',
                max_rounds=rounds, batch_size=batch, batch_fill_policy=intent.batch_fill_policy, repeat_policy=intent.repeat_policy,
                budget={'experimental_wells': queries, 'llm_calls': llm_calls, 'tool_calls': tool_calls})
            task = TaskSpec.model_validate(raw)
            validate_task(task)
        except (ValueError, TypeError) as exc:
            questions.append(str(exc))
    if questions:
        pending = {'protocol_mode': protocol_mode, 'full_plate': full_plate, 'goal': goal, 'intent': intent.model_dump(), 'questions': questions,
            'inputs': entries, 'data_root': str(Path(data_root).resolve()), 'local_tools': local.model_dump(mode='json'),
            'llm_calls': llm_calls, 'tool_calls': tool_calls}
        store.put('intake', 'pending', pending)
        store.event('goal_needs_clarification', {'questions': questions})
        (out/'clarification.json').write_text(json.dumps(pending, ensure_ascii=False, indent=2))
        return {'campaign': None, 'questions': questions, 'directory': str(out)}
    missing = [r for r in doctor(local) if r['status'] == 'missing_requirements']
    if missing:
        raise ValueError('Configured protein tool environments are missing: '+json.dumps(missing))
    tool_names = ['research_python', 'research_fit_predict', *names]
    skills = []
    if task.candidates:
        tool_names += ['library_check', 'library_sample']
    if local.esmc is not None:
        from proteinrsi.protein.tools import TOOL_NAMES
        tool_names += list(TOOL_NAMES)
        skills.append('esmc600m-analysis')
    if any(e.enabled for e in local.engines.values()):
        skills.append('local-protein-tools')
    from proteinrsi.research.contracts import ResearchConfig
    campaign = Campaign.initialize(str(out), task,
        workflow=Workflow(tool_names=list(dict.fromkeys(tool_names)), skill_names=skills, analysis_tool_rounds=3),
        protein_config=local.esmc, local_tools=local,
        research_config=ResearchConfig(resource_selection='llm', enable_generated_code=True, protocol_mode=protocol_mode))
    spent = intake.usage()['llm_calls']['committed']
    if spent:
        campaign.store.reserve('goal-intake', 'llm_calls', spent, {'goal': goal})
        campaign.store.settle('goal-intake')
    for key, value in intake.all('llm').items():
        campaign.store.put('llm', key, value, immutable=True)
    for event in intake.events():
        campaign.store.event(event['kind'], {**event['payload'], 'intake_timestamp': event['timestamp']})
    campaign.store.put('configuration', 'goal_intent', intent.model_dump(), immutable=True)
    campaign.store.put('configuration', 'goal_route', {'route': intent.route}, immutable=True)
    campaign.store.event('goal_parsed', {'goal': goal, 'intent': intent.model_dump()})
    campaign.store.event_sink = event_sink
    (out/'task.json').write_text(task.model_dump_json(indent=2))
    return {'campaign': campaign, 'dataset': dataset, 'route': intent.route, 'questions': []}
