import pytest
from evaluation.experiments.analyze_recovery_matrix import generator_accuracy


def trial(n=1000, missing=1):
    return dict(configuration={'live_read_rps':100}, live_generator_stop_seconds=n/100,
                live_target_opportunities=n, live_generator_lag_skips=missing,
                live_reads=[{'offered_index':i+1,'status_code':500} for i in range(n-missing)])


def test_accuracy_counts_cloud_errors_as_issued_not_skipped():
    result=generator_accuracy(trial())
    assert result['ledger_reconciled']
    assert result['issued_attempts']==999 and result['skip_fraction']==.001
    assert result['issuance_ratio']==.999 and result['issued_rps']==99.9


@pytest.mark.parametrize('bad', ['missing_target','bad_clock','unaccounted','double_count','duplicate','negative','zero'])
def test_unknown_or_inconsistent_load_is_never_valid(bad):
    data=trial()
    if bad=='missing_target': del data['live_target_opportunities']
    elif bad=='bad_clock': data['live_generator_stop_seconds']=9
    elif bad=='unaccounted': data['live_generator_lag_skips']=0
    elif bad=='double_count': data['live_generator_cap_skips']=1
    elif bad=='duplicate': data['live_reads'][-1]['offered_index']=1
    elif bad=='negative': data['live_generator_lag_skips']=-1
    elif bad=='zero': data=trial(0,0)
    result=generator_accuracy(data)
    assert not result['ledger_reconciled'] and result['skip_fraction'] is None


def test_cap_and_lag_skips_both_consume_accuracy_budget():
    data=trial(2000,1)
    data['live_reads'].pop()
    data['live_generator_cap_skips']=1
    result=generator_accuracy(data)
    assert result['ledger_reconciled'] and result['skip_fraction']==.001
    assert result['cap_skips']==result['lag_skips']==1
