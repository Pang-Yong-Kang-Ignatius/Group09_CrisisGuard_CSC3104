import time
import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from dashboard import main
from dashboard import test_traffic as module
from cloud.traffic import traffic_headers
from tests.unit.test_project_e2e import provisioned_stack, stack

LIVE='local-live-private-credential-'*2
REPLAY='local-replay-private-credential-'*2
ACTION={'X-CrisisGuard-Local-Action':'test-traffic'}
RECORDS=[('P1','patient_registration'),('E1','emergency_record'),('A1','patient_discharge')]


@pytest.fixture
def env(monkeypatch):
    for k in ('AWS_REGION','AWS_DEFAULT_REGION','AWS_EXECUTION_ENV','EC2_INSTANCE_ID','APP_ENV','ENVIRONMENT','DEPLOYMENT_ENV'):
        monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('CRISISGUARD_DEPLOYMENT_MODE','local-development')
    monkeypatch.setenv('DASHBOARD_TEST_TRAFFIC_ENABLED','1')
    monkeypatch.setenv('DATABASE_URL','postgresql://postgres:5432/test')
    monkeypatch.setenv('CLOUD_LIVE_TELEMETRY_TOKEN',LIVE)
    monkeypatch.setenv('CLOUD_REPLAY_TELEMETRY_TOKEN',REPLAY)
    # Local-demo unit tests must not inherit the CI runner's DMI identity.
    # Keep other file reads real; the EC2-veto test overrides this explicitly.
    read_text = module.Path.read_text
    def local_host_identity(path, *args, **kwargs):
        if path == module.Path('/sys/class/dmi/id/sys_vendor'):
            return 'Local development host'
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(module.Path, 'read_text', local_host_identity)
    monkeypatch.setattr(module,'INTERVAL_SECONDS',0)
    monkeypatch.setattr(module,'applied_targets',lambda:RECORDS)
    main.app.state.test_traffic.job=None


@pytest.fixture
def cloud(monkeypatch):
    calls=[]
    failures={}
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['follow_redirects'] is False
            assert kwargs['trust_env'] is False
            assert kwargs['limits'].max_connections==3
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def get(self,url,headers):
            event_id=url.rsplit('/',1)[1]
            n=1+sum(c[0]==event_id for c in calls)
            calls.append((event_id,dict(headers),asyncio.get_running_loop().time()))
            result=failures.get((event_id,n))
            if isinstance(result,Exception):raise result
            if result=='slow':await asyncio.sleep(10)
            if isinstance(result,int):return httpx.Response(result)
            if result=='invalid':return httpx.Response(200,json={'token':LIVE})
            return httpx.Response(200,json={'event_id':event_id,'event_type':dict(RECORDS)[event_id],'status':'applied'})
    monkeypatch.setattr(module.httpx,'AsyncClient',Client)
    return calls,failures


@pytest.fixture
def client(env,cloud):
    with TestClient(main.app,base_url='http://localhost',client=('127.0.0.1',1234)) as c:
        yield c


def finish(client):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        r=client.get('/api/test-traffic');assert r.status_code==200
        if not r.json()['active']:return r.json()
        time.sleep(.005)
    pytest.fail('Test job did not finish')


@pytest.mark.parametrize('key,value',[('DASHBOARD_TEST_TRAFFIC_ENABLED','0'),
                                     ('CRISISGUARD_DEPLOYMENT_MODE','production'),
                                     ('AWS_REGION','test-region'),('AWS_DEFAULT_REGION','test-region'),
                                     ('APP_ENV','production'),('DEPLOYMENT_ENV','staging')])
