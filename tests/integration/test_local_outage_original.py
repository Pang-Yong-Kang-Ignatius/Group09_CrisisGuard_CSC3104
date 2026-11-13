"""Opt-in browser verification on the EXISTING original localhost:8080 stack.

RUN_ORIGINAL_OUTAGE_TEST=1 pytest this file. Stops/restores only its three APIs.
Creates no Docker project/container/image/volume and writes no synthetic events.
Requires Playwright and Edge on Windows (or installed Playwright Chromium).
"""
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from infrastructure.scripts.local_outage_agent import Control, APIS, create_app, docker


@pytest.mark.skipif(os.getenv('RUN_ORIGINAL_OUTAGE_TEST')!='1',reason='Explicit opt-in: controls original local APIs')
def test_existing_dashboard_outage_and_restore(tmp_path):
    browser_api=pytest.importorskip('playwright.sync_api')
    with socket.socket() as sock: sock.bind(('127.0.0.1',8765))
    control=Control()
    assert all(row['healthy'] for row in control.status()['services'].values()), 'Start with healthy original APIs'
    def snapshot():
        ids=docker('ps','-aq').splitlines()
        rows=json.loads(docker('inspect',*ids)) if ids else []
        return {row['Id']:{'state':{key:row['State'][key] for key in ('Running','Pid','StartedAt','FinishedAt','ExitCode')},
                           'mounts':row['Mounts'],'restarts':row['RestartCount']} for row in rows}
    original=snapshot()
    volumes=docker('volume','ls','-q')
    images=set(docker('image','ls','--no-trunc','--format','{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())
    token=secrets.token_urlsafe(32)
    server=uvicorn.Server(uvicorn.Config(create_app(control,token),host='127.0.0.1',port=8765,
                                        proxy_headers=False,access_log=False,log_level='warning'))
    thread=threading.Thread(target=server.run,daemon=True)
    browser=None
    attempted=False
    try:
        thread.start()
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.1)
        assert server.started
        for headers in ({},{'Origin':'http://localhost:8080','Authorization':'Bearer invalid'},
                        {'Origin':'http://evil.example','Authorization':'Bearer '+token}):
            assert httpx.post('http://127.0.0.1:8765/operation',json={'action':'outage'},headers=headers).status_code==403
        with browser_api.sync_playwright() as playwright:
            edge=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
            browser=playwright.chromium.launch(headless=True,**({'executable_path':str(edge)} if edge.exists() else {}))
            page=browser.new_page(viewport={'width':1440,'height':1050})
            page.goto('http://localhost:8080/#recovery')
            assert page.locator('#outage-stop').is_disabled()
            page.locator('#outage-token').fill(token)
            page.locator('#outage-connect').click()
            page.wait_for_function("document.getElementById('outage-message').textContent.includes('Authorized original project:')")
            assert page.locator('#outage-token').input_value()==''
            stop=page.get_by_role('button',name='Simulate Cloud Outage')
            restore=page.get_by_role('button',name='Restore Cloud',exact=True)
            attempted=True
            for _ in range(2):
                stop.click()
                assert stop.is_disabled() and restore.is_disabled()
                page.wait_for_function("document.getElementById('outage-message').textContent.includes('Cloud outage confirmed')",timeout=120000)
                page.wait_for_function("!document.getElementById('outage-restore').disabled")
                assert all(not row['running'] and not row['healthy'] for row in control.status()['services'].values())
                queues=httpx.get('http://localhost:8080/api/hospitals',timeout=30).json()
                assert all(row['status']=='healthy' for row in queues.values())
                assert control.validate()['postgres']['State']['Running']
                page.screenshot(path=str(tmp_path/'original-outage.png'))
                restore.click()
                assert stop.is_disabled() and restore.is_disabled()
                page.wait_for_function("document.getElementById('outage-message').textContent.includes('Cloud health confirmed')",timeout=240000)
                page.wait_for_function("!document.getElementById('outage-stop').disabled")
                assert all(row['running'] and row['healthy'] for row in control.status()['services'].values())
            for origin in ('http://localhost:8080','http://127.0.0.1:8080'):
                page.goto(origin)
                assert page.locator('#edge').count()==1 and page.locator('#history').count()==1
                assert page.locator('#outage-stop').count()==1
            page.screenshot(path=str(tmp_path/'original-restored.png'))
            browser.close();browser=None
    finally:
        if attempted: control.operate('restore')  # Never leave original Cloud APIs stopped on a test failure.
        if browser: browser.close()
        server.should_exit=True
        if thread.is_alive(): thread.join(timeout=10)
        current=snapshot()
        targets={control.ids[name] for name in APIS}
        assert set(original)==set(current)
        assert all(current[key]==row for key,row in original.items() if key not in targets)
        assert all(current[key]['mounts']==row['mounts'] for key,row in original.items())
        assert docker('volume','ls','-q')==volumes
        # Docker image listing order is not stable; tag-to-ID identity must be unchanged.
        assert set(docker('image','ls','--no-trunc','--format','{{.Repository}}:{{.Tag}} {{.ID}}').splitlines())==images
