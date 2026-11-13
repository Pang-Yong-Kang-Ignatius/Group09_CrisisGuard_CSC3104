"""Safety checks for the scoped EC2 network fault helper; no real firewall calls."""
import json
import subprocess

import pytest

from infrastructure.scripts import aws_outage_host as fault


@pytest.mark.parametrize('name,host', [
    ('../../unsafe', '172.31.23.56'), ('CG_OUTAGE_X;id', '172.31.23.56'),
    ('CG_OUTAGE_TEST', '8.8.8.8'), ('CG_OUTAGE_TEST', '127.0.0.1'),
    ('CG_OUTAGE_TEST', '169.254.169.254'), ('CG_OUTAGE_TEST', '::1'),
])
def test_reject_unsafe_scope(name, host):
    with pytest.raises(ValueError):
        fault.validate(name, host)


def test_wrong_hospital_cannot_arm(monkeypatch):
    monkeypatch.setattr(fault.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(fault, 'container', lambda: ('a'*64, '172.18.0.2', {'SITE_ID': 'Hospital_B', 'AUTO_REPLAY': '1'}))
    monkeypatch.setattr(fault, 'run', lambda *a, **kw: pytest.fail('Must reject before a host command'))
    with pytest.raises(RuntimeError, match='Hospital_A'):
        fault.arm('CG_OUTAGE_TEST', '172.31.23.56')


def setup_arm(monkeypatch, tmp_path, fail_timer=False):
    monkeypatch.setattr(fault.os, 'geteuid', lambda: 0, raising=False)
    env = {'SITE_ID': 'Hospital_A', 'AUTO_REPLAY': '1',
           **{k: f'http://172.31.23.56:{p}/events' for k,p in
              [('CLOUD_URL',8001),('EMERGENCY_URL',8003),('ADMIN_URL',8004)]}}
    monkeypatch.setattr(fault, 'container', lambda: ('a'*64, '172.18.0.2', env))
    monkeypatch.setattr(fault, 'paths', lambda test_id: (tmp_path/'state.json', tmp_path/'rollback.sh'))
    commands = []
    firewall = {'present': False}
    def fake_run(args, check=True):
        commands.append(args)
        if fail_timer and args[:2] == ['systemctl', 'is-active']:
            raise subprocess.CalledProcessError(3, args)
        if '-I' in args:
            firewall['present'] = True
        if '-D' in args:
            firewall['present'] = False
        return subprocess.CompletedProcess(args, int(not firewall['present']) if '-C' in args else 0, '', '')
    monkeypatch.setattr(fault, 'run', fake_run)
    return commands


def test_rollback_armed_before_rule_and_removes_exact_scope(monkeypatch, tmp_path):
    commands = setup_arm(monkeypatch, tmp_path)
    fault.arm('CG_OUTAGE_TEST', '172.31.23.56')
    inserted = next(c for c in commands if '-I' in c)
    assert commands.index(next(c for c in commands if c[0] == 'systemd-run')) < commands.index(inserted)
    assert commands.index(next(c for c in commands if c[:2] == ['systemctl','is-active'])) < commands.index(inserted)
    saved = json.loads((tmp_path/'state.json').read_text())
    assert inserted[6:] == saved['selectors']
    assert '--dports 8001,8003,8004' in (tmp_path/'rollback.sh').read_text()
    assert '-D DOCKER-USER' in (tmp_path/'rollback.sh').read_text()
    assert '22' not in saved['selectors'] and '-F' not in saved['selectors']
    result = fault.restore('CG_OUTAGE_TEST', '172.31.23.56')
    assert result['rule_absent']
    assert next(c for c in commands if '-D' in c)[5:] == saved['selectors']
    assert not (tmp_path/'state.json').exists()


def test_inactive_timer_prevents_fault(monkeypatch, tmp_path):
    commands = setup_arm(monkeypatch, tmp_path, fail_timer=True)
    with pytest.raises(subprocess.CalledProcessError):
        fault.arm('CG_OUTAGE_TEST', '172.31.23.56')
    assert not any('-I' in c for c in commands)
    assert not (tmp_path/'state.json').exists()


def test_firewall_error_does_not_claim_restoration_or_cancel_timer(monkeypatch, tmp_path):
    setup_arm(monkeypatch, tmp_path)
    fault.arm('CG_OUTAGE_TEST', '172.31.23.56')
    commands = []
    def unavailable(args, check=True):
        commands.append(args)
        return subprocess.CompletedProcess(args, 4, '', 'resource unavailable')
    monkeypatch.setattr(fault, 'run', unavailable)
    with pytest.raises(RuntimeError, match='timer retained'):
        fault.restore('CG_OUTAGE_TEST', '172.31.23.56')
    assert (tmp_path/'state.json').exists()
    assert not any(c[0] == 'systemctl' for c in commands)
