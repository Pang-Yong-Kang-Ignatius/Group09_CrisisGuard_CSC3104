"""Original-project outage agent security; no Docker daemon used."""
from copy import deepcopy
import json

from fastapi.testclient import TestClient
import pytest
from infrastructure.scripts import local_outage_agent as agent


@pytest.fixture
def control(monkeypatch):
    rows=[]
    for name in sorted(agent.REQUIRED):
        rows.append({'Id':name+'-id','Config':{'Labels':{'com.docker.compose.project':agent.PROJECT,
            'com.docker.compose.service':name,'com.docker.compose.project.working_dir':str(agent.ROOT),
            'com.docker.compose.project.config_files':str(agent.ROOT/'compose.yaml')},
            'Cmd':[agent.MODULES.get(name,'other')]},'State':{'Running':True},
            'HostConfig':{'PortBindings':{'8080/tcp' if name=='dashboard' else '8000/tcp':
                [{'HostIp':'127.0.0.1','HostPort':'8080' if name=='dashboard' else '8001'}]}}})
    monkeypatch.delenv('DOCKER_HOST',raising=False)
    def docker(*args):
        if args[0]=='context': return json.dumps([{'Endpoints':{'docker':{'Host':'npipe://local'}}}])
        if args[0]=='ps':
            assert args[-1]=='label=com.docker.compose.project=crisisguard-local'
            return '\n'.join(row['Id'] for row in rows)
        if args[0]=='inspect': return json.dumps(rows)
        raise AssertionError(args)
    monkeypatch.setattr(agent,'docker',docker)
    return agent.Control(), rows


@pytest.mark.parametrize('fault',['project','checkout','compose','public','dashboard_port','command','missing'])
def test_wrong_project_or_service_refused(control,fault):
    _,rows=control
    patient=next(row for row in rows if row['Id']=='patient-id')
    dashboard=next(row for row in rows if row['Id']=='dashboard-id')
    labels=patient['Config']['Labels']
    if fault=='project': labels['com.docker.compose.project']='other'
    if fault=='checkout': labels['com.docker.compose.project.working_dir']=str(agent.ROOT/'other')
    if fault=='compose': labels['com.docker.compose.project.config_files']='elsewhere.yaml'
    if fault=='public': patient['HostConfig']['PortBindings']['8000/tcp'][0]['HostIp']='0.0.0.0'
    if fault=='dashboard_port': dashboard['HostConfig']['PortBindings']['8080/tcp'][0]['HostPort']='18080'
    if fault=='command': patient['Config']['Cmd']=['unrelated:app']
    if fault=='missing': rows.remove(patient)
    with pytest.raises((RuntimeError,ValueError)): agent.Control()


def test_optional_controller_preserved(control,monkeypatch):
    _,rows=control
    row=deepcopy(rows[0]);row['Id']='controller-id';row['Config']['Labels']['com.docker.compose.service']='controller';rows.append(row)
    instance=agent.Control()
    calls=[]
    monkeypatch.setattr(agent,'docker',lambda *args:calls.append(args))
    monkeypatch.setattr(instance,'validate',lambda:None)
    monkeypatch.setattr(instance,'wait',lambda *args:{'verified':True})
    instance.operate('outage');instance.operate('restore')
    assert calls==[('stop','--time','10','patient-id','emergency-id','admin-id'),('start','patient-id'),('start','emergency-id','admin-id')]
    assert 'controller' not in instance.ids


def test_container_recreation_fails_closed(control):
    instance,rows=control;rows[0]['Id']='changed'
    with pytest.raises(RuntimeError,match='changed'): instance.operate('outage')
    assert not instance.lock.locked()


def test_busy_operation_is_rejected(control):
    instance,_=control;instance.lock.acquire()
    try:
        with pytest.raises(agent.HTTPException) as err: instance.operate('restore')
        assert err.value.status_code==409
    finally: instance.lock.release()


@pytest.mark.parametrize('fault',['missing','invalid','peer','origin','host','unicode'])
def test_unauthorized_never_operates(control,monkeypatch,fault):
    instance,_=control
    monkeypatch.setattr(instance,'operate',lambda _:pytest.fail('Unauthorized operation'))
    headers={'Origin':'http://localhost:8080','Authorization':'Bearer token'};peer='127.0.0.1'
    if fault=='missing': headers.pop('Authorization')
    if fault=='invalid': headers['Authorization']='Bearer bad'
    if fault=='origin': headers['Origin']='http://evil.example'
    if fault=='peer': peer='192.168.1.1';headers['X-Forwarded-For']='127.0.0.1'
    if fault=='host': headers['Host']='evil.example'
    if fault=='unicode': headers=[(b'Origin',b'http://localhost:8080'),(b'Authorization',b'Bearer \xff')]
    with TestClient(agent.create_app(instance,'token'),base_url='http://127.0.0.1:8765',client=(peer,1234)) as client:
        assert client.post('/operation',json={'action':'outage'},headers=headers).status_code==403


@pytest.mark.parametrize('body',[{'action':'restart'},{'action':'outage','container':'postgres'},{'action':['outage']},{}])
def test_arbitrary_operations_refused(control,body):
    instance,_=control
    with TestClient(agent.create_app(instance,'token'),base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as client:
        assert client.post('/operation',json=body,headers={'Origin':'http://localhost:8080','Authorization':'Bearer token'}).status_code==422


@pytest.mark.parametrize('origin',['http://localhost:8080','http://127.0.0.1:8080'])
def test_allowed_origins_and_sanitized_failure(control,monkeypatch,origin):
    instance,_=control
    def fail(_): raise RuntimeError('postgresql://user:private-password@db')
    monkeypatch.setattr(instance,'operate',fail)
    with TestClient(agent.create_app(instance,'token'),base_url='http://127.0.0.1:8765',client=('127.0.0.1',1234)) as client:
        headers={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'}
        assert client.options('/operation',headers=headers).headers['Access-Control-Allow-Origin']==origin
        response=client.post('/operation',json={'action':'outage'},headers={**headers,'Authorization':'Bearer token'})
        assert response.status_code==503 and 'private-password' not in response.text


def test_unhealthy_restore_is_not_reported_success(control,monkeypatch):
    instance,_=control
    monkeypatch.setattr(instance,'status',lambda:{'services':{name:{'healthy':False,'running':True} for name in agent.APIS}})
    times=iter([0,0,100]);monkeypatch.setattr(agent.time,'monotonic',lambda:next(times));monkeypatch.setattr(agent.time,'sleep',lambda _:None)
    with pytest.raises(RuntimeError,match='timed out'): instance.wait(True)


def test_remote_docker_context_rejected(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST','tcp://remote.example:2375')
    with pytest.raises(ValueError,match='local Docker'): agent.Control()
