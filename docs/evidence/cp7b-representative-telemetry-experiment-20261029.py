"""Three disposable Cloud telemetry calibration replicas, no original resources.

Reuses the previous calibration summary function and the policy/fairness Docker
isolation helpers. Synthetic live traffic is read-only; real hospital writes
retain grant enforcement and authenticated replay provenance. Database locks
and an upstream delay proxy are explicitly diagnostic perturbations.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import asdict
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,HERE/path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


fair=load('fairness_reuse','cp7b-sustained-fairness-experiment-20261029.py')
base=fair.base
old=load('calibration_reuse','cp7b-cloud-telemetry-calibration-20261028.py')
PHASES=[
    {'name':'live_6rps','seconds':75,'live_rps':6,'replay_rps':0,'cap':4,'diagnostic':False},
    {'name':'live_24rps','seconds':75,'live_rps':24,'replay_rps':0,'cap':16,'diagnostic':False},
    {'name':'mixed_live_replay','seconds':75,'live_rps':12,'replay_rps':6,'cap':16,'diagnostic':False},
    {'name':'db_wait_low_concurrency','seconds':45,'live_rps':2,'replay_rps':0,'cap':4,'diagnostic':True,'locks':True},
    {'name':'executor_wait_bounded','seconds':45,'live_rps':2,'replay_rps':0,'cap':4,'diagnostic':True,'locks':True,'patient_rps':50,'patient_cap':64},
    {'name':'upstream_delay_300ms','seconds':45,'live_rps':6,'replay_rps':0,'cap':8,'diagnostic':True,'proxy':True},
]


def canonical(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


class Trial(base.Runner):
    def __init__(self,work,image,n,volumes,diagnostics_only=False,fixture_seed=False):
        self.n=n
        self.diagnostics_only=diagnostics_only
        self.fixture_seed=fixture_seed
        self.project=f'cg-telemetry-{n}-{uuid.uuid4().hex[:8]}'
        folder=work/self.project
        folder.mkdir()
        super().__init__(folder,image,folder/'raw.json',1)
        self.original_volumes=volumes
        self.output={'trial':n,'project':self.project,'started_at':base.utcnow(),'phases':[],'result':'running'}
        self.settings=fair.settings_for('aimd')
        self.settings['POSTGRES_DB']='telemetry_eval'
        self.settings['POSTGRES_USER']='telemetry_eval'
        sys.path.insert(0,str(ROOT))
        from cloud.controller.admission import Settings
        # Use source-default admission thresholds and timings, unchanged.
        self.controller_settings=asdict(Settings())
        for key,value in self.controller_settings.items():
            self.settings['CONTROLLER_ADMISSION_'+key.upper()]=str(value)
        self.settings['PROXY_PORT']=str(base.free_ports(1)[0])
        self.db_url=(f"postgresql://{self.settings['POSTGRES_USER']}:{self.settings['POSTGRES_PASSWORD']}"
                     f"@127.0.0.1:{self.settings['POSTGRES_PORT']}/{self.settings['POSTGRES_DB']}")
        self.urls={s:f"http://127.0.0.1:{self.settings[s.upper()+'_PORT']}" for s in base.SERVICES}
        self.controller=f"http://127.0.0.1:{self.settings['CONTROLLER_PORT']}"
        self.hospitals=dict(zip(base.SITES,[f"http://127.0.0.1:{self.settings[k]}" for k in ('HOSPITAL_A_PORT','HOSPITAL_B_PORT')]))
        self.expected={}

    def save(self):
        self.raw_path.write_text(json.dumps(self.output,indent=2,default=str),encoding='utf-8')

    def query(self,sql,args=()):
        with psycopg.connect(self.db_url,connect_timeout=3,row_factory=dict_row) as conn:
            return conn.execute(sql,args).fetchall()

    async def json_get(self,client,url,headers=None):
        response=await client.get(url,headers=headers)
        response.raise_for_status()
        return response.json()

    async def metrics(self,client,service):
        started=time.perf_counter()
        requested=base.utcnow()
        result=await self.json_get(client,self.urls[service]+'/metrics')
        return {'requested_at':requested,'received_at':base.utcnow(),
                'fetch_duration_ms':(time.perf_counter()-started)*1000,**result}

    async def control(self,client):
        result=await self.json_get(client,self.controller+'/admission/status',
                                  {'X-Cloud-Token':self.settings['CONTROLLER_CLOUD_TOKEN']})
        return {key:result.get(key) for key in ('server_time','state','reason','rate_rps','telemetry_fresh','allowed','denied','observed_at')}

    async def capture(self,client,phase):
        for site,address in self.hospitals.items():
            if phase=='seed':
                events=base.chain(f'TEL-{self.n}-seed',site)
            else:
                events=fair.batch(f'TEL-{self.n}-mixed',site,0,8,2)
            for event in events:
                response=await client.post(address+'/capture',json=event)
                assert response.status_code==201
                self.expected[event['event_id']]=event

    async def wait_events(self,client,timeout=150):
        deadline=time.monotonic()+timeout
        while True:
            rows=await asyncio.to_thread(self.query,'SELECT event_id,status FROM events')
            if len(rows)==len(self.expected) and all(r['status']=='applied' for r in rows):
                local=await asyncio.gather(*(self.json_get(client,url+'/events') for url in self.hospitals.values()))
                if all(all(e['status']=='applied' for e in records) for records in local):
                    return
            assert time.monotonic()<deadline,'Replay did not finish within bound'
            await asyncio.sleep(.5)

    async def request(self,client,phase,service,kind,ordinal,permit):
        started=time.perf_counter()
        at=base.utcnow()
        route=f"/events/{self.seed_ids[service]}"
        url=(f"http://127.0.0.1:{self.settings['PROXY_PORT']}/{service}" if phase.get('proxy') else self.urls[service])+route
        headers={'X-CrisisGuard-Traffic':kind,
                 'X-CrisisGuard-Telemetry-Token':self.settings['CLOUD_'+kind.upper()+'_TELEMETRY_TOKEN']}
        try:
            response=await client.get(url,headers=headers)
            code=response.status_code
            error=None
            body=response.json() if response.headers.get('content-type','').startswith('application/json') else {}
            body_status=body.get('status')
        except Exception as exc:
            code,body_status,error=None,None,type(exc).__name__
        finally:
            permit.release()
        return {'service':service,'phase':phase['name'],'kind':kind,'ordinal':ordinal,'started_at':at,
                'finished_at':base.utcnow(),'client_latency_ms':(time.perf_counter()-started)*1000,
                'status_code':code,'body_status':body_status,'error':error}

    async def drive(self,client,phase,service):
        rate=phase.get('patient_rps',phase['live_rps']) if service=='patient' else phase['live_rps']
        replay_rate=phase['replay_rps']
        cap=phase.get('patient_cap',phase['cap']) if service=='patient' else phase['cap']
        permit=asyncio.Semaphore(cap)
        tasks=[]
        skipped={'live':0,'replay':0}
        start=asyncio.get_running_loop().time()
        schedule=sorted([(i/rate,'live',i) for i in range(int(rate*phase['seconds']))]+
                        [(i/replay_rate,'replay',i) for i in range(int(replay_rate*phase['seconds']))])
        for offset,kind,n in schedule:
            await asyncio.sleep(max(0,start+offset-asyncio.get_running_loop().time()))
            if permit.locked():
                skipped[kind]+=1
                continue
            await permit.acquire()
            tasks.append(asyncio.create_task(self.request(client,phase,service,kind,n,permit)))
        records=await asyncio.gather(*tasks)
        return records,{'service':service,'offered':len(schedule),'client_concurrency_cap':cap,
                        'skipped_generator_opportunities':skipped,'duration_seconds':asyncio.get_running_loop().time()-start}

    async def lock_pulses(self,phase,result):
        start=time.monotonic()
        # Session timeout releases the lock even if the harness stops abruptly.
        conn=await psycopg.AsyncConnection.connect(self.db_url,autocommit=True)
        try:
            await conn.execute("SET idle_in_transaction_session_timeout='4s'")
            await conn.execute("SET lock_timeout='2s'")
            for index in range(int(phase['seconds']/5)):
                await asyncio.sleep(max(0,start+index*5-time.monotonic()))
                await conn.execute('BEGIN')
                try:
                    await conn.execute('LOCK TABLE public.events IN ACCESS EXCLUSIVE MODE')
                    at=base.utcnow()
                    await asyncio.sleep(2)
                finally:
                    await conn.execute('ROLLBACK')
                result['lock_pulses'].append({'lock_acquired_at':at,'released_at':base.utcnow(),'requested_hold_seconds':2})
        finally:
            await conn.close()

    async def sample(self,client,phase,result,stop):
        async def service_loop(service):
            while not stop.is_set():
                try:
                    result['metrics_samples'].append(await self.metrics(client,service))
                except Exception as exc:
                    result['monitor_errors'].append({'service':service,'at':base.utcnow(),'error':type(exc).__name__})
                await asyncio.sleep(.25 if phase.get('locks') else .5)
        async def controller_loop():
            while not stop.is_set():
                try:
                    result['controller_samples'].append(await self.control(client))
                except Exception as exc:
                    result['monitor_errors'].append({'service':'controller','at':base.utcnow(),'error':type(exc).__name__})
                await asyncio.sleep(1)
        await asyncio.gather(*(service_loop(s) for s in base.SERVICES),controller_loop())

    async def clean_window(self,client):
        await asyncio.sleep(61)
        snapshots={s:await self.metrics(client,s) for s in base.SERVICES}
        assert all(m['latency_sample_count']==0 and m['live_p95_latency_ms'] is None and m['executor_queue_depth']==0
                   for m in snapshots.values()),'Window did not expire or executor did not drain'
        return snapshots

    async def workload(self,config):
        async with httpx.AsyncClient(timeout=15,trust_env=False,
                limits=httpx.Limits(max_connections=100,max_keepalive_connections=80)) as client:
            deadline=time.monotonic()+70
            while True:
                initial=await self.control(client)
                if initial['state']=='Normal' and initial['telemetry_fresh']:
                    break
                assert time.monotonic()<deadline,'Fresh Normal telemetry unavailable'
                await asyncio.sleep(1)
            if self.fixture_seed:
                # Diagnostic reads do not need a second admission experiment.
                # This seeds only the uniquely named disposable database; the
                # representative mixed phase already uses real granted writes.
                fixtures=[]
                for site in base.SITES:
                    fixtures.extend(base.chain(f'TEL-{self.n}-seed',site))
                    fixtures.extend(fair.batch(f'TEL-{self.n}-mixed',site,0,8,2))
                def seed():
                    with psycopg.connect(self.db_url) as conn:
                        assert conn.execute('SELECT count(*) FROM events').fetchone()[0]==0
                        for event in fixtures:
                            conn.execute('INSERT INTO events(event_id,site_id,event_type,base_priority,prerequisite_ids,payload) VALUES (%s,%s,%s,%s,%s,%s)',
                                (event['event_id'],event['site_id'],event['event_type'],event['base_priority'],Jsonb(event['prerequisite_ids']),Jsonb(event['payload'])))
                await asyncio.to_thread(seed)
                self.expected={event['event_id']:event for event in fixtures}
                self.output['dataset_preparation']='direct synthetic fixtures in fresh disposable DB; no admission claim'
            else:
                await self.capture(client,'seed')
                await self.wait_events(client)
            if self.diagnostics_only and not self.fixture_seed:
                # Recreate the same 24-event dataset for the unfinished
                # diagnostics without repeating completed traffic phases.
                await self.capture(client,'mixed')
                await self.wait_events(client)
            seed_rows=await asyncio.to_thread(self.query,'SELECT event_id,event_type FROM events ORDER BY event_id')
            self.seed_ids={s:next(r['event_id'] for r in seed_rows if base.ROUTE[r['event_type']]==s) for s in base.SERVICES}
            self.output['controller_initial']=initial
            self.output['clean_initial_window']=await self.clean_window(client)
            for phase in PHASES:
                if self.diagnostics_only and not phase['diagnostic']:
                    continue
                result={'definition':phase,'started_at':base.utcnow(),'requests':[],'metrics_samples':[],
                        'controller_samples':[],'monitor_errors':[],'lock_pulses':[]}
                self.output['phases'].append(result)
                result['baseline']={s:await self.metrics(client,s) for s in base.SERVICES}
                stop=asyncio.Event()
                sampler=asyncio.create_task(self.sample(client,phase,result,stop))
                pulse=asyncio.create_task(self.lock_pulses(phase,result)) if phase.get('locks') else None
                if phase['name']=='mixed_live_replay':
                    await self.capture(client,'mixed')
                t=time.monotonic()
                streams=await asyncio.gather(*(self.drive(client,phase,s) for s in base.SERVICES))
                for records,offered in streams:
                    result['requests'].extend(records)
                    result.setdefault('offered_load',[]).append(offered)
                if pulse: await pulse
                if phase['name']=='mixed_live_replay':
                    await self.wait_events(client)
                result['measured_duration_seconds']=time.monotonic()-t
                stop.set()
                await sampler
                result['end_metrics']={s:await self.metrics(client,s) for s in base.SERVICES}
                result['ended_at']=base.utcnow()
                result['client_summary']=old.summarize_requests(result['requests'])
                for service in base.SERVICES:
                    before=result['baseline'][service]
                    after=result['end_metrics'][service]
                    own=[r for r in result['requests'] if r['service']==service]
                    delta=after['total_requests']-before['total_requests']
                    assert delta>=len(own)
                    if phase['name']!='mixed_live_replay':
                        assert delta==len(own),'Unexpected monitored request population'
                    assert after['unclassified_latency_sample_count']==0
                    assert after['executor_worker_limit']==40
                assert all(r['status_code']==200 and r['error'] is None for r in result['requests']),'Client errors in bounded workload'
                self.save()
                print(f"PHASE trial{self.n} {phase['name']}: {len(result['requests'])} requests; p95={result['client_summary']['client_p95_latency_ms_nearest_rank']:.2f}ms",flush=True)
                result['expiry']=await self.clean_window(client)
                self.save()
            self.output['controller_final']=await self.control(client)

    def execute_trial(self):
        config=None
        try:
            config,resolved=self.resolve(self.project,self.work,self.settings)
            # Start hospitals in replay mode only in this fresh test project.
            for site in ('hospital-a','hospital-b'):
                resolved['services'][site]['environment']['AUTO_REPLAY']='1'
            proxy_source=(HERE/'cp7b-representative-telemetry-proxy-20261029.py').read_text(encoding='utf-8')
            resolved['services']['delay-proxy']={
                'image':self.image,'pull_policy':'never',
                'command':['python','-c',proxy_source+"\nimport uvicorn; uvicorn.run(app,host='0.0.0.0',port=8000,access_log=False)"],
                'ports':[{'target':8000,'published':self.settings['PROXY_PORT'],'host_ip':'127.0.0.1','protocol':'tcp'}],
                'networks':{'default':None},
                'healthcheck':{'test':['CMD','python','-c',"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"],
                               'interval':'3s','timeout':'3s','retries':20}}
            config.write_text(json.dumps({k:v for k,v in resolved.items() if not k.startswith('x-')}),encoding='utf-8')
            self.compose_call(self.project,config,'up','-d','--no-build','--pull','never','--wait','--wait-timeout','180')
            self.output['controller_configuration']=self.controller_settings
            # Psycopg async I/O on Windows requires a selector loop. Runner
            # keeps this choice local to each trial thread (no global policy).
            factory=asyncio.SelectorEventLoop if os.name=='nt' else None
            with asyncio.Runner(loop_factory=factory) as event_runner:
                event_runner.run(self.workload(config))
            rows=self.query('SELECT * FROM events ORDER BY event_id')
            grants=self.query('SELECT hospital_id,event_id,status FROM controller_grants ORDER BY event_id,grant_id')
            tickets=self.query('SELECT * FROM controller_tickets ORDER BY queue_position')
            actual={r['event_id']:r for r in rows}
            assert len(rows)==len(self.expected)==24 and set(actual)==set(self.expected)
            assert all(r['status']=='applied' for r in rows)
            for event_id,event in self.expected.items():
                assert all(actual[event_id][k]==v for k,v in event.items())
                assert all(actual[p]['applied_at']<=actual[event_id]['applied_at'] for p in event['prerequisite_ids'])
            expected_grants=0 if self.fixture_seed else 24
            assert len([g for g in grants if g['status']=='completed'])==expected_grants and not tickets
            sqlite={}
            probe="import json,sqlite3;c=sqlite3.connect('/data/hospital.db');print(json.dumps({'integrity':c.execute('PRAGMA integrity_check').fetchone()[0],'count':c.execute('SELECT count(*) FROM events').fetchone()[0],'pending':c.execute(\"SELECT count(*) FROM events WHERE status!='applied'\").fetchone()[0]}))"
            for site in ('hospital-a','hospital-b'):
                sqlite[site]=json.loads(self.compose_capture(self.project,config,'exec','-T',site,'python','-c',probe))
                assert sqlite[site]=={'integrity':'ok','count':0 if self.fixture_seed else 12,'pending':0}
            self.output.update(result='passed',postgres_events=rows,grants=grants,sqlite=sqlite,
                integrity={'exact_event_set_and_contents':True,'dependency_order':True,'completed_grants':expected_grants,'remaining_tickets':0,
                           'diagnostic_fixture_seed':self.fixture_seed},
                final_event_sha256=canonical(rows),finished_at=base.utcnow())
            if self.n==1:
                env=os.environ.copy()
                env['DATABASE_URL']=self.db_url
                paths=['tests/unit/test_cloud_live_metrics.py','tests/unit/test_cloud_metrics.py','tests/unit/test_admission.py',
                       'tests/integration/test_cloud_telemetry.py','tests/integration/test_cloud_apis.py']
                junit=HERE/'cp7b-representative-telemetry-focused-tests-20261029.xml'
                test=subprocess.run([sys.executable,'-m','pytest',*paths,'-q','-p','no:cacheprovider',
                    '--basetemp='+str(self.work/'pytest-temp'),'--junitxml='+str(junit)],cwd=ROOT,env=env,
                    text=True,capture_output=True,timeout=150)
                (self.work/'pytest.log').write_text(test.stdout+test.stderr,encoding='utf-8')
                suites=ET.parse(junit).getroot().findall('testsuite')
                self.output['focused_tests']={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
                assert test.returncode==0 and all(self.output['focused_tests'][k]==0 for k in ('failures','errors','skipped'))
                assert canonical(self.query('SELECT * FROM events ORDER BY event_id'))==self.output['final_event_sha256']
            print(f'PASS telemetry trial{self.n}: 24 synthetic events preserved',flush=True)
        except Exception as exc:
            self.output.update(result='failed',error=f'{type(exc).__name__}: {exc}',finished_at=base.utcnow())
            print(f"FAIL telemetry trial{self.n}: {self.output['error']}",flush=True)
        finally:
            self.save()
            if config:
                try:
                    volumes=self.docker('volume','ls','-q','--filter','label=com.docker.compose.project='+self.project).splitlines()
                    assert not(set(volumes)&self.original_volumes)
                    self.compose_call(self.project,config,'down','--remove-orphans')
                    for volume in volumes:
                        metadata=json.loads(self.docker('volume','inspect',volume))[0]
                        assert metadata['Labels']['com.docker.compose.project']==self.project
                        self.docker('volume','rm',volume)
                    for resource in ('volume','network'):
                        assert not self.docker(resource,'ls','-q','--filter','label=com.docker.compose.project='+self.project)
                    assert not self.docker('ps','-aq','--filter','label=com.docker.compose.project='+self.project)
                    self.output['cleanup']='passed'
                except Exception as exc:
                    self.output.update(result='failed',cleanup_error=f'{type(exc).__name__}: {exc}')
                finally:
                    config.unlink(missing_ok=True)
                    self.save()
        return self.output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--diagnostics-only',action='store_true',help='Run only the three diagnostics after seeding the same event dataset')
    parser.add_argument('--fixture-seed',action='store_true',help='For diagnostic-only recovery: seed disposable read fixtures directly, without repeating replay preparation')
    parser.add_argument('--trial-ids',default='1,2,3',help='Replicas to run; use an individual ID to resume only a failed replica')
    parser.add_argument('--preservation-baseline',type=Path,help='Original-resource baseline for a recovery overlapping this experiment\'s other disposable projects')
    args=parser.parse_args()
    trial_ids=[int(n) for n in args.trial_ids.split(',')]
    assert trial_ids and len(set(trial_ids))==len(trial_ids) and all(n in (1,2,3) for n in trial_ids)
    assert not args.fixture_seed or args.diagnostics_only
    assert not args.output.exists(),'Refuse evidence overwrite'
    work=Path(tempfile.mkdtemp(prefix='cp7b-telemetry-',dir=ROOT/'.venv'))
    image='crisisguard-telemetry-eval:'+uuid.uuid4().hex[:12]
    inventory=base.Runner(work,image,args.output,1)
    before,volumes=inventory.inventory()
    concurrent=[]
    if args.preservation_baseline:
        saved=json.loads(args.preservation_baseline.read_text(encoding='utf-8'))
        original={key:{**value,'mounts':[tuple(m) for m in value['mounts']]}
                  for key,value in saved['baseline_containers'].items()}
        original_volumes=set(saved['baseline_volumes'])
        assert all(before.get(key)==value for key,value in original.items())
        assert original_volumes<=volumes
        extra={key:value for key,value in before.items() if key not in original}
        assert all(value['name'].startswith('/cg-telemetry-') for value in extra.values())
        assert all(v.startswith('cg-telemetry-') for v in volumes-original_volumes)
        concurrent=list(extra)
        before,volumes=original,original_volumes
    report={'schema':'cp7b-representative-telemetry-v1','started_at':base.utcnow(),'image':image,
        'source_sha256':fair.source_hashes(),'baseline_containers':before,'baseline_volumes':sorted(volumes),
        'overlapping_disposable_test_container_ids_at_start':concurrent,
        'phases':[p for p in PHASES if not args.diagnostics_only or p['diagnostic']],
        'design':{'trials':len(trial_ids),'trial_ids':trial_ids,'diagnostics_only':args.diagnostics_only,'fixture_seed':args.fixture_seed,
            'concurrent_projects':len(trial_ids),'window_seconds':60,'expiry_between_phases_seconds':61,
            'application_modified':False,'executor_capacity_modified':False,
            'live_population':'authenticated existing-event status GETs; grant-bearing writes cannot be classified live by the current contract',
            'mixed_population':'authenticated live/replay status GETs plus actual Hospital A/B grant-authorized replay POSTs',
            'diagnostics':'2s disposable-DB table locks every 5s, capacity unchanged at 40; separate 300ms pre-Cloud proxy delay'},'trials':[]}
    try:
        inventory.run(['docker','build','-q','-t',image,'-f',str(ROOT/'infrastructure/docker/Dockerfile'),str(ROOT)],timeout=1200)
        probe="import pathlib,hashlib,json;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for d in ('cloud','edge','dashboard') for p in pathlib.Path(d).rglob('*.py') if '__pycache__' not in p.parts}))"
        assert json.loads(inventory.docker('run','--rm','--network','none','--entrypoint','python',image,'-c',probe))==report['source_sha256']
        report['image_source_match']=True
        report['image_id']=inventory.docker('image','inspect',image,'--format','{{.Id}}')
        with ThreadPoolExecutor(max_workers=len(trial_ids)) as pool:
            tasks=[pool.submit(Trial(work,image,n,volumes,args.diagnostics_only,args.fixture_seed).execute_trial) for n in trial_ids]
            for task in as_completed(tasks):
                report['trials'].append(task.result())
                args.output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
    finally:
        after,after_volumes=inventory.inventory()
        report['preservation']={'containers':len(before),'volumes':len(volumes),
            'container_identity_image_mounts_equal':before==after,'volume_set_equal':volumes==after_volumes,
            'application_sources_equal':fair.source_hashes()==report['source_sha256']}
        report['result']='passed' if len(report['trials'])==len(trial_ids) and all(t['result']=='passed' and t.get('cleanup')=='passed' for t in report['trials']) and all(report['preservation'][k] for k in ('container_identity_image_mounts_equal','volume_set_equal','application_sources_equal')) else 'failed'
        report['finished_at']=base.utcnow()
        report['trials'].sort(key=lambda t:t['trial'])
        args.output.write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
        print(f"FINAL {report['result']}; {args.output}; private logs={work}",flush=True)
    if report['result']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
