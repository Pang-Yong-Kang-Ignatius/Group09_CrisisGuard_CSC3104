"""Pytest-only evidence collector. Never imported by production services.

Function entry != semantic verification. HTTP observations retain no bodies,
headers, credentials or query strings. Only passed tests support route evidence.
"""
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
PREFIXES = tuple(str(ROOT / p).casefold() + os.sep for p in
                 ('cloud', 'edge', 'dashboard', 'evaluation', 'infrastructure/scripts'))
CALLED = set()
REQUESTS = []
OUTCOMES = {}
CURRENT = None
ORIGINAL = None


def profile(frame, event, arg):
    if event == 'call':
        code = frame.f_code
        if code.co_filename.casefold().startswith(PREFIXES):
            CALLED.add((code.co_filename, code.co_firstlineno, code.co_name))


def pytest_configure(config):
    global ORIGINAL
    from starlette.testclient import TestClient
    ORIGINAL = TestClient.request

    def request(client, method, url, *args, **kwargs):
        response = ORIGINAL(client, method, url, *args, **kwargs)
        # Resolve against actual route order, including telemetry's nested route.
        scope = {'type': 'http', 'method': method.upper(), 'path': response.request.url.path,
                 'root_path': ''}
        from starlette.routing import Match
        for route in getattr(client.app, 'routes', []):
            match, _ = route.matches(scope)
            if match == Match.FULL:
                endpoint = getattr(route, 'endpoint', None)
                service_module = next((getattr(r.endpoint, '__module__', '')
                                       for r in client.app.routes if hasattr(r, 'endpoint')
                                       and getattr(r.endpoint, '__module__', '').startswith(('cloud.', 'edge.', 'dashboard.'))
                                       and getattr(r.endpoint, '__module__', '') != 'cloud.telemetry'),
                                      getattr(endpoint, '__module__', ''))
                REQUESTS.append({'test': CURRENT, 'module': getattr(endpoint, '__module__', ''),
                                 'service_module': service_module,
                                 'method': method.upper(), 'route': route.path,
                                 'status': response.status_code})
                break
        return response

    TestClient.request = request
    sys.setprofile(profile)
    threading.setprofile(profile)


def pytest_runtest_setup(item):
    global CURRENT
    CURRENT = item.nodeid


def pytest_runtest_logreport(report):
    OUTCOMES.setdefault(report.nodeid, {})[report.when] = report.outcome


def pytest_sessionfinish(session, exitstatus):
    sys.setprofile(None)
    threading.setprofile(None)
    target = os.environ.get('CG_TEST_PROBE_OUTPUT')
    if target:
        passed = {key for key, value in OUTCOMES.items()
                  if value.get('call') == 'passed' and all(v == 'passed' for v in value.values())}
        Path(target).write_text(json.dumps({
            'functions': [list(row) for row in sorted(CALLED)],
            'requests': [row for row in REQUESTS if row['test'] in passed],
            'outcomes': OUTCOMES,
        }, indent=2), encoding='utf-8')


def pytest_unconfigure(config):
    if ORIGINAL:
        from starlette.testclient import TestClient
        TestClient.request = ORIGINAL
    sys.setprofile(None)
    threading.setprofile(None)
