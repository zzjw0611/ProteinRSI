# SPDX-License-Identifier: MIT
"""Offline contracts and real process execution of explicitly FAKE engine fixtures.

These tests do not validate ProteinMPNN/RFdiffusion/Protenix/PyRosetta science.
"""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from jsonschema import Draft202012Validator
from proteinrsi.cli import main
from proteinrsi.contracts import TaskSpec, Workflow, digest
from proteinrsi.localtools.artifacts import ArtifactStore, file_sha256
from proteinrsi.localtools.catalog import descriptions, tool_spec
from proteinrsi.localtools.config import EngineConfig, LocalToolsConfig, load_config
from proteinrsi.localtools.execution import LocalJob, clean_env, inspect_engine
from proteinrsi.localtools.functions import sequence_qc, inspect_structure, interface_geometry, compare_structures, validate_msa, proteinmpnn_design, protenix_predict
from proteinrsi.localtools.pdbio import read_pdb, write_normalized
from proteinrsi.localtools.registry import register_local_tools, configured_names
from proteinrsi.runtime import Campaign
from proteinrsi.tools import ToolCall, ToolGateway

def pdb_fixture(path, chain_sequences=None, shift=0):
    three = {'A': 'ALA', 'C': 'CYS', 'D': 'ASP', 'E': 'GLU', 'V': 'VAL', 'G': 'GLY'}
    lines = []
    serial = 0
    for ci, (chain, sequence) in enumerate((chain_sequences or {'B': 'ACDE'}).items()):
        for i, aa in enumerate(sequence, 1):
            for ai, atom in enumerate(('N', 'CA', 'C', 'O')):
                serial += 1
                x, y, z = (i * 3.8 + shift, ci * 4.0 + ai * 0.2, i % 2 * 0.3)
                lines.append(f'ATOM  {serial:5d} {atom:^4s} {three[aa]:3s} {chain}{i:4d}    {x:8.3f}{y:8.3f}{z:8.3f}{1.0:6.2f}{20.0:6.2f}          {atom[0]:>2s}')
        lines.append('TER')
    path.write_text('\n'.join(lines) + '\nEND\n')
    return path

@pytest.fixture
def local_campaign(tmp_path):
    task = TaskSpec(name='Fixture only', reference_sequence='ACDE', mutable_positions=[2], max_mutations=1, candidates=['ACDE', 'AVDE'], batch_size=3)
    return Campaign.initialize(str(tmp_path / 'campaign'), task, local_tools=LocalToolsConfig())

@pytest.fixture
def fake_mpnn(tmp_path, monkeypatch):
    import proteinrsi.localtools.execution as execution
    monkeypatch.setitem(execution.IMPORTS, 'proteinmpnn', ['json'])
    repo = tmp_path / 'fake mpnn repo'
    repo.mkdir()
    script = repo / 'protein_mpnn_run.py'
    script.write_text('import json, os, sys\nfrom pathlib import Path\nargs=dict(zip(sys.argv[1::2],sys.argv[2::2]))\nassert \'PROTEINRSI_API_KEY\' not in os.environ\nassert \'HTTP_PROXY\' not in os.environ\nassert os.environ[\'HF_HUB_OFFLINE\']==\'1\'\nassert args[\'--pdb_path_chains\']==\'B\'\nfixed=json.loads(Path(args[\'--fixed_positions_jsonl\']).read_text())\nassert fixed[\'input\'][\'B\']==[1,3,4]\nout=Path(args[\'--out_folder\'])/\'seqs\';out.mkdir(parents=True)\nn=int(args[\'--num_seq_per_target\'])\n(out/\'input.fa\').write_text(">native, designed_chains=[\'B\']\\nACDE\\n" +\n \'\'.join(f\'>sample={i}, score=1.0\\nAVDE\\n\' for i in range(n)))\n')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'FAKE engine fixture'], check=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    assets = tmp_path / 'fake assets'
    assets.mkdir()
    (assets / 'v_48_020.pt').write_text('NOT MODEL WEIGHTS; fixture')
    return EngineConfig(enabled=True, repo=str(repo), revision=sha, python=sys.executable, assets=str(assets), asset_sha256={'v_48_020.pt': file_sha256(assets / 'v_48_020.pt')}, model_name='v_48_020', cuda_devices='', license_reviewed=True)

