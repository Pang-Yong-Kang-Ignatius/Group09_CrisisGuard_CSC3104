"""Safe project-wide verification orchestration; never imports service entrypoints.

Only an allowlist of mocked integrations is collected by default. PostgreSQL
requires a labeled, mount-free scratch container and matching server identity.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PRODUCTION = ('cloud', 'edge', 'dashboard', 'evaluation', 'infrastructure/scripts')
MOCKED = ('tests/integration/test_cloud_apis.py', 'tests/integration/test_manual_replay_security.py')
POSTGRES = ('tests/integration/test_controller_postgres.py',
            'tests/integration/test_project_e2e_postgres.py',
            'tests/integration/test_cp8_postgres.py',
            'tests/integration/test_admission_deferral_postgres.py')
EVALUATION_DOCUMENTS = (
    'docs/EVIDENCE_INDEX.md', 'docs/reports/CORE_REQUIREMENTS_AUDIT.md',
    'docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md',
    'docs/reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md',
    'docs/reports/H5_W5_REVIEW_20261103.md',
)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding='utf-8')


def run(command, *, env=None, timeout=300, log=None):
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if log:
        log.write_text(result.stdout + result.stderr, encoding='utf-8')
    return result


def repository_revision():
    """Return an owned Git revision, or None for a downloaded source archive."""
    if not (ROOT / '.git').exists():
        return None
    try:
        top = run(['git', 'rev-parse', '--show-toplevel'])
        head = run(['git', 'rev-parse', 'HEAD'])
        if (top.returncode == 0 and Path(top.stdout.strip()).resolve() == ROOT.resolve()
                and head.returncode == 0 and re.fullmatch(r'[0-9a-fA-F]{40,64}', head.stdout.strip())):
            return head.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def missing_evaluation_documents():
    return [path for path in EVALUATION_DOCUMENTS if not (ROOT / path).is_file()]


def junit_counts(path):
    if not path.exists():
        return None
    cases = list(ET.parse(path).getroot().iter('testcase'))
    failed = sum(c.find('failure') is not None or c.find('error') is not None for c in cases)
    skipped = sum(c.find('skipped') is not None for c in cases)
    return {'total': len(cases), 'passed': len(cases) - failed - skipped,
            'failed': failed, 'skipped': skipped}


def subset_status(path, fragments):
    """Report an overlapping selected suite only when it actually executed."""
    if not path.exists():
        return 'NOT VERIFIED'
    cases = [c for c in ET.parse(path).getroot().iter('testcase')
             if any(token in c.get('classname', '') for token in fragments)]
    if not cases:
        return 'NOT VERIFIED'
    if any(c.find('failure') is not None or c.find('error') is not None for c in cases):
        return 'FAIL'
    return 'PASS' if any(c.find('skipped') is None for c in cases) else 'SKIP'


def suite_status(code, counts):
    if code or not counts or counts['failed']:
        return 'FAIL'
    return 'PASS' if counts['passed'] else 'SKIP'


def test_environment(directory):
    env = os.environ.copy()
    # Do not allow inherited deployment settings or auto-loaded plugins to select
    # live services. Individual tests explicitly override their synthetic settings.
    prefixes = ('CONTROLLER_', 'CLOUD_', 'METAGUARD_', 'SCHEDULER_', 'AUTO_REPLAY',
                'HOSPITAL_', 'DASHBOARD_', 'MANUAL_REPLAY', 'ENABLE_MANUAL_REPLAY',
                'CRISISGUARD_', 'PYTEST_', 'COVERAGE_', 'CG_TEST_', 'NODE_', 'AWS_',
                'POSTGRES_', 'PG')
    for key in list(env):
        if key.startswith(prefixes) or key in ('DATABASE_URL', 'SITE_ID', 'EMERGENCY_URL', 'ADMIN_URL'):
            del env[key]
    env.update(DATABASE_URL='postgresql://blocked:blocked@127.0.0.1:1/verification_disabled',
               HOSPITAL_DB_PATH=str(directory / 'hospital.sqlite3'),
               CLOUD_URL='http://127.0.0.1:1/events', EMERGENCY_URL='http://127.0.0.1:1/events',
               ADMIN_URL='http://127.0.0.1:1/events', AUTO_REPLAY='0',
               AWS_EC2_METADATA_DISABLED='true', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
               PYTHONPATH=str(ROOT), PYTHONUTF8='1')
    return env


def source_inventory():
    files = sorted({p for folder in PRODUCTION for p in (ROOT / folder).rglob('*.py')
                    if not {'__pycache__', 'results', 'generated', 'archive', 'tests'} & set(p.parts)})
    inventory, routes = [], []
    for file in files:
        relative = file.relative_to(ROOT).as_posix()
        tree = ast.parse(file.read_text(encoding='utf-8-sig'))
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                functions.append({'name': node.name, 'line': node.lineno, 'first_line': first,
                                  'end_line': node.end_lineno})
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and dec.func.attr in ('get', 'post', 'put', 'patch', 'delete', 'options', 'head')
                            and dec.args and isinstance(dec.args[0], ast.Constant)):
                        routes.append({'service': relative, 'method': dec.func.attr.upper(),
                                       'route': dec.args[0].value, 'handler': node.name,
                                       'line': node.lineno})
        inventory.append({'file': relative, 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                          'functions': functions})
    # setup_telemetry registers this nested route for all three Cloud services.
    telemetry_routes = [r for r in routes if r['service'] == 'cloud/telemetry.py']
    routes = [({**r, 'service': 'dashboard/main.py', 'registration_source': r['service']}
               if r['service'] == 'dashboard/test_traffic.py' else r)
              for r in routes if r['service'] != 'cloud/telemetry.py']
    for service in ('patient', 'emergency', 'admin'):
        routes.extend({**r, 'service': f'cloud/{service}_service/main.py',
                       'registration_source': 'cloud/telemetry.py'} for r in telemetry_routes)
    return inventory, routes


def route_matrix(routes, requests):
    result = []
    for route in routes:
        module = route['service'][:-3].replace('/', '.')
        evidence = [r for r in requests if r['method'] == route['method'] and r['route'] == route['route']
                    and (r.get('service_module') or r['module']) == module]
        # Status observations demonstrate only an exercised response class, not
        # assertion completeness, credential variants or concurrent behavior.
        codes = sorted({r['status'] for r in evidence})
        scenarios = []
        for label, predicate in [('success', lambda c: 200 <= c < 300),
                                 ('invalid input', lambda c: c in (400, 422)),
                                 ('missing resource', lambda c: c == 404),
                                 ('unauthorized', lambda c: c == 401),
                                 ('forbidden', lambda c: c == 403),
                                 ('conflict', lambda c: c == 409),
                                 ('dependency failure', lambda c: c in (502, 503, 504)),
                                 ('admission denial', lambda c: c == 429)]:
            if any(predicate(c) for c in codes):
                scenarios.append(label)
        path = route['route']
        relevant = relevant_scenarios(route)
        missing = [s for s in relevant if s not in scenarios]
        if route['method'] == 'POST':
            missing.append('concurrency (not inferred from HTTP status)')
        result.append({**route, 'test_references': sorted({r['test'] for r in evidence}),
                       'observed_statuses': codes, 'covered_scenarios': scenarios,
                       'missing_scenarios': missing, 'status': 'NOT VERIFIED',
                       'scenario_relevance': 'Conservative review candidates; no assertion of exhaustive contract coverage'})
    return result


def relevant_scenarios(route):
    """Source-reviewed contracts, rather than demanding auth on public reads."""
    source, path, method = route['service'], route['route'], route['method']
    if '_service/' in source:
        if path == '/events' and method == 'POST':
            return ['success', 'invalid input', 'unauthorized', 'forbidden', 'conflict', 'dependency failure']
        if path == '/events/{event_id}':
            return ['success', 'missing resource', 'dependency failure']
        # Metrics DB failure is a 200 with database_healthy=false, not a 503.
        return ['success'] if path in ('/', '/metrics') else ['success', 'dependency failure']
    if source == 'edge/capture_api/main.py':
        return {'/': ['success'], '/events': ['success'], '/recovery/status': ['success'],
                '/scheduler/inspect': ['success', 'invalid input'],
                '/capture': ['success', 'invalid input', 'forbidden', 'conflict'],
                '/replay': ['success', 'forbidden', 'missing resource', 'dependency failure']}[path]
    if source == 'dashboard/main.py':
        relevant = ['success'] if path == '/health' else ['success', 'forbidden']
        if method == 'POST' or path.endswith('/scheduler/inspect') or path == '/api/history':
            relevant.append('invalid input')
        if '{' in path or path.endswith('/replay'):
            relevant.append('missing resource')
        if path not in ('/', '/health', '/api/status-codes'):
            relevant.append('dependency failure')
        # Upstream conflict codes are embedded in a response envelope, not the
        # dashboard route status; they require separate assertion review.
        return relevant
    if source == 'cloud/controller/main.py':
        if path == '/health':
            return ['success']
        relevant = ['success', 'unauthorized', 'forbidden', 'dependency failure']
        if '{' in path or method == 'POST':
            relevant.append('invalid input')
        if 'grant_id' in path or path.startswith('/grants/{hospital_id}') or path.endswith('/grant'):
            relevant.append('missing resource')
        if 'grant_id' in path and method == 'POST':
            relevant.append('conflict')
        if path.endswith(('/next', '/acknowledge')):
            relevant.append('admission denial')
        return relevant
    # Loopback outage-agent endpoints have their own credential/origin contract.
    return ['success', 'invalid input', 'unauthorized', 'forbidden', 'dependency failure']


def postgres_target(container):
    """Read-only isolation proof BEFORE pytest can create any schema.

    A loopback URL/name alone is never accepted. Require explicit scratch intent,
    no persistent mounts, no existing user objects, and matching server system ID.
    Never serialize connection strings, Docker environment or credentials.
    """
    if not container:
        raise ValueError('CG_TEST_POSTGRES_CONTAINER is unset; no isolation proof')
    if not re.fullmatch(r'[a-fA-F0-9]{12,64}', container):
        raise ValueError('Use an explicit Docker container ID, not a name')
    require_local_docker()
    checked = run(['docker', 'inspect', container], timeout=20)
    if checked.returncode:
        raise ValueError('Scratch container is unavailable')
    data = json.loads(checked.stdout)[0]
    label = data['Config'].get('Labels') or {}
    nonce = label.get('org.crisisguard.test-isolation', '')
    try:
        uuid.UUID(nonce)
    except ValueError:
        raise ValueError('Missing UUID scratch-isolation label') from None
    if any(m.get('Type') != 'tmpfs' for m in data.get('Mounts', [])) or not data['State']['Running']:
        raise ValueError('Scratch container must be running without persistent mounts (use PostgreSQL tmpfs)')
    ports = data['NetworkSettings']['Ports'].get('5432/tcp') or []
    local = [p for p in ports if p['HostIp'] == '127.0.0.1']
    if len(local) != 1:
        raise ValueError('Scratch PostgreSQL must have one explicit IPv4 loopback port')
    settings = dict(v.split('=', 1) for v in data['Config'].get('Env', []) if '=' in v)
    user = settings.get('POSTGRES_USER', 'postgres')
    database = settings.get('POSTGRES_DB', user)
    password = settings.get('POSTGRES_PASSWORD')
    if not password:
        raise ValueError('Scratch PostgreSQL must use password authentication')
    import psycopg
    from psycopg.conninfo import make_conninfo
    url = make_conninfo(host='127.0.0.1', port=local[0]['HostPort'], dbname=database,
                        user=user, password=password, connect_timeout=3)
    query = 'SELECT system_identifier::text FROM pg_control_system()'
    internal = run(['docker', 'exec', container, 'psql', '-U', user, '-d', database, '-Atc', query], timeout=20)
    if internal.returncode:
        raise ValueError('Cannot establish scratch server identity')
    with psycopg.connect(url, options='-c default_transaction_read_only=on') as conn:
        identity = conn.execute(query).fetchone()[0]
        if identity != internal.stdout.strip():
            raise ValueError('Database endpoint does not match scratch container identity')
        objects = conn.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'").fetchone()[0]
        schemas = conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('pg_catalog','information_schema','public') AND nspname NOT LIKE 'pg_%'").fetchone()[0]
        if objects or schemas:
            raise ValueError('Scratch database already has user objects; refusing mutation')
    return url, {'container_id': data['Id'], 'nonce': nonce, 'system_identifier': identity,
                 'persistent_mounts': 0, 'existing_user_objects': 0}


def require_local_docker():
    host = os.environ.get('DOCKER_HOST')
    if not host:
        context = run(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], timeout=20)
        if context.returncode:
            raise ValueError('Cannot establish local Docker endpoint')
        host = context.stdout.strip()
    if not host.startswith(('npipe://', 'unix://')):
        raise ValueError('Remote Docker endpoints are not allowed')


def docker_checks():
    checks = []
    if not shutil.which('docker'):
        return [{'check': 'Docker availability', 'status': 'SKIP'}]
    try:
        require_local_docker()
    except ValueError:
        return [{'check': 'Local Docker endpoint', 'status': 'NOT VERIFIED'}]
    compose = run(['docker', 'compose', '--env-file', '.env.example', '-f', 'compose.yaml',
                   '--profile', 'metaguard', 'config', '--quiet'], timeout=30)
    checks.append({'check': 'Compose explicit base configuration', 'status': 'PASS' if not compose.returncode else 'FAIL'})
    containers = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=crisisguard-local'], timeout=20)
    if containers.returncode or not containers.stdout.strip():
        checks.append({'check': 'Local eight-service deployment', 'status': 'NOT VERIFIED'})
        return checks
    inspected = run(['docker', 'inspect', *containers.stdout.split()], timeout=20)
    if inspected.returncode:
        return checks + [{'check': 'Container inspection', 'status': 'FAIL'}]
    by_service = {}
    for item in json.loads(inspected.stdout):
        service = item['Config']['Labels'].get('com.docker.compose.service')
        by_service[service] = item
    import urllib.request
    for service in ('postgres', 'patient', 'emergency', 'admin', 'hospital-a', 'hospital-b', 'controller', 'dashboard'):
        item = by_service.get(service)
        if not item:
            checks.append({'check': service, 'status': 'NOT VERIFIED'})
            continue
        state = item['State']
        health = state.get('Health', {}).get('Status')
        checks.append({'check': service + ' container health', 'status': 'PASS' if state['Running'] and health == 'healthy' else 'FAIL' if health == 'unhealthy' else 'NOT VERIFIED'})
        if service == 'postgres':
            # The existing container's pg_isready healthcheck is read-only evidence.
            continue
        port_key = '8080/tcp' if service == 'dashboard' else '8000/tcp'
        ports = item['NetworkSettings']['Ports'].get(port_key) or []
        local = next((p for p in ports if p['HostIp'] in ('127.0.0.1', '0.0.0.0')), None)
        if not local or not state['Running']:
            checks.append({'check': service + ' HTTP readiness', 'status': 'NOT VERIFIED'})
            continue
        path = '/' if service.startswith('hospital') else '/health'
        try:
            # No remote hosts, redirects, credentials, POSTs or response bodies.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            with opener.open(f"http://127.0.0.1:{int(local['HostPort'])}{path}", timeout=3) as response:
                status = 'PASS' if response.status == 200 else 'FAIL'
        except Exception:
            status = 'FAIL'
        checks.append({'check': service + ' HTTP readiness', 'status': status})
    return checks


def javascript_coverage(directory):
    modules = []
    scripts = []
    for raw in (directory / 'v8').glob('*.json'):
        scripts.extend(json.loads(raw.read_text(encoding='utf-8'))['result'])
    for path in sorted((ROOT / 'dashboard/static').glob('*.js')):
        matches = [s for s in scripts if s['url'].replace('file:///', '').replace('\\', '/').casefold() == path.as_posix().casefold()]
        blocks = {}
        for script in matches:
            for function in script['functions']:
                for block in function['ranges']:
                    key = (block['startOffset'], block['endOffset'])
                    blocks[key] = blocks.get(key, False) or block['count'] > 0
        modules.append({'file': path.relative_to(ROOT).as_posix(), 'mapped_complete_module': bool(matches),
                        'observed_ranges': len(blocks), 'executed_ranges': sum(blocks.values()),
                        'range_coverage_percent': 100 * sum(blocks.values()) / len(blocks) if blocks else None,
                        'limitation': 'V8 nested ranges, not browser/line/branch coverage; slices and transformed scripts remain unattributed'})
    write_json(directory / 'javascript-coverage.json', modules)
    return modules


def generate_audit(directory, summary):
    from infrastructure.scripts.project_audit_catalog import COMPONENTS, RECOVERY, PROPOSAL_STATUSES
    inventory, routes = source_inventory()
    probe = json.loads((directory / 'probe.json').read_text(encoding='utf-8')) if (directory / 'probe.json').exists() else {}
    called = {(Path(f).resolve().relative_to(ROOT).as_posix(), line, name) for f, line, name in probe.get('functions', [])}
    for module in inventory:
        for function in module['functions']:
            function['exercised'] = (module['file'], function['first_line'], function['name']) in called
    matrix = route_matrix(routes, probe.get('requests', []))
    write_json(directory / 'source-inventory.json', inventory)
    write_json(directory / 'api-routes.json', matrix)
    extensions = {'.py', '.cjs', '.js', '.css', '.html', '.yaml', '.yml', '.ps1', '.bat', '.sh', '.txt'}
    tracked = run(['git', 'ls-files', '-z']).stdout.split('\0')
    additional = [str(p.relative_to(ROOT)) for parent in ('infrastructure/scripts', 'tests/unit')
                  for p in (ROOT / parent).glob('*') if p.is_file()]
    hashes = {}
    for name in sorted(set(tracked + additional + ['test.project.ps1', '.coveragerc', 'requirements-test.txt'])):
        path = ROOT / name
        if path.is_file() and (path.suffix in extensions or name == '.coveragerc'):
            hashes[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(directory / 'source-and-test-hashes.json', hashes)
    js = javascript_coverage(directory)
    passed_files = {key.split('::')[0] for key, result in probe.get('outcomes', {}).items()
                    if result.get('call') == 'passed' and all(v == 'passed' for v in result.values())}
    report = ['# CrisisGuard project testing and coverage audit', '',
              f"Generated UTC: {summary['created_utc']}. Source revision: `{summary['revision'] or 'NOT VERIFIED (Git metadata unavailable)'}`.",
              'Working-tree source hashes, Git status and detailed results are retained in the unique local run.',
              f"Results directory: `{directory.relative_to(ROOT).as_posix()}` (ignored; never historical evidence).", '',
              f"Environment: Python `{summary['python']}`; Node `{summary['node']}`; `{sys.platform}`.",
              'Only Python 3.11 is supported by the runner. No dependencies were installed.', '',
              '| Check | Status |', '|---|---|']
    report += [f'| {key} | {value} |' for key, value in summary['checks'].items()]
    report += ['', 'PASS means that an executed check succeeded. SKIP is never a pass. NOT VERIFIED means insufficient evidence.',
               'A passing regression suite does not establish proposal acceptance or full component correctness.', '',
               '| Suite | Total | Passed | Failed | Skipped |', '|---|---:|---:|---:|---:|']
    for name, counts in summary['tests'].items():
        if counts:
            report.append(f"| {name} | {counts['total']} | {counts['passed']} | {counts['failed']} | {counts['skipped']} |")
    if summary['details'].get('Docker health'):
        report += ['', 'Read-only Docker subchecks:', '']
        report += [f"- {item['check']}: {item['status']}" for item in summary['details']['Docker health']]
    if summary['details'].get('PostgreSQL integration'):
        report += ['', str(summary['details']['PostgreSQL integration'])]
    report += ['', 'Python counts are pytest cases, including parametrization. Node counts include one legacy queue script as a file-level test; internal assertions are not counted as individual cases.', '',
               '## Python line, branch and function evidence', '']
    coverage = directory / 'coverage.json'
    if coverage.exists():
        totals = json.loads(coverage.read_text(encoding='utf-8'))['totals']
        lines = totals['num_statements']
        branches = totals.get('num_branches', 0)
        report += [f"Line coverage: {totals['covered_lines']}/{lines} ({100 * totals['covered_lines'] / lines if lines else 0:.2f}%).",
                   f"Branch coverage: {totals.get('covered_branches', 0)}/{branches} ({100 * totals.get('covered_branches', 0) / branches if branches else 0:.2f}%).",
                   'See coverage.json, coverage.xml, coverage.txt and htmlcov/ for missing lines and branches.']
    else:
        report += ['Line coverage: **NOT VERIFIED**. Branch coverage: **NOT VERIFIED**.',
                   '`coverage.py` is unavailable or its execution failed; no percentages are inferred from test counts or function entries.',
                   'To enable on a later run, explicitly install requirements-test.txt into Python 3.11. The runner never installs it.']
    functions = [f for module in inventory for f in module['functions']]
    report += [f"Function entries: {sum(f['exercised'] for f in functions)}/{len(functions)} discovered Python function definitions observed by a pytest profiling hook (collection/setup/call/teardown and new threads).",
               'Function entries include failing tests and setup; an entry does not prove correct return values, all branches, subprocess execution or semantic verification.', '',
               '| Production module | Functions observed / discovered | Unexercised functions (first 12; complete inventory in JSON) |',
               '|---|---:|---|']
    for module in inventory:
        funcs = module['functions']
        missing = [f"{f['name']}:{f['line']}" for f in funcs if not f['exercised']]
        report.append(f"| `{module['file']}` | {sum(f['exercised'] for f in funcs)}/{len(funcs)} | {', '.join(missing[:12]) or 'None observed'}{' …' if len(missing) > 12 else ''} |")
    report += ['', '## JavaScript coverage', '',
               'Native NODE_V8_COVERAGE raw files are retained. The preload names only complete byte-identical VM modules.',
               'Most existing harnesses slice/prepend JavaScript without source offsets; those anonymous paths cannot be accurately mapped to production files. No project-wide percentage or browser coverage is claimed.', '',
               '| Dashboard module | Measured ranges | Executed ranges | Status |', '|---|---:|---:|---|']
    report += [f"| `{m['file']}` | {m['observed_ranges']} | {m['executed_ranges']} | {'NOT VERIFIED (partial V8 ranges only)' if m['mapped_complete_module'] else 'NOT VERIFIED (unmapped VM/browser paths)'} |" for m in js]
    report += ['', 'Navigation, metrics, capture, history, queue/recovery, Inspector, test traffic, errors and loading/disabled states have DOM-model tests. Actual browser navigation, accessibility, rendering and concurrency remain NOT VERIFIED.', '',
               '## Component coverage matrix', '', '| Component | Source | Executed test references | Status / remaining scope |', '|---|---|---|---|']
    for component, source, patterns, limitation in COMPONENTS:
        refs = sorted(p for p in passed_files if any(token in p for token in patterns.split(',')))
        js_refs = [p.relative_to(ROOT).as_posix() for p in (ROOT / 'tests/unit').glob('*.cjs') if any(token in p.name for token in patterns.split(','))]
        if summary['checks']['JavaScript unit tests'] == 'PASS':
            refs += js_refs
        report.append(f"| {component} | `{source}` | {'; '.join(refs) or 'No passed reference established'} | NOT VERIFIED ({'partial test evidence' if refs else 'no passed reference'}): {limitation} |")
    report += ['', 'Source discovery additionally inventories all production Python modules above, six dashboard scripts, AWS launcher/shell files, Compose variants, batch scripts, static HTML/CSS and GitHub workflow configuration. AWS launcher Python is exercised by existing tests but is outside the requested coverage.py roots; shell/YAML/batch/HTML/CSS are not Python coverage denominators.', '',
               '## API route coverage matrix', '',
               'Routes are discovered via AST without importing services; nested telemetry routes are expanded for all three Cloud services. Framework OpenAPI/docs/static mounts are excluded. Observations are actual TestClient responses from passed tests, not inferred from test names.',
               'Response classes are evidence of execution, not proof of complete scenario assertions. Candidate missing scenarios need contract review; concurrency is never inferred from HTTP status.', '',
               '| Service | Method | Route | Passed test references (first 3; full JSON retained) | Observed scenarios | Missing / review candidates | Status |',
               '|---|---|---|---|---|---|---|']
    for route in matrix:
        report.append(f"| `{route['service']}` | {route['method']} | `{route['route']}` | {'; '.join(route['test_references'][:3]) or 'None'} | {', '.join(route['covered_scenarios']) or 'None'} | {', '.join(route['missing_scenarios']) or 'Assertion completeness review'} | {route['status']} |")
    report += ['', '## Proposal requirement coverage matrix', '',
               'Original GROUP09 proposal PDF and appendices are **not available in this checkout**. Requirement text below is the retained R01–R24 transcription in docs/reports/CORE_REQUIREMENTS_AUDIT.md, not a fresh verification against the original PDF. Every transcribed requirement is retained; complete authoritative proposal coverage remains NOT VERIFIED.', '',
               '| Requirement (retained transcription) | Classification | Current interpretation / evidence |', '|---|---|---|']
    text = (ROOT / 'docs/reports/CORE_REQUIREMENTS_AUDIT.md').read_text(encoding='utf-8')
    for line in text.splitlines():
        if not re.match(r'\| R\d\d:', line):
            continue
        fields = line.split('|')
        identifier = fields[1].strip()[:3]
        status, note = PROPOSAL_STATUSES.get(identifier, ('Implemented but partially verified', 'Unit evidence for selected invariants; full distributed scope remains open'))
        report.append(f"| {fields[1].strip()} | {status} | {note}; source/tests: {fields[3].strip()} / {fields[4].strip()} |")
    report += [
        '| Original AIMD recovery/routine regression guardrail | Performance target failed | docs/archive/evaluation/CP8_SUBMISSION_EVALUATION_20261101.md: reduced H1/H2 drain +278.52% / +276.34%; original findings preserved |',
        '| Live SLO <=+1 percentage point in reduced H1/H2 | Performance target achieved | Only the documented reduced study; docs/reports/AIMD_RECOVERY_DIAGNOSIS_20261101.md explains startup/exposure limitations |',
        '| Urgent p95 >=10% gain against RR | Performance target not established | No qualifying RR/skewed comparison |',
        '| Full D2 matrix / final D3 acceptance | Performance target not established | Full D2 incomplete; tests cannot substitute for experiments |',
        '| Experimental pressure-health / bounded-deferral pilots | Implemented but partially verified | Improvements in some measurements; opt-in, separate pilot scope, no acceptance promotion |',
        '| R02 appendix dedicated schema-version, enforced workflow-ID and payload-hash columns | Not implemented | Metadata may be supplied in payload; grant binding uses a digest. This differs from a dedicated persisted schema field contract |',
        '', 'Current evidence index: docs/EVIDENCE_INDEX.md. Reduced H1/H2 and historical scheduling/cost evidence remain valid within their documented methods, provenance, exclusions and uncertainty. No historical file was overwritten.', '',
        '## Distributed recovery evidence', '',
        '| Scenario | Unit / mocked evidence | Historical Docker / AWS reference under docs/evidence | Fresh evidence limitation |', '|---|---|---|---|']
    report += [f'| {scenario} | {tests} | {evidence} | {gap}; isolated integration only if explicitly recorded in this run |' for scenario, tests, evidence, gap in RECOVERY]
    report += ['', '## Important gaps and limitations', '',
               '- Authentication and replay regressions cannot establish full SQL fencing/duplicate-domain effects without real isolated transactions.',
               '- Durable storage, advisory-lock ordering, concurrent grants, lost ACK/commit reconciliation, DRR rollback and restart behavior need isolated PostgreSQL evidence.',
               '- Missing branches in infrastructure smoke/update/backup and live evaluation runners cannot safely be exercised by executing those entrypoints automatically.',
               '- No browser-level keyboard, screen-reader, responsive-layout, console, real-network or hidden-tab verification occurred.',
               '- No fresh Docker startup, AWS, outage injection, performance runs or final hosted CI. Docker opt-in reports read-only observations only.',
               '- API status observations do not establish every invalid-credential variant, secret non-exposure or true concurrent behavior; focused assertions remain in source tests.',
               '- Metrics database failures return HTTP 200 with an unhealthy flag; dashboard proxy failures/conflicts can be embedded in envelopes. Status-only route evidence does not establish these semantics.',
               '- Profiling does not measure subprocess functions. Missing line/branch instrumentation is never replaced by fabricated percentages.', '',
               '## Reproduction', '',
               'Run `.\\test.project.ps1 -Quick`, then `.\\test.project.ps1 -Full`. Optional `-WithDocker` performs read-only checks. Optional `-WithIntegration` requires the explicit isolated PostgreSQL proof described in docs/testing.md.',
               'Full/default runs generate unique machine-readable summary/JUnit, function inventory, route matrix, raw V8 and optional coverage.py JSON/XML/HTML. This report is a current audit snapshot; historical evidence is preserved.', '']
    (directory / 'PROJECT_TEST_COVERAGE.md').write_text('\n'.join(report), encoding='utf-8')
    shutil.copyfile(directory / 'PROJECT_TEST_COVERAGE.md', ROOT / 'docs/evidence/PROJECT_TEST_COVERAGE.md')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--with-docker', action='store_true')
    parser.add_argument('--with-integration', action='store_true')
    parser.add_argument('--end-to-end', action='store_true')
    parser.add_argument('--e2e-resume')
    parser.add_argument('--confirm-isolation')
    parser.add_argument('--e2e-cleanup-only', action='store_true')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 11):
        parser.error('Python 3.11 is required')
    if args.quick and (args.full or args.end_to_end):
        parser.error('Quick cannot be combined with Full or EndToEnd')
    if (args.e2e_resume or args.confirm_isolation or args.e2e_cleanup_only) and not args.end_to_end:
        parser.error('E2E continuation options require --end-to-end')
    if (args.confirm_isolation or args.e2e_cleanup_only) and not args.e2e_resume:
        parser.error('Confirmation/cleanup requires --e2e-resume for a reviewed stack')
    if args.end_to_end and not args.full:
        from infrastructure.scripts.project_e2e import run_e2e
        return run_e2e(resume=args.e2e_resume, confirmation=args.confirm_isolation,
                       cleanup_only=args.e2e_cleanup_only)[0]
    os.chdir(ROOT)
    directory = ROOT / '.project-test-results' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    env = test_environment(directory)
    revision = repository_revision()
    summary = {'created_utc': datetime.now(timezone.utc).isoformat(),
               'revision': revision,
               'python': sys.version.split()[0], 'node': None, 'mode': 'Quick' if args.quick else 'Full',
               'checks': {}, 'tests': {}, 'details': {}}
    checks = summary['checks']
    checks['Source revision'] = 'PASS' if revision else 'NOT VERIFIED'
    (directory / 'git-status.txt').write_text(
        run(['git', 'status', '--short']).stdout if revision else
        'NOT VERIFIED: Git metadata unavailable for this source tree.\n', encoding='utf-8')
    print('CRISISGUARD PROJECT VERIFICATION', flush=True)

    def check(name, action):
        print(f'Running {name}...', flush=True)
        try:
            return action()
        except Exception as exc:
            # Exception strings can contain credentials; retain only the class.
            checks[name] = 'FAIL'
            summary['details'][name] = {'error_type': type(exc).__name__}
            return None

    def python_tests():
        covered = not args.quick and importlib.util.find_spec('coverage') is not None
        command = [sys.executable]
        if covered:
            command += ['-m', 'coverage', 'run', '--rcfile=.coveragerc', '-m']
            env['COVERAGE_FILE'] = str(directory / '.coverage')
            env['COVERAGE_RCFILE'] = str(ROOT / '.coveragerc')
        else:
            command += ['-m']
        command += ['pytest', 'tests/unit', *MOCKED, '-q', '-p', 'no:cacheprovider',
                    '--basetemp=' + str(directory / 'pytest'), '--junitxml=' + str(directory / 'python.xml')]
        if not args.quick:
            command += ['-p', 'infrastructure.scripts.project_test_probe']
            env['CG_TEST_PROBE_OUTPUT'] = str(directory / 'probe.json')
        result = run(command, env=env, timeout=600, log=directory / 'python.log')
        counts = junit_counts(directory / 'python.xml')
        summary['tests']['Python safe regression'] = counts
        checks['Python unit tests'] = suite_status(result.returncode, counts)
        # These are overlapping portions of the same suite, not additional cases.
        checks['API contract checks'] = subset_status(directory / 'python.xml', ('test_cloud_apis', 'test_controller_api', 'test_scheduler_inspector_api'))
        checks['Security regression'] = subset_status(directory / 'python.xml', ('test_metaguard_reliability_security', 'test_project_security_gaps', 'test_manual_replay_security', 'test_dashboard_replay_authorization'))
        checks['Python coverage'] = 'SKIP' if args.quick else 'NOT VERIFIED'
        if covered:
            results = [run([sys.executable, '-m', 'coverage', fmt, '--rcfile=.coveragerc', *options], env=env,
                           log=directory / ('coverage-' + fmt + '.log')) for fmt, options in (
                ('json', ['-o', str(directory / 'coverage.json')]),
                ('xml', ['-o', str(directory / 'coverage.xml')]),
                ('html', ['-d', str(directory / 'htmlcov')]), ('report', []))]
            (directory / 'coverage.txt').write_text(results[-1].stdout, encoding='utf-8')
            checks['Python coverage'] = 'PASS' if all(r.returncode == 0 for r in results) else 'FAIL'
    check('Python unit tests', python_tests)
    checks.setdefault('Python coverage', 'NOT VERIFIED')
    checks.setdefault('API contract checks', 'NOT VERIFIED')
    checks.setdefault('Security regression', 'NOT VERIFIED')

    def node_tests():
        if not shutil.which('node'):
            checks['JavaScript unit tests'] = 'SKIP'
            checks['JavaScript coverage'] = 'NOT VERIFIED' if not args.quick else 'SKIP'
            return
        summary['node'] = run(['node', '--version']).stdout.strip()
        node_env = env.copy()
        command = ['node']
        if not args.quick:
            node_env['NODE_V8_COVERAGE'] = str(directory / 'v8')
            command += ['--require', str(ROOT / 'infrastructure/scripts/project_v8_probe.cjs')]
        # Keep OS isolation between files without Node spawning children, which
        # is blocked in some Windows sandboxes. Each Python-launched Node process
        # loads one file; production VM semantics/assertions remain unchanged.
        combined = ET.Element('testsuites')
        exitcode = 0
        for file in sorted((ROOT / 'tests/unit').glob('*.cjs')):
            target = directory / (file.stem + '.xml')
            result = run([*command, '--test', '--test-isolation=none', '--test-reporter=junit',
                          '--test-reporter-destination=' + str(target), str(file)],
                         env=node_env, timeout=60, log=directory / (file.stem + '.log'))
            exitcode = exitcode or result.returncode
            if not target.exists():
                exitcode = exitcode or 1
                case = ET.SubElement(combined, 'testcase', name=file.name)
                ET.SubElement(case, 'failure', message='Node did not produce JUnit; see per-file log')
                continue
            tree = ET.parse(target).getroot()
            cases = list(tree.iter('testcase'))
            if not cases:
                # Historical queue harness uses top-level assertions, not node:test.
                if file.name != 'test_dashboard_queue.cjs':
                    exitcode = exitcode or 1
                case = ET.SubElement(combined, 'testcase', name=file.name + ' (script assertions)')
                if result.returncode or file.name != 'test_dashboard_queue.cjs':
                    ET.SubElement(case, 'failure', message='Script failed or no tests discovered')
            else:
                combined.extend(cases)
        ET.ElementTree(combined).write(directory / 'javascript.xml', encoding='utf-8', xml_declaration=True)
        counts = junit_counts(directory / 'javascript.xml')
        summary['tests']['JavaScript (including queue file test)'] = counts
        checks['JavaScript unit tests'] = suite_status(exitcode, counts)
        checks['JavaScript coverage'] = 'SKIP' if args.quick else 'NOT VERIFIED'
        summary['details']['JavaScript coverage'] = ('Skipped in Quick mode' if args.quick else
            'Raw V8 collected; sliced VM/browser paths prevent complete attribution')
    check('JavaScript unit tests', node_tests)
    checks.setdefault('JavaScript coverage', 'NOT VERIFIED')

    checks['PostgreSQL integration'] = 'SKIP'
    if args.with_integration:
        def integration():
            try:
                url, proof = postgres_target(os.environ.get('CG_TEST_POSTGRES_CONTAINER'))
            except (ValueError, OSError, subprocess.TimeoutExpired):
                checks['PostgreSQL integration'] = 'NOT VERIFIED'
                summary['details']['PostgreSQL integration'] = 'Isolation not established; no tests collected and no database mutation. See docs/testing.md.'
                return
            write_json(directory / 'postgres-isolation.json', proof)
            isolated_env = test_environment(directory)
            isolated_env['CONTROLLER_TEST_DATABASE_URL'] = url
            result = run([sys.executable, '-m', 'pytest', *POSTGRES, '-q', '-p', 'no:cacheprovider',
                          '--basetemp=' + str(directory / 'pg-pytest'), '--junitxml=' + str(directory / 'postgres.xml')],
                         env=isolated_env, timeout=300, log=directory / 'postgres.log')
            counts = junit_counts(directory / 'postgres.xml')
            summary['tests']['Isolated PostgreSQL'] = counts
            checks['PostgreSQL integration'] = suite_status(result.returncode, counts)
        check('PostgreSQL integration', integration)
    checks['Docker health'] = 'SKIP'
    if args.with_docker:
        def docker():
            details = docker_checks()
            summary['details']['Docker health'] = details
            checks['Docker health'] = 'FAIL' if any(c['status'] == 'FAIL' for c in details) else 'PASS' if all(c['status'] == 'PASS' for c in details) else 'NOT VERIFIED'
        check('Docker health', docker)
    checks['Evaluation evidence'] = 'SKIP' if args.quick else 'NOT VERIFIED'
    checks['Proposal acceptance'] = 'NOT VERIFIED'
    if not args.quick:
        def audit():
            missing = missing_evaluation_documents()
            checks['Evaluation evidence'] = 'FAIL' if missing else 'PASS'
            summary['details']['Evaluation evidence'] = {
                'scope': 'Required document availability only; no performance acceptance inferred',
                'required_documents': EVALUATION_DOCUMENTS, 'missing_documents': missing}
            checks['Coverage audit generation'] = 'PASS'
            generate_audit(directory, summary)
        check('Coverage audit generation', audit)
    if args.end_to_end:
        from infrastructure.scripts.project_e2e import run_e2e
        code, results = run_e2e(resume=args.e2e_resume, confirmation=args.confirm_isolation,
                                cleanup_only=args.e2e_cleanup_only)
        checks['Real system E2E'] = 'PASS' if code == 0 else 'NOT VERIFIED' if code == 2 else 'FAIL'
        summary['details']['Real system E2E'] = {'results': str(results), 'exit_code': code}
    write_json(directory / 'summary.json', summary)
    print()
    for name, status in checks.items():
        print(f'{name:28} {status}')
    failed = any(value == 'FAIL' for value in checks.values())
    print('\nOverall: ' + ('EXECUTED CHECKS FAILED' if failed else 'EXECUTED CHECKS PASSED (see SKIP / NOT VERIFIED)'))
    print('Results: ' + str(directory.relative_to(ROOT)))
    return 1 if failed else 2 if args.end_to_end and checks.get('Real system E2E') == 'NOT VERIFIED' else 0


if __name__ == '__main__':
    raise SystemExit(main())
