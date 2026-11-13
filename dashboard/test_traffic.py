"""Local demo capture and bounded read traffic, guarded for development only."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import os
from pathlib import Path
import secrets
from threading import Lock
from urllib.parse import quote, urlsplit

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError
from starlette.concurrency import run_in_threadpool

from cloud.database.db import get_connection
from cloud.database.db import get_cloud_event
from cloud.traffic import traffic_headers

INTERVAL_SECONDS = .2  # five starts/second/API; no catch-up bursts
DEADLINE_SECONDS = 40
COUNTS = (10, 50, 100)
DEMO_EVENTS = (
    {'event_id': 'CG_LOCAL_MONITOR_V1_PATIENT', 'site_id': 'Hospital_A',
     'event_type': 'patient_registration', 'base_priority': 1, 'prerequisite_ids': [],
     'payload': {'patient_id': 'SYNTHETIC_LOCAL_MONITOR_V1', 'test_source': 'crisisguard_monitoring_demo'}},
    {'event_id': 'CG_LOCAL_MONITOR_V1_EMERGENCY', 'site_id': 'Hospital_A',
     'event_type': 'emergency_record', 'base_priority': 5,
     'prerequisite_ids': ['CG_LOCAL_MONITOR_V1_PATIENT'],
     'payload': {'patient_id': 'SYNTHETIC_LOCAL_MONITOR_V1', 'test_source': 'crisisguard_monitoring_demo'}},
    {'event_id': 'CG_LOCAL_MONITOR_V1_DISCHARGE', 'site_id': 'Hospital_A',
     'event_type': 'patient_discharge', 'base_priority': 2,
     'prerequisite_ids': ['CG_LOCAL_MONITOR_V1_EMERGENCY'],
     'payload': {'patient_id': 'SYNTHETIC_LOCAL_MONITOR_V1', 'test_source': 'crisisguard_monitoring_demo'}},
)


class TrafficInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    count: StrictInt = 100


def local_enabled(services):
    if (os.getenv('DASHBOARD_TEST_TRAFFIC_ENABLED') != '1'
            or os.getenv('CRISISGUARD_DEPLOYMENT_MODE') != 'local-development'):
        return False
    if any(os.getenv(k) for k in ('AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_EXECUTION_ENV', 'EC2_INSTANCE_ID')):
        return False
    if any(os.getenv(k, '').lower() in ('production', 'prod', 'aws', 'staging')
           for k in ('APP_ENV', 'ENVIRONMENT', 'DEPLOYMENT_ENV')):
        return False
    # Independent EC2 veto where the host exposes DMI identity; no metadata calls.
    try:
        if 'amazon' in Path('/sys/class/dmi/id/sys_vendor').read_text().lower():
            return False
    except OSError:
        pass
    for name, port in (('patient', 8001), ('emergency', 8003), ('admin', 8004)):
        if services.get(name) not in (f'http://{name}:8000', f'http://127.0.0.1:{port}', f'http://localhost:{port}'):
            return False
    try:
        database = urlsplit(os.getenv('DATABASE_URL', 'postgresql://127.0.0.1:5433/crisisguard'))
        if database.scheme not in ('postgres', 'postgresql') or database.hostname not in ('postgres', 'localhost', '127.0.0.1'):
            return False
    except ValueError:
        return False
    return True


def applied_targets():
    """Read only bounded ID/type metadata; never fetch or return clinical bodies."""
    with get_connection() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '3000ms'")
        return conn.execute('''SELECT DISTINCT ON (event_type) event_id, event_type
            FROM events WHERE status = 'applied' AND event_type = ANY(%s)
            ORDER BY event_type, applied_at DESC NULLS LAST, event_id DESC''',
            (['patient_registration', 'encounter_creation', 'emergency_record', 'patient_discharge'],)).fetchall()


class TestTraffic:
    def __init__(self, services, event_types, hospitals=None):
        self.services, self.event_types = services, event_types
        self.hospitals = hospitals or {}
        self.lock = Lock()
        self.prepare_lock = asyncio.Lock()
        self.job = None
        self.task = None

    def require_enabled(self):
        if not local_enabled(self.services):
            raise HTTPException(404, 'Local test traffic is disabled')

    def require_preparation_enabled(self):
        self.require_enabled()
        if (os.getenv('METAGUARD_REQUIRED') != '1'
                or self.hospitals.get('hospital-a') not in
                ('http://hospital-a:8000', 'http://127.0.0.1:8000', 'http://localhost:8000')):
            raise HTTPException(404, 'Local demo preparation is disabled')
        live, replay = os.getenv('CLOUD_LIVE_TELEMETRY_TOKEN', ''), os.getenv('CLOUD_REPLAY_TELEMETRY_TOKEN', '')
        if len(live) < 32 or len(replay) < 32 or live == replay:
            raise HTTPException(503, 'Distinct local telemetry credentials are unavailable')

    async def hospital_events(self):
        try:
            response = await run_in_threadpool(httpx.request, 'GET',
                self.hospitals['hospital-a']+'/events', timeout=5, trust_env=False, follow_redirects=False)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Invalid hospital queue')
            return {row.get('event_id'): row for row in rows}
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, 'Local hospital queue unavailable') from error

    @staticmethod
    def same_event(row, expected):
        return all(row.get(key) == value for key, value in expected.items())

    async def preparation_status(self, rows=None):
        if rows is None:
            rows = await self.hospital_events()
        outcomes = {}
        for service, expected in zip(('patient', 'emergency', 'admin'), DEMO_EVENTS):
            local = rows.get(expected['event_id'])
            try:
                remote = await run_in_threadpool(get_cloud_event, expected['event_id'])
            except Exception as error:
                raise HTTPException(503, 'Cloud record status unavailable') from error
            if ((local is None and remote is not None)
                    or (local is not None and not self.same_event(local, expected))
                    or (remote is not None and (remote.get('site_id') != expected['site_id']
                        or remote.get('event_type') != expected['event_type']))):
                outcomes[service] = 'conflict'
            elif local is None and remote is None:
                outcomes[service] = 'not_prepared'
            elif local is not None and local.get('status') == 'applied' and remote and remote.get('status') == 'applied':
                outcomes[service] = 'applied'
            else:
                outcomes[service] = 'pending'
        state = ('conflict' if 'conflict' in outcomes.values() else
                 'ready' if all(v == 'applied' for v in outcomes.values()) else
                 'not_prepared' if all(v == 'not_prepared' for v in outcomes.values()) else 'pending')
        return {'status': state, 'services': outcomes}

    async def prepare_records(self):
        if self.prepare_lock.locked():
            raise HTTPException(409, 'Demo preparation already running')
        async with self.prepare_lock:
            rows = await self.hospital_events()
            before = await self.preparation_status(rows)
            if before['status'] == 'conflict':
                raise HTTPException(409, 'A demo event ID has conflicting content')
            for expected in DEMO_EVENTS:
                if expected['event_id'] in rows:
                    continue
                try:
                    response = await run_in_threadpool(httpx.request, 'POST',
                        self.hospitals['hospital-a']+'/capture', json=expected,
                        timeout=8, trust_env=False, follow_redirects=False)
                    body = response.json()
                except (httpx.HTTPError, ValueError) as error:
                    raise HTTPException(503, 'Capture unconfirmed; inspect hospital queue before retry') from error
                if (response.status_code != 201 or not isinstance(body, dict)
                        or body.get('event_id') != expected['event_id'] or body.get('status') != 'pending'):
                    raise HTTPException(503, 'Capture not confirmed; inspect hospital queue before retry')
            return await self.preparation_status()

    def snapshot(self):
        job = deepcopy(self.job)
        if job:
            for item in job['services'].values():
                item['not_attempted'] = job['count']-item['attempted']
                item['in_flight'] = item['attempted']-item['succeeded']-item['failed']
        return {'enabled': True, 'active': self.lock.locked(), 'counts': COUNTS,
                'rate_per_api_rps': 5, 'max_concurrency': 3, 'deadline_seconds': DEADLINE_SECONDS,
                'run': job}

    @staticmethod
    def error(item, label):
        item['errors'][label] = item['errors'].get(label, 0) + 1

    async def service_reads(self, client, name, target, headers):
        item = self.job['services'][name]
        if target is None:
            item['status'] = 'no_applied_record'
            return
        event_id, event_type = target
        item['status'] = 'running'
        for _ in range(self.job['count']):
            started = asyncio.get_running_loop().time()
            item['attempted'] += 1
            try:
                response = await client.get(self.services[name]+'/events/'+quote(event_id, safe=''), headers=headers)
                if response.status_code != 200:
                    item['failed'] += 1
                    self.error(item, f'HTTP {response.status_code}')
                    if response.status_code == 404:
                        item['status'] = 'record_unavailable'
                        return
                else:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if (isinstance(body, dict) and body.get('event_id') == event_id
                            and body.get('event_type') == event_type and body.get('status') == 'applied'):
                        item['succeeded'] += 1
                    else:
                        item['failed'] += 1
                        self.error(item, 'Invalid event response')
            except httpx.RequestError:
                item['failed'] += 1
                self.error(item, 'Connection failed or timed out')
                item['status'] = 'connection_failed'
                return
            if item['attempted'] < self.job['count']:
                await asyncio.sleep(max(0, INTERVAL_SECONDS-(asyncio.get_running_loop().time()-started)))
        item['status'] = 'completed' if not item['failed'] else 'completed_with_errors'

    async def run(self, headers):
        try:
            async with asyncio.timeout(DEADLINE_SECONDS):
                records = await run_in_threadpool(applied_targets)
                targets = {name: next(((eid, kind) for eid, kind in records if kind in types), None)
                           for name, types in self.event_types.items()}
                async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=False,
                                             limits=httpx.Limits(max_connections=3, max_keepalive_connections=3)) as client:
                    await asyncio.gather(*(self.service_reads(client, name, targets[name], headers) for name in self.services))
                self.job['status'] = ('completed' if all(s['succeeded'] == self.job['count'] for s in self.job['services'].values())
                                      else 'completed_with_errors')
        except TimeoutError:
            self.job['status'] = 'timed_out'
            self.job['error'] = 'Execution deadline reached'
        except asyncio.CancelledError:
            self.job['status'] = 'cancelled'
            raise
        except Exception:
            # Do not return exception text, URLs, bodies, configuration or headers.
            self.job['status'] = 'failed'
            self.job['error'] = 'Applied-record lookup or traffic execution failed'
        finally:
            for item in self.job['services'].values():
                unconfirmed = item['attempted']-item['succeeded']-item['failed']
                if unconfirmed:
                    item['failed'] += unconfirmed
                    self.error(item, 'Request unconfirmed at execution limit')
                item['not_attempted'] = self.job['count']-item['attempted']
                if item['status'] in ('pending', 'running'):
                    item['status'] = self.job['status']
            self.lock.release()

    async def shutdown(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            finally:
                # Cancellation before the coroutine's first step never enters
                # run()'s finally block. Release that unstarted job as well.
                if self.lock.locked():
                    self.job['status'] = 'cancelled'
                    self.lock.release()


def install(app, services, event_types, hospitals=None):
    generator = TestTraffic(services, event_types, hospitals)
    app.state.test_traffic = generator

    @app.get('/api/test-traffic', include_in_schema=False)
    async def status():
        generator.require_enabled()
        return generator.snapshot()

    @app.get('/api/demo-records', include_in_schema=False)
    async def demo_records_status():
        generator.require_preparation_enabled()
        return await generator.preparation_status()

    @app.post('/api/demo-records', include_in_schema=False)
    async def prepare_demo_records(request: Request):
        generator.require_preparation_enabled()
        if request.headers.get('X-CrisisGuard-Local-Action') != 'prepare-demo-records':
            raise HTTPException(403, 'Explicit local action header required')
        return await generator.prepare_records()

    @app.post('/api/test-traffic', status_code=202, include_in_schema=False)
    async def start(request: Request):
        generator.require_enabled()
        # Non-credential CSRF guard: cross-origin browser requests cannot set this
        # header without a CORS preflight, which this local dashboard does not allow.
        if request.headers.get('X-CrisisGuard-Local-Action') != 'test-traffic':
            raise HTTPException(403, 'Explicit local action header required')
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 256:
                raise HTTPException(413, 'Test traffic input is too large')
        try:
            body = TrafficInput.model_validate_json(payload)
        except ValidationError:
            raise HTTPException(422, 'Supply only an integer count: 10, 50 or 100') from None
        if body.count not in COUNTS:
            raise HTTPException(422, 'Request count must be 10, 50 or 100 per API')
        headers = traffic_headers('live')
        replay_token = os.getenv('CLOUD_REPLAY_TELEMETRY_TOKEN', '')
        if (not headers or len(replay_token) < 32
                or os.getenv('CLOUD_LIVE_TELEMETRY_TOKEN') == replay_token):
            raise HTTPException(503, 'Distinct live telemetry credentials are unavailable')
        if not generator.lock.acquire(blocking=False):
            raise HTTPException(409, 'Test traffic is already running')
        generator.job = {'id': secrets.token_hex(12), 'count': body.count, 'status': 'running',
                         'services': {name: {'status': 'pending', 'attempted': 0, 'succeeded': 0,
                                             'failed': 0, 'not_attempted': body.count, 'errors': {}}
                                      for name in services}}
        try:
            generator.task = asyncio.create_task(generator.run(headers))
        except BaseException:
            generator.lock.release()
            raise
        return generator.snapshot()

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous_lifespan(application) as state:
            try:
                yield state
            finally:
                await generator.shutdown()

    app.router.lifespan_context = lifespan