def test_all_descriptions_have_schemas_and_limits():
    items = descriptions()
    assert len(items) == 13
    assert len({d['name'] for d in items}) == 13
    for d in items:
        spec = tool_spec(d['name'], 'test')
        assert spec.description
        Draft202012Validator.check_schema(spec.input_schema)
        Draft202012Validator.check_schema(spec.output_schema)
        assert spec.input_schema['additionalProperties'] is False

def test_templates_do_not_pretend_engines_are_installed():
    config = load_config('configs/protein_tools.json')
    assert set(config.engines) == {'proteinmpnn', 'rfdiffusion', 'protenix', 'pyrosetta'}
    assert not any((e.enabled for e in config.engines.values()))
    assert len(configured_names(config)) == 5

@pytest.mark.parametrize('update', [dict(revision='main'), dict(repo='relative'), dict(asset_sha256={'../bad': '0' * 64}), dict(image='example:latest'), dict(cuda_devices='0; echo hello')])
def test_config_rejects_unsafe_or_unpinned_values(update):
    with pytest.raises(ValueError):
        EngineConfig(**update)

def test_artifact_integrity_and_path_denial(local_campaign, tmp_path):
    artifacts = ArtifactStore(local_campaign.store)
    p = pdb_fixture(tmp_path / 'input.pdb')
    record = artifacts.put(p, 'pdb')
    assert artifacts.resolve(record['ref']).read_text() == p.read_text()
    with pytest.raises(ValueError):
        artifacts.resolve('/etc/passwd')
    with pytest.raises(ValueError):
        artifacts.resolve(record['ref'], kind='a3m')
    stored = artifacts.resolve(record['ref'])
    stored.chmod(420)
    stored.write_text('tampered')
    with pytest.raises(ValueError):
        artifacts.resolve(record['ref'])

def test_science_utilities(local_campaign, tmp_path):
    a = ArtifactStore(local_campaign.store)
    first = a.put(pdb_fixture(tmp_path / 'a.pdb', {'A': 'ACDE', 'B': 'AVDE'}), 'pdb')['ref']
    second = a.put(pdb_fixture(tmp_path / 'b.pdb', {'A': 'ACDE', 'B': 'AVDE'}, shift=12), 'pdb')['ref']
    assert inspect_structure({'structure_ref': first}, a)['chains']['A']['sequence'] == 'ACDE'
    assert interface_geometry({'structure_ref': first, 'chain_a': 'A', 'chain_b': 'B', 'cutoff_angstrom': 8}, a)['ca_contact_pairs'] > 0
    r = compare_structures({'reference_ref': first, 'reference_chain': 'A', 'prediction_ref': second, 'prediction_chain': 'A'}, a)
    assert r['ca_rmsd_angstrom'] < 1e-08
    assert sequence_qc({'reference': 'ACDE', 'sequences': ['AVDE']})['qc'][0]['substitutions'] == [{'position': 2, 'from': 'C', 'to': 'V'}]
    with pytest.raises(ValueError):
        interface_geometry({'structure_ref': first, 'chain_a': 'A', 'chain_b': 'A', 'cutoff_angstrom': 8}, a)

def test_a3m_query_matches(local_campaign, tmp_path):
    p = tmp_path / 'x.a3m'
    p.write_text('>query\nACDE\n>homolog\nAV-DE\n')
    artifacts = ArtifactStore(local_campaign.store)
    ref = artifacts.put(p, 'a3m')['ref']
    with pytest.raises(ValueError):
        validate_msa({'msa_ref': ref, 'sequence': 'ACDE'}, artifacts)
    p.write_text('>query\nACDE\n>homolog\nAVd-E\n')
    ref = artifacts.put(p, 'a3m')['ref']
    assert validate_msa({'msa_ref': ref, 'sequence': 'ACDE'}, artifacts)['records'] == 2
    with pytest.raises(ValueError):
        validate_msa({'msa_ref': ref, 'sequence': 'AVDE'}, artifacts)

def test_normalization_preserves_explicit_mapping(tmp_path):
    source = pdb_fixture(tmp_path / 'x.pdb')
    chains = read_pdb(source, complete_backbone=True)
    destination = tmp_path / 'y.pdb'
    write_normalized(chains, destination, rename={'B': 'A'})
    assert read_pdb(destination)['A']['sequence'] == 'ACDE'

