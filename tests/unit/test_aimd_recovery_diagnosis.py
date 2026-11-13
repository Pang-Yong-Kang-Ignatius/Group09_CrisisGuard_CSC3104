from dataclasses import replace
from datetime import timedelta
import pytest
from cloud.controller.admission import Settings,assess,advance
from evaluation.experiments.run_recovery_matrix import admission_settings
from tests.unit.test_admission import NOW,metrics
from docs.evidence.analyze_h1_h2_admission_20261101 import live_counts


def study_config(**kwargs):
    return replace(Settings(),normal_rps=20,caution_rps=5,recovery_rps=2,
                   poll_seconds=1,max_age_seconds=5,timeout_seconds=1,
                   dwell_seconds=3,healthy_samples=2,additive_rps=2,
                   healthy_latency_ms=50,caution_latency_ms=80,recovery_error_rate=.15,**kwargs)


@pytest.mark.parametrize('offered,tier',[(36,1),(84,2)])
def test_good_latency_reads_still_trigger_legacy_throughput_guard(monkeypatch,offered,tier):
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY','aimd')
    config=study_config();samples=metrics();samples['patient'].update(throughput_rps=offered,average_latency_ms=7,error_rate=0)
    severity,reason,healthy,_=assess(samples,config,NOW)
    assert severity==tier and reason=='patient:throughput_rps' and not healthy
    row=dict(state='Recovery',state_since=NOW-timedelta(seconds=30),healthy_count=0,
             rate_rps=20,rate_healthy_count=0,reason='outage')
    for index in range(8):advance(row,severity,healthy,NOW+timedelta(seconds=index),config)
    assert row['rate_rps']==2 and row['state']=='Recovery'


def test_hysteresis_holds_recovery_but_genuinely_healthy_windows_restore_it(monkeypatch):
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY','aimd')
    config=study_config()
    row=dict(state='Recovery',state_since=NOW-timedelta(seconds=30),healthy_count=0,
             rate_rps=2,rate_healthy_count=0,reason='outage')
    advance(row,1,False,NOW,config)
    assert row['state']=='Recovery'
    for i in range(1,8):advance(row,0,True,NOW+timedelta(seconds=i),config)
    assert row['state']=='Normal' and row['rate_rps']>2


@pytest.mark.parametrize('bad', ['stale','database','latency','errors'])
def test_throughput_configuration_cannot_disable_other_safety_signals(bad):
    # Illustrative unit values only, not a recommended/calibrated profile.
    config=study_config(healthy_throughput_rps=100,caution_throughput_rps=150,recovery_throughput_rps=200)
    samples=metrics();samples['patient']['throughput_rps']=84
    if bad=='stale':samples=metrics(NOW-timedelta(seconds=6))
    elif bad=='database':samples['patient']['database_healthy']=False
    elif bad=='latency':samples['patient']['average_latency_ms']=1001
    else:samples['patient']['error_rate']=.2
    assert assess(samples,config,NOW)[0]==2


@pytest.mark.parametrize('profile',[(0,20,50),(10,10,50),(50,20,10),(10,float('nan'),50),(10,20,float('inf'))])
def test_experiment_profile_rejects_unsafe_thresholds(profile):
    with pytest.raises(ValueError):admission_settings('aimd',20,profile)


def test_experiment_profile_is_opt_in_and_changes_only_throughput_settings():
    baseline=admission_settings('aimd',20)
    changed=admission_settings('aimd',20,(100,150,200))
    prefix='CONTROLLER_ADMISSION_'
    original={k:v for k,v in baseline.items() if k.startswith(prefix)}
    assert not any('THROUGHPUT' in k for k in original)
    assert all(changed[k]==v for k,v in original.items())
    assert changed[prefix+'HEALTHY_THROUGHPUT_RPS']=='100'
    assert len([k for k in changed if 'THROUGHPUT' in k])==3


def test_exposure_denominator_can_reduce_fraction_without_reducing_failures():
    failure={'error':'ConnectError','latency_ms':1000}
    early=[failure]+[{'status_code':200,'latency_ms':7} for _ in range(9)]
    longer=early+[{'status_code':200,'latency_ms':7} for _ in range(90)]
    assert live_counts(early)['violations']==live_counts(longer)['violations']==1
    assert live_counts(early)['violation_fraction']==.1
    assert live_counts(longer)['violation_fraction']==.01


@pytest.mark.parametrize('rate,now_offset,expected',[(20,.1,False),(20,.6,True),(2,.1,False)])
def test_existing_pacing_preserves_old_deadline_and_extends_on_rate_cut(monkeypatch,rate,now_offset,expected):
    from unittest.mock import Mock
    from cloud.controller import admission
    monkeypatch.setenv('CONTROLLER_ADMISSION_POLICY','aimd')
    config=study_config()
    row=dict(state='Normal',rate_rps=rate,observed_at=NOW,last_issue_at=NOW,
             next_issue_at=NOW+timedelta(seconds=.5 if rate==20 else .05))
    monkeypatch.setattr(admission,'settings',lambda:config)
    monkeypatch.setattr(admission,'load',lambda cur,cfg:(row,NOW+timedelta(seconds=now_offset)))
    cur=Mock();cur.fetchone.return_value={'count':0}
    assert admission.reserve(cur,'issue') is expected
    decision=cur.execute.call_args.args[1][-1].obj
    assert decision['allowed'] is expected
    assert decision['reason']==('admitted' if expected else 'rate_limit')
