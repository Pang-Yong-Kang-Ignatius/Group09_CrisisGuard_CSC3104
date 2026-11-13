"""Offline AWS configuration audit and disposable three-project deployment test.

No AWS calls. --ci runs PostgreSQL/contracts only: units and TX dashboard smoke
already have their own jobs. Local default runs the complete applicable suite.
"""
import argparse
from http.client import HTTPException
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

import httpx
import psycopg

if __package__:
    from . import audit_deployment as audit, smoke_deployment as smoke, database_backup as backup
else:
    import audit_deployment as audit
    import smoke_deployment as smoke
    import database_backup as backup

ROOT = Path(__file__).resolve().parents[2]


def sanitize(text, secrets_to_hide=()):
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return re.sub(r'(?i)((?:password|token|secret)[\w-]*[\s\"\x27]*[:=][\s\"\x27]*)[^\s,;\"\x27}]+',
                  r'\1[REDACTED]', text)


def command_result(result, log_file, secrets_to_hide, *, capture=False, allow_failure=False):
    output = sanitize(result.stdout + result.stderr, secrets_to_hide)
    if not capture:
        with log_file.open('a', encoding='utf-8') as log:
            log.write(output)
    if result.returncode and not (allow_failure and result.returncode == 1):
        # Do not print argv: future commands may contain credentials.
        print(f'Deployment command exited {result.returncode}:\n{output[-12000:]}', flush=True)
        raise RuntimeError(f'Deployment command exited {result.returncode}; see sanitized output above')
    return result.stdout.strip()


def startup(call):
    # --pull never applies to PostgreSQL too. Fresh hosted runners have no cache.
    call('pull', '--policy', 'missing', 'postgres')
    call('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180')


def diagnostics(projects, secrets_to_hide):
    """Read only owned projects; never dump inspect Config/Env or Compose config."""
    for name, args, env in projects:
        print('Docker diagnostics for '+name, flush=True)
        def read(command):
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=20)
                return result.stdout, result.stderr
            except (OSError, subprocess.TimeoutExpired) as error:
                return '', type(error).__name__
        ids, error = read(args+['ps', '-aq'])
        if error:
            print(sanitize(error, secrets_to_hide), flush=True)
        for container in ids.splitlines():
            # Docker IDs only; no container selection outside this unique project.
            if not re.fullmatch(r'[0-9a-f]{12,64}', container):
                continue
            state, error = read(['docker', 'inspect', '--format',
                                 '{{.Name}} {{json .State}} restarts={{.RestartCount}}', container])
            print(sanitize(state+error, secrets_to_hide)[-12000:], flush=True)
        logs, error = read(args+['logs', '--no-color', '--tail', '40'])
        print(sanitize(logs+error, secrets_to_hide)[-16000:], flush=True)