def test_disabled_and_production_fail_closed(client,cloud,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    assert client.get('/api/test-traffic').status_code==404
    assert client.post('/api/test-traffic',json={'count':10},headers=ACTION).status_code==404
    assert not cloud[0]


def test_default_disabled_and_ec2_veto(client,cloud,monkeypatch):
    monkeypatch.delenv('DASHBOARD_TEST_TRAFFIC_ENABLED')
    assert client.get('/api/test-traffic').status_code==404
    monkeypatch.setenv('DASHBOARD_TEST_TRAFFIC_ENABLED','1')
    monkeypatch.setattr(module.Path,'read_text',lambda self:'Amazon EC2')
    assert client.get('/api/test-traffic').status_code==404
    assert not cloud[0]


def test_nonlocal_targets_rejected(client,cloud,monkeypatch):
    monkeypatch.setitem(main.SERVICES,'patient','http://example.com')
    assert client.get('/api/test-traffic').status_code==404
    assert not cloud[0]


@pytest.mark.parametrize('database', ['postgresql://remote.example:5432/crisisguard',
                                     'postgresql://host.docker.internal:5432/crisisguard'])
def test_remote_database_disables_local_traffic(client,cloud,monkeypatch,database):
    monkeypatch.setenv('DATABASE_URL',database)
    assert client.get('/api/test-traffic').status_code==404
    assert client.post('/api/test-traffic',json={'count':10},headers=ACTION).status_code==404
    assert not cloud[0]


@pytest.mark.parametrize('body',[{'count':0},{'count':11},{'count':101},{'count':True},{'count':'10'},{'count':10.5},{'count':10,'token':LIVE}])
def test_bounded_strict_input_without_echo(client,cloud,body):
    response=client.post('/api/test-traffic',json=body,headers=ACTION)
    assert response.status_code==422
    assert LIVE not in response.text
    assert not cloud[0]


def test_bad_json_and_oversized_input(client,cloud):
    assert client.post('/api/test-traffic',content='{',headers=ACTION).status_code==422
    assert client.post('/api/test-traffic',content='x'*257,headers=ACTION).status_code==413
    assert not cloud[0]


@pytest.mark.parametrize('headers',[{},dict(ACTION,Origin='http://evil.example'),dict(ACTION,**{'Sec-Fetch-Site':'cross-site'})])
def test_local_action_and_origin_guards(client,cloud,headers):
    assert client.post('/api/test-traffic',json={'count':10},headers=headers).status_code==403
    assert not cloud[0]


@pytest.mark.parametrize('peer,base',[('203.0.113.1','http://localhost'),('127.0.0.1','http://evil.example')])
def test_nonlocal_peer_and_host_denied(env,cloud,peer,base):
    with TestClient(main.app,base_url=base,client=(peer,1234)) as c:
        assert c.post('/api/test-traffic',json={'count':10},headers=ACTION).status_code==403
    assert not cloud[0]


@pytest.mark.parametrize('token',['','short',REPLAY])
def test_existing_live_credentials_required(client,cloud,monkeypatch,token):
    monkeypatch.setenv('CLOUD_LIVE_TELEMETRY_TOKEN',token)
    response=client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    assert response.status_code==503 and LIVE not in response.text and REPLAY not in response.text
    assert not cloud[0]


@pytest.mark.parametrize('token',['','short',LIVE])
def test_replay_credentials_required_and_distinct(client,cloud,monkeypatch,token):
    monkeypatch.setenv('CLOUD_REPLAY_TELEMETRY_TOKEN',token)
    response=client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    assert response.status_code==503 and LIVE not in response.text and REPLAY not in response.text
    assert not cloud[0]


def test_success_authentication_and_no_payload_or_secret_exposure(client,cloud):
    response=client.post('/api/test-traffic',json={'count':10},headers=dict(ACTION,Authorization='Bearer browser-token'))
    assert response.status_code==202
    result=finish(client)
    assert len(cloud[0])==30
    assert {c[0] for c in cloud[0]}=={'P1','E1','A1'}
    assert all(c[1]==traffic_headers('live') for c in cloud[0])
    assert result['run']['status']=='completed'
    assert all(s['succeeded']==10 and s['failed']==0 and s['not_attempted']==0 for s in result['run']['services'].values())
    serialized=str(result)
    assert all(secret not in serialized for secret in (LIVE,REPLAY,'browser-token','P1','E1','A1'))


def test_e2e_generated_configuration_starts_bounded_live_job(provisioned_stack,client,cloud,monkeypatch):
    environment=provisioned_stack.config['services']['dashboard']['environment']
    for key in ('CLOUD_LIVE_TELEMETRY_TOKEN','CLOUD_REPLAY_TELEMETRY_TOKEN'):
        monkeypatch.delenv(key,raising=False)
    for key,value in environment.items():
        monkeypatch.setenv(key,str(value))
    response=client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    assert response.status_code==202
    result=finish(client)
    assert result['run']['status']=='completed' and len(cloud[0])==30
    assert all(call[1]==traffic_headers('live') for call in cloud[0])
    assert all(secret not in response.text+str(result) for secret in
               (environment['CLOUD_LIVE_TELEMETRY_TOKEN'],environment['CLOUD_REPLAY_TELEMETRY_TOKEN']))


def test_missing_records_do_not_generate_wrong_service_reads(client,cloud,monkeypatch):
    monkeypatch.setattr(module,'applied_targets',lambda:RECORDS[:1])
    client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    result=finish(client)['run']['services']
    assert result['patient']['succeeded']==10
    assert result['emergency']['status']=='no_applied_record' and result['emergency']['not_attempted']==10
    assert result['admin']['attempted']==0
    assert {c[0] for c in cloud[0]}=={'P1'}


def test_partial_failures_and_bad_200_are_not_successes(client,cloud):
    cloud[1]['P1',2]=503
    cloud[1]['P1',3]='invalid'
    cloud[1]['E1',1]=404
    cloud[1]['A1',1]=httpx.ConnectError(LIVE)
    client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    run=finish(client)['run'];results=run['services']
    assert run['status']=='completed_with_errors'
    assert results['patient']['succeeded']==8 and results['patient']['failed']==2
    assert results['emergency']['failed']==1 and results['emergency']['not_attempted']==9
    assert results['admin']['status']=='connection_failed' and results['admin']['failed']==1
    assert LIVE not in str(run)


def test_overlap_rejected_and_timeout_accounts_for_unconfirmed(client,cloud,monkeypatch):
    monkeypatch.setattr(module,'DEADLINE_SECONDS',.05)
    for eid,_ in RECORDS:cloud[1][eid,1]='slow'
    assert client.post('/api/test-traffic',json={'count':10},headers=ACTION).status_code==202
    assert client.post('/api/test-traffic',json={'count':10},headers=ACTION).status_code==409
    result=finish(client)
    assert result['run']['status']=='timed_out' and not result['active']
    assert len(cloud[0])==3
    assert all(s['failed']==1 and s['not_attempted']==9 for s in result['run']['services'].values())


def test_database_failure_is_sanitized_and_releases_lock(client,cloud,monkeypatch):
    def broken():raise RuntimeError(LIVE)
    monkeypatch.setattr(module,'applied_targets',broken)
    client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    result=finish(client)
    assert result['run']['status']=='failed' and LIVE not in str(result)
    assert not result['active'] and not cloud[0]


def test_query_is_read_only_and_fetches_no_clinical_payload(monkeypatch):
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql,*args):calls.append(sql);return self
        def fetchall(self):return RECORDS
    monkeypatch.setattr(module,'get_connection',Connection)
    assert module.applied_targets()==RECORDS
    assert calls[0]=='SET TRANSACTION READ ONLY'
    assert 'statement_timeout' in calls[1]
    assert 'payload' not in calls[2] and 'SELECT DISTINCT' in calls[2]
    assert all(not any(word in sql.upper() for word in ('INSERT ','UPDATE ','DELETE ')) for sql in calls)


