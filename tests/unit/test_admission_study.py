import copy
from types import SimpleNamespace
import pytest
from evaluation.experiments.run_recovery_matrix import (
    admission_plan, reuse_admission_trials, admission_settings, REVIEWED_GENERATOR_SHA256,
)


def fixture():
    cases=admission_plan(120)
    case=cases[0]
    args=SimpleNamespace(live_read_target='preseed',live_slo_ms=250,live_workers=128,
                         poll_seconds=.25,timeout=300,activation='automatic',admission_ceiling=20.)
    config=dict(live_read_rps=case['live_read_rps'],live_read_target='preseed',live_slo_ms=250,
                live_concurrency_cap=128,poll_seconds=.25,drain_timeout_seconds=300,
                replay_activation='automatic',scheduling='bounded',bypass_limit=1,
                inheritance='1',local_selection='priority',admission_mode='aimd',
                reference_event_cost_units={e['event_id']:1 for e in case['workload']['events']},
                controller={k:v for k,v in admission_settings('aimd',20).items() if k.startswith('CONTROLLER_ADMISSION_')})
    sources={'edge/example.py':'same','evaluation/workloads/generate_workload.py':'same'}
    trial=dict(condition=case['condition'],seed=case['seed'],policy='bounded_on',configuration=config,
               result='passed',cleanup='passed',live_generator_stop_seconds=1,live_target_opportunities=36,
               live_generator_lag_skips=0,live_reads=[{'status_code':200,'latency_ms':1} for _ in range(36)],
               samples=[],completion_intervals={e['event_id']:[1,2] for e in case['workload']['events']})
    raw=dict(design={'cases':[copy.deepcopy(case)],'source_sha256':dict(sources,**{'evaluation/experiments/run_recovery_matrix.py':REVIEWED_GENERATOR_SHA256})},
             trials=[trial],source_unchanged_during_run=True,preservation={'states':True})
    return cases,raw,args,sources


def test_reduced_study_is_20_paired_arms_in_two_conditions():
    cases=admission_plan(120)
    assert len(cases)==10 and sum(len(c['arms']) for c in cases)==20
    assert {c['seed'] for c in cases}==set(range(3104,3109))
    assert {c['live_read_rps'] for c in cases}=={36,84}
    assert all(set(c['arms'])=={('bounded_on','aimd'),('bounded_on','constant')} for c in cases)
    assert cases==admission_plan(120)
    for seed in range(3104,3109):
        assert len({str(c['workload']) for c in cases if c['seed']==seed})==1
    assert len({tuple(c['arms']) for c in cases})==2


def test_only_compatible_arm_removed_without_touching_evidence():
    cases,raw,args,sources=fixture()
    previous=copy.deepcopy(raw)
    reused=reuse_admission_trials(cases,[raw],args,sources)
    assert len(reused)==1 and reused[0]['admission']=='aimd'
    assert sum(len(c['arms']) for c in cases)==19
    assert raw==previous
    with pytest.raises(ValueError,match='Duplicate'):
        reuse_admission_trials(cases,[raw],args,sources)


@pytest.mark.parametrize('change',['source','generator','workload','workers','admission','costs','invalid'])
def test_incompatible_reuse_is_rejected(change):
    cases,raw,args,sources=fixture()
    cfg=raw['trials'][0]['configuration']
    if change=='source': raw['design']['source_sha256']['edge/example.py']='changed'
    elif change=='generator': raw['design']['source_sha256']['evaluation/experiments/run_recovery_matrix.py']='unreviewed'
    elif change=='workload': raw['design']['cases'][0]['workload']['events'][0]['payload']['changed']=True
    elif change=='workers': cfg['live_concurrency_cap']=64
    elif change=='admission': cfg['controller']['CONTROLLER_ADMISSION_NORMAL_RPS']='10'
    elif change=='costs': cfg['reference_event_cost_units']={}
    elif change=='invalid': raw['preservation']['states']=False
    with pytest.raises(ValueError): reuse_admission_trials(cases,[raw],args,sources)


@pytest.mark.parametrize('git_head', ['a' * 40, None], ids=['git-checkout', 'github-zip'])
def test_exact_single_arm_replacement_plan(tmp_path,monkeypatch,git_head):
    import hashlib,sys,json
    from types import SimpleNamespace
    from evaluation.experiments import run_recovery_matrix as matrix
    from infrastructure.scripts import project_verify as provenance
    out=tmp_path/'replacement'
    source=tmp_path/'source'
    source.mkdir()
    monkeypatch.setattr(provenance, 'ROOT', source)
    if git_head:
        (source/'.git').mkdir()
        monkeypatch.setattr(provenance, 'run', lambda command, **kwargs: SimpleNamespace(
            returncode=0, stdout=(str(source) if command[-1]=='--show-toplevel' else git_head)+'\n'))
    else:
        monkeypatch.setattr(provenance, 'run', lambda *args, **kwargs: pytest.fail('Git called for a ZIP'))
    monkeypatch.setattr(sys,'argv',['run','--mode','admission','--capacity-rps','120',
        '--activation','automatic','--live-read-target','preseed','--live-slo-ms','250',
        '--acceptance-condition','ratio1-1_depth3_balanced_load70','--acceptance-seed','3108',
        '--admission-arm','constant','--plan-only','--output',str(out)])
    matrix.main()
    design=json.loads((out/'plan.json').read_text())
    assert design['git_head'] == git_head
    assert design['git_head_status'] == ('PASS' if git_head else 'NOT VERIFIED')
    generator = matrix.ROOT / 'evaluation/experiments/run_recovery_matrix.py'
    assert design['source_sha256']['evaluation/experiments/run_recovery_matrix.py'] == hashlib.sha256(generator.read_bytes()).hexdigest()
    assert len(design['cases'])==1
    assert design['cases'][0]['arms']==[['bounded_on','constant']]
    assert design['generator_protocol']=='serialized-stop-v2'