def verify_links(docker, call, ports, peer, query, wait, run_id):
    """Disconnect only this run's hospital peer interface, keeping capture online."""
    evidence = []
    for offline in (0, 1):
        connected = 1 - offline
        project = offline + 1
        site = ('Hospital_A', 'Hospital_B')[offline]
        cid = call(project, 'ps', '-q', 'hospital').strip()
        assert re.fullmatch(r'[a-f0-9]{12,64}', cid)
        networks = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Networks}}', cid))
        assert peer.name in networks and len(networks) == 2
        wait(lambda: query("SELECT COUNT(*) FROM controller_grants WHERE status IN "
                           "('issued','acknowledged','reconciliation_required')") == [(0,)])
        chains = [smoke.event_chain(run_id, 'link-' + str(offline), hospital)
                  for hospital in ('Hospital_A', 'Hospital_B')]
        started = datetime.now(timezone.utc).isoformat()
        docker('network', 'disconnect', peer.name, cid)
        try:
            remaining = json.loads(docker('inspect', '--format', '{{json .NetworkSettings.Networks}}', cid))
            assert peer.name not in remaining and len(remaining) == 1
            for index, chain in enumerate(chains):
                for event in reversed(chain):
                    smoke.request(ports[4 + index], '/capture', event, expected=201)
            for port in ports[:3]:
                assert smoke.request(port, '/health')['status'] == 'healthy'
            wait(lambda: all(any(row['event_id'] == item['event_id'] and row['status'] == 'applied'
                                 for row in smoke.request(ports[4 + connected], '/events'))
                             for item in chains[connected]))
            ids = {item['event_id'] for item in chains[offline]}
            offline_rows = [row for row in smoke.request(ports[4 + offline], '/events') if row['event_id'] in ids]
            smoke.check_records(offline_rows, chains[offline], 'pending')
            for item in chains[offline]:
                assert query('SELECT COUNT(*) FROM events WHERE event_id=%s', (item['event_id'],)) == [(0,)]
            # The active peer progressed while all four remote endpoints were absent.
            call(offline + 1, 'restart', 'hospital')
            wait(lambda: len(smoke.request(ports[4 + offline], '/events')) >= len(chains[offline]))
            smoke.check_records([row for row in smoke.request(ports[4 + offline], '/events')
                                 if row['event_id'] in ids], chains[offline], 'pending')
        finally:
            docker('network', 'connect', '--alias', 'hospital-' + ('a' if offline == 0 else 'b'), peer.name, cid)
        wait(lambda: all(any(row['event_id'] == item['event_id'] and row['status'] == 'applied'
                             for row in smoke.request(ports[4 + offline], '/events'))
                         for item in chains[offline]))
        for chain in chains:
            previous = None
            for item in chain:
                rows = query('SELECT applied_at FROM events WHERE event_id=%s', (item['event_id'],))
                assert len(rows) == 1 and (previous is None or rows[0][0] >= previous)
                previous = rows[0][0]
        evidence.append({'disconnected': site, 'started_at': started,
                         'restored_at': datetime.now(timezone.utc).isoformat(),
                         'pending_during_fault': 4, 'other_site_applied_during_fault': 4,
                         'applied_after_restore': 8, 'restart_retained_pending': True,
                         'cloud_services_stopped': False,
                         'scope': 'Local Docker peer-interface partition, including APIs and controller; not AWS or a physical link'})
    return evidence