def test_rate_limit_uses_start_spacing_without_catchup(monkeypatch):
    generator=module.TestTraffic(main.SERVICES,main.EVENT_TYPES)
    generator.job={'count':10,'services':{'patient':{'status':'pending','attempted':0,'succeeded':0,'failed':0,'errors':{}}}}
    clock=[0.]
    starts=[]
    class Loop:
        def time(self):return clock[0]
    async def pause(delay):clock[0]+=delay
    class Client:
        async def get(self,*args,**kwargs):
            starts.append(clock[0]);clock[0]+=.01
            return httpx.Response(200,json={'event_id':'P1','event_type':'patient_registration','status':'applied'})
    async def check():
        monkeypatch.setattr(module.asyncio,'get_running_loop',lambda:Loop())
        monkeypatch.setattr(module.asyncio,'sleep',pause)
        await generator.service_reads(Client(),'patient',('P1','patient_registration'),{})
    asyncio.run(check())
    assert len(starts)==10
    assert all(b-a>=.199999 for a,b in zip(starts,starts[1:]))


def test_shutdown_cancels_traffic_and_releases_lock(client,cloud):
    for eid,_ in RECORDS:cloud[1][eid,1]='slow'
    client.post('/api/test-traffic',json={'count':10},headers=ACTION)
    client.portal.call(main.app.state.test_traffic.shutdown)
    assert not main.app.state.test_traffic.lock.locked()
    assert main.app.state.test_traffic.job['status']=='cancelled'