def test_fake_mpnn_end_to_end_real_subprocess(local_campaign, fake_mpnn, tmp_path, monkeypatch):
    monkeypatch.setenv('PROTEINRSI_API_KEY', 'MUST_NOT_REACH_WORKER')
    monkeypatch.setenv('HTTP_PROXY', 'MUST_NOT_REACH_WORKER')
    artifact = ArtifactStore(local_campaign.store).put(pdb_fixture(tmp_path / 'x.pdb'), 'pdb')['ref']
    config = LocalToolsConfig(engines={'proteinmpnn': fake_mpnn})
    gateway = ToolGateway(local_campaign.store)
    register_local_tools(gateway, local_campaign.store, config)
    args = {'backbone_ref': artifact, 'reference': 'ACDE', 'mutable_positions': [2], 'design_chain': 'B', 'num_sequences': 2, 'temperature': 0.1, 'seed': 17}
    for _ in range(2):
        result = gateway.call(ToolCall(name='proteinmpnn_design', arguments=args), local_campaign.view().task, allowed=['proteinmpnn_design'], context_key='one')
        assert [c['sequence'] for c in result['candidates']] == ['AVDE', 'AVDE']
    assert len(local_campaign.store.all('local_jobs')) == 1
    assert local_campaign.store.usage()['tool_calls']['committed'] == 1
    assert next(iter(local_campaign.store.all('local_jobs').values()))['state'] == 'validated'

def test_doctor_detects_corrupt_weights_and_dirty_code(fake_mpnn):
    assert inspect_engine('proteinmpnn', fake_mpnn)['status'] == 'configured_not_inference_tested'
    (Path(fake_mpnn.assets) / 'v_48_020.pt').write_text('changed')
    report = inspect_engine('proteinmpnn', fake_mpnn)
    assert 'Asset hash mismatch: v_48_020.pt' in report['problems']
    (Path(fake_mpnn.repo) / 'protein_mpnn_run.py').write_text('changed')
    assert 'Tracked upstream files are modified' in inspect_engine('proteinmpnn', fake_mpnn)['problems']

def test_missing_engine_fails_not_fake_result(local_campaign):
    job = LocalJob(local_campaign.store, 'proteinmpnn', EngineConfig())
    with pytest.raises(RuntimeError, match='unavailable'):
        job.run({})

def test_env_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'secret')
    assert 'AWS_SECRET_ACCESS_KEY' not in clean_env(EngineConfig(), tmp_path)

def test_fixed_residues_fail_before_engine(local_campaign, tmp_path):
    ref = ArtifactStore(local_campaign.store).put(pdb_fixture(tmp_path / 'x.pdb', {'B': 'VCDE'}), 'pdb')['ref']
    with pytest.raises(ValueError, match='Fixed backbone'):
        proteinmpnn_design({'backbone_ref': ref, 'design_chain': 'B'}, local_campaign.view().task, local_campaign.store, EngineConfig())

def test_msa_mode_must_be_explicit(local_campaign):
    args = {'sequence': 'AVDE', 'assembly': 'monomer', 'msa_mode': 'none', 'msas': {'B': {'unpaired': 'bad'}}}
    with pytest.raises(ValueError, match='MSA-free'):
        protenix_predict(args, local_campaign.view().task, local_campaign.store, EngineConfig())

def test_cli_inventory_and_import(local_campaign, tmp_path, capsys):
    main(['tools', 'list', '--config', 'configs/protein_tools.json'])
    assert len(json.loads(capsys.readouterr().out)) == 13
    main(['tools', 'describe', '--name', 'proteinmpnn_design'])
    assert json.loads(capsys.readouterr().out)['capability'] == 'sequence.inverse_fold'
    p = pdb_fixture(tmp_path / 'x.pdb')
    main(['artifact-import', '--campaign', str(local_campaign.store.root), '--file', str(p), '--kind', 'pdb'])
    assert json.loads(capsys.readouterr().out)['ref'].startswith('artifact:')

def test_workflow_legacy_identity_preserved():
    w = Workflow()
    previous = w.model_dump()
    previous.pop('design_tool_rounds')
    previous.pop('analysis_tool_rounds')
    assert w.version == 'w-' + digest(previous)[:16]

