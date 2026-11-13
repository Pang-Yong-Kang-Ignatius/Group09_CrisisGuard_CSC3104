"""Loopback-only controls for this checkout's original crisisguard-local project.

Run: python -m infrastructure.scripts.local_outage_agent --allow-local-demo
Paste the temporary terminal token into http://localhost:8080. No AWS support.
"""
import argparse
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
PROJECT = 'crisisguard-local'
APIS = ('patient', 'emergency', 'admin')
REQUIRED = {*APIS, 'postgres', 'hospital-a', 'hospital-b', 'dashboard'}
ORIGINS = {'http://localhost:8080', 'http://127.0.0.1:8080'}
MODULES = {name: f'cloud.{name}_service.main:app' for name in APIS}


def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=45)
    if result.returncode:
        # Do not expose inspect Env, Docker stderr or private deployment settings.
        raise RuntimeError('Docker operation failed; check Docker Desktop')
    return result.stdout.strip()


def same_path(value, target):
    # Docker Desktop Compose labels may contain doubled Windows separators.
    return bool(value) and Path(value.replace('\\\\', '\\')).resolve() == target.resolve()


class Control:
    def __init__(self):
        endpoint = os.getenv('DOCKER_HOST') or json.loads(docker('context', 'inspect'))[0]['Endpoints']['docker']['Host']
        if not endpoint.startswith(('unix://', 'npipe://')):
            raise ValueError('A local Docker socket/context is required')
        self.lock = threading.Lock()
        self.ids, self.urls = {}, {}
        rows = self.rows()
        names = {self.service(row) for row in rows}
        if not REQUIRED <= names or names - REQUIRED - {'controller'} or len(names) != len(rows):
            raise ValueError('Expected the original seven-service project (optional controller is preserved)')
        for row in rows:
            name = self.service(row)
            labels = row['Config']['Labels']
            if not same_path(labels.get('com.docker.compose.project.working_dir', ''), ROOT):
                raise ValueError('Project belongs to a different repository checkout')
            sources = labels.get('com.docker.compose.project.config_files', '').split(',')
            if not any(same_path(source, ROOT/'compose.yaml') for source in sources):
                raise ValueError('Project must use this checkout root compose.yaml')
            if name not in REQUIRED:
                continue
            self.ids[name] = row['Id']
            if name in APIS:
                if MODULES[name] not in (row['Config'].get('Cmd') or []):
                    raise ValueError('Unexpected Cloud API command')
                self.urls[name] = self.url(row, '8000/tcp')
            if name == 'dashboard' and self.url(row, '8080/tcp') != 'http://127.0.0.1:8080':
                raise ValueError('Original dashboard must be published on loopback port 8080')

    @staticmethod
    def service(row):
        labels = row['Config'].get('Labels', {})
        if labels.get('com.docker.compose.project') != PROJECT:
            raise RuntimeError('Compose project identity mismatch')
        return labels.get('com.docker.compose.service')

    def rows(self):
        ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project='+PROJECT).splitlines()
        return json.loads(docker('inspect', *ids)) if ids else []

    @staticmethod
    def url(row, port):
        bindings = row['HostConfig'].get('PortBindings', {}).get(port, [])
        if len(bindings) != 1 or bindings[0]['HostIp'] != '127.0.0.1':
            raise ValueError('Cloud and dashboard ports require explicit loopback bindings')
        return 'http://127.0.0.1:'+str(int(bindings[0]['HostPort']))

    def validate(self):
        rows = {self.service(row): row for row in self.rows()}
        if {name: row['Id'] for name, row in rows.items() if name in REQUIRED} != self.ids:
            raise RuntimeError('Project containers changed; restart agent to authorize again')
        return rows

    def status(self):
        rows = self.validate()
        services = {}
        for name in APIS:
            try:
                healthy = httpx.get(self.urls[name]+'/health', timeout=1, trust_env=False).status_code == 200
            except httpx.HTTPError:
                healthy = False
            services[name] = {'healthy': healthy, 'running': rows[name]['State']['Running']}
        return {'project': PROJECT, 'services': services, 'busy': self.lock.locked()}

    def wait(self, desired, names=APIS):
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            status = self.status()
            if all(status['services'][name]['healthy'] == desired and
                   status['services'][name]['running'] == desired for name in names):
                return status
            time.sleep(.5)
        raise RuntimeError('API health verification timed out')

    def operate(self, action):
        if not self.lock.acquire(blocking=False):
            raise HTTPException(409, 'A Cloud operation is already running')
        try:
            self.validate()
            if action == 'outage':
                docker('stop', '--time', '10', *(self.ids[name] for name in APIS))
                return self.wait(False)
            if action != 'restore':
                raise ValueError('Unknown operation')
            docker('start', self.ids['patient'])
            self.wait(True, ('patient',))
            docker('start', self.ids['emergency'], self.ids['admin'])
            return self.wait(True)
        finally:
            self.lock.release()


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['outage', 'restore']


def create_app(control, token):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authorize(request: Request, call_next):
        try:
            local = ipaddress.ip_address(request.client.host).is_loopback
        except (ValueError, AttributeError):
            local = False
        origin = request.headers.get('origin')
        if not local or request.headers.get('host') != '127.0.0.1:8765' or origin not in ORIGINS:
            return JSONResponse({'detail': 'Local authorized dashboard access only'}, status_code=403)
        headers = {'Access-Control-Allow-Origin': origin, 'Vary': 'Origin', 'Cache-Control': 'no-store'}
        if request.method == 'OPTIONS':
            if request.headers.get('access-control-request-method') not in ('GET', 'POST') or any(
                h.strip().lower() not in ('authorization', 'content-type')
                for h in request.headers.get('access-control-request-headers', '').split(',') if h.strip()):
                return JSONResponse({'detail': 'Invalid preflight'}, status_code=403)
            return JSONResponse({}, headers={**headers, 'Access-Control-Allow-Methods': 'GET, POST',
                                            'Access-Control-Allow-Headers': 'Authorization, Content-Type'})
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer '+token).encode()):
            return JSONResponse({'detail': 'Authorization required'}, status_code=403, headers=headers)
        response = await call_next(request)
        response.headers.update(headers)
        return response

    @app.get('/status')
    def status():
        try:
            return control.status()
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            raise HTTPException(503, 'Cannot verify original project; check Docker Desktop or restart agent') from None

    @app.post('/operation')
    def operation(body: Operation):
        try:
            return control.operate(body.action)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
            raise HTTPException(503, 'Operation not confirmed; check actual API health before retrying') from None
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-local-demo', action='store_true', required=True)
    parser.parse_args()
    control = Control()
    token = secrets.token_urlsafe(32)
    print('Authorized original local project: '+PROJECT, flush=True)
    print('Temporary dashboard token: '+token, flush=True)
    print('Keep this terminal open. Restore Cloud before closing it.', flush=True)
    import uvicorn
    uvicorn.run(create_app(control, token), host='127.0.0.1', port=8765,
                proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