def test_override_is_explicit_and_aws_configuration_unchanged():
    from pathlib import Path
    import yaml
    override=yaml.safe_load(Path('infrastructure/docker/compose.dashboard-test-traffic.local.yaml').read_text())
    assert set(override['services'])=={'dashboard'}
    config=override['services']['dashboard']
    assert config['environment']=={
        'CRISISGUARD_DEPLOYMENT_MODE':'local-development',
        'DASHBOARD_TEST_TRAFFIC_ENABLED':'1',
        'CLOUD_REPLAY_TELEMETRY_TOKEN':'${CLOUD_REPLAY_TELEMETRY_TOKEN:-}',
    }
    assert config['command'][-2:]==['--workers','1']
    for path in [Path('compose.yaml'),*Path('infrastructure/aws').glob('*.yaml')]:
        assert 'DASHBOARD_TEST_TRAFFIC_ENABLED' not in path.read_text()


def test_local_compose_resolves_both_distinct_dashboard_tokens(tmp_path,monkeypatch):
    from hmac import compare_digest
    import json
    import os
    import re
    import shutil
    import subprocess
    from pathlib import Path
    from infrastructure.scripts import init_local_env

    if shutil.which('docker') is None:
        pytest.skip('Docker Compose CLI unavailable')
    compose_version=subprocess.run(['docker','compose','version'],
                                   capture_output=True,text=True,timeout=10)
    if compose_version.returncode:
        pytest.skip('Docker Compose plugin unavailable')
    root=Path(__file__).resolve().parents[2]
    (tmp_path/'.env.example').write_bytes((root/'.env.example').read_bytes())
    arguments=['--root',str(tmp_path),'--with-local-test-traffic']
    assert init_local_env.main(arguments)==0
    env_file=tmp_path/'.env'
    first_bytes=env_file.read_bytes()
    assert init_local_env.main(arguments)==0
    assert env_file.read_bytes()==first_bytes
    keys=init_local_env.REQUIRED_TOKENS+init_local_env.LOCAL_TEST_TRAFFIC_TOKENS
    assignments=dict(line.split('=',1) for line in first_bytes.decode().splitlines()
                     if '=' in line and not line.lstrip().startswith('#'))
    values={key:assignments[key] for key in keys}
    assert all(re.fullmatch(r'[0-9a-f]{64}',value) for value in values.values())
    assert len(set(values.values()))==len(keys)
    files=['compose.yaml','infrastructure/docker/compose.metaguard.yaml',
           'infrastructure/docker/compose.dashboard-test-traffic.local.yaml']
    if (root/'compose.override.yaml').is_file():
        files.append('compose.override.yaml')
    command=['docker','compose','--env-file',str(env_file),'-p','cg-test-traffic-config']
    for file in files:
        command.extend(('-f',file))
    command.extend(('--profile','metaguard','config','--format','json'))
    environment=os.environ.copy()
    for key in keys:
        environment.pop(key,None)
    result=subprocess.run(command,cwd=root,env=environment,
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0, 'Local Compose configuration failed'
    services=json.loads(result.stdout)['services']
    dashboard=services['dashboard']['environment']
    assert len(services)==8
    assert dashboard['CRISISGUARD_DEPLOYMENT_MODE']=='local-development'
    assert dashboard['DASHBOARD_TEST_TRAFFIC_ENABLED']=='1'
    assert dashboard['METAGUARD_REQUIRED']=='1'
    assert services['hospital-a']['environment']['METAGUARD_ENABLED']=='1'
    assert all(compare_digest(dashboard.get(key,''),values[key])
               for key in init_local_env.LOCAL_TEST_TRAFFIC_TOKENS)
    assert not compare_digest(dashboard['CLOUD_LIVE_TELEMETRY_TOKEN'],
                              dashboard['CLOUD_REPLAY_TELEMETRY_TOKEN'])
    assert all(compare_digest(services[name]['environment'].get(key,''),values[key])
               for name in ('patient','emergency','admin')
               for key in init_local_env.LOCAL_TEST_TRAFFIC_TOKENS)
    assert all(compare_digest(services[name]['environment'].get('CLOUD_REPLAY_TELEMETRY_TOKEN',''),
                              values['CLOUD_REPLAY_TELEMETRY_TOKEN'])
               for name in ('hospital-a','hospital-b'))
    for key in ('DASHBOARD_TEST_TRAFFIC_ENABLED','CRISISGUARD_DEPLOYMENT_MODE','METAGUARD_REQUIRED',
                'CLOUD_LIVE_TELEMETRY_TOKEN','CLOUD_REPLAY_TELEMETRY_TOKEN','DATABASE_URL'):
        monkeypatch.setenv(key,dashboard[key])
    for key in ('AWS_REGION','AWS_DEFAULT_REGION','AWS_EXECUTION_ENV','EC2_INSTANCE_ID',
                'APP_ENV','ENVIRONMENT','DEPLOYMENT_ENV'):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.setattr(module.Path,'read_text',lambda self:'Generic local host')
    local_services={name:dashboard[f'DASHBOARD_{name.upper()}_URL']
                    for name in ('patient','emergency','admin')}
    assert module.local_enabled(local_services)
    module.TestTraffic(local_services, {}, {'hospital-a': dashboard['DASHBOARD_HOSPITAL_A_URL']}).require_preparation_enabled()


def test_standard_launcher_selects_local_overlay_after_metaguard_before_user_override():
    from pathlib import Path
    launcher=Path('start.project.bat').read_text()
    compose_line=next(line for line in launcher.splitlines() if line.startswith('set "COMPOSE_ARGS='))
    assert compose_line.index('compose.yaml') < compose_line.index('compose.metaguard.yaml')
    assert compose_line.index('compose.metaguard.yaml') < compose_line.index('compose.dashboard-test-traffic.local.yaml')
    assert compose_line.endswith('--profile metaguard"')
    assert launcher.index('set "COMPOSE_ARGS=') < launcher.index('if exist "compose.override.yaml"')
    assert launcher.count('infrastructure/docker/compose.dashboard-test-traffic.local.yaml') == 1
    assert 'START_PROJECT=crisisguard-local' in launcher
    assert launcher.index('docker context inspect') < launcher.index('--with-local-test-traffic')
    assert 'npipe:////./pipe/' in launcher
    assert 'docker compose %COMPOSE_ARGS% up -d --build --wait postgres patient emergency admin controller hospital-a hospital-b dashboard' in launcher


def test_controls_precede_metrics_and_never_embed_credentials(client):
    html=client.get('/').text
    script=client.get('/static/test-traffic.js').text
    assert html.index('id="test-traffic"') < html.index('id="metrics-grid"')
    assert 'value="100" selected' in html
    assert 'Generate Test Traffic' in html
    assert LIVE not in html+script and REPLAY not in html+script
    assert 'Telemetry-Token' not in script and 'Authorization' not in script
