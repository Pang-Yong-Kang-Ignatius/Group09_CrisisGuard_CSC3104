"""Dashboard recovery adapters expose observations without upstream secrets."""
import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from dashboard import main


@pytest.mark.parametrize('state', ['Normal', 'Caution', 'Recovery'])
def test_monitoring_reads_existing_endpoints_and_omits_secrets(monkeypatch, state):
    monkeypatch.setenv('DASHBOARD_CONTROLLER_URL', 'http://controller:8000')
    monkeypatch.setenv('DASHBOARD_CONTROLLER_TOKEN', 'private-token')

    async def hospital(method, url, **kwargs):
        assert method == 'GET' and url.endswith('/recovery/status')
        return httpx.Response(200, json={'enabled': True, 'replayable_backlog': 3,
            'last_error': 'private-token', 'configuration': 'secret'}, request=httpx.Request(method, url))

    async def controller(self, url, **kwargs):
        assert url == 'http://controller:8000/admission/status'
        assert kwargs['headers'] == {'x-cloud-token': 'private-token'}
        return httpx.Response(200, json={'state': state, 'enabled': True, 'rate_rps': 2,
            'inflight_limit': 4, 'settings': {'token': 'private-token'}}, request=httpx.Request('GET', url))

    monkeypatch.setattr(main, 'hospital_request', hospital)
    monkeypatch.setattr(httpx.AsyncClient, 'get', controller)
    result = asyncio.run(main.recovery_monitoring())
    assert result['controller'] == {'status': 'available', 'state': state, 'enabled': True,
                                   'rate_rps': 2, 'inflight_limit': 4}
    assert result['hospitals']['hospital-a']['replayable_backlog'] == 3
    assert 'private-token' not in str(result)
    assert 'configuration' not in str(result)


@pytest.mark.parametrize('failure', ['http', 'json', 'shape', 'connect'])
def test_monitoring_failure_has_no_invented_state_or_error_details(monkeypatch, failure):
    monkeypatch.setenv('DASHBOARD_CONTROLLER_URL', 'http://controller:8000')
    monkeypatch.setenv('DASHBOARD_CONTROLLER_TOKEN', 'private-token')

    def response(method, url):
        req = httpx.Request(method, url)
        if failure == 'connect':
            raise httpx.ConnectError('private-token', request=req)
        if failure == 'http':
            return httpx.Response(403, text='private-token', request=req)
        if failure == 'json':
            return httpx.Response(200, text='private-token', request=req)
        return httpx.Response(200, json=[], request=req)

    async def hospital(method, url, **kwargs):
        if 'hospital-b' in url or url.startswith(main.HOSPITALS['hospital-b']):
            return httpx.Response(200, json={'enabled': False}, request=httpx.Request(method, url))
        return response(method, url)

    async def controller(self, url, **kwargs):
        return response('GET', url)

    monkeypatch.setattr(main, 'hospital_request', hospital)
    monkeypatch.setattr(httpx.AsyncClient, 'get', controller)
    result = asyncio.run(main.recovery_monitoring())
    assert result['controller'] == {'status': 'unavailable'}
    assert result['hospitals']['hospital-a'] == {'status': 'unavailable'}
    assert result['hospitals']['hospital-b']['status'] == 'available'
    assert 'private-token' not in str(result)


def test_unconfigured_controller_is_explicit(monkeypatch):
    monkeypatch.delenv('DASHBOARD_CONTROLLER_URL', raising=False)
    monkeypatch.delenv('DASHBOARD_CONTROLLER_TOKEN', raising=False)
    async def hospital(method, url, **kwargs):
        return httpx.Response(200, json={}, request=httpx.Request(method, url))
    monkeypatch.setattr(main, 'hospital_request', hospital)
    assert asyncio.run(main.recovery_monitoring())['controller'] == {'status': 'not_configured'}


@pytest.mark.parametrize('headers', [{'host': 'evil.example'}, {'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'}])
def test_monitoring_obeys_local_dashboard_access_checks(headers):
    with TestClient(main.app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.get('/api/recovery', headers=headers).status_code == 403

@pytest.mark.parametrize('freshness', [True, False])
def test_controller_freshness_is_forwarded_without_private_configuration(monkeypatch, freshness):
    monkeypatch.setenv('DASHBOARD_CONTROLLER_URL', 'http://controller:8000')
    monkeypatch.setenv('DASHBOARD_CONTROLLER_TOKEN', 'private-token')
    async def hospital(method,url,**kwargs):
        return httpx.Response(200,json={},request=httpx.Request(method,url))
    async def controller(self,url,**kwargs):
        return httpx.Response(200,json={'state':'Normal','rate_rps':1.2,'telemetry_fresh':freshness,
            'policy':'aimd','settings':{'token':'private-token'}},request=httpx.Request('GET',url))
    monkeypatch.setattr(main,'hospital_request',hospital)
    monkeypatch.setattr(httpx.AsyncClient,'get',controller)
    result=asyncio.run(main.recovery_monitoring())['controller']
    assert result['telemetry_fresh'] is freshness and result['policy']=='aimd'
    assert result['rate_rps']==1.2 and 'private-token' not in str(result)