def test_bounded_tool_dialogue_and_analyst_tools(local_campaign):
    from proteinrsi.agents import Team
    gateway = ToolGateway(local_campaign.store)
    calls = []
    from proteinrsi.tools import ToolSpec
    gateway.register(ToolSpec(name='fixture', capability='structure.inspect', description='fixture', implementation_version='fake-v1', task_kinds=['variant_design'], input_schema={'type': 'object'}, output_schema={'type': 'object'}), lambda a: calls.append(a) or {'stage': a['stage']})

    class Scripted:
        b = 0
        c = 0

        def complete(self, role, instructions, context, schema):
            if role == 'A':
                return {'rationale': 'test'}
            if role == 'B':
                self.b += 1
                if self.b < 3:
                    return {'tool_calls': [{'name': 'fixture', 'arguments': {'stage': self.b}}]}
                assert context['tool_results'][-1]['stage'] == 2
                return {'candidates': [{'sequence': 'AVDE'}]}
            if role == 'C-tools':
                self.c += 1
                return {'rationale': 'test', 'tool_calls': [] if self.c == 2 else [{'name': 'fixture', 'arguments': {'stage': 3}}]}
            if role == 'C':
                return {'ranking': ['AVDE'], 'summary': 'test'}
            if role == 'A-selection':
                return {'ranking': ['AVDE'], 'summary': 'test'}
            raise AssertionError(role)
    view = local_campaign.view()
    view.workflow.tool_names = ['fixture']
    view.workflow.analysis_tool_rounds = 2
    result = Team(local_campaign.store, Scripted(), gateway).run(view)
    assert result[0].sequence == 'AVDE'
    assert len(calls) == 3

def unchecked_fixture_engine(tmp_path, monkeypatch, name, script_text, **kwargs):
    """A deliberately FAKE upstream program; bypass only production asset preflight."""
    import proteinrsi.localtools.execution as execution
    from proteinrsi.localtools.execution import ENTRYPOINTS
    repo = tmp_path / name
    repo.mkdir()
    script = repo / ENTRYPOINTS[name]
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(script_text)
    monkeypatch.setattr(execution, 'inspect_engine', lambda *a, **k: {'status': 'runtime_checked', 'revision': '0' * 40, 'runtime': {'fixture': True}})
    return EngineConfig(enabled=True, python=sys.executable, repo=str(repo), assets=str(repo), model_name='fixture', cuda_devices='', license_reviewed=True, **kwargs)

def test_rfdiffusion_roles_not_assumed(tmp_path, monkeypatch):
    from proteinrsi.localtools.functions import rfdiffusion_binder
    payload = pdb_fixture(tmp_path / 'source.pdb', {'A': 'G' * 10, 'B': 'ACDE'}).read_text()
    code = f"import sys\nfrom pathlib import Path\nassert any('contigmap.contigs=[A1-4/0 10-10]' == a for a in sys.argv)\nassert 'inference.design_startnum=17' in sys.argv\nPath('outputs').mkdir(exist_ok=True)\nPath('outputs/design_17.pdb').write_text({payload!r})\n"
    config = unchecked_fixture_engine(tmp_path, monkeypatch, 'rfdiffusion', code)
    task = TaskSpec(name='Synthetic binder fixture', kind='binder_design', reference_sequence='G' * 10, target_sequence='ACDE', mutable_positions=list(range(1, 11)), max_mutations=10)
    c = Campaign.initialize(str(tmp_path / 'c'), task)
    ref = ArtifactStore(c.store).put(pdb_fixture(tmp_path / 'target.pdb', {'X': 'ACDE'}), 'pdb')['ref']
    result = rfdiffusion_binder({'target_ref': ref, 'target_chain': 'X', 'length': 10, 'num_designs': 1, 'seed': 17, 'hotspots': []}, task, c.store, config)
    assert result['backbones'][0]['target_chain'] == 'B'
    assert result['backbones'][0]['design_chain'] == 'A'
    assert 'candidates' not in result

def test_protenix_direct_runner_offline_and_chain_validation(local_campaign, tmp_path, monkeypatch):
    pytest.importorskip('Bio')
    pdb = pdb_fixture(tmp_path / 'temp.pdb', {'B': 'AVDE'}).read_text()
    code = f"import sys\nfrom pathlib import Path\nfrom Bio.PDB import PDBParser,MMCIFIO\nargs=dict(zip(sys.argv[1::2],sys.argv[2::2]))\nassert args['--use_msa']=='false'\nassert args['--use_template']=='false'\nassert '--use_msa_server' not in args\nPath('tmp.pdb').write_text({pdb!r})\nwriter=MMCIFIO();writer.set_structure(PDBParser(QUIET=True).get_structure('p','tmp.pdb'))\nPath('outputs').mkdir()\nwriter.save('outputs/prediction.cif')\n"
    config = unchecked_fixture_engine(tmp_path, monkeypatch, 'protenix', code)
    args = {'sequence': 'AVDE', 'assembly': 'monomer', 'msa_mode': 'none', 'msas': {}, 'seeds': [17], 'samples': 1, 'steps': 20, 'cycles': 4, 'dtype': 'fp32'}
    result = protenix_predict(args, local_campaign.view().task, local_campaign.store, config)
    assert result['input_entities'] == {'B': 'AVDE'}
    assert {a['kind'] for a in result['artifacts']} == {'cif', 'pdb'}
    assert 'not calibrated' in result['warning']

