"""Configuration, stale status, redaction and no-overwrite regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from types import ModuleType

import pytest

from infrastructure.scripts import verify_aws_recovery as checker


def event(site="Hospital_A"):
    return {"event_id": "ANY_TEAM_" + site, "site_id": site, "event_type": "patient_registration",
            "base_priority": 1, "prerequisite_ids": [], "payload": {"patient_id": "SYNTHETIC_TEST"}}


def probe():
    return {"endpoint_source": "hospital_runtime_configuration", "site_id": "Hospital_A",
            "capture": {"site_id": "Hospital_A"}, "auto_replay_configured": True,
            "records": [dict(event(), status="applied")],
            "cloud": {name: {"healthy": True} for name in checker.SERVICES},
            "recovery": {"enabled": True, "cloud_reachable": True,
                         "service_health": dict.fromkeys(checker.SERVICES, True),
                         "last_check_at": datetime.now(timezone.utc).isoformat(),
                         "last_error_present": False, "replayable_backlog": 0}}


@pytest.mark.parametrize("fault", ["empty", "duplicate", "wrong_type", "wrong_priority", "string_payload", "extra_field"])
def test_bad_expectations_rejected(fault):
    rows = [event()]
    if fault == "empty": rows = []
    elif fault == "duplicate": rows.append(event())
    elif fault == "wrong_type": rows[0]["event_type"] = "unknown"
    elif fault == "wrong_priority": rows[0]["base_priority"] = True
    elif fault == "string_payload": rows[0]["payload"] = "invalid"
    else: rows[0]["unexpected"] = "value"
    with pytest.raises(ValueError):
        checker.validate_expected(rows, full=True)


@pytest.mark.parametrize("fault", ["disabled", "partial_health", "stale", "no_timestamp", "error", "backlog_unknown"])
def test_incomplete_recovery_cannot_pass(fault):
    value = probe()
    state = value["recovery"]
    if fault == "disabled": value["auto_replay_configured"] = False
    elif fault == "partial_health": state["service_health"]["admin"] = False
    elif fault == "stale": state["last_check_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    elif fault == "no_timestamp": state["last_check_at"] = None
    elif fault == "error": state["last_error_present"] = True
    else: state["replayable_backlog"] = None
    with pytest.raises(RuntimeError):
        checker.check_recovery(value, 30)


def setup_host(monkeypatch, tmp_path, value=None):
    settings = tmp_path / "private.env"
    compose = tmp_path / "custom.yaml"
    settings.write_text("PRIVATE_PASSWORD=DO_NOT_PRINT\n")
    compose.write_text("services: {}\n")
    commands = []
    def fake_command(args, *, stdin=None):
        commands.append((args, stdin))
        return json.dumps({"Service": "custom-hospital", "State": "running", "Health": "healthy"})
    monkeypatch.setattr(checker, "command", fake_command)
    monkeypatch.setattr(checker, "container_json", lambda *args: deepcopy(value if value is not None else probe()))
    return ["hospital", "--env-file", str(settings), "--compose-file", str(compose),
            "--project-name", "different-team", "--hospital-service", "custom-hospital"], commands


def test_custom_configuration_and_shared_full_manifest(monkeypatch, tmp_path, capsys):
    args, commands = setup_host(monkeypatch, tmp_path)
    manifest = tmp_path / "events.json"
    manifest.write_text(json.dumps([event(), event("Hospital_B")]))
    assert checker.main(args + ["--expect-events", str(manifest), "--require-drained"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["record_verification"] == "full_record"
    assert report["expected_events_for_site"] == 1
    assert report["verified_events"][0]["event_id"] == event()["event_id"]
    assert "--project-name" in commands[0][0] and "different-team" in commands[0][0]
    assert "payload" not in json.dumps(report) and "DO_NOT_PRINT" not in json.dumps(report)


def test_no_expectations_are_explicitly_skipped(monkeypatch, tmp_path, capsys):
    args, _ = setup_host(monkeypatch, tmp_path)
    assert checker.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["record_verification"] == "not_requested"
    assert next(c for c in report["checks"] if c["name"] == "expected_events")["result"] == "skipped"


def test_full_record_comparison_distinguishes_boolean_from_number():
    expected = event()
    expected['payload'] = {'flag': False}
    record = dict(expected, status='applied', payload={'flag': 0})
    with pytest.raises(RuntimeError, match='different content'):
        checker.check_events([record], [expected])


@pytest.mark.parametrize("fault", ["wrong_site", "missing_event", "undrained", "disabled", "cloud_down"])
def test_real_failures_produce_nonzero_and_structured_failure(monkeypatch, tmp_path, capsys, fault):
    value = probe()
    if fault == "wrong_site": value["capture"]["site_id"] = "Hospital_B"
    elif fault == "missing_event": value["records"] = []
    elif fault == "undrained": value["records"][0]["status"] = "pending"
    elif fault == "disabled": value["auto_replay_configured"] = False
    else: value["cloud"]["emergency"]["healthy"] = False
    args, _ = setup_host(monkeypatch, tmp_path, value)
    assert checker.main(args + ["--expect-event", event()["event_id"], "Hospital_A", "patient_registration",
                                "--require-drained"]) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "failed"


def test_manual_mode_requires_explicit_opt_out(monkeypatch, tmp_path, capsys):
    value = probe()
    value["auto_replay_configured"] = False
    value["recovery"]["enabled"] = False
    args, _ = setup_host(monkeypatch, tmp_path, value)
    assert checker.main(args + ["--allow-manual-replay"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert next(c for c in report["checks"] if c["name"] == "automatic_replay")["result"] == "skipped"


def test_report_refuses_overwrite_before_docker(monkeypatch, tmp_path, capsys):
    path = tmp_path / "existing.json"
    path.write_text("keep this")
    monkeypatch.setattr(checker, "verify", lambda *args: pytest.fail("No probe should run"))
    assert checker.main(["cloud", "--output", str(path)]) == 1
    assert path.read_text() == "keep this"
    assert json.loads(capsys.readouterr().out)["result"] == "failed"


def test_timeout_report_does_not_dump_arguments_or_stderr(capsys):
    report = {"checks": []}
    def failure():
        raise subprocess.TimeoutExpired(["secret-password"], 60, stderr="token=secret")
    assert checker.checked(report, "probe", failure) is None
    assert "secret" not in json.dumps(report)
    assert report["checks"][0]["result"] == "failed"


def test_config_data_cannot_become_executable_code(monkeypatch):
    captured = {}
    def fake_command(args, *, stdin=None):
        captured.update(script=stdin)
        return '{}'
    monkeypatch.setattr(checker, "command", fake_command)
    malicious = "'); raise RuntimeError('injected'); #"
    checker.container_json(["docker", "compose"], "patient", "print('{}')", {"event_ids": [malicious]})
    namespace = {}
    exec(captured["script"], namespace)
    assert namespace["config"]["event_ids"] == [malicious]


@pytest.mark.parametrize('args', [
    ['cloud', '--require-drained'], ['cloud', '--site-id', 'Hospital_A'],
    ['hospital', '--patient-port', '9001'], ['hospital', '--cloud-host', 'http://user:password@host'],
])
def test_invalid_options_rejected_before_any_probe(monkeypatch, capsys, args):
    monkeypatch.setattr(checker, 'verify', lambda *a: pytest.fail('No Docker probe expected'))
    assert checker.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['result'] == 'failed'
    assert 'password@host' not in json.dumps(report)


def test_database_probe_uses_read_only_transaction_and_parameterized_ids(monkeypatch, capsys):
    calls = []
    injected = "x'); DELETE FROM events; --"
    class Cursor:
        description = []
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, query, params=None):
            assert connection.read_only is True
            calls.append((query, params))
        def fetchone(self): return (0, 0, 0)
        def fetchall(self): return []
    class Connection:
        read_only = False
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def cursor(self): return Cursor()
    connection = Connection()
    module = ModuleType('cloud.database.db')
    module.get_connection = lambda: connection
    monkeypatch.setitem(sys.modules, 'cloud.database.db', module)
    exec(checker.DATABASE, {'config': {'event_ids': [injected]}})
    assert all(injected not in query for query, _ in calls)
    assert calls[-1][1] == ([injected],)
    assert json.loads(capsys.readouterr().out)['total'] == 0


def test_remote_probe_redacts_bad_urls_and_replay_exception_text(monkeypatch, capsys):
    from io import BytesIO
    import urllib.request
    module = ModuleType('edge.capture_api.cloud_client')
    module.get_service_urls = lambda: dict.fromkeys(checker.SERVICES, 'http://user:SECRET_PASSWORD@cloud:8001/events?token=SECRET_TOKEN')
    monkeypatch.setitem(sys.modules, 'edge.capture_api.cloud_client', module)
    class Response(BytesIO):
        status = 200
    def fake_open(url, timeout):
        assert 'SECRET' not in url
        body = {'site_id': 'Hospital_A'} if url.endswith(':8000/') else [] if url.endswith('/events') else {
            'enabled': True, 'last_error': 'SECRET_PASSWORD SECRET_TOKEN'}
        return Response(json.dumps(body).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', fake_open)
    monkeypatch.setenv('SITE_ID', 'Hospital_A')
    exec(checker.PROBE, {'config': {'hospital': True, 'cloud_host': None, 'ports': [8001,8003,8004], 'timeout': 3}})
    output = capsys.readouterr().out
    report = json.loads(output)
    assert all(not row['healthy'] for row in report['cloud'].values())
    assert report['recovery']['last_error_present'] is True
    assert 'SECRET' not in output and 'user:' not in output
