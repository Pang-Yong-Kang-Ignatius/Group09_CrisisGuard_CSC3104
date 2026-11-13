"""AWS preflight rejects unsafe settings without altering local development."""
from copy import deepcopy
import json
from subprocess import CompletedProcess

import pytest

from infrastructure.scripts import audit_deployment as audit


def service():
    return {'restart': 'unless-stopped', 'healthcheck': {'test': ['CMD', 'health']},
            'logging': {'driver': 'json-file', 'options': {'max-size': '10m', 'max-file': '3'}}}


def cloud():
    config = {'name': 'team-cloud', 'services': {name: service() for name in ('patient','emergency','admin','postgres')}}
    pg = config['services']['postgres']
    pg['environment'] = {'POSTGRES_USER': 'team', 'POSTGRES_DB': 'team', 'POSTGRES_PASSWORD': 'a'*48}
    pg['volumes'] = [{'type': 'volume', 'source': 'postgres-data', 'target': '/var/lib/postgresql/data'}]
    for name,port in [('patient',8001),('emergency',8003),('admin',8004)]:
        config['services'][name].update(environment={'DATABASE_URL': 'postgresql://team:'+'a'*48+'@postgres:5432/team'},
            ports=[{'host_ip':'172.31.1.2','published':str(port),'target':8000}])
    return config


def hospital():
    value = service()
    value.update(environment={'HOSPITAL_DB_PATH':'/data/hospital.db','SITE_ID':'Hospital_A','AUTO_REPLAY':'1',
        'CLOUD_URL':'http://172.31.1.2:8001/events','EMERGENCY_URL':'http://172.31.1.2:8003/events',
        'ADMIN_URL':'http://172.31.1.2:8004/events'},
        ports=[{'host_ip':'127.0.0.1','published':'8000','target':8000}],
        volumes=[{'type':'volume','source':'hospital-data','target':'/data'}])
    return {'name':'team-hospital-a','services':{'hospital':value}}


@pytest.mark.parametrize('role,factory', [('cloud',cloud),('hospital',hospital)])
def test_valid_configuration_and_input_untouched(role,factory):
    config=factory()
    before=deepcopy(config)
    assert audit.audit(role,config)['result']=='passed'
    assert config==before


@pytest.mark.parametrize('fault', ['public_bind','wildcard_bind','db_port','weak_password','wrong_database',
    'duplicate_ports','missing_health','unbounded_logs','no_restart','bind_database','readonly_database','placeholder'])
def test_cloud_rejects_unsafe_settings(fault):
    config=cloud()
    pg,patient=config['services']['postgres'],config['services']['patient']
    if fault=='public_bind': patient['ports'][0]['host_ip']='8.8.8.8'
    elif fault=='wildcard_bind': patient['ports'][0]['host_ip']='0.0.0.0'
    elif fault=='db_port': pg['ports']=[{'published':'5432','target':5432}]
    elif fault=='weak_password': pg['environment']['POSTGRES_PASSWORD']='crisisguard_dev'
    elif fault=='wrong_database': patient['environment']['DATABASE_URL']='postgresql://other'
    elif fault=='duplicate_ports': patient['ports'][0]['published']='8003'
    elif fault=='missing_health': pg['healthcheck']['disable']=True
    elif fault=='unbounded_logs': patient['logging']['options']={}
    elif fault=='no_restart': patient.pop('restart')
    elif fault=='bind_database': pg['volumes'][0]['type']='bind'
    elif fault=='readonly_database': pg['volumes'][0]['read_only']=True
    else: config['name']='REPLACE_PROJECT'
    with pytest.raises(ValueError): audit.audit('cloud',config)


@pytest.mark.parametrize('fault', ['public_capture','missing_site','manual_replay','bad_interval','missing_admin',
    'public_cloud','same_endpoint','different_cloud','embedded_credentials','query','unpersisted_path'])
def test_hospital_rejects_incomplete_or_unsafe_settings(fault):
    config=hospital()
    svc=config['services']['hospital']
    env=svc['environment']
    if fault=='public_capture': svc['ports'][0]['host_ip']='0.0.0.0'
    elif fault=='missing_site': env.pop('SITE_ID')
    elif fault=='manual_replay': env['AUTO_REPLAY']='0'
    elif fault=='bad_interval': env['AUTO_REPLAY_INTERVAL_SECONDS']='nan'
    elif fault=='missing_admin': env.pop('ADMIN_URL')
    elif fault=='public_cloud': env['CLOUD_URL']='http://8.8.8.8:8001/events'
    elif fault=='same_endpoint': env['ADMIN_URL']=env['CLOUD_URL']
    elif fault=='different_cloud': env['ADMIN_URL']='http://172.31.2.3:8004/events'
    elif fault=='embedded_credentials': env['ADMIN_URL']='http://user:secret@172.31.1.2:8004/events'
    elif fault=='query': env['ADMIN_URL']+='?token=secret'
    else: env['HOSPITAL_DB_PATH']='/tmp/hospital.db'
    with pytest.raises(ValueError): audit.audit('hospital',config)


@pytest.mark.parametrize('command_failure',[False,True])
def test_cli_is_config_only_and_suppresses_private_values(tmp_path,monkeypatch,capsys,command_failure):
    env=tmp_path/'private.env'
    env.write_text('PASSWORD=DO_NOT_PRINT')
    calls=[]
    def fake(command,**kwargs):
        calls.append(command)
        config=cloud()
        config['services']['postgres']['environment']['POSTGRES_PASSWORD']='DO_NOT_PRINT'
        return CompletedProcess(command,1 if command_failure else 0,json.dumps(config),'DO_NOT_PRINT')
    monkeypatch.setattr(audit.subprocess,'run',fake)
    assert audit.main(['cloud','--env-file',str(env)])==1
    assert len(calls)==1 and calls[0][-3:]==['config','--format','json']
    assert 'DO_NOT_PRINT' not in capsys.readouterr().out


@pytest.mark.parametrize('name',['.env','.env.local','infrastructure/aws/cloud.env','labsuser.pem',
    'local.private.json','.aws/credentials','release-checkpoints/checkpoint.json','hospital.db-wal'])
def test_repository_audit_rejects_private_artifact_paths(name):
    with pytest.raises(ValueError): audit.audit_repository([name])


def test_repository_audit_allows_examples_and_sanitized_evidence():
    assert audit.audit_repository(['.env.example','infrastructure/aws/cloud.env.example',
        'docs/evidence/aws-outage-20261023.json'])['result']=='passed'