def verify(ci=False):
    run_id = 'cg-meta-deploy-' + uuid.uuid4().hex[:12]
    # Stable ignored evidence location for local runs and hosted CI alike.
    (ROOT / '.venv').mkdir(exist_ok=True)
    directory = ROOT / '.venv' / run_id
    directory.mkdir()
    image = 'crisisguard-metaguard-test:' + run_id
    ports = smoke.free_ports(7)
    password, operator, a_token, b_token = [secrets.token_hex(32) for _ in range(4)]
    child = os.environ.copy()
    for key in tuple(child):
        if key.startswith(('COMPOSE_', 'CONTROLLER_', 'HOSPITAL_')):
            child.pop(key)
    child['AWS_EC2_METADATA_DISABLED'] = 'true'
    report = {'run': run_id, 'aws_used': False, 'checks': [], 'artifacts': str(directory)}
    files, projects, owned = [], [], []
    peer = smoke.PeerNetwork(run_id+'-peers')

    def run(args, *, capture=False, env=None, allow_failure=False):
        result = subprocess.run(args, cwd=ROOT, env=env or child, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=900)
        return command_result(result, directory/'run.log', (password, operator, a_token, b_token),
                              capture=capture, allow_failure=allow_failure)

    def docker(*args):
        return run(['docker', *args], capture=True)

    def snapshot():
        ids = docker('ps','-aq').splitlines()
        rows = json.loads(docker('inspect',*ids)) if ids else []
        keys = ('Status','Running','Pid','StartedAt','FinishedAt','ExitCode')
        return {row['Id']: {'state':{key:row['State'][key] for key in keys},
                            'restarts':row['RestartCount'], 'mounts':row['Mounts']} for row in rows}

    def passed(name):
        report['checks'].append(name)
        print('PASS '+name, flush=True)

    original = snapshot()
    original_volumes = set(docker('volume','ls','-q').splitlines())
    original_images = set(docker('image','ls','--no-trunc','--format','{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())
    assert not docker('image','ls','-q',image)
    cloud = {'CLOUD_PROJECT':run_id+'-cloud', 'CLOUD_BIND_ADDRESS':'172.31.1.2',
             'PATIENT_PORT':str(ports[0]), 'EMERGENCY_PORT':str(ports[1]), 'ADMIN_PORT':str(ports[2]),
             'CONTROLLER_PORT':str(ports[3]), 'POSTGRES_USER':'cp7_verify', 'POSTGRES_DB':'cp7_verify',
             'POSTGRES_PASSWORD':password, 'CONTROLLER_CLOUD_TOKEN':operator,
             'HOSPITAL_A_CONTROLLER_TOKEN':a_token, 'HOSPITAL_B_CONTROLLER_TOKEN':b_token,
             'CONTROLLER_ADMISSION_POLICY':'aimd', 'CONTROLLER_ADMISSION_ADDITIVE_RPS':'0.2',
             'CONTROLLER_ADMISSION_POLL_SECONDS':'1', 'CONTROLLER_ADMISSION_MAX_AGE_SECONDS':'5',
             'CONTROLLER_ADMISSION_TIMEOUT_SECONDS':'1', 'CONTROLLER_ADMISSION_DWELL_SECONDS':'3',
             'CONTROLLER_ADMISSION_HEALTHY_SAMPLES':'2'}
    settings = [cloud]
    for index, token in enumerate((a_token,b_token)):
        settings.append({'HOSPITAL_PROJECT':run_id+'-'+('a' if index==0 else 'b'),
            'HOSPITAL_BIND_ADDRESS':'127.0.0.1', 'HOSPITAL_PORT':str(ports[4+index]),
            'SITE_ID':'Hospital_A' if index==0 else 'Hospital_B', 'AUTO_REPLAY':'1',
            'AUTO_REPLAY_INTERVAL_SECONDS':'.5', 'HOSPITAL_CONTROLLER_TOKEN':token,
            'CONTROLLER_URL':f"http://172.31.1.2:{ports[3]}",
            'CLOUD_URL':f"http://172.31.1.2:{ports[0]}/events",
            'EMERGENCY_URL':f"http://172.31.1.2:{ports[1]}/events",
            'ADMIN_URL':f"http://172.31.1.2:{ports[2]}/events"})
    try:
        for index, values in enumerate(settings):
            role = 'cloud' if index==0 else 'hospital'
            name = values['CLOUD_PROJECT' if index==0 else 'HOSPITAL_PROJECT']
            assert not docker('ps','-aq','--filter','label=com.docker.compose.project='+name)
            assert not docker('volume','ls','-q','--filter','label=com.docker.compose.project='+name)
            assert not docker('network','ls','-q','--filter','label=com.docker.compose.project='+name)
            env_file = directory/(name+'.env')
            env_file.write_text(''.join(f'{key}={value}\n' for key,value in values.items()), encoding='utf-8')
            env_file.chmod(0o600)
            files.append(env_file)
            command = ['docker','compose','--env-file',str(env_file),'-p',name]
            for source in (ROOT/f'infrastructure/docker/compose.{role}.yaml',
                           ROOT/f'infrastructure/aws/compose.{role}.override.yaml',
                           ROOT/f'infrastructure/aws/compose.metaguard.{role}.yaml'):
                command += ['-f',str(source)]
            environment = dict(child)
            for key in values:
                environment.pop(key,None)
            config = json.loads(run(command+['config','--format','json'], capture=True, env=environment))
            audit.audit(role,config)  # Actual AWS bind policy, never used on the test host.
            runtime = {key:value for key,value in config.items() if not key.startswith('x-')}
            runtime.setdefault('networks',{})['peers'] = {'external':True,'name':peer.name}
            for service, value in runtime['services'].items():
                if service != 'postgres':
                    value['image'] = image
                    value.pop('build',None)
                    value['pull_policy'] = 'never'
                    alias = service if index==0 else 'hospital-'+('a' if index==1 else 'b')
                    value['networks'] = {'default':{},'peers':{'aliases':[alias]}}
                for port in value.get('ports',[]):
                    port['host_ip'] = '127.0.0.1'
            if index==0:
                # Local-only SQL test access; audited AWS configuration exposes none.
                runtime['services']['postgres']['ports'] = [{'host_ip':'127.0.0.1','published':str(ports[6]),'target':5432,'protocol':'tcp'}]
            else:
                runtime['services']['hospital']['environment'].update(
                    CONTROLLER_URL='http://controller:8000', CLOUD_URL='http://patient:8000/events',
                    EMERGENCY_URL='http://emergency:8000/events', ADMIN_URL='http://admin:8000/events')
            for volume in runtime.get('volumes',{}).values():
                assert not volume.get('external') and volume['name'].startswith(name+'_')
                assert volume['name'] not in original_volumes
            private_config = directory/(name+'.json')
            private_config.write_text(json.dumps(runtime), encoding='utf-8')
            private_config.chmod(0o600)
            files.append(private_config)
            projects.append((name,['docker','compose','--env-file',str(env_file),'-p',name,'-f',str(private_config)],environment))
        passed('Three real AWS Compose configurations pass private-listener, credential, persistence and restart audits')
        run(['docker','build','-f',str(ROOT/'infrastructure/docker/Dockerfile'),'-t',image,str(ROOT)])
        peer.create()
        for index in range(3):
            owned.append(index)
            call = lambda *args, index=index: run(projects[index][1]+list(args), env=projects[index][2])
            call('config','--quiet')
            if index == 0:
                startup(call)
            else:
                call('up','-d','--no-build','--pull','never','--wait','--wait-timeout','180')
        def call(index,*args):
            return run(projects[index][1]+list(args), env=projects[index][2])
        def api(method,path,body=None, *, token=operator, expected=200):
            headers = {'X-Cloud-Token':token} if token==operator else {'Authorization':'Bearer '+token}
            response = httpx.request(method,f'http://127.0.0.1:{ports[3]}'+path,json=body,headers=headers,timeout=10)
            assert response.status_code == expected, (path,response.status_code)
            for secret in (password,operator,a_token,b_token):
                assert secret not in response.text
            return response.json()
        db_url = f'postgresql://cp7_verify:{password}@127.0.0.1:{ports[6]}/cp7_verify'
        def query(sql,args=()):
            with psycopg.connect(db_url,connect_timeout=2) as conn:
                cur = conn.execute(sql,args)
                return cur.fetchall() if cur.description else []
        def wait(check,timeout=100):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:
                    if check():
                        return
                except (httpx.HTTPError,psycopg.OperationalError,OSError,HTTPException):
                    pass
                time.sleep(.25)
            raise RuntimeError('Split deployment recovery timed out')
        def state():
            response=httpx.get(f'http://127.0.0.1:{ports[3]}/admission/status',headers={'X-Cloud-Token':operator},timeout=5)
            response.raise_for_status()
            return response.json()
        chains=[smoke.event_chain(run_id,'split',site) for site in ('Hospital_A','Hospital_B')]
        call(0,'stop','controller','patient','emergency','admin')
        for index,chain in enumerate(chains):
            for event in reversed(chain):
                smoke.request(ports[4+index],'/capture',event,expected=201)
        call(0,'up','-d','--no-build','--wait','patient','emergency','admin')
        time.sleep(2)
        assert query('SELECT COUNT(*) FROM events') == [(0,)]
        call(0,'up','-d','--no-build','--wait','controller')
        for index,chain in enumerate(chains):
            wait(lambda index=index: all(e['status']=='applied' for e in smoke.request(ports[4+index],'/events')))
            smoke.check_records(smoke.request(ports[4+index],'/events'),chain,'applied')
        assert query("SELECT COUNT(*) FROM controller_grants WHERE status='completed'") == [(8,)]
        api('POST','/tickets',{'hospital_id':'Hospital_B','event_id':'FORGED','priority':5},token=a_token,expected=403)
        passed('Independent Hospital A/B projects replay eight dependent events through authenticated private controller/Cloud endpoints')

        # A committed result unknown to a stopped hospital is resolved automatically.
        call(0,'stop','controller')
        uncertain={'event_id':run_id+'-uncertain','site_id':'Hospital_A','event_type':'patient_registration',
                   'base_priority':2,'prerequisite_ids':[],'payload':{'patient_id':'synthetic'}}
        smoke.request(ports[4],'/capture',uncertain,expected=201)
        call(1,'stop','hospital')
        call(0,'up','-d','--no-build','--wait','controller')
        sys.path.insert(0,str(ROOT))
        from cloud.metaguard import event_digest
        api('POST','/tickets',{'hospital_id':'Hospital_A','event_id':uncertain['event_id'],'priority':2,'event_digest':event_digest(uncertain)})
        selected={}
        def allocate():
            selected.update(api('POST','/grants/next'))
            return 'grant_id' in selected
        wait(allocate)
        gid=selected['grant_id']
        def acknowledge():
            response=httpx.post(f'http://127.0.0.1:{ports[3]}/grants/{gid}/acknowledge',json={'hospital_id':'Hospital_A'},headers={'Authorization':'Bearer '+a_token},timeout=10)
            assert response.status_code in (200,429)
            return response.status_code==200
        wait(acknowledge)
        response=httpx.post(f'http://127.0.0.1:{ports[0]}/events',json=uncertain,
            headers={'Authorization':'Bearer '+a_token,'X-MetaGuard-Grant':gid},timeout=10)
        assert response.status_code==201
        query("UPDATE controller_grants SET expires_at=clock_timestamp()-INTERVAL '1 second' WHERE grant_id=%s",(gid,))
        call(0,'restart','controller')
        call(0,'up','-d','--no-build','--wait','controller')
        call(1,'start','hospital')
        wait(lambda:any(e['event_id']==uncertain['event_id'] and e['status']=='applied' for e in smoke.request(ports[4],'/events')))
        assert query('SELECT status FROM controller_grants WHERE grant_id=%s',(gid,)) == [('completed',)]
        assert query('SELECT COUNT(*) FROM controller_grants WHERE event_id=%s',(uncertain['event_id'],)) == [(1,)]
        passed('Controller restart automatically reconciles committed uncertain work across the hospital boundary without reissue')

        before=state()['allowed']
        call(0,'stop','postgres')
        api('GET','/admission/status',expected=503)
        for index,site in enumerate(('Hospital_A','Hospital_B')):
            smoke.request(ports[4+index],'/capture',{'event_id':run_id+'-outage-'+site,'site_id':site,
                'event_type':'patient_registration','base_priority':1,'prerequisite_ids':[],'payload':{}},expected=201)
        call(0,'start','postgres')
        wait(lambda:state()['allowed']>=before)
        for index in range(2):
            wait(lambda index=index:all(e['status']=='applied' for e in smoke.request(ports[4+index],'/events')))
        assert query('SELECT COUNT(*) FROM events') == [(11,)]
        call(0,'stop','admin')
        wait(lambda:state()['state']=='Recovery')
        assert 'error' in state()['metrics']['admin']
        call(0,'start','admin')
        wait(lambda:state()['state']=='Normal')
        report['events_applied']=11
        report['admission']=state()
        passed('PostgreSQL persistence/outage recovery and missing-endpoint adaptive Recovery/Normal transitions pass across split projects')

        report['independent_links'] = verify_links(docker, call, ports, peer, query, wait, run_id)
        report['events_applied'] += 16
        passed('Each hospital independently loses APIs/controller while the other applies records; pending data survives restart and drains after reconnection')

        # Policy backout keeps the compatible controller binary, schema and volumes.
        wait(lambda: query("SELECT COUNT(*) FROM controller_grants WHERE status IN "
                           "('issued','acknowledged','reconciliation_required')") == [(0,)])
        for index in (1, 2):
            call(index, 'stop', 'hospital')
        ctl_id = call(0, 'ps', '-q', 'controller')
        image_before = docker('inspect', '--format', '{{.Image}}', ctl_id)
        pg_id = call(0, 'ps', '-q', 'postgres')
        pg_mounts = docker('inspect', '--format', '{{json .Mounts}}', pg_id)
        stored_events = query('SELECT event_id,site_id,status FROM events ORDER BY event_id')
        cloud_runtime = Path(projects[0][1][-1])
        runtime = json.loads(cloud_runtime.read_text(encoding='utf-8'))
        assert state()['policy'] == 'aimd'
        runtime['services']['controller']['environment']['CONTROLLER_ADMISSION_POLICY'] = 'fixed'
        cloud_runtime.write_text(json.dumps(runtime), encoding='utf-8')
        call(0, 'up', '-d', '--no-build', '--no-deps', '--wait', 'controller')
        wait(lambda: state()['policy'] == 'fixed' and state()['state'] == 'Normal')
        assert docker('inspect', '--format', '{{.Image}}', call(0, 'ps', '-q', 'controller')) == image_before
        assert call(0, 'ps', '-q', 'postgres') == pg_id
        assert docker('inspect', '--format', '{{json .Mounts}}', pg_id) == pg_mounts
        assert query('SELECT event_id,site_id,status FROM events ORDER BY event_id') == stored_events
        for index in (1, 2):
            call(index, 'start', 'hospital')
            wait(lambda index=index: bool(smoke.request(ports[index + 3], '/')))
            item = {'event_id': run_id + '-fixed-backout-' + str(index),
                    'site_id': ('Hospital_A', 'Hospital_B')[index - 1],
                    'event_type': 'patient_registration', 'base_priority': 2,
                    'prerequisite_ids': [], 'payload': {'patient_id': 'synthetic'}}
            smoke.request(ports[index + 3], '/capture', item, expected=201)
            wait(lambda item=item: query('SELECT status FROM events WHERE event_id=%s', (item['event_id'],)) == [('applied',)])
        report['events_applied'] += 2
        assert query('SELECT COUNT(*) FROM events') == [(report['events_applied'],)]
        report['policy_backout'] = {'from': 'aimd', 'to': 'fixed', 'same_controller_image': True,
                                    'same_postgres_container_and_mounts': True,
                                    'all_previous_events_retained': True, 'new_events_applied': 2,
                                    'scope': 'Local candidate-policy backout; no older binary or database restore'}
        passed('AIMD to fixed policy backout retains controller image, PostgreSQL, prior events and protected replay')

        wait(lambda: query("SELECT COUNT(*) FROM controller_grants WHERE status IN "
                           "('issued','acknowledged','reconciliation_required')") == [(0,)])
        call(0, 'stop', 'controller')
        stored_events = query('SELECT event_id,site_id,status FROM events ORDER BY event_id')
        report['backup_restores'] = []
        copies = directory / 'host-export-copies'
        copies.mkdir()
        for index in range(3):
            kind = 'postgres' if index == 0 else 'sqlite'
            expected_tables = None
            if index == 0:
                tables = query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
                expected_tables = {name: query(backup.table_fingerprint_sql(name))[0][0] for (name,) in tables}
            source = directory / ('cloud.dump' if index == 0 else 'hospital-' + str(index) + '.sqlite3')
            saved = backup.backup(kind, projects[index][1], source, env=projects[index][2])
            # Export outside Docker storage, verify transfer, then restore that copy.
            copied = copies / source.name
            shutil.copyfile(source, copied)
            backup.verify_checksum(copied, saved['sha256'])
            expected = [row[0] for row in stored_events if index == 0 or row[1] == ('Hospital_A', 'Hospital_B')[index - 1]]
            restored = backup.restore_postgres(copied, expected, expected_tables=expected_tables) if index == 0 else backup.restore_sqlite(copied, expected)
            report['backup_restores'].append({'role': ('cloud', 'Hospital_A', 'Hospital_B')[index],
                'sha256': saved['sha256'], 'bytes': saved['bytes'],
                'expected_events_found': restored['expected_events_found'],
                'copy_checksum_verified': True, 'isolated_restore': True,
                'verified_postgres_tables': restored.get('verified_tables'),
                'scope': 'Disposable local databases; host copy is not an off-machine AWS backup'})
        call(0, 'start', 'controller')
        wait(lambda: state()['policy'] == 'fixed')
        passed('Cloud and both hospital exports pass copied-file checksums and isolated restores')

        test_env=dict(child,CONTROLLER_TEST_DATABASE_URL=db_url,DATABASE_URL=db_url)
        paths=['tests/integration/test_controller_postgres.py','tests/integration/test_metaguard_reliability.py',
               'tests/integration/test_cloud_apis.py','tests/integration/test_cloud_telemetry.py',
               'tests/integration/test_manual_replay_security.py'] if ci else ['tests/unit','tests/integration']
        if not ci:
            test_env['RUN_DASHBOARD_DOCKER']='1'
        result_file=directory/'pytest.xml'
        output=run([sys.executable,'-m','pytest',*paths,'-q','-p','no:cacheprovider',
                    '--basetemp='+str(directory/'pytest-temp'),'--junitxml='+str(result_file)],env=test_env,allow_failure=True)
        (directory/'pytest.log').write_text(output,encoding='utf-8')
        suites=ET.parse(result_file).getroot().findall('testsuite')
        report['pytest']={key:sum(int(s.attrib.get(key,0)) for s in suites) for key in ('tests','failures','errors','skipped')}
        assert not report['pytest']['failures'] and not report['pytest']['errors']
        passed('Selected regressions: '+str(report['pytest']['tests'])+' passed')
        report['result']='passed'
    except Exception as error:
        diagnostics([projects[index] for index in owned], (password, operator, a_token, b_token))
        report.update(result='failed',error=sanitize(str(error), (password, operator, a_token, b_token)))
        raise
    finally:
        errors=[]
        for index in reversed(owned):
            name,args,env=projects[index]
            try:
                run(args+['logs','--no-color'],env=env)
                volumes=docker('volume','ls','-q','--filter','label=com.docker.compose.project='+name).splitlines()
                assert not set(volumes)&original_volumes
                run(args+['down','--remove-orphans'],env=env)
                for volume in volumes:
                    assert json.loads(docker('volume','inspect',volume))[0]['Labels']['com.docker.compose.project']==name
                    docker('volume','rm',volume)
            except Exception:
                errors.append(name)
        try:
            peer.remove()
            current=snapshot()
            assert all(current.get(key)==value for key,value in original.items())
            assert original_volumes<=set(docker('volume','ls','-q').splitlines())
            assert original_images<=set(docker('image','ls','--no-trunc','--format','{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())
            assert not errors
            report['preserved']={'containers':len(original),'volumes':len(original_volumes),'images':True}
            report['cleanup']='passed; own containers/networks/volumes removed; test image retained'
            passed('Existing resource states, volumes and images preserved; disposable projects removed')
        except Exception:
            report['result']='failed'
            report['cleanup_errors']=errors or ['preservation check']
            raise
        finally:
            for file in files:
                file.unlink(missing_ok=True)
            (directory/'report.json').write_text(json.dumps(report,indent=2,default=str),encoding='utf-8')
            print('Evidence: '+str(directory/'report.json'),flush=True)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ci',action='store_true',help='Only PostgreSQL/contracts; reuse existing unit and TX Docker CI jobs')
    verify(parser.parse_args().ci)
