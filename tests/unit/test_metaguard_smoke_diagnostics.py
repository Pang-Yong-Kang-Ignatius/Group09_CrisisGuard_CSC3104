"""Fresh-runner image preparation and safe deployment failure evidence."""
import subprocess

import pytest

from infrastructure.scripts import smoke_metaguard_deployment as smoke


def test_startup_prepares_database_on_empty_cache():
    cached = {'unique-app:run'}
    calls = []
    def call(*args):
        calls.append(args)
        if args == ('pull', '--policy', 'missing', 'postgres'):
            cached.add('postgres:16')
        else:
            assert 'postgres:16' in cached
            assert '--no-build' in args and args[args.index('--pull')+1] == 'never'
    smoke.startup(call)
    assert calls[0] == ('pull', '--policy', 'missing', 'postgres')
    assert len(calls) == 2


def test_pull_failure_does_not_attempt_startup():
    calls = []
    def call(*args):
        calls.append(args)
        raise RuntimeError('registry unavailable')
    with pytest.raises(RuntimeError):
        smoke.startup(call)
    assert calls == [('pull', '--policy', 'missing', 'postgres')]


def test_failure_prints_and_records_sanitized_docker_error(tmp_path, capsys):
    result = subprocess.CompletedProcess([], 1, '',
        'postgres:16 unavailable password=private token=other postgresql://user:pass@db/test exact-secret')
    path = tmp_path/'run.log'
    with pytest.raises(RuntimeError, match='exited 1'):
        smoke.command_result(result, path, ('exact-secret',))
    for text in (capsys.readouterr().out, path.read_text()):
        assert 'postgres:16 unavailable' in text
        for secret in ('private', 'other', 'user:pass', 'exact-secret'):
            assert secret not in text


def test_capture_failure_also_prints_errors(tmp_path, capsys):
    with pytest.raises(RuntimeError):
        smoke.command_result(subprocess.CompletedProcess([], 2, '', 'Docker error'),
                             tmp_path/'log', (), capture=True)
    assert 'Docker error' in capsys.readouterr().out


def test_diagnostics_only_select_owned_containers_and_state(monkeypatch, capsys):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        output = 'a'*64 if args[-2:] == ['ps', '-aq'] else 'unhealthy exact-secret'
        return subprocess.CompletedProcess(args, 0, output, '')
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    smoke.diagnostics([('unique-project', ['docker', 'compose', '-p', 'unique-project'], {})], ('exact-secret',))
    output = capsys.readouterr().out
    assert 'unhealthy' in output and 'exact-secret' not in output
    inspect = next(args for args in calls if args[1] == 'inspect')
    assert '.State' in inspect[3] and '.Config' not in inspect[3]
    assert all(args[:2] == ['docker', 'inspect'] or 'unique-project' in args for args in calls)


def test_diagnostic_timeout_is_reported_without_masking_failure(monkeypatch, capsys):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired('docker', 20)
    monkeypatch.setattr(smoke.subprocess, 'run', run)
    smoke.diagnostics([('unique', ['docker', 'compose', '-p', 'unique'], {})], ())
    assert 'TimeoutExpired' in capsys.readouterr().out
