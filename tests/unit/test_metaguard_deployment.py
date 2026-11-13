"""Opt-in deployment gates; no AWS clients or private configuration access."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from infrastructure.scripts import audit_deployment as audit
from tests.unit.test_deployment_audit import cloud, hospital, service


def cloud_config():
    config=cloud()
    identities=json.dumps({'Hospital_A':'a'*64,'Hospital_B':'b'*64})
    controller=service()
    controller.update(environment={'DATABASE_URL':config['services']['patient']['environment']['DATABASE_URL'],
        'CONTROLLER_HOSPITAL_TOKENS':identities,'CONTROLLER_CLOUD_TOKEN':'c'*64,'CONTROLLER_ADMISSION_ENABLED':'1',
        'CONTROLLER_ADMISSION_POLICY':'fixed','CONTROLLER_ADMISSION_ADDITIVE_RPS':'0.2'},
        ports=[{'host_ip':'172.31.1.2','published':'8005','target':8000}],
        depends_on={'postgres':{'condition':'service_healthy'}},
        healthcheck={'test':['CMD','python','/health get_connection']})
    config['services']['controller']=controller
    for name in ('patient','emergency','admin'):
        config['services'][name]['environment'].update(METAGUARD_REQUIRED='1', CONTROLLER_HOSPITAL_TOKENS=identities)
    return config


def hospital_config():
    config=hospital()
    config['services']['hospital']['environment'].update(METAGUARD_ENABLED='1',
        CONTROLLER_URL='http://172.31.1.2:8005', HOSPITAL_CONTROLLER_TOKEN='a'*64)
    return config


@pytest.mark.parametrize('role,factory',[('cloud',cloud_config),('hospital',hospital_config)])
def test_valid_opt_in_deployment_is_read_only(role,factory):
    config=factory()
    before=deepcopy(config)
    assert audit.audit(role,config)['result']=='passed'
    assert config==before


@pytest.mark.parametrize('policy', ['fixed', 'aimd'])
def test_accept_explicit_admission_policy(policy):
    config = cloud_config()
    config['services']['controller']['environment']['CONTROLLER_ADMISSION_POLICY'] = policy
    assert audit.audit('cloud', config)['result'] == 'passed'


@pytest.mark.parametrize('policy', [None, '', 'adaptive', 'AIMD'])
def test_reject_missing_or_unknown_admission_policy(policy):
    config = cloud_config()
    config['services']['controller']['environment']['CONTROLLER_ADMISSION_POLICY'] = policy
    with pytest.raises(ValueError, match='admission policy'):
        audit.audit('cloud', config)


@pytest.mark.parametrize('rate', [None, '', '0', '-0.1', 'nan', 'inf', 'invalid'])
def test_reject_invalid_additive_rate(rate):
    config = cloud_config()
    env = config['services']['controller']['environment']
    if rate is None:
        env.pop('CONTROLLER_ADMISSION_ADDITIVE_RPS')
    else:
        env['CONTROLLER_ADMISSION_ADDITIVE_RPS'] = rate
    with pytest.raises(ValueError):
        audit.audit('cloud', config)


@pytest.mark.parametrize('fault',['public','duplicate_port','weak_operator','reused_operator','duplicate_hospitals',
    'wrong_database','missing_dependency','liveness_only','no_admission','partial_protection','different_identities',
    'external_metrics','fractional_cap','reversed_rates','missing_controller','list_identities',
    'nan_poll','invalid_threshold','bad_sample_count'])
def test_reject_unsafe_metaguard_cloud_deployment(fault):
    config=cloud_config()
    ctl=config['services']['controller']
    env=ctl['environment']
    if fault=='public': ctl['ports'][0]['host_ip']='0.0.0.0'
    elif fault=='duplicate_port': ctl['ports'][0]['published']='8001'
    elif fault=='weak_operator': env['CONTROLLER_CLOUD_TOKEN']='short'
    elif fault=='reused_operator': env['CONTROLLER_CLOUD_TOKEN']='a'*64
    elif fault=='duplicate_hospitals': env['CONTROLLER_HOSPITAL_TOKENS']=json.dumps({'Hospital_A':'a'*64,'Hospital_B':'a'*64})
    elif fault=='wrong_database': env['DATABASE_URL']='postgresql://wrong'
    elif fault=='missing_dependency': ctl['depends_on']={}
    elif fault=='liveness_only': ctl['healthcheck']['test']=['CMD','/health']
    elif fault=='no_admission': env['CONTROLLER_ADMISSION_ENABLED']='0'
    elif fault=='partial_protection': config['services']['patient']['environment']['METAGUARD_REQUIRED']='0'
    elif fault=='different_identities': config['services']['patient']['environment']['CONTROLLER_HOSPITAL_TOKENS']='{}'
    elif fault=='external_metrics': env['CONTROLLER_PATIENT_METRICS_URL']='http://external.invalid/metrics'
    elif fault=='fractional_cap': env['CONTROLLER_ADMISSION_NORMAL_INFLIGHT']='4.5'
    elif fault=='reversed_rates': env['CONTROLLER_ADMISSION_RECOVERY_RPS']='5'
    elif fault=='missing_controller': config['services'].pop('controller')
    elif fault=='list_identities': env['CONTROLLER_HOSPITAL_TOKENS']='["Hospital_A","Hospital_B"]'
    elif fault=='nan_poll': env['CONTROLLER_ADMISSION_POLL_SECONDS']='nan'
    elif fault=='invalid_threshold': env['CONTROLLER_ADMISSION_CAUTION_LATENCY_MS']='1'
    else: env['CONTROLLER_ADMISSION_HEALTHY_SAMPLES']='1.5'
    with pytest.raises((ValueError,TypeError)):
        audit.audit('cloud',config)


@pytest.mark.parametrize('fault',['public','credentials_in_url','wrong_host','weak_token','operator_leak','peer_leak','database_leak'])
def test_reject_unsafe_metaguard_hospital_deployment(fault):
    config=hospital_config()
    env=config['services']['hospital']['environment']
    if fault=='public': env['CONTROLLER_URL']='http://8.8.8.8:8005'
    elif fault=='credentials_in_url': env['CONTROLLER_URL']='http://user:secret@172.31.1.2:8005'
    elif fault=='wrong_host': env['CONTROLLER_URL']='http://172.31.1.3:8005'
    elif fault=='weak_token': env['HOSPITAL_CONTROLLER_TOKEN']='short'
    elif fault=='operator_leak': env['CONTROLLER_CLOUD_TOKEN']='c'*64
    elif fault=='peer_leak': env['HOSPITAL_B_CONTROLLER_TOKEN']='b'*64
    else: env['DATABASE_URL']='postgresql://private'
    with pytest.raises(ValueError):
        audit.audit('hospital',config)


def test_ci_preserves_tx_jobs_and_avoids_repeated_units():
    text=(Path(__file__).resolve().parents[2]/'.github/workflows/ci.yml').read_text()
    assert '  unit-tests:' in text and '  docker-deployment:' in text
    new=text.split('  metaguard-integration:',1)[1]
    assert 'needs: unit-tests' in new and 'smoke_metaguard_deployment.py --ci' in new
    assert 'pytest tests/unit' not in new and 'run: aws ' not in new.lower()


def test_opt_in_does_not_change_launcher_bootstrap_or_expose_database():
    root=Path(__file__).resolve().parents[2]
    fragment=(root/'infrastructure/aws/metaguard-ingress.yaml').read_text()
    assert 'SourceSecurityGroupId:' in fragment and 'CidrIp:' not in fragment
    assert '5432' not in fragment
    shell=(root/'infrastructure/aws/deploy.sh').read_text()
    assert '--metaguard' in shell and 'config --quiet' in shell and '"${audit[@]}"' in shell
