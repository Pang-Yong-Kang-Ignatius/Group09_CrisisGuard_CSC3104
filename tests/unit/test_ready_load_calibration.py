import asyncio
from datetime import datetime, timezone
import httpx
import pytest
from evaluation.experiments import calibrate_ready_load as module


def sample(fresh=True,pressure=False):
    return {'health':{'fresh':fresh,'pressure':pressure}}


def test_generator_accuracy_is_independent_of_cloud_errors():
    rows=[{'status':503,'latency_ms':3,'lag_ms':1} for _ in range(999)]+[{'skip':'lag'}]
    s=module.client_summary(rows,1000)
    assert s['generator_valid'] and s['failure_fraction']==1
    assert s['issuance_ratio']==.999 and s['lag_skips']==1
    assert module.classify(s,[sample(pressure=True)])=='overload_candidate'
    assert module.classify(s,[sample()])=='client_path_degraded_without_cloud_confirmation'
    assert module.classify(s,[sample(fresh=False)])=='telemetry_invalid'


def test_missing_or_missed_requests_never_look_healthy():
    assert not module.client_summary([],0)['generator_valid']
    assert not module.client_summary([{'skip':'cap'}]*2,1000)['generator_valid']
    s=module.client_summary([{'skip':'safety'}]*10,10)
    assert s['safety_withheld']==10 and s['lag_skips']==0 and not s['generator_valid']


@pytest.mark.parametrize('url',['http://example.com:8001','https://localhost:8001','http://user:secret@localhost:8001','http://localhost:8001/path','http://localhost:8001?token=secret'])
def test_only_local_fixed_targets_allowed(url):
    with pytest.raises(ValueError):module.base_url(url)


def test_health_labels_do_not_use_throughput():
    now=datetime.now(timezone.utc)
    clouds={name:dict(service=name,sampled_at=now.isoformat(),database_healthy=True,error_rate=0,
                      executor_queue_pressure=0,live_p95_latency_ms=8,throughput_rps=10000)
            for name in ('patient','emergency','admin')}
    c={'telemetry_fresh':True,'settings':{'max_age_seconds':5}}
    assert module.health_snapshot(clouds,c,now)=={'fresh':True,'pressure':False}
    clouds['patient']['executor_queue_pressure']=.5
    assert module.health_snapshot(clouds,c,now)['pressure']
    clouds['admin']['sampled_at']='invalid'
    assert not module.health_snapshot(clouds,c,now)['fresh']


@pytest.mark.parametrize('field,value',[('error_rate',float('nan')),('executor_queue_pressure',-1),('live_p95_latency_ms',float('inf'))])
def test_malformed_metrics_are_not_healthy(field,value):
    now=datetime.now(timezone.utc)
    clouds={name:dict(service=name,sampled_at=now.isoformat(),database_healthy=True,error_rate=0,
                      executor_queue_pressure=0,live_p95_latency_ms=8)
            for name in ('patient','emergency','admin')}
    clouds['patient'][field]=value
    assert not module.health_snapshot(clouds,{'telemetry_fresh':True,'settings':{'max_age_seconds':5}},now)['fresh']


def test_fixed_window_records_real_calls_and_fixed_denominator(monkeypatch):
    async def observe(*args):
        return {'cloud':{'patient':{'live_latency_sample_count':1,'live_p95_latency_ms':1}},'health':{'fresh':True,'pressure':False}}
    monkeypatch.setattr(module,'observe',observe)
    captured=[]
    async def respond(request):
        captured.append(request)
        return httpx.Response(200)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await module.fixed_window(client,client,['http://localhost:8001/events/KNOWN'],{},'',
                'operator',{'X-CrisisGuard-Traffic':'live'},20,.2,4,sample_interval=.02)
    result=asyncio.run(run())
    assert result['summary']['scheduled']==4 and len(captured)==4
    assert result['classification']=='healthy_observed'
    assert all(0<=r['started_seconds']<.2 for r in result['outcomes'])
    assert all(r.headers['X-CrisisGuard-Traffic']=='live' for r in captured)


def test_unsafe_telemetry_stops_and_preserves_unissued_opportunities(monkeypatch):
    async def observe(*args):
        return {'cloud':{'patient':{'live_latency_sample_count':1,'live_p95_latency_ms':1}},'health':{'fresh':False,'pressure':False}}
    monkeypatch.setattr(module,'observe',observe)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200))) as client:
            return await module.fixed_window(client,client,['http://localhost:8001/events/KNOWN'],{},'','operator',{},100,.1,4,sample_interval=.01)
    result=asyncio.run(run())
    assert result['safety_stopped'] and result['summary']['safety_withheld']>0
    assert result['classification']=='safety_stopped_incomplete_window'
    assert len(result['outcomes'])==10