@pytest.mark.parametrize('scenario', ['timeout', 'output_quota', 'network'])
def test_worker_failure_modes(local_campaign, tmp_path, monkeypatch, scenario):
    code = {'timeout': 'import time; time.sleep(15)', 'output_quota': "from pathlib import Path; Path('huge').write_bytes(b'x'*(2*1024*1024))", 'network': "import socket; socket.create_connection(('example.com',443),timeout=1)"}[scenario]
    config = unchecked_fixture_engine(tmp_path, monkeypatch, 'proteinmpnn', code, timeout_seconds=1, max_output_mb=1)
    job = LocalJob(local_campaign.store, 'proteinmpnn', config)
    args = {'design_chain': 'B', 'fixed_positions': [], 'num_sequences': 1, 'temperature': 0.1, 'seed': 17}
    with pytest.raises((RuntimeError, TimeoutError)):
        job.run(args)
    assert local_campaign.store.get('local_jobs', job.job_id)['state'] == 'failed'

def test_artifact_symlink_rejected(local_campaign, tmp_path):
    p = tmp_path / 'secret'
    p.write_text('not for tool')
    link = tmp_path / 'link.pdb'
    link.symlink_to(p)
    with pytest.raises(ValueError):
        ArtifactStore(local_campaign.store).put(link, 'pdb')

def test_multiple_pdb_models_rejected(tmp_path):
    p = pdb_fixture(tmp_path / 'x.pdb')
    text = p.read_text()
    p.write_text('MODEL        1\n' + text + 'ENDMDL\nMODEL        2\n' + text)
    with pytest.raises(ValueError, match='Multiple PDB'):
        read_pdb(p)

def test_isolated_esmc_setting_is_not_a_chat_model(tmp_path):
    from proteinrsi.protein.esmc import ESMCConfig, ESMC600M
    from proteinrsi.protein.isolated import IsolatedESMCBackend
    from proteinrsi.storage import Store
    config = ESMCConfig(worker_python=sys.executable)
    model = ESMC600M(Store(tmp_path / 'c'), config)
    assert isinstance(model.backend, IsolatedESMCBackend)
    with pytest.raises(ValueError):
        ESMCConfig(worker_python='relative-python')

def test_analyst_cannot_generate_new_candidates(local_campaign):
    from proteinrsi.agents import Team
    from proteinrsi.tools import ToolSpec
    gateway = ToolGateway(local_campaign.store)
    gateway.register(ToolSpec(name='generator', capability='sequence.inverse_fold', implementation_version='fake', task_kinds=['variant_design'], input_schema={'type': 'object'}, output_schema={'type': 'object'}), lambda a: {})

    class Fake:

        def complete(self, role, instructions, context, schema):
            if role == 'A':
                return {'rationale': 'test'}
            if role == 'B':
                return {'candidates': [{'sequence': 'AVDE'}]}
            if role == 'C-tools':
                assert not context['available_tools']
                return {'rationale': 'test', 'tool_calls': [{'name': 'generator', 'arguments': {}}]}
            raise AssertionError(role)
    view = local_campaign.view()
    view.workflow.tool_names = ['generator']
    view.workflow.analysis_tool_rounds = 2
    with pytest.raises(PermissionError):
        Team(local_campaign.store, Fake(), gateway).run(view)


def test_explicit_protein_config_overrides_missing_worker_template(tmp_path, capsys):
    campaign = tmp_path / "campaign"
    main(["init", "--task", "examples/wetlab_task.json", "--out", str(campaign),
          "--local-tools", "configs/protein_tools.json",
          "--protein-config", "examples/esmc600m.json"])
    from proteinrsi.storage import Store
    store = Store(campaign)
    assert store.get("configuration", "protein_model")["worker_python"] is None
    assert store.get("configuration", "local_tools")["esmc"]["worker_python"] is None
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
